"""The LSP manager: one per daemon, lazily one server per (server, root).

Ported from opencode's ``packages/opencode/src/lsp/lsp.ts`` (MIT, commit
95daf90).  Upstream's Effect service becomes a plain object owned by
:class:`~snowpea_core.server.app_server.Core`; the substance — pick the servers
whose extensions match, resolve a root per server, start at most one client per
``(server, root)`` even under concurrent calls, fan a request out over every
matching client — is upstream's.

Two things are ours, both from the M13 contract: a crashed server is restarted
once and then reported ``broken`` (AC-45) instead of being dropped on the first
failure, and an idle server is shut down after ``lsp.idleTimeoutSec``.

# MIT License
#
# Copyright (c) 2025 opencode
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.lsp import servers as server_registry
from snowpea_core.lsp.client import LspClient, LspError, path_to_uri, uri_to_path
from snowpea_core.lsp.language import extension_of, language_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.lsp.manager")

#: How long an edit waits for a server that is not running yet (contract §3).
START_BUDGET_SEC = 3.0
#: Symbol kinds ``lsp_workspace_symbols`` keeps; upstream's ``kinds``.
SYMBOL_KINDS: frozenset[int] = frozenset({5, 6, 11, 12, 13, 14, 23, 10})
#: Workspace symbols returned per server, as upstream caps them.
MAX_WORKSPACE_SYMBOLS = 10
#: How often the janitor looks for servers past ``lsp.idleTimeoutSec``.
SWEEP_INTERVAL_SEC = 30.0

ServerState = Literal["starting", "ready", "broken", "stopped"]

#: ``(server id, root)`` — one client each.
Key = tuple[str, str]


@dataclass
class Entry:
    """One (server, root) pair and whatever state it has reached."""

    server_id: str
    root: Path
    state: ServerState = "starting"
    client: LspClient | None = None
    #: Crashes seen so far; the second one is terminal (AC-45).
    crashes: int = 0
    last_used: float = field(default_factory=time.monotonic)

    @property
    def pid(self) -> int | None:
        client = self.client
        if client is None or client.process.returncode is not None:
            return None
        return client.process.pid


class LspManager:
    """Every language server this daemon is talking to."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.entries: dict[Key, Entry] = {}
        self._spawning: dict[Key, asyncio.Task[Entry]] = {}
        self._janitor: asyncio.Task[None] | None = None
        #: Session to emit ``lsp.diagnostics`` into, set for the duration of a
        #: touch so the client's push callback knows who asked.
        self._session_id: str | None = None

    # -- settings --------------------------------------------------------

    @property
    def settings(self) -> Any:
        """``settings.lsp``, read at call time so a hot reload lands for free."""
        return self.core.settings.lsp

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    def catalog(self) -> dict[str, server_registry.ServerInfo]:
        return server_registry.catalog(
            disabled=self.settings.disabled, overrides=self.settings.servers
        )

    # -- starting --------------------------------------------------------

    def _matching(self, file: Path) -> list[server_registry.ServerInfo]:
        extension = extension_of(file)
        return [
            server
            for server in self.catalog().values()
            if server.extensions and server.serves(extension)
        ]

    async def _start(self, server: server_registry.ServerInfo, root: Path, key: Key) -> Entry:
        entry = self.entries.setdefault(key, Entry(server_id=server.id, root=root))
        entry.state = "starting"
        initialization = server.initialization(root) if server.initialization else {}
        process = await server_registry.spawn(
            server,
            root,
            home=self.core.paths.home,
            allow_install=bool(self.settings.autoInstall),
        )
        if process is None:
            # Not installed is not a failure: the tool and the edit carry on
            # without diagnostics (contract §2).
            entry.state = "stopped"
            entry.client = None
            return entry
        client = LspClient(
            server_id=server.id,
            process=process,
            root=root,
            initialization=initialization,
            on_diagnostics=self._on_diagnostics,
        )
        try:
            await client.start()
        except (LspError, OSError) as exc:
            log.warning("language server %s failed to initialize: %s", server.id, exc)
            with contextlib.suppress(Exception):
                await client.shutdown()
            entry.crashes += 1
            entry.state = "broken" if entry.crashes > 1 else "stopped"
            entry.client = None
            return entry
        entry.client = client
        entry.state = "ready"
        entry.last_used = time.monotonic()
        self._ensure_janitor()
        return entry

    async def _entry_for(
        self, server: server_registry.ServerInfo, root: Path
    ) -> Entry | None:
        """The live entry for ``(server, root)``, starting it at most once."""
        key = (server.id, str(root))
        entry = self.entries.get(key)
        if entry is not None:
            if entry.state == "broken":
                return None
            client = entry.client
            if client is not None and client.alive:
                entry.last_used = time.monotonic()
                return entry
            if client is not None:
                # The process died under us.  One restart, then broken (AC-45).
                entry.crashes += 1
                entry.client = None
                if entry.crashes > 1:
                    entry.state = "broken"
                    log.warning(
                        "language server %s at %s crashed twice; marking it broken",
                        server.id,
                        root,
                    )
                    return None
                log.info("language server %s at %s exited; restarting once", server.id, root)
            elif entry.state == "stopped" and entry.crashes == 0:
                # Never started (no binary), and nothing has changed since.
                return None
        inflight = self._spawning.get(key)
        if inflight is None:
            inflight = asyncio.ensure_future(self._start(server, root, key))
            self._spawning[key] = inflight
            try:
                entry = await inflight
            finally:
                if self._spawning.get(key) is inflight:
                    del self._spawning[key]
        else:
            entry = await inflight
        return entry if entry.state == "ready" and entry.client is not None else None

    async def clients_for(
        self, file: Path, workdir: Path, *, budget: float | None = None
    ) -> list[LspClient]:
        """Every live client that serves ``file``; may start them.

        ``budget`` caps the wait, so an edit never blocks longer than the
        contract's three seconds on a server that is still coming up.
        """
        if not self.enabled:
            return []
        file = Path(file)
        workdir = Path(workdir).resolve()
        tasks = []
        for server in self._matching(file):
            root = server_registry.find_root(server, file.resolve(), workdir)
            if root is None:
                continue
            tasks.append(self._entry_for(server, root))
        if not tasks:
            return []
        gather = asyncio.gather(*tasks, return_exceptions=True)
        try:
            entries = await (asyncio.wait_for(gather, budget) if budget else gather)
        except TimeoutError:
            # The servers keep starting in the background; this edit just does
            # not get to wait for them.
            return [
                entry.client
                for entry in self.entries.values()
                if entry.client is not None and entry.client.alive
            ]
        clients = []
        for entry in entries:
            if isinstance(entry, BaseException) or entry is None:
                if isinstance(entry, BaseException):
                    log.debug("starting a language server raised", exc_info=entry)
                continue
            if entry.client is not None:
                clients.append(entry.client)
        return clients

    # -- diagnostics -----------------------------------------------------

    def _on_diagnostics(self, path: Path, diagnostics: list[dict[str, Any]]) -> None:
        """Publish ``lsp.diagnostics`` for the session that triggered the touch."""
        session_id = self._session_id
        if session_id is None or self.core.hub is None:
            return
        from snowpea_core.lsp.diagnostic import counts
        from snowpea_core.session import events

        errors, warnings = counts(diagnostics)
        event = events.lsp_diagnostics(
            str(path), count=len(diagnostics), errors=errors, warnings=warnings
        )
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(
                self.core.hub.emit_event(session_id, event)
            )

    async def touch_file(
        self,
        path: Path | str,
        workdir: Path | str,
        *,
        wait_for_diagnostics: bool = False,
        budget: float = START_BUDGET_SEC,
        session_id: str | None = None,
    ) -> list[LspClient]:
        """Tell every matching server that ``path`` changed, and read it back.

        Never raises: a missing server, a dead server or a slow one all leave
        the caller with fewer clients, which is a ``Diagnostics`` block that is
        absent rather than an edit that failed (contract §3).
        """
        file = Path(path)
        previous_session, self._session_id = self._session_id, session_id
        try:
            clients = await self.clients_for(file, Path(workdir), budget=budget)
            if not clients:
                return []
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return []
            resolved = file.resolve()
            seen = {client: client.publish_seq.get(resolved, 0) for client in clients}
            await asyncio.gather(
                *(client.open_file(resolved, text) for client in clients),
                return_exceptions=True,
            )
            if wait_for_diagnostics:
                await asyncio.gather(
                    *(
                        client.wait_for_diagnostics(resolved, since=seen[client])
                        for client in clients
                    ),
                    return_exceptions=True,
                )
            return clients
        except Exception:  # noqa: BLE001 - the LSP layer never fails an edit
            log.debug("touching %s failed", path, exc_info=True)
            return []
        finally:
            self._session_id = previous_session

    def diagnostics(self, path: Path | str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Known diagnostics, for one file or for everything the servers saw."""
        merged: dict[str, list[dict[str, Any]]] = {}
        wanted = Path(path).resolve() if path is not None else None
        for entry in self.entries.values():
            client = entry.client
            if client is None:
                continue
            if wanted is not None:
                items = client.diagnostics_for(wanted)
                if items:
                    merged.setdefault(str(wanted), []).extend(items)
                continue
            for file, items in client.all_diagnostics().items():
                if items:
                    merged.setdefault(str(file), []).extend(items)
        return merged

    # -- requests --------------------------------------------------------

    async def _fan_out(
        self, file: Path, workdir: Path, method: str, params: dict[str, Any]
    ) -> list[Any]:
        clients = await self.clients_for(file, workdir, budget=START_BUDGET_SEC)
        if not clients:
            return []
        resolved = file.resolve()
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        await asyncio.gather(
            *(client.open_file(resolved, text) for client in clients), return_exceptions=True
        )
        answers = await asyncio.gather(
            *(client.try_request(method, params) for client in clients),
            return_exceptions=True,
        )
        return [item for item in answers if item and not isinstance(item, BaseException)]

    @staticmethod
    def _position(file: Path, line: int, character: int) -> dict[str, Any]:
        return {
            "textDocument": {"uri": path_to_uri(file)},
            "position": {"line": max(0, line), "character": max(0, character)},
        }

    @staticmethod
    def _locations(answers: list[Any]) -> list[dict[str, Any]]:
        """Flatten ``Location | Location[] | LocationLink[]`` into locations."""
        out: list[dict[str, Any]] = []
        for answer in answers:
            items = answer if isinstance(answer, list) else [answer]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "targetUri" in item:
                    out.append(
                        {
                            "uri": item["targetUri"],
                            "range": item.get("targetSelectionRange")
                            or item.get("targetRange")
                            or {},
                        }
                    )
                elif "uri" in item:
                    out.append(item)
        return out

    async def definition(
        self, file: Path, workdir: Path, line: int, character: int
    ) -> list[dict[str, Any]]:
        answers = await self._fan_out(
            file, workdir, "textDocument/definition", self._position(file, line, character)
        )
        return self._locations(answers)

    async def references(
        self, file: Path, workdir: Path, line: int, character: int
    ) -> list[dict[str, Any]]:
        params = self._position(file, line, character)
        params["context"] = {"includeDeclaration": True}
        answers = await self._fan_out(file, workdir, "textDocument/references", params)
        return self._locations(answers)

    async def hover(
        self, file: Path, workdir: Path, line: int, character: int
    ) -> dict[str, Any] | None:
        answers = await self._fan_out(
            file, workdir, "textDocument/hover", self._position(file, line, character)
        )
        for answer in answers:
            if isinstance(answer, dict) and answer.get("contents"):
                return answer
        return None

    async def document_symbols(self, file: Path, workdir: Path) -> list[dict[str, Any]]:
        answers = await self._fan_out(
            file,
            workdir,
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path_to_uri(file)}},
        )
        out: list[dict[str, Any]] = []
        for answer in answers:
            if isinstance(answer, list):
                out.extend(item for item in answer if isinstance(item, dict))
        return out

    async def workspace_symbols(self, query: str) -> list[dict[str, Any]]:
        """Ask every live server; only the interesting kinds come back."""
        clients = [
            entry.client
            for entry in self.entries.values()
            if entry.client is not None and entry.client.alive
        ]
        if not clients:
            return []
        answers = await asyncio.gather(
            *(client.try_request("workspace/symbol", {"query": query}) for client in clients),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        for answer in answers:
            if not isinstance(answer, list):
                continue
            kept = [
                item
                for item in answer
                if isinstance(item, dict) and item.get("kind") in SYMBOL_KINDS
            ]
            out.extend(kept[:MAX_WORKSPACE_SYMBOLS])
        return out

    async def prepare_rename(
        self, file: Path, workdir: Path, line: int, character: int
    ) -> Any:
        answers = await self._fan_out(
            file, workdir, "textDocument/prepareRename", self._position(file, line, character)
        )
        return answers[0] if answers else None

    async def rename(
        self, file: Path, workdir: Path, line: int, character: int, new_name: str
    ) -> dict[str, Any] | None:
        """The ``WorkspaceEdit`` that renames the symbol, or ``None``.

        Applying it is the caller's job and is a ``write``, which is why
        ``lsp_rename`` carries that permission tag (contract §3).
        """
        params = self._position(file, line, character)
        params["newName"] = new_name
        answers = await self._fan_out(file, workdir, "textDocument/rename", params)
        for answer in answers:
            if isinstance(answer, dict) and (
                answer.get("changes") or answer.get("documentChanges")
            ):
                return answer
        return None

    # -- lifecycle -------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """``lsp.status`` rows, one per (server, root) this daemon has touched."""
        catalog = self.catalog()
        rows = []
        for entry in self.entries.values():
            server = catalog.get(entry.server_id)
            extension = server.extensions[0] if server and server.extensions else ""
            rows.append(
                {
                    "id": entry.server_id,
                    "root": str(entry.root),
                    "state": entry.state,
                    "languageId": language_id(f"x{extension}") if extension else "",
                    "pid": entry.pid,
                }
            )
        return rows

    def _ensure_janitor(self) -> None:
        if self._janitor is not None and not self._janitor.done():
            return
        with contextlib.suppress(RuntimeError):
            self._janitor = asyncio.get_running_loop().create_task(
                self._sweep_forever(), name="lsp-idle-sweep"
            )

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SEC)
            if not await self.sweep_idle():
                self._janitor = None
                return

    async def sweep_idle(self) -> bool:
        """Stop servers idle past ``lsp.idleTimeoutSec``; True while any remain."""
        timeout = float(self.settings.idleTimeoutSec or 0)
        now = time.monotonic()
        alive = 0
        for entry in list(self.entries.values()):
            client = entry.client
            if client is None:
                continue
            if timeout > 0 and now - entry.last_used > timeout:
                log.info("stopping idle language server %s at %s", entry.server_id, entry.root)
                with contextlib.suppress(Exception):
                    await client.shutdown()
                entry.client = None
                entry.state = "stopped"
                continue
            alive += 1
        return alive > 0

    async def shutdown(self) -> None:
        """Stop every server; called from ``Daemon.stop``."""
        if self._janitor is not None:
            self._janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._janitor
            self._janitor = None
        clients = [entry.client for entry in self.entries.values() if entry.client is not None]
        for entry in self.entries.values():
            entry.client = None
            entry.state = "stopped"
        await asyncio.gather(*(client.shutdown() for client in clients), return_exceptions=True)


def manager_for(core: Core) -> LspManager:
    """The daemon's :class:`LspManager`, created on first use."""
    existing = getattr(core, "lsp", None)
    if existing is None:
        existing = LspManager(core)
        core.lsp = existing
    return existing


__all__ = [
    "MAX_WORKSPACE_SYMBOLS",
    "START_BUDGET_SEC",
    "SWEEP_INTERVAL_SEC",
    "SYMBOL_KINDS",
    "Entry",
    "Key",
    "LspManager",
    "ServerState",
    "manager_for",
    "uri_to_path",
]
