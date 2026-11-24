"""External CLI sessions run as terminal sessions (M37 §2.8, capability ``externalPty``).

``external.open`` / ``external.create`` with ``transport: "pty"`` start the CLI
in a terminal the daemon holds (:mod:`snowpea_core.terminal`), with the CLI's
terminal profile (:mod:`snowpea_core.terminal.profiles`): the new-session argv,
or the resume argv for an existing session id.  The session's row then says
``host: "terminal"`` (view only for the structured calls: ``external.send``
answers ``external_view_only``, as for any CLI in a terminal) and
``terminal: {kind: "pty", terminalId, status}``; a client that knows
``externalPty`` shows the terminal pane, an old one keeps its read-only view.

* A session already live elsewhere (the person's own terminal) stays view
  only: no second CLI is started on it.
* A session whose terminal is gone (a reboot lost its holder) is free again
  (``host: "none"``) and opening it with ``pty`` resumes it from its session
  id — the one the CLI's hooks reported (``terminal.report``) or, for Claude
  Code, the id snowpea gave it with ``--session-id``.
* A new Codex, agy or Cursor session has no id until its ``SessionStart``
  hook reports one; until then (at most :data:`SESSION_WAIT_SEC` on create)
  its row is keyed by the terminal id, and it is re-keyed once the id arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.external.pty")

#: How long ``external.create {transport: "pty"}`` waits for a hook to name the session.
SESSION_WAIT_SEC = 8.0


class PtyBridge:
    """The link between external session rows and the terminals that run them."""

    def __init__(self, core: Core) -> None:
        self.core = core
        #: terminal id -> (cli, session id or the terminal id while unknown).
        self.links: dict[str, tuple[str, str]] = {}
        self.session_wait = SESSION_WAIT_SEC
        self._listening: Any = None

    # -- terminals -----------------------------------------------------------
    def terminals(self) -> Any:
        from snowpea_core.server.terminal_handlers import ensure_manager

        manager = ensure_manager(self.core)
        if self._listening is not manager:
            manager.listeners.append(self._on_terminal_event)
            self._listening = manager
        return manager

    def live(self, cli: str, session_id: str) -> Any:
        """The live terminal running ``cli`` session ``session_id`` (or keyed by its id)."""
        manager = getattr(self.core, "terminals", None)
        if manager is None:
            return None
        for term in manager.terminals():
            if not term.alive or (term.spec.external or {}).get("cli") != cli:
                continue
            if term.session_id == session_id or (not term.session_id and term.id == session_id):
                return term
        return None

    @staticmethod
    def wire(term: Any) -> dict[str, Any]:
        """``ExternalTerminal`` of kind ``pty``."""
        return {
            "kind": "pty", "pane": "", "session": "", "window": "",
            "terminalId": term.id, "status": term.status.status.wire(),
            "modeState": "unknown", "prompt": None,
        }

    @staticmethod
    def row_status(term: Any) -> str:
        """``ExternalSessionRow.status`` from the terminal's state."""
        state = term.status.state
        if not term.alive:
            return "idle"
        if state in ("working", "blocked"):
            return "running"
        return "error" if state == "error" else "idle"

    async def start(
        self, conn: Any, cli: str, cwd: str, *, resume: str | None, options: list[str],
        confirm_dangerous: bool, trust: bool, title: str | None = None,
        model: str | None = None,
    ) -> Any:
        """Start ``cli`` in a new terminal (its profile, its gates) and return the terminal."""
        from snowpea_core.terminal import profiles

        manager = self.terminals()
        params = SimpleNamespace(
            profile=cli, cwd=cwd, noProject=False, options=list(options or []),
            confirmDangerous=bool(confirm_dangerous), resumeSessionId=resume, trust=trust,
            title=title, snowpeaSessionId=None, model=model,
        )
        spec = await profiles.build_spec(self.core, conn, params, cwd)
        settings = getattr(self.core.settings, "terminals", None)
        spec.cols = int(getattr(settings, "headlessCols", 160) or 160)
        spec.rows = int(getattr(settings, "headlessRows", 48) or 48)
        info = await manager.create(spec)
        term = manager.get(info["terminalId"])
        self.links[term.id] = (cli, term.session_id or term.id)
        return term

    async def wait_session_id(self, term: Any, timeout: float | None = None) -> str | None:
        """The CLI's session id once a hook reported it (``None`` after ``timeout``)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.session_wait if timeout is None else timeout)
        while not term.session_id and term.alive and loop.time() < deadline:
            await asyncio.sleep(0.1)
        return term.session_id  # type: ignore[no-any-return]

    # -- events --------------------------------------------------------------
    def _on_terminal_event(self, method: str, params: dict[str, Any]) -> Any:
        terminal_id = str(params.get("terminalId") or "")
        if terminal_id not in self.links:
            # A terminal restored after a daemon restart: link it on first sight.
            manager = getattr(self.core, "terminals", None)
            term = None
            with contextlib.suppress(Exception):
                term = manager.get(terminal_id) if manager is not None else None
            cli = (getattr(term, "spec", None) and (term.spec.external or {}).get("cli"))
            if not cli:
                return None
            self.links[terminal_id] = (cli, term.session_id or term.id)
        return self._apply(method, terminal_id, params)

    async def _apply(self, method: str, terminal_id: str, params: dict[str, Any]) -> None:
        manager = getattr(self.core, "external", None)
        if manager is None:
            return
        cli, key = self.links[terminal_id]
        if method == "terminal.sessionId":
            session_id = str(params.get("sessionId") or "")
            if session_id and session_id != key:
                self.links[terminal_id] = (cli, session_id)
                await manager.pty_rekey(cli, key, session_id)
            return
        if method == "terminal.status":
            await manager.pty_changed(cli, key)
        elif method in ("terminal.exit", "terminal.closed"):
            if method == "terminal.closed":
                self.links.pop(terminal_id, None)
            await manager.pty_ended(cli, key)


def attach(core: Core, manager: Any) -> PtyBridge | None:
    """Give the external manager its terminal bridge (when this core has terminals)."""
    try:
        from snowpea_core.server.terminal_handlers import ensure_manager  # noqa: F401
    except ImportError:  # pragma: no cover - a core without terminals
        return None
    bridge = PtyBridge(core)
    manager.pty = bridge
    with contextlib.suppress(Exception):
        bridge.terminals()
    return bridge


__all__ = ["SESSION_WAIT_SEC", "PtyBridge", "attach"]
