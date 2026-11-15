"""The Jarvis service: one supervising session per daemon (M19 WP3).

Jarvis is the reserved named agent ``jarvis`` (``agent/named.py``): a persistent
``kind="agent"`` session with memory namespace ``agent:jarvis``, working in
``jarvis.folder`` (default ``~/Snowpea/Jarvis``, created on enable) on the model
``jarvis.model``.  ``session.list`` marks it ``role: "jarvis"``; a client
without capability ``jarvis`` sees an ordinary named agent.

Lifecycle: ``jarvis.enable`` sets ``jarvis.enabled`` and opens (or reopens, with
its history) the session; a daemon started with the setting on does the same at
boot; ``jarvis.disable`` clears the setting and parks the session — closed, its
row and history kept for the next enable.  An outside edit of ``settings.json``
is followed through :meth:`JarvisService.apply_settings`.

While running, the :class:`~snowpea_core.jarvis.intake.Intake` watches every
session and wakes Jarvis by starting a ``[system]`` turn in its session.  The
delegation engine (:class:`~snowpea_core.jarvis.delegation.DelegationEngine`,
through :mod:`snowpea_core.jarvis.ports`) decides where every approval and
question goes: the service installs itself as the approval and question
queues' router, so ``ApprovalRequest.route`` / ``QuestionRequest.route`` are on
every request before it reaches a client.  Grants, pauses and takeovers are
the engine's alone (persisted); ``jarvis.takeover`` / ``jarvis.handback`` are
its handlers.  Decisions are recorded in the engine's ledger, which announces
each as ``jarvis.decision``; escalations go out as ``jarvis.escalation``,
honouring ``jarvis.notify.*``, and the ones that are not a waiting request are
kept open (:class:`~snowpea_core.jarvis.escalations.EscalationBook`) until a
person dismisses them.  :attr:`JarvisService.hooks` is where messenger
delivery (WP7), external sessions (WP5/6) and further tools (WP8) attach.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent import named
from snowpea_core.jarvis import overview
from snowpea_core.jarvis.classify import ClassifyRequest
from snowpea_core.jarvis.clock import Clock, iso
from snowpea_core.jarvis.delegation import (
    DelegationEngine,
    PauseState,
    RouteResult,
    _wire_events,
)
from snowpea_core.jarvis.escalations import EscalationBook
from snowpea_core.jarvis.intake import Digest, Intake
from snowpea_core.jarvis.ports import Hooks, build_engine
from snowpea_core.jarvis.projects import ProjectBook
from snowpea_core.server.protocol import (
    ApprovalRequest,
    JarvisAccessNotification,
    JarvisDecision,
    JarvisEscalationNotification,
    JarvisStateNotification,
    JarvisStatusResult,
    QuestionRequest,
    SessionJarvisState,
    SessionSummary,
    SettingsChangedNotification,
)
from snowpea_core.session.workspaces import canonical

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.jarvis")

#: The reserved named agent Jarvis is.
JARVIS_NAME = "jarvis"
#: ``turn.started.reason`` of a turn the intake started.
WAKE_REASON = "jarvis-intake"
#: Longest pause of the tick loop when nothing is due (budget recovery, expiry).
IDLE_TICK_SEC = 15.0
#: ``importance`` levels the messenger hook receives.
MESSENGER_IMPORTANCE = frozenset({"high", "urgent"})
#: Approvals whose classification the service keeps for a later re-check.
ROUTED_KEPT = 4096


class JarvisError(Exception):
    """A Jarvis request that cannot be carried out; ``code`` is a protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JarvisService:
    """Owns the Jarvis session, its intake and its notifications."""

    def __init__(
        self,
        core: Core,
        *,
        clock: Clock | None = None,
        engine: DelegationEngine | None = None,
    ) -> None:
        self.core = core
        self.clock = clock or Clock()
        self.home = Path(core.paths.home) / "jarvis"
        self.engine: DelegationEngine = engine or build_engine(core, now=self.clock.now)
        # One engine for the service, its tools and the jarvis.delegation.* handlers.
        core.jarvis_delegation = self.engine  # type: ignore[attr-defined]
        if not self.engine.events_wired:
            _wire_events(core, self.engine)
        self.engine.pause_listeners.append(self._on_pause)
        self.engine.ledger.listeners.append(self._on_ledger)
        self.hooks = Hooks()
        self.projects = ProjectBook(core, self.home / "projects.json")
        self.escalations = EscalationBook(self.home / "escalations.json")
        self.intake: Intake | None = None
        self.running = False
        self.problems: list[str] = []
        self._lock = asyncio.Lock()
        self._tick_task: asyncio.Task[None] | None = None
        self._tick_wake = asyncio.Event()
        self._escalation_ids: dict[tuple[Any, ...], str] = {}
        #: request id -> (what the engine classified, project), for re-checks.
        self._classified: dict[str, tuple[ClassifyRequest, str | None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        stored = self._registry().stored(JARVIS_NAME)
        self._session_id: str | None = stored.session_id if stored else None
        self.engine.jarvis_session_id = self._session_id
        sessions = getattr(core, "sessions", None)
        if sessions is not None:
            sessions.summary_extras = self.summary_fields
        overview.register_context_provider()
        from snowpea_core.jarvis import external as external_sessions

        external_sessions.attach(self)

    # -- small reads -----------------------------------------------------
    @property
    def session_id(self) -> str | None:
        """The Jarvis session's id; ``None`` before Jarvis mode was first enabled."""
        return self._session_id

    @property
    def enabled(self) -> bool:
        return bool(self.core.settings.jarvis.enabled)

    def folder(self) -> Path:
        return Path(os.path.expanduser(self.core.settings.jarvis.folder)).resolve()

    def now_iso(self) -> str:
        return iso(self.clock.now())

    def session(self) -> Session | None:
        """The live Jarvis session, when running."""
        if self._session_id is None:
            return None
        session = self.core.sessions.get(self._session_id)
        if session is None or session.closed_at is not None:
            return None
        return session

    def label(self, digest: Digest) -> str:
        """How a session is named in Jarvis's notes: title, project and id."""
        project = self.projects.name_of(digest.project) if digest.project else "?"
        title = (digest.title or "untitled").strip()
        if len(title) > 60:
            title = title[:59] + "…"
        return f'"{title}" ({project}, {digest.session_id})'

    def _registry(self) -> named.NamedAgentRegistry:
        return named.registry(self.core)

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """At daemon boot: come back if the setting is on."""
        if self.enabled:
            async with self._lock:
                await self._activate()
            await self.emit_state("started")

    async def stop(self) -> None:
        """At daemon shutdown: stop watching; the session closes with the others."""
        async with self._lock:
            await self._deactivate(close_session=False)

    async def enable(self, *, model: str | None = None, folder: str | None = None) -> None:
        """``jarvis.enable``: set the switches, then start (idempotent)."""
        async with self._lock:
            settings = self.core.settings.jarvis
            changed = not settings.enabled
            if model is not None:
                profile = model.strip() or None
                if profile is not None and not self._model_known(profile):
                    raise JarvisError("invalid_params", f"unknown model profile {profile!r}")
                changed = changed or profile != settings.model
                settings.model = profile
            if folder is not None:
                if not folder.strip():
                    raise JarvisError("invalid_params", "folder must not be empty")
                path = Path(os.path.expanduser(folder.strip()))
                if not path.is_absolute():
                    raise JarvisError("invalid_params", "folder must be an absolute path")
                changed = changed or folder.strip() != settings.folder
                settings.folder = folder.strip()
            settings.enabled = True
            self._make_folder()
            if changed:
                await self._save_settings()
            if self.running:
                await self._apply_session_settings()
            else:
                await self._activate()
        if changed:
            await self._announce_settings()
        await self.emit_state("enabled")

    async def disable(self) -> None:
        """``jarvis.disable``: clear the switch and park the session (kept)."""
        async with self._lock:
            settings = self.core.settings.jarvis
            changed = bool(settings.enabled)
            if changed:
                settings.enabled = False
                await self._save_settings()
            await self._deactivate(close_session=True)
        if changed:
            await self._announce_settings()
        await self.emit_state("disabled")

    async def apply_settings(self) -> None:
        """Follow ``jarvis.*`` after ``settings.json`` changed (outside edit, settings.set)."""
        async with self._lock:
            if self.enabled and not self.running:
                await self._activate()
                reason = "started"
            elif not self.enabled and self.running:
                await self._deactivate(close_session=True)
                reason = "stopped"
            elif self.running:
                await self._apply_session_settings()
                reason = "settings"
            else:
                return
        await self.emit_state(reason)  # type: ignore[arg-type]

    async def _save_settings(self) -> None:
        core = self.core
        try:
            core.settings.save(core.paths)
        except OSError as exc:
            raise JarvisError("internal", f"could not write settings.json: {exc}") from exc
        mark = getattr(core, "mark_settings_saved", None)
        if callable(mark):
            mark()

    async def _announce_settings(self) -> None:
        """``settings.changed`` for the switches enable/disable wrote (not re-applied)."""
        payload = SettingsChangedNotification(scope="global", keys=["jarvis"])
        with contextlib.suppress(Exception):
            await self.core.hub.notify("settings.changed", payload.model_dump(mode="json"))

    def _make_folder(self) -> None:
        try:
            self.folder().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JarvisError(
                "invalid_params", f"could not create the Jarvis folder {self.folder()}: {exc}"
            ) from exc

    def _model_known(self, profile: str) -> bool:
        from snowpea_core.config.model_routing import model_config_for, resolve_reference

        try:
            route = resolve_reference(
                self.core.settings,
                profile,
                config=model_config_for(self.core.settings, self.folder()),
            )
        except Exception:  # noqa: BLE001 - an unreadable profile is an unknown one
            return False
        return bool(route.resolved())

    async def _activate(self) -> None:
        self.problems = []
        try:
            self._make_folder()
        except JarvisError as exc:
            self.problems.append(str(exc))
            log.warning("Jarvis mode is on but could not start: %s", exc)
            return
        try:
            session = await self._open_session()
        except Exception as exc:  # noqa: BLE001 - a broken session must not stop the daemon
            self.problems.append(f"could not open the Jarvis session: {exc}")
            log.exception("could not open the Jarvis session")
            return
        await self._apply_session_settings(session)
        with contextlib.suppress(Exception):
            await self.projects.roots()  # primes the read limits' project list
        intake = Intake(self)
        self.intake = intake
        await intake.seed()
        intake.attach()
        self.running = True
        self.core.approvals.router = self.route_approval
        self.core.questions.router = self.route_question
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        log.info("Jarvis mode running (session %s, folder %s)", session.id, self.folder())

    async def _deactivate(self, *, close_session: bool) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
            self._tick_task = None
        if self.intake is not None:
            self.intake.detach()
        self.running = False
        if getattr(self.core.approvals, "router", None) == self.route_approval:
            self.core.approvals.router = None
        if getattr(self.core.questions, "router", None) == self.route_question:
            self.core.questions.router = None
        self._classified.clear()
        if not close_session:
            return
        self._registry().detach(JARVIS_NAME)
        if self._session_id is not None:
            with contextlib.suppress(Exception):
                await self.core.sessions.close(self._session_id)
            self.core.lifecycle.set_counter("sessions", len(self.core.sessions))
        log.info("Jarvis mode parked")

    async def _open_session(self) -> Session:
        """Reopen Jarvis's session with its history, or create it the first time."""
        core = self.core
        registry = self._registry()
        stored = registry.stored(JARVIS_NAME)
        folder = self.folder()
        session: Session | None = None
        if stored is not None:
            session = core.sessions.get(stored.session_id)
            if session is None:
                session = await core.sessions.restore(stored.session_id)
            if session is not None:
                if Path(session.workdir) != folder:
                    await self._move_session(session, folder)
                session.unattended = True
                session.memory_namespace = named.namespace_for(JARVIS_NAME)
                session.origin_surface = named.namespace_for(JARVIS_NAME)
                if session.agent != JARVIS_NAME:
                    session.agent = JARVIS_NAME
                    if core.store is not None:
                        await core.store.update_agent(session.id, JARVIS_NAME)
                registry.attach(stored)
                await core.sessions.announce_sessions_changed("created", session.id)
        if session is None:
            if stored is not None:
                # The row outlived its stored conversation: start a fresh one.
                registry._require_store().delete(JARVIS_NAME)
            agent = await registry.create(JARVIS_NAME, None, workdir=folder, reserved=True)
            session = core.sessions.get(agent.session_id)
            assert session is not None
            if core.store is not None:
                await core.store.update_agent(session.id, JARVIS_NAME)
        self._session_id = session.id
        self.engine.jarvis_session_id = session.id
        session.role = "jarvis"
        self.install_tools(session)
        core.lifecycle.set_counter("sessions", len(core.sessions))
        return session

    async def _move_session(self, session: Session, folder: Path) -> None:
        """``jarvis.folder`` changed: Jarvis works in the new folder from now on."""
        session.workdir = folder
        store = self.core.store
        if store is not None:
            await asyncio.to_thread(
                store._execute,
                "UPDATE sessions SET workdir = ? WHERE id = ?",
                (str(folder), session.id),
            )

    async def _apply_session_settings(self, session: Session | None = None) -> None:
        """Folder and model of the running session, after enable or a settings change."""
        session = session or self.session()
        if session is None:
            return
        try:
            self._make_folder()
        except JarvisError as exc:
            self.problems.append(str(exc))
            return
        if Path(session.workdir) != self.folder():
            await self._move_session(session, self.folder())
        model = self.core.settings.jarvis.model
        try:
            await self.core.sessions.set_model(session, model)
        except ValueError as exc:
            problem = f"jarvis.model: {exc}"
            if problem not in self.problems:
                self.problems.append(problem)

    async def _tick_loop(self) -> None:
        while True:
            intake = self.intake
            if intake is None:
                return
            due = intake.due_in()
            wait = IDLE_TICK_SEC if due is None else min(IDLE_TICK_SEC, due + 0.05)
            self._tick_wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._tick_wake.wait(), timeout=wait)
            try:
                await intake.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad batch
                log.warning("jarvis intake tick failed", exc_info=True)

    def poke(self) -> None:
        """Re-plan the tick loop (something was queued)."""
        self._tick_wake.set()

    # -- waking Jarvis ---------------------------------------------------
    async def deliver_wake(self, text: str) -> bool | None:
        """Hand ``text`` to Jarvis: a new turn (True), a notice to a running one (False).

        ``None`` when there is no live Jarvis session to tell.
        """
        session = self.session()
        if session is None:
            return None
        task = getattr(session, "turn_task", None)
        if task is not None and not task.done():
            session.pending_notices.append(text)
            return False
        from snowpea_core.agent import loop as agent_loop

        agent_loop.start_turn(
            self.core,
            session,
            text,
            unattended=True,
            initiator="system",
            reason=WAKE_REASON,
        )
        return True

    # -- the person steps in -----------------------------------------------
    async def person_typed(
        self, session_id: str, project: str | None, surface: str | None = None
    ) -> None:
        """The takeover hook: the person wrote into a supervised session themselves.

        The engine pauses Jarvis there (``jarvis.quietMinutes``, extended by
        each further input); its pause listener announces ``jarvis.state``.
        """
        self.engine.note_human_input(session_id, project=project, surface=surface)
        await self.settle()

    async def settle(self) -> None:
        """Wait for the announcements the engine's listeners scheduled."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _on_pause(self, _target: str, state: PauseState) -> None:
        if self.running:
            self._soon(self.emit_state("paused" if state.paused else "resumed"))

    def _on_ledger(self, record: dict[str, Any]) -> None:
        """Forward important decisions to the messenger hook (the wire event is the engine's)."""
        if record.get("type") != "decision" or not self.hooks.notify:
            return
        settings = self.core.settings.jarvis
        if not (settings.notify.messenger and settings.proactive.decided):
            return
        if record.get("importance") not in MESSENGER_IMPORTANCE:
            return
        entry = {k: v for k, v in record.items() if k not in ("type", "audit")}
        self._soon(self._run_hooks("decision", {"entry": entry}))

    def _soon(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _require_running(self) -> None:
        if not self.enabled:
            raise JarvisError(
                "jarvis_disabled", "Jarvis mode is off; turn it on with jarvis.enable"
            )

    # -- routing (the approval and question queues' router) -----------------
    def in_jarvis_family(self, session: Any) -> bool:
        """``session`` is Jarvis's own, or a sub-agent below it."""
        jarvis_id = self._session_id
        if jarvis_id is None or session is None:
            return False
        current: Any = session
        for _ in range(16):
            if getattr(current, "id", None) == jarvis_id:
                return True
            parent = getattr(current, "parent_session_id", None)
            if not parent:
                return False
            current = self.core.sessions.get(parent)
            if current is None:
                return False
        return False

    @staticmethod
    def browser_host(session: Any) -> bool:
        """Owned by a browser connection: its host tools, or a browser-owned job."""
        return bool(
            getattr(session, "host_tools_from", None) or getattr(session, "owned_by", None)
        )

    def readable(self, target: Any) -> bool:
        """Jarvis may read and instruct ``target`` (a session or a digest).

        Everything exposed is, except a browser-host session while
        ``jarvis.exposeBrowser`` is off: that one is listed with its status only.
        """
        browser = (
            target.browser if isinstance(target, Digest) else self.browser_host(target)
        )
        return not browser or bool(self.core.settings.jarvis.exposeBrowser)

    def budget_exhausted(self) -> bool:
        intake = self.intake
        return bool(intake is not None and intake.budget.exhausted())

    def classify_request(
        self, request: ApprovalRequest | dict[str, Any], session: Any, tags: tuple[str, ...] = ()
    ) -> tuple[ClassifyRequest, str | None]:
        """What the engine judges for one approval, and the session's project."""
        if isinstance(request, dict):
            request = ApprovalRequest.model_validate(
                {"risk": "medium", "timeoutSec": 0, "args": {}, **request}
            )
        project = None
        if session is not None:
            # An external session's proxy carries its registered project root.
            project = canonical(getattr(session, "project", None) or session.workdir)
        return (
            ClassifyRequest.from_approval(
                request,
                tags=tags,
                workdir=project,
                jarvis_session=self.in_jarvis_family(session)
                or request.sessionId == self._session_id,
                browser_owned=self.browser_host(session),
                home=str(self.core.paths.home),
            ),
            project,
        )

    def route_approval(
        self,
        request: ApprovalRequest,
        session: Any,
        tags: tuple[str, ...] = (),
        interactive: bool = False,
    ) -> str | None:
        """``ApprovalRequest.route``: the engine's verdict, remembered for the re-check."""
        if not self.running:
            return None
        result = self._route_approval(request, session, tags)
        if request.external is not None:
            from snowpea_core.jarvis import external as external_sessions

            external_sessions.note_request(self, session)
        return result.wire

    def _route_approval(
        self, request: ApprovalRequest | dict[str, Any], session: Any, tags: tuple[str, ...] = ()
    ) -> RouteResult:
        classify, project = self.classify_request(request, session, tags)
        request_id = (
            request.requestId if isinstance(request, ApprovalRequest) else request["requestId"]
        )
        external = (
            request.external if isinstance(request, ApprovalRequest) else request.get("external")
        )
        person_only = bool(
            request.personOnly if isinstance(request, ApprovalRequest)
            else request.get("personOnly")
        )
        result = self.engine.route(
            classify,
            project=project,
            external=external,
            exposed=self.projects.exposed(project),
            budget_exhausted=self.budget_exhausted(),
            request_id=request_id,
            person_only=person_only,
        )
        self._classified[request_id] = (classify, project)
        while len(self._classified) > ROUTED_KEPT:
            self._classified.pop(next(iter(self._classified)))
        return result

    def routed_approval(self, request: dict[str, Any], session: Any) -> RouteResult:
        """The engine's route for a pending approval (routing it now if it was not)."""
        request_id = str(request.get("requestId") or "")
        result = self.engine.routed(request_id)
        if result is not None and request_id in self._classified:
            return result
        return self._route_approval(request, session)

    def classified(self, request_id: str) -> tuple[ClassifyRequest, str | None] | None:
        return self._classified.get(request_id)

    def route_question(
        self, request: QuestionRequest | dict[str, Any], session: Any, interactive: bool = False
    ) -> str | None:
        """``QuestionRequest.route``: Jarvis answers how-to questions unless paused."""
        if not self.running:
            return None
        project = canonical(session.workdir) if session is not None else None
        return self.engine.route_question(
            getattr(session, "id", None),
            exposed=self.projects.exposed(project),
            budget_exhausted=self.budget_exhausted(),
            jarvis_session=self.in_jarvis_family(session),
            browser_owned=self.browser_host(session),
        )

    def forget_request(self, request_id: str) -> None:
        """A request was answered or withdrawn."""
        self.engine.forget(request_id)
        self._classified.pop(request_id, None)

    def install_tools(self, session: Session | None = None) -> None:
        """Give the Jarvis session its own tools, read-only built-ins and prompt."""
        from snowpea_core.jarvis import tools

        session = session or self.session()
        if session is not None:
            tools.install(self, session)

    def path_hidden(self, path: Path | str) -> bool:
        """True when ``path`` lies in a project whose ``jarvis.expose`` is off."""
        from snowpea_core.config.project import ProjectSettings

        resolved = Path(os.path.realpath(os.path.expanduser(str(path))))
        user_home = Path(os.path.realpath(Path.home()))
        # The home folder's .snowpea/ is snowpea's own home, not a project.
        stop = {user_home, *user_home.parents, Path(os.path.realpath(self.core.paths.home))}
        for candidate in (resolved, *resolved.parents):
            if candidate in stop:
                break
            if ProjectSettings.path_for(candidate).is_file() and not self.projects.exposed(
                canonical(candidate)
            ):
                return True
        return False

    async def record_access(
        self,
        *,
        session_id: str,
        project: str | None,
        host: str | None,
        action: str,
        summary: str,
        external: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Jarvis read or instructed a session: a ledger line and ``jarvis.access``.

        For an external session ``session_id`` is ``ext:<cli>:<id>`` and
        ``external`` its ref.
        """
        record = self.engine.ledger.append(
            {
                "type": "access",
                "sessionId": session_id,
                "external": external,
                "project": project,
                "hostToolsFrom": host,
                "action": action,
                "summary": " ".join(str(summary).split())[:160],
            }
        )
        payload = JarvisAccessNotification(
            id=record["id"],
            ts=record["ts"],
            sessionId=session_id,
            external=external,  # type: ignore[arg-type]
            project=project,
            hostToolsFrom=host,
            action=action,  # type: ignore[arg-type]
            summary=record["summary"],
        ).model_dump(mode="json")
        with contextlib.suppress(Exception):
            await self.core.hub.notify("jarvis.access", payload)
        return payload

    def record_decision(self, **fields: Any) -> JarvisDecision:
        """One ledger decision; the engine's listener announces ``jarvis.decision``."""
        return self.engine.ledger.record_decision(**fields)

    def session_state(self, session_id: str, project: str | None) -> SessionJarvisState:
        return self.engine.session_state(
            session_id,
            project=project,
            exposed=self.projects.exposed(project),
            enabled=self.enabled,
        )

    def summary_fields(self, session_id: str) -> dict[str, Any]:
        """``role`` / ``jarvis`` for a ``sessions.changed`` row (1.53.0)."""
        if self._session_id is not None and session_id == self._session_id:
            return {"role": "jarvis"}
        if not self.enabled:
            return {}
        session = self.core.sessions.get(session_id)
        if session is None or getattr(session, "kind", "chat") == "subagent":
            return {}
        state = self.session_state(session_id, canonical(session.workdir))
        return {"jarvis": state.model_dump(mode="json")}

    def decorate(self, row: SessionSummary) -> SessionSummary:
        """``SessionSummary.role`` and ``.jarvis`` for ``session.list`` (1.53.0)."""
        if self._session_id is not None and row.sessionId == self._session_id:
            return row.model_copy(update={"role": "jarvis", "jarvis": None})
        if not self.enabled:
            return row
        if row.kind == "subagent":
            return row
        project = canonical(row.workdir)
        return row.model_copy(update={"jarvis": self.session_state(row.sessionId, project)})

    # -- status and events ---------------------------------------------------
    async def status(self) -> JarvisStatusResult:
        settings = self.core.settings.jarvis
        pauses = self.engine.paused() if self.enabled else []
        return JarvisStatusResult(
            enabled=bool(settings.enabled),
            running=self.running,
            sessionId=self._session_id,
            model=settings.model,
            folder=str(self.folder()),
            delegationDefault=settings.delegation.default,
            budget=self.intake.budget.snapshot() if self.running and self.intake else None,
            paused=pauses,
            hiddenProjects=await self.projects.hidden(),
            problems=list(self.problems),
        )

    async def emit_state(self, reason: str) -> None:
        payload = JarvisStateNotification(
            reason=reason,  # type: ignore[arg-type]
            status=await self.status(),
        ).model_dump(mode="json")
        with contextlib.suppress(Exception):
            await self.core.hub.notify("jarvis.state", payload)

    async def on_budget(self, exhausted: bool) -> None:
        """The allowance ran out (or came back)."""
        if exhausted:
            await self.escalate(
                reason="budget",
                summary="Jarvis is out of its allowance; everything goes to you until it recovers",
                importance="high",
            )
        await self.emit_state("budget")

    async def escalate(
        self,
        *,
        reason: str,
        summary: str,
        importance: str = "high",
        session_id: str | None = None,
        project: str | None = None,
        title: str | None = None,
        request_kind: str | None = None,
        request_id: str | None = None,
        guarded_class: str | None = None,
    ) -> dict[str, Any] | None:
        """Send one ``jarvis.escalation`` (``jarvis.notify.*``); returns its params, or None."""
        notify = self.core.settings.jarvis.notify
        key = (reason, session_id, request_id) if (session_id or request_id) else None
        if key is not None and key in self._escalation_ids:
            escalation_id = self._escalation_ids[key]
        else:
            seed = f"{reason}|{session_id}|{request_id}|{self.now_iso()}"
            escalation_id = "je-" + hashlib.sha1(seed.encode()).hexdigest()[:12]
            if key is not None:
                self._escalation_ids[key] = escalation_id
                while len(self._escalation_ids) > 1000:
                    self._escalation_ids.pop(next(iter(self._escalation_ids)))
        params = JarvisEscalationNotification(
            id=escalation_id,
            ts=self.now_iso(),
            sessionId=session_id,
            project=project,
            title=title,
            reason=reason,  # type: ignore[arg-type]
            summary=summary,
            importance=importance,  # type: ignore[arg-type]
            requestKind=request_kind,  # type: ignore[arg-type]
            requestId=request_id,
            guardedClass=guarded_class,  # type: ignore[arg-type]
        ).model_dump(mode="json")
        if request_id is None:
            # Not a waiting request: kept open until a person dismisses it.
            self.escalations.add(params)
        sent = False
        if notify.inApp or notify.os:
            with contextlib.suppress(Exception):
                await self.core.hub.notify("jarvis.escalation", params)
            sent = True
        if notify.messenger and importance in MESSENGER_IMPORTANCE:
            await self._run_hooks("escalation", params)
            sent = True
        return params if sent else None

    async def _run_hooks(self, kind: str, params: dict[str, Any]) -> None:
        for hook in list(self.hooks.notify):
            try:
                await hook(kind, params)
            except Exception:  # noqa: BLE001 - a dead messenger must not stop Jarvis
                log.warning("jarvis %s hook failed", kind, exc_info=True)

    # -- overview --------------------------------------------------------
    def overview_rows(
        self,
        *,
        project: str | None = None,
        include_idle: bool = True,
        include_external: bool = True,
        limit: int | None = None,
    ) -> list[Any]:
        self._require_running()
        return overview.build_rows(
            self,
            project=project,
            include_idle=include_idle,
            include_external=include_external,
            limit=limit,
        )


def service_of(core: Core) -> JarvisService:
    """The daemon's Jarvis service, built on first use so any ``Core`` has one."""
    existing = getattr(core, "jarvis", None)
    if existing is None:
        existing = JarvisService(core)
        core.jarvis = existing
    return existing  # type: ignore[no-any-return]


async def start_jarvis(core: Core) -> None:
    """Daemon boot: build the service and bring Jarvis back if it was on."""
    try:
        await service_of(core).start()
    except Exception:  # noqa: BLE001 - Jarvis must never keep the daemon from starting
        log.exception("could not start Jarvis mode")


__all__ = [
    "JARVIS_NAME",
    "WAKE_REASON",
    "JarvisError",
    "JarvisService",
    "service_of",
    "start_jarvis",
]
