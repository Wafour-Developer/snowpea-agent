"""Session bookkeeping and the ``session.event`` broadcast hub (contract §4)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from snowpea_core.config.model_routing import route_for
from snowpea_core.config.paths import utc_now
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.server.protocol import Mode, SessionEvent, SessionSummary
from snowpea_core.session import events as event_builders
from snowpea_core.session.session import Session
from snowpea_core.session.store import Store, StoreClosed

log = logging.getLogger("snowpea.session")

DEFAULT_MODE: Mode = "accept"



class SessionManager:
    """Creates, tracks and closes sessions; resolves per-project defaults."""

    def __init__(
        self,
        store: Store | None = None,
        settings: Settings | None = None,
        hub: EventHub | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self.store = store
        self.settings = settings or Settings()
        self.hub = hub
        #: Coroutines run with the session id when a session closes, so a tool
        #: that holds per-session state (a browser context, say) can release it.
        self.on_close: list[Any] = []

    def bind(self, store: Store, settings: Settings, hub: EventHub) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.store = store
        self.settings = settings
        self.hub = hub

    # -- defaults ------------------------------------------------------
    def default_mode(self, workdir: Path | str) -> Mode:
        """Project ``defaultMode`` if the workdir declares one, else ``accept``."""
        project = ProjectSettings.load(workdir)
        return project.defaultMode or DEFAULT_MODE

    def max_concurrent(self, workdir: Path | str, override: int | None = None) -> int:
        """``agents.max_concurrent``: request > project > global settings."""
        if override is not None:
            return max(1, override)
        project = ProjectSettings.load(workdir)
        if project.agents.max_concurrent is not None:
            return max(1, project.agents.max_concurrent)
        return max(1, self.settings.agents.max_concurrent)

    # -- lifecycle -----------------------------------------------------
    async def create(
        self,
        workdir: Path | str,
        mode: Mode | None = None,
        provider: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        definition_model: str | None = None,
        max_concurrent: int | None = None,
        origin_surface: str | None = None,
        origin_conn: Any = None,
        session_id: str | None = None,
    ) -> Session:
        """Register a new session and persist its row.

        ``session_id`` re-opens a session under an id that already exists on
        disk, which is how a named agent comes back after a restart with the
        same id its gateway bindings and job history refer to (M7 §6).
        """
        resolved_dir = Path(workdir).expanduser()
        route = route_for(
            self.settings,
            provider=provider,
            model=model,
            agent=agent,
            definition_model=definition_model,
        )
        session = Session(
            id=session_id or f"s-{uuid.uuid4().hex[:12]}",
            workdir=resolved_dir,
            mode=mode or self.default_mode(resolved_dir),
            provider=route.provider,
            model=route.model,
            agent=agent,
            origin_surface=origin_surface,
            created_at=utc_now(),
            max_concurrent=self.max_concurrent(resolved_dir, max_concurrent),
            origin_conn=origin_conn,
        )
        self._sessions[session.id] = session
        if self.store is not None:
            await self.store.insert_session(
                session.id,
                str(session.workdir),
                session.mode,
                session.provider,
                session.model,
                session.origin_surface,
                session.created_at,
            )
        log.info("session %s created (%s, mode=%s)", session.id, session.workdir, session.mode)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[SessionSummary]:
        return [s.summary() for s in self._sessions.values() if s.closed_at is None]

    def next_seq(self, session: Session) -> int:
        return session.next_seq()

    async def set_mode(self, session: Session, mode: Mode) -> Mode:
        session.mode = mode
        if self.store is not None:
            await self.store.update_mode(session.id, mode)
        return mode

    async def close(self, session_id: str) -> bool:
        """Close a session, cancelling any in-flight turn."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.closed_at = utc_now()
        session.interrupt.set()
        task = session.turn_task
        if task is not None and not task.done():
            task.cancel()
        for hook in list(self.on_close):
            try:
                await hook(session_id)
            except Exception:  # noqa: BLE001 - one bad hook must not block close
                log.debug("session close hook failed for %s", session_id, exc_info=True)
        backend = getattr(session, "backend", None)
        if backend is not None:
            try:
                await backend.close()
            except Exception:  # noqa: BLE001 - a dead container must not block close
                log.debug("backend close failed for session %s", session_id, exc_info=True)
        if self.store is not None:
            try:
                await self.store.close_session(session_id, session.closed_at)
            except StoreClosed:
                log.debug(
                    "skipping close_session write for %s: session store is closed", session_id
                )
        log.info("session %s closed", session_id)
        return True

    async def close_all(self, *, timeout: float = 5.0) -> None:
        """Cancel and close every live session (CORE-session-race).

        ``Daemon.stop`` calls this before closing the session store, memory
        and gateway: a turn task cancelled only *after* those are torn down
        can still land its final ``turn.done`` write on an already-closed
        store. Every ``turn_task`` is cancelled up front, then all of them are
        awaited together under one bounded ``timeout`` so a task stuck outside
        any cancellable await (e.g. mid-``asyncio.to_thread``) cannot block
        shutdown forever. Each session is then closed the normal way (see
        :meth:`close`), which is safe here because the store is still open.
        """
        session_ids = list(self._sessions.keys())
        tasks: list[asyncio.Task[Any]] = []
        for session_id in session_ids:
            session = self._sessions.get(session_id)
            if session is None:
                continue
            session.interrupt.set()
            task = session.turn_task
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
                )
            except TimeoutError:
                log.warning(
                    "close_all: %d turn task(s) did not finish within %ss", len(tasks), timeout
                )
        for session_id in session_ids:
            await self.close(session_id)

    def __len__(self) -> int:
        return len(self._sessions)


class EventHub:
    """Assigns sequence numbers, persists events and fans them out."""

    def __init__(self, store: Store | None = None, sessions: SessionManager | None = None) -> None:
        self._subscribers: list[tuple[Any, str | None]] = []
        #: Authenticated connections that receive plain notifications even
        #: before they open a session — that is what "broadcast to every
        #: authenticated client" means for the shared approval queue (M5 §4).
        self._clients: list[Any] = []
        self.store = store
        self.sessions = sessions

    def bind(self, store: Store, sessions: SessionManager) -> None:
        self.store = store
        self.sessions = sessions

    # -- subscriptions -------------------------------------------------
    def subscribe(self, conn: Any, session_id: str | None = None) -> None:
        """Subscribe ``conn``; ``session_id=None`` means every session."""
        if (conn, session_id) in self._subscribers:
            return
        self._subscribers.append((conn, session_id))

    def unsubscribe(self, conn: Any) -> None:
        self._subscribers = [entry for entry in self._subscribers if entry[0] is not conn]
        self._clients = [client for client in self._clients if client is not conn]

    def register_client(self, conn: Any) -> None:
        """Add an authenticated connection to the notification broadcast set."""
        if not any(client is conn for client in self._clients):
            self._clients.append(conn)

    def clients(self) -> list[Any]:
        return list(self._clients)

    def subscribers_for(self, session_id: str) -> list[Any]:
        seen: list[Any] = []
        for conn, filter_id in self._subscribers:
            if filter_id is not None and filter_id != session_id:
                continue
            if getattr(conn, "closed", False):
                continue
            if conn not in seen:
                seen.append(conn)
        return seen

    # -- emit ----------------------------------------------------------
    async def emit(self, session_id: str, kind: str, payload: dict[str, Any]) -> SessionEvent:
        """Number, persist and broadcast one ``session.event``."""
        session = self.sessions.get(session_id) if self.sessions else None
        seq = session.next_seq() if session is not None else 0
        body = event_builders.validate(kind, payload)
        ts = utc_now()
        if self.store is not None:
            try:
                await self.store.append_event(session_id, seq, kind, body, ts)
            except StoreClosed:
                # The daemon closed the session store out from under a turn
                # still winding down at shutdown (CORE-session-race); the
                # event is dropped rather than raised into the turn task.
                log.debug("dropping %s event for %s: session store is closed", kind, session_id)
        event = SessionEvent(sessionId=session_id, seq=seq, kind=kind, payload=body, ts=ts)
        params = event.model_dump(mode="json")
        for conn in self.subscribers_for(session_id):
            try:
                await conn.notify("session.event", params)
            except Exception:  # noqa: BLE001 - a dead socket must not stop the turn
                log.debug("dropping session.event for a closed connection", exc_info=True)
        return event

    async def emit_event(self, session_id: str, event: event_builders.Event) -> SessionEvent:
        """Emit a ``(kind, payload)`` pair built by :mod:`session.events`."""
        kind, payload = event
        return await self.emit(session_id, kind, payload)

    async def notify(self, method: str, params: dict[str, Any], *, exclude: Any = None) -> None:
        """Send a plain notification to every subscribed connection."""
        seen: list[Any] = []
        targets = [conn for conn, _ in self._subscribers] + self._clients
        for conn in targets:
            if conn is exclude or any(other is conn for other in seen):
                continue
            if getattr(conn, "closed", False):
                continue
            seen.append(conn)
            try:
                await conn.notify(method, params)
            except Exception:  # noqa: BLE001
                log.debug("dropping %s for a closed connection", method, exc_info=True)


__all__ = ["DEFAULT_MODE", "EventHub", "SessionManager", "utc_now"]
