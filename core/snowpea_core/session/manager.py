"""Session bookkeeping and the ``session.event`` broadcast hub (contract §4)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast

from snowpea_core.agent.team_config import active_team
from snowpea_core.config.model_routing import (
    ModelRoute,
    model_config_for,
    resolve_reference,
    route_for,
)
from snowpea_core.config.paths import utc_now
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.providers import effort as effort_scale
from snowpea_core.server.protocol import Mode, SessionEvent, SessionKind, SessionSummary
from snowpea_core.session import events as event_builders
from snowpea_core.session.history import History, message_from_json
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

    async def announce_sessions_changed(self, reason: str, session_id: str) -> None:
        """Tell every authenticated client the session list just moved.

        The IDE listens for ``sessions.changed`` and re-reads ``session.list``;
        without this, a TUI/CLI session only appeared after the IDE window
        regained focus.
        """
        if self.hub is None:
            return
        try:
            await self.hub.notify(
                "sessions.changed",
                {"reason": reason, "sessionId": session_id},
            )
        except Exception:  # noqa: BLE001 - a dead socket must not fail create/close
            log.debug("could not broadcast sessions.changed", exc_info=True)
        #: ``(agent_name, workdir) -> the definition's ``model:`` field``.
        #: Injected by ``wire_core`` because resolving a definition needs the
        #: plugin loader, which lives on ``Core``.  Three of the five session
        #: creators used to forget to pass ``definition_model`` by hand; making
        #: it derived here is what stops that from happening again
        #: (CORE-model-assignment B-P2-1).
        self.definition_model_for: Any = None

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
        session_pin: ModelRoute | None = None,
        parent_session_id: str | None = None,
        kind: SessionKind = "chat",
        job_id: str | None = None,
        effort: str | None = None,
        deny_exec: bool = False,
    ) -> Session:
        """Register a new session and persist its row.

        ``session_id`` re-opens a session under an id that already exists on
        disk, which is how a named agent comes back after a restart with the
        same id its gateway bindings and job history refer to (M7 §6).

        ``kind``, ``parent_session_id`` and ``job_id`` say what opened the
        session so ``session.list`` can tell a human's thread from a scheduled
        run or a spawned child, and link the two (CORE-session-kind).
        """
        resolved_dir = Path(workdir).expanduser()
        selected_team = active_team(self.settings, resolved_dir)
        if definition_model is None and agent:
            definition_model = self._definition_model(agent, resolved_dir)
        route = route_for(
            self.settings,
            provider=provider,
            model=model,
            agent=agent,
            definition_model=definition_model,
            workdir=resolved_dir,
            session_pin=session_pin,
        )
        session = Session(
            id=session_id or f"s-{uuid.uuid4().hex[:12]}",
            workdir=resolved_dir,
            mode=mode or self.default_mode(resolved_dir),
            provider=route.provider,
            model=route.model,
            agent=agent,
            team=selected_team.name if selected_team else None,
            team_agents=selected_team.agents if selected_team else (),
            origin_surface=origin_surface,
            created_at=utc_now(),
            max_concurrent=self.max_concurrent(resolved_dir, max_concurrent),
            origin_conn=origin_conn,
            parent_session_id=parent_session_id,
            kind=kind,
            job_id=job_id,
            effort=effort_scale.normalize(effort),
            deny_exec=deny_exec,
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
                parent_session_id=session.parent_session_id,
                kind=session.kind,
                job_id=session.job_id,
            )
            if session.effort:
                await self.store.update_effort(session.id, session.effort)
        log.info("session %s created (%s, mode=%s)", session.id, session.workdir, session.mode)
        await self.announce_sessions_changed("created", session.id)
        return session

    def _definition_model(self, agent: str, workdir: Path) -> str | None:
        """The agent definition's ``model:`` field, or ``None`` if unknown."""
        lookup = self.definition_model_for
        if lookup is None:
            return None
        try:
            return lookup(agent, workdir)
        except Exception:  # noqa: BLE001 - routing must not fail on a bad file
            log.debug("could not read the definition model for %s", agent, exc_info=True)
            return None

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def restore(self, session_id: str, *, origin_conn: Any = None) -> Session | None:
        """Rehydrate a persisted conversation after a daemon restart."""
        live = self.get(session_id)
        if live is not None:
            return live
        if self.store is None:
            return None
        row = await self.store.session(session_id)
        if row is None:
            return None
        # A thread whose daemon was killed mid-turn has a ``turn.started`` with
        # no ``turn.done``, so every replay of it ends on a turn that never
        # finishes.  The startup scan fixes the rows that existed then; this
        # covers one restored afterwards (CORE-dangling-turns).
        try:
            await self.store.repair_dangling_turns(session_id)
        except StoreClosed:  # pragma: no cover - shutdown race
            log.debug("skipping the turn repair for %s: session store is closed", session_id)
        workdir = Path(str(row["workdir"])).expanduser()
        selected_team = active_team(self.settings, workdir)
        stored_messages = await self.store.messages(session_id)
        history = History(
            messages=[
                message_from_json(str(item["role"]), item["content"])
                for item in stored_messages
            ]
        )
        session = Session(
            id=session_id,
            workdir=workdir,
            mode=row["mode"],
            provider=row.get("provider"),
            model=row.get("model"),
            team=selected_team.name if selected_team else None,
            team_agents=selected_team.agents if selected_team else (),
            origin_surface=row.get("origin_surface"),
            created_at=str(row["created_at"]),
            parent_session_id=row.get("parent_session_id"),
            kind=cast(SessionKind, row.get("kind") or "chat"),
            job_id=row.get("job_id"),
            effort=effort_scale.normalize(row.get("effort")),
            max_concurrent=self.max_concurrent(workdir),
            history=history,
            seq=await self.store.max_seq(session_id),
            context_used=history.estimate_tokens(),
            origin_conn=origin_conn,
        )
        self._sessions[session.id] = session
        await self.store.reopen_session(session.id)
        log.info("session %s restored (%s, mode=%s)", session.id, session.workdir, session.mode)
        return session

    def list(self) -> list[SessionSummary]:
        return [s.summary() for s in self._sessions.values() if s.closed_at is None]

    def next_seq(self, session: Session) -> int:
        return session.next_seq()

    async def set_mode(self, session: Session, mode: Mode) -> Mode:
        session.mode = mode
        if self.store is not None:
            await self.store.update_mode(session.id, mode)
        return mode

    async def set_model(self, session: Session, reference: str | None) -> ModelRoute:
        """Pin ``session`` to a model, or clear the pin, and persist it.

        Returns the route now in effect.  ``None``/``"inherit"`` clears the pin
        and re-runs the configured routing for this session's agent and
        workdir, so clearing goes back to what a fresh session would get
        rather than to nothing (CORE-model-assignment B-P2-3).

        Raises :class:`ValueError` when ``reference`` resolves to nothing — a
        pin the user asked for must not silently become something else.
        """
        text = str(reference or "").strip()
        if not text or text == "inherit":
            route = route_for(
                self.settings,
                agent=session.agent,
                definition_model=(
                    self._definition_model(session.agent, Path(session.workdir))
                    if session.agent
                    else None
                ),
                workdir=session.workdir,
            )
        else:
            route = resolve_reference(
                self.settings,
                text,
                config=model_config_for(self.settings, session.workdir),
            )
            if not route.resolved():
                raise ValueError(
                    f"unknown model {text!r}: not a profile id, a 'vendor:model' pair "
                    "or a known vendor"
                )
        session.provider = route.provider
        session.model = route.model
        if self.store is not None:
            await self.store.update_model(session.id, route.provider, route.model)
        return route

    async def set_effort(self, session: Session, effort: str | None) -> str | None:
        """Pin how hard ``session`` may think, or clear the pin.

        Returns the pin now stored (``None`` when it was cleared).  ``None``
        and ``"auto"`` both clear it, so the session goes back to what
        ``agent.effortBy`` / ``agent.effort`` would give a fresh one rather
        than to nothing.  Raises :class:`ValueError` for a tier that does not
        exist: a pin the user asked for must not silently become something
        else (CORE-effort).
        """
        text = str(effort or "").strip().lower()
        if text and text not in (effort_scale.AUTO, *effort_scale.EFFORTS):
            raise ValueError(
                f"unknown effort {text!r}: one of {', '.join(effort_scale.EFFORTS)} or auto"
            )
        session.effort = effort_scale.normalize(text)
        if self.store is not None:
            await self.store.update_effort(session.id, session.effort)
        return session.effort

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
        await self.announce_sessions_changed("closed", session_id)
        return True

    async def finish_open_turns(self, reason: str = "interrupted") -> tuple[str, ...]:
        """Close every turn still in flight, before the store is torn down.

        ``Daemon.stop`` cancels the turn tasks, and a turn cancelled during
        shutdown deliberately skips its own final write so it cannot race the
        closing store (CORE-session-race in :func:`agent.loop.run_turn`).  The
        cost was a thread that replays forever as "still thinking": the event
        log kept ``turn.started`` with nothing after it.  Emitting here, while
        the hub and the store are both still up, is what closes it — exactly
        once, because the turn id is claimed off the session first
        (CORE-dangling-turns).

        Returns the turn ids it closed.  (A tuple, not a list: this class has a
        method called ``list``, which shadows the builtin in an annotation.)
        """
        if self.hub is None:
            return ()
        finished: list[str] = []
        for session in list(self._sessions.values()):
            turn_id = session.current_turn
            if not turn_id:
                continue
            session.current_turn = None
            session.finished_turns.add(turn_id)
            # The prompt this turn began with is in the history already; keep
            # it, or a resume would open on a conversation that never asked.
            await persist_history(self.store, session)
            try:
                await self.hub.emit_event(
                    session.id, event_builders.turn_done(turn_id, reason, synthetic=True)
                )
            except StoreClosed:  # pragma: no cover - the store went first
                log.debug("could not close turn %s: session store is closed", turn_id)
                continue
            except Exception:  # noqa: BLE001 - shutdown must finish regardless
                log.debug("could not close turn %s", turn_id, exc_info=True)
                continue
            finished.append(turn_id)
        if finished:
            log.info("closed %d turn(s) still in flight at shutdown", len(finished))
        return tuple(finished)

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


async def persist_history(store: Any, session: Any) -> bool:
    """Write ``session.history`` to the store as it stands; True when written.

    Called at the end of every turn *and* on every interruption, so the user
    message a turn began with survives even when the turn does not: a resumed
    session used to forget any prompt whose turn was stopped, because the
    snapshot was written only by the loop's own finish, which the synthetic
    close had already claimed (CORE-dangling-turns).
    """
    from snowpea_core.session.history import message_to_json

    if store is None:
        return False
    try:
        await store.replace_messages(
            session.id,
            [
                {"role": message.role, "content": message_to_json(message)}
                for message in session.history.snapshot()
            ],
        )
    except Exception:  # noqa: BLE001 - persistence must not fail a turn
        log.debug("could not persist history for %s", session.id, exc_info=True)
        return False
    return True


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
            except (StoreClosed, sqlite3.Error) as exc:
                if isinstance(exc, StoreClosed):
                    # The daemon closed the session store out from under a turn
                    # still winding down at shutdown (CORE-session-race); the
                    # event is dropped rather than raised into the turn task.
                    log.debug("dropping %s event for %s: session store is closed", kind, session_id)
                else:
                    # The store already retried and logged; a disk that will not
                    # take the row must not swallow the answer the model just
                    # produced, so the event is still broadcast below.
                    log.error(
                        "dropping %s event for %s: session store write failed",
                        kind,
                        session_id,
                        exc_info=True,
                    )
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
