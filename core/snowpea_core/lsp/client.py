"""One JSON-RPC 2.0 connection to one language server, over stdio.

Ported from opencode's ``packages/opencode/src/lsp/client.ts`` (MIT, commit
95daf90).  Upstream leans on ``vscode-jsonrpc`` for the framing; there is no
equivalent dependency here worth taking for ~80 lines, so
:meth:`LspClient._read_message` implements the ``Content-Length`` framing of
the LSP base protocol directly and the rest — the initialize handshake and its
capabilities, ``didOpen``/``didChange`` versioning, the push/pull diagnostic
split, the server-to-client requests that must be answered or the server hangs
— follows upstream closely.

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
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

from snowpea_core.lsp.language import language_id

log = logging.getLogger("snowpea.lsp.client")

#: Default ceiling on a request the agent is waiting for (contract §2).
REQUEST_TIMEOUT_SEC = 8.0
#: The handshake is slower than a request: a cold gopls or rust-analyzer
#: indexes before it answers ``initialize``.
INITIALIZE_TIMEOUT_SEC = 45.0
#: How long ``wait_for_diagnostics`` lets a server think before giving up.
DIAGNOSTICS_TIMEOUT_SEC = 5.0
#: Quiet period after a publish before the diagnostics are taken as settled;
#: servers that emit twice (parse, then typecheck) would otherwise be read
#: halfway through.  Upstream's ``DIAGNOSTICS_DEBOUNCE_MS``.
DIAGNOSTICS_DEBOUNCE_SEC = 0.15

#: ``textDocumentSync.change`` value meaning "send me ranges, not whole files".
SYNC_INCREMENTAL = 2

#: Server-to-client requests that must be answered or the server stalls waiting
#: for a reply; the value is what we answer with.
_STATIC_REPLIES: dict[str, Any] = {
    "window/workDoneProgress/create": None,
    "workspace/diagnostic/refresh": None,
    "workspace/semanticTokens/refresh": None,
    "workspace/codeLens/refresh": None,
    "workspace/inlayHint/refresh": None,
}


def path_to_uri(path: Path | str) -> str:
    """``/a/b.py`` -> ``file:///a/b.py``."""
    return "file://" + pathname2url(str(Path(path).resolve()))


def uri_to_path(uri: str) -> Path | None:
    """``file:///a/b.py`` -> ``Path("/a/b.py")``; ``None`` for any other scheme."""
    if not uri.startswith("file://"):
        return None
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


class LspError(Exception):
    """The server answered a request with an error, or never answered it."""


class LspClient:
    """A live language server: the connection, its documents, its diagnostics."""

    def __init__(
        self,
        *,
        server_id: str,
        process: asyncio.subprocess.Process,
        root: Path,
        initialization: dict[str, Any] | None = None,
        on_diagnostics: Callable[[Path, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self.server_id = server_id
        self.root = Path(root)
        self.process = process
        self.initialization = initialization or {}
        self.on_diagnostics = on_diagnostics
        self.capabilities: dict[str, Any] = {}
        #: ``publishDiagnostics``, keyed by resolved path.
        self.push: dict[Path, list[dict[str, Any]]] = {}
        #: ``textDocument/diagnostic`` answers, keyed the same way.
        self.pull: dict[Path, list[dict[str, Any]]] = {}
        #: Documents this connection has opened: path -> version.
        self.documents: dict[Path, int] = {}
        #: How many times the server has published about each file.  A caller
        #: reads it before an edit and waits for it to move, so a second edit
        #: to the same file is not answered from the first edit's publish.
        self.publish_seq: dict[Path, int] = {}
        self.ready = False
        self.exited = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._published: asyncio.Event = asyncio.Event()
        #: Set whenever the server registers or unregisters
        #: ``textDocument/diagnostic``; :meth:`wait_for_diagnostics` retries a
        #: pull when it moves.
        self._registered: asyncio.Event = asyncio.Event()
        #: True once the server has registered ``textDocument/diagnostic`` at
        #: all.  Sticky: pyright registers and unregisters it repeatedly while
        #: it settles, and a server that has offered the method once answers it
        #: afterwards, so the flag decides only whether a pull is worth trying.
        self._pull_registered = False

    # -- framing ---------------------------------------------------------

    async def _read_message(self) -> dict[str, Any] | None:
        """One framed message, or ``None`` when the server closed its stdout."""
        stdout = self.process.stdout
        assert stdout is not None  # created with stdout=PIPE
        length = 0
        while True:
            line = await stdout.readline()
            if not line:
                return None
            header = line.strip()
            if not header:
                break
            name, _, value = header.decode("ascii", "replace").partition(":")
            if name.strip().lower() == "content-length":
                with contextlib.suppress(ValueError):
                    length = int(value.strip())
        if length <= 0:
            return None
        body = await stdout.readexactly(length)
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.debug("%s sent a malformed message", self.server_id)
            return {}
        return message if isinstance(message, dict) else {}

    async def _send(self, message: dict[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None or self.exited:
            raise LspError(f"{self.server_id} is not running")
        body = json.dumps(message).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        async with self._write_lock:
            stdin.write(frame)
            await stdin.drain()

    # -- dispatch --------------------------------------------------------

    async def _pump(self) -> None:
        """Read messages until the server exits; resolve futures as they land."""
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                if message:
                    await self._dispatch(message)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken server must not kill the daemon
            log.debug("%s reader stopped", self.server_id, exc_info=True)
        finally:
            self.exited = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LspError(f"{self.server_id} exited"))
            self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            future = self._pending.pop(int(message["id"]), None)
            if future is None or future.done():
                return
            error = message.get("error")
            if error:
                future.set_exception(LspError(str(error.get("message", error))))
            else:
                future.set_result(message.get("result"))
            return
        method = str(message.get("method", ""))
        if method in ("client/registerCapability", "client/unregisterCapability"):
            self._note_registration(method, message.get("params") or {})
        if "id" in message:
            await self._answer(message, method)
            return
        if method == "textDocument/publishDiagnostics":
            self._publish(message.get("params") or {})

    async def _answer(self, message: dict[str, Any], method: str) -> None:
        """Reply to a server-to-client request; silence here hangs the server."""
        if method == "workspace/configuration":
            items = (message.get("params") or {}).get("items") or []
            result: Any = [self._configuration(item.get("section")) for item in items]
        elif method == "workspace/workspaceFolders":
            result = [{"name": self.root.name, "uri": path_to_uri(self.root)}]
        elif method in _STATIC_REPLIES:
            result = _STATIC_REPLIES[method]
        else:
            result = None
        with contextlib.suppress(LspError):
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _note_registration(self, method: str, params: dict[str, Any]) -> None:
        """Watch for dynamic ``textDocument/diagnostic`` registration.

        pyright advertises no static ``diagnosticProvider``; it registers the
        method at runtime and then stops publishing, so a client that only
        looks at the initialize response never sees a single diagnostic.
        """
        key = "registrations" if method == "client/registerCapability" else "unregisterations"
        entries = params.get(key)
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("method") != "textDocument/diagnostic":
                continue
            if method == "client/registerCapability":
                self._pull_registered = True
            self._registered.set()

    @property
    def pull_supported(self) -> bool:
        """True when ``textDocument/diagnostic`` is worth asking for."""
        return bool(self.capabilities.get("diagnosticProvider")) or self._pull_registered

    def _configuration(self, section: str | None) -> Any:
        """The ``initializationOptions`` value a server asks for by dotted path."""
        if not section:
            return self.initialization or None
        value: Any = self.initialization
        for key in section.split("."):
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    def _publish(self, params: dict[str, Any]) -> None:
        path = uri_to_path(str(params.get("uri", "")))
        if path is None:
            return
        diagnostics = params.get("diagnostics")
        items = [item for item in diagnostics if isinstance(item, dict)] if isinstance(
            diagnostics, list
        ) else []
        self.push[path] = items
        self.publish_seq[path] = self.publish_seq.get(path, 0) + 1
        self._published.set()
        if self.on_diagnostics is not None:
            try:
                self.on_diagnostics(path, self.diagnostics_for(path))
            except Exception:  # noqa: BLE001 - a listener must not break the reader
                log.debug("diagnostics listener raised", exc_info=True)

    # -- requests --------------------------------------------------------

    async def request(
        self, method: str, params: Any = None, *, timeout: float = REQUEST_TIMEOUT_SEC
    ) -> Any:
        """Send a request and wait for its answer; raises :class:`LspError`."""
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise LspError(f"{self.server_id} did not answer {method} in {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def try_request(
        self, method: str, params: Any = None, *, timeout: float = REQUEST_TIMEOUT_SEC
    ) -> Any:
        """:meth:`request`, but a failure is ``None`` rather than an exception.

        Every capability the tools expose is optional in LSP, and a server that
        does not implement ``textDocument/hover`` answering with an error must
        read as "no hover here", not as a failed tool call.
        """
        try:
            return await self.request(method, params, timeout=timeout)
        except (LspError, asyncio.IncompleteReadError):
            return None

    async def notify(self, method: str, params: Any = None) -> None:
        with contextlib.suppress(LspError):
            await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Run the ``initialize``/``initialized`` handshake.

        Raises :class:`LspError` when the server never completes it; the
        manager turns that into ``broken`` rather than into a failed edit.
        """
        self._reader = asyncio.create_task(self._pump(), name=f"lsp-{self.server_id}")
        result = await self.request(
            "initialize",
            {
                "processId": self.process.pid,
                "rootUri": path_to_uri(self.root),
                "workspaceFolders": [
                    {"name": self.root.name, "uri": path_to_uri(self.root)}
                ],
                "initializationOptions": dict(self.initialization),
                "capabilities": {
                    "window": {"workDoneProgress": True},
                    "workspace": {
                        "configuration": True,
                        "workspaceFolders": True,
                        "didChangeWatchedFiles": {"dynamicRegistration": True},
                        "workspaceEdit": {"documentChanges": True},
                        "symbol": {"dynamicRegistration": False},
                    },
                    "textDocument": {
                        "synchronization": {"didOpen": True, "didChange": True},
                        "publishDiagnostics": {"versionSupport": False},
                        "diagnostic": {
                            "dynamicRegistration": True,
                            "relatedDocumentSupport": True,
                        },
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": True},
                        "references": {"dynamicRegistration": False},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "rename": {"prepareSupport": True},
                    },
                },
            },
            timeout=INITIALIZE_TIMEOUT_SEC,
        )
        self.capabilities = (result or {}).get("capabilities") or {}
        await self.notify("initialized", {})
        if self.initialization:
            await self.notify(
                "workspace/didChangeConfiguration", {"settings": dict(self.initialization)}
            )
        self.ready = True

    async def shutdown(self) -> None:
        """Ask the server to exit, then make sure it did."""
        self.ready = False
        with contextlib.suppress(Exception):
            await self.request("shutdown", timeout=2.0)
        await self.notify("exit")
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
        self.exited = True

    @property
    def alive(self) -> bool:
        """True while the process is up and the handshake has completed."""
        return self.ready and not self.exited and self.process.returncode is None

    # -- documents -------------------------------------------------------

    @property
    def _sync_kind(self) -> int:
        sync = self.capabilities.get("textDocumentSync")
        if isinstance(sync, int):
            return sync
        if isinstance(sync, dict) and isinstance(sync.get("change"), int):
            return int(sync["change"])
        return 1

    async def open_file(self, path: Path, text: str) -> int:
        """``didOpen`` the first time, ``didChange`` afterwards; returns the version.

        Diagnostics are deliberately *not* cleared on a change: clangd and
        friends only re-emit when the content really differs, so wiping here
        would lose errors on a no-op touch (upstream's note in ``notify.open``).
        """
        path = path.resolve()
        uri = path_to_uri(path)
        version = self.documents.get(path)
        if version is None:
            self.push.pop(path, None)
            self.pull.pop(path, None)
            await self.notify(
                "workspace/didChangeWatchedFiles",
                {"changes": [{"uri": uri, "type": 1}]},
            )
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id(path),
                        "version": 0,
                        "text": text,
                    }
                },
            )
            self.documents[path] = 0
            return 0
        await self.notify(
            "workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 2}]}
        )
        next_version = version + 1
        changes: list[dict[str, Any]] = [{"text": text}]
        if self._sync_kind == SYNC_INCREMENTAL:
            lines = text.splitlines() or [""]
            changes = [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": len(lines) + 1, "character": 0},
                    },
                    "text": text,
                }
            ]
        await self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": next_version}, "contentChanges": changes},
        )
        self.documents[path] = next_version
        return next_version

    async def close_file(self, path: Path) -> None:
        path = path.resolve()
        if self.documents.pop(path, None) is None:
            return
        await self.notify(
            "textDocument/didClose", {"textDocument": {"uri": path_to_uri(path)}}
        )

    # -- diagnostics -----------------------------------------------------

    def diagnostics_for(self, path: Path) -> list[dict[str, Any]]:
        """Push and pull diagnostics for one file, de-duplicated."""
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        resolved = path.resolve()
        for item in [*self.push.get(resolved, []), *self.pull.get(resolved, [])]:
            key = json.dumps(
                {
                    "code": item.get("code"),
                    "severity": item.get("severity"),
                    "message": item.get("message"),
                    "source": item.get("source"),
                    "range": item.get("range"),
                },
                sort_keys=True,
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def all_diagnostics(self) -> dict[Path, list[dict[str, Any]]]:
        """Every file this connection knows a diagnostic for."""
        return {path: self.diagnostics_for(path) for path in {*self.push, *self.pull}}

    async def pull_diagnostics(self, path: Path) -> bool:
        """Ask for ``textDocument/diagnostic``; True when the server answered.

        Servers that publish (pyright, gopls) ignore this; servers that only
        answer on request (rust-analyzer's ``diagnosticProvider``) need it.
        """
        if not self.pull_supported:
            return False
        report = await self.try_request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": path_to_uri(path)}},
            timeout=3.0,
        )
        if not isinstance(report, dict) or not isinstance(report.get("items"), list):
            return False
        self.pull[path.resolve()] = [
            item for item in report["items"] if isinstance(item, dict)
        ]
        return True

    async def wait_for_diagnostics(
        self, path: Path, *, since: int = 0, timeout: float = DIAGNOSTICS_TIMEOUT_SEC
    ) -> None:
        """Block until the server has said something about ``path``, or ``timeout``.

        Servers split into two camps and some are in both.  Push servers
        (gopls, clangd) send ``publishDiagnostics``; ``since`` is
        :attr:`publish_seq` read *before* the edit, so a second edit to the
        same file is not answered out of the first edit's publish, and each
        publish starts a short debounce that a further publish restarts.  Pull
        servers (pyright) answer ``textDocument/diagnostic`` instead, and
        register the method *after* the first ``didOpen`` — hence the retry
        whenever a registration lands.
        """
        resolved = path.resolve()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if self.pull_supported and await self.pull_diagnostics(resolved):
                return
            fresh = self.publish_seq.get(resolved, 0) > since
            self._published.clear()
            self._registered.clear()
            published = asyncio.ensure_future(self._published.wait())
            registered = asyncio.ensure_future(self._registered.wait())
            done, pending = await asyncio.wait(
                {published, registered},
                timeout=min(DIAGNOSTICS_DEBOUNCE_SEC, remaining) if fresh else remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                # Nothing moved: either the debounce expired on a publish we
                # already have, or the server has nothing to say.
                return
        if self.publish_seq.get(resolved, 0) <= since:
            await self.pull_diagnostics(resolved)


__all__ = [
    "DIAGNOSTICS_DEBOUNCE_SEC",
    "DIAGNOSTICS_TIMEOUT_SEC",
    "INITIALIZE_TIMEOUT_SEC",
    "REQUEST_TIMEOUT_SEC",
    "SYNC_INCREMENTAL",
    "LspClient",
    "LspError",
    "path_to_uri",
    "uri_to_path",
]
