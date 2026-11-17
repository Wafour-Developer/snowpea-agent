"""Questions the agent puts to the human (the ``ask_user`` tool).

This is the approval queue's smaller sibling and deliberately looks like it: a
question is asked of the session's *origin* connection when there is one, and
otherwise sits in a shared queue any authenticated client may answer, with
``question.pending`` / ``question.resolved`` announcing both ends so a second
surface (an IDE, a bound Telegram chat) can see what is going on.

What it does not have is everything approvals need and a question does not:
no scopes, no allowlist, no caching.  An answer is worth exactly one answer;
asking the same thing twice is the agent's problem, not the queue's.

A client that cannot ask a human — headless ``snowpea -c``, a dead socket —
answers nothing, and an empty answer is reported to the tool as *declined*
rather than silently becoming a choice the user never made.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import QuestionItem, QuestionRequest
from snowpea_core.session import events as event_builders
from snowpea_core.session.session import (
    CLIENT_QUESTION_CARD_SEC,
    asking_conn,
    client_timed_out,
    surface_name,
    withdraw,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.manager import EventHub
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.questions")

#: Alias so the annotations below still mean the builtin ``list`` even though
#: :class:`QuestionQueue` defines a method called ``list``.
QuestionRequests = list[QuestionRequest]

#: Extra seconds the outer wait gives the origin call to report its own timeout.
GRACE_SECONDS = 2.0


@dataclass
class Answer:
    """What came back.  ``declined`` and ``timed_out`` are not the same thing.

    ``declined`` is a person pressing Esc, or a surface that has no way to ask;
    ``timed_out`` is nobody there at all.  Both leave ``selected`` empty, and
    the tool says which happened so the model does not treat silence as assent.
    """

    selected: list[str] = field(default_factory=list)
    text: str | None = None
    timed_out: bool = False
    declined: bool = False
    by: str = "unknown"
    #: The answerer was checked as a person (a ``personOnly`` question, 1.54.0).
    person: bool = False
    #: The checked answerer's connection kind (``person_gate.connection_kind``):
    #: a remote web login's answer keeps ``web.remote.maxMode`` (review 7).
    person_kind: str | None = None

    @property
    def answered(self) -> bool:
        return bool(self.selected) or bool(self.text)


@dataclass
class _Pending:
    request: QuestionRequest
    future: asyncio.Future[list[Answer]]
    origin_conn: Any = None
    task: asyncio.Task[None] | None = None
    #: A person at a TUI or desktop session is waited for without a clock.
    no_limit: bool = False
    #: The session asking (who may answer a ``personOnly`` question depends on it).
    session: Any = None
    #: 0.2.91: the core's own clock (``questions.timeoutSec`` when the asker is
    #: waited for without one): empty answers after it are the client's timeout.
    clock_sec: float = 0.0


class QuestionQueue:
    """Questions waiting on a human."""

    def __init__(self, settings: Settings | None = None, hub: EventHub | None = None) -> None:
        self.settings = settings or Settings()
        self.hub = hub
        self._pending: dict[str, _Pending] = {}
        #: Jarvis mode's router (1.53.0): ``router(request, session,
        #: interactive)`` returns ``QuestionRequest.route`` before the question
        #: goes on the wire, or ``None`` (Jarvis mode off).  Must not block.
        self.router: Any = None

    def bind(self, settings: Settings, hub: EventHub) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.settings = settings
        self.hub = hub

    # -- queries -------------------------------------------------------
    def count(self, session_id: str) -> int:
        return sum(1 for e in self._pending.values() if e.request.sessionId == session_id)

    async def _announce_status(
        self, session_id: str, status: str, host: str | None = None
    ) -> None:
        if self.hub is None:
            return
        with contextlib.suppress(Exception):
            await self.hub.notify(
                "sessions.changed",
                {
                    "reason": "question",
                    "sessionId": session_id,
                    "status": status,
                    "hostToolsFrom": host,
                },
            )

    def list(self, session_id: str | None = None, conn: Any = None) -> QuestionRequests:
        """Pending questions: the unattended ones, plus ``conn``'s own.

        Unlike an approval, a question is worth showing to a second surface
        even when it was asked of an origin: an IDE that can see the terminal
        is waiting on the same person.  The origin still gets the blocking
        call; this is only what ``question.list`` reports.
        """
        return [
            entry.request
            for entry in self._pending.values()
            if session_id is None or entry.request.sessionId == session_id
        ]

    def get(self, request_id: str) -> QuestionRequest | None:
        entry = self._pending.get(request_id)
        return entry.request if entry else None

    @property
    def timeout_sec(self) -> int:
        return max(1, int(self.settings.questions.timeoutSec))

    # -- the ask -------------------------------------------------------
    async def ask(
        self,
        session: Session,
        questions: list[QuestionItem],
        *,
        timeout_sec: int | None = None,
        cancel_event: asyncio.Event | None = None,
        person_only: bool = False,
        kind: str | None = None,
    ) -> list[Answer]:
        """Put a batch of questions to the human; block until they answer.

        One request, one answer set, however many questions: the surface owns
        how it walks the user through them, and can let them go back and change
        an earlier answer before submitting.  The returned list always has one
        entry per question, so the caller never has to check a length.

        ``person_only`` (security review 4, C1; ``kind: "mode"`` for a mode
        picker): only a person may answer — routed to the person, never Jarvis,
        a connector or an in-process caller; a browser only for a session it
        hosts (or opened), with ``actor: "person"``.  Every answer that comes
        back from such a question has ``person`` set.
        """
        if not questions:
            return []
        if getattr(session, "owned_by", None):
            # Nobody may put words into a browser-owned job's run (1.23.0).
            return _declined(len(questions), by="owned-run")
        # 0.2.91: the surface the person is prompting from (the turn's asker),
        # else the session's open origin, else nobody (every surface sees it).
        origin = asking_conn(session, self._lookup())
        # An explicit timeout_sec is a caller's own deadline and always counts
        # (0 expires at once).  Otherwise a person at the session is waited for.
        # An interactive browser chat has a person too; only jobs (owned runs,
        # declined above) and sessions with no live client keep the clock.
        no_limit = timeout_sec is None and origin is not None
        if timeout_sec is not None:
            timeout = timeout_sec
        else:
            timeout = 0 if no_limit else self.timeout_sec  # on the wire, 0: no limit
        request = QuestionRequest(
            requestId=f"qu-{uuid.uuid4().hex[:12]}",
            sessionId=session.id,
            questions=list(questions),
            timeoutSec=timeout,
            personOnly=True if person_only else None,
            kind=kind if person_only and kind else None,  # type: ignore[arg-type]
            neverDelegable=True if person_only else None,
        )
        if self.router is not None:
            try:
                route = self.router(request, session, origin is not None)
            except Exception:  # noqa: BLE001 - a broken router leaves it to the person
                log.warning("question router failed for %s", request.requestId, exc_info=True)
                route = "person"
            if person_only and route is not None:
                route = "person"  # before any text rule: never Jarvis
            if route is not None:
                request = request.model_copy(update={"route": route})
        loop = asyncio.get_running_loop()
        entry = _Pending(
            request=request, future=loop.create_future(), origin_conn=origin, no_limit=no_limit,
            session=session, clock_sec=float(timeout if timeout else self.timeout_sec),
        )
        self._pending[request.requestId] = entry
        asked_at = loop.time()
        host = getattr(session, "host_tools_from", None)
        await self._announce_status(session.id, "awaiting_question", host)
        await self._broadcast_pending(request, exclude=origin, host=host)
        tell = getattr(self.hub, "tell_observers", None)
        if tell is not None:
            await tell("question.pending", {"request": request.model_dump(mode="json")})
        # 0.2.91: the conversation's record says what was asked and where.
        await self._record(session.id, event_builders.question_asked(request, origin))
        if origin is not None:
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
        outer = (
            None
            if no_limit
            else float(timeout) + (GRACE_SECONDS if entry.task is not None else 0.0)
        )
        try:
            answers = await self._await_answers(entry, outer, cancel_event)
        except RpcError as exc:  # pragma: no cover - transport failure
            log.debug("question %s failed on the wire: %s", request.requestId, exc)
            answers = _declined(len(questions), by="error")
        finally:
            self._pending.pop(request.requestId, None)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.task
        if not self.count(session.id):
            await self._announce_status(session.id, "running", host)
        await self._resolve(request, answers)
        padded = _padded(answers, len(questions))
        await self._record(session.id, event_builders.question_answered(
            request, padded, int((loop.time() - asked_at) * 1000)))
        return padded

    async def _record(self, session_id: str, event: Any) -> None:
        """Store ``question.asked`` / ``.answered`` with a live session's events (0.2.91)."""
        lookup = self._lookup()
        emit = getattr(self.hub, "emit_event", None)
        if emit is None or lookup is None or lookup(session_id) is None:
            return
        try:
            await emit(session_id, event)
        except Exception:  # noqa: BLE001 - the record is a side note
            log.debug("could not record %s for %s", event[0], session_id, exc_info=True)

    async def _broadcast_pending(
        self, request: QuestionRequest, exclude: Any = None, host: str | None = None
    ) -> None:
        """Announce the question so a second surface can show it too."""
        if self.hub is None:
            return
        await self.hub.notify(
            "question.pending",
            {"request": request.model_dump(mode="json"), "hostToolsFrom": host},
            exclude=exclude,
        )

    async def _await_answers(
        self, entry: _Pending, timeout: float | None, cancel_event: asyncio.Event | None
    ) -> list[Answer]:
        """Wait for the answer, the timeout, or an interrupt — whichever comes first."""
        waiters: list[asyncio.Future[Any]] = [entry.future]
        watcher: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            watcher = asyncio.ensure_future(cancel_event.wait())
            waiters.append(watcher)
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        count = len(entry.request.questions)
        if entry.future.done():
            return entry.future.result()
        if cancel_event is not None and cancel_event.is_set():
            return _declined(count, by="interrupted")
        return [Answer(timed_out=True, by="timeout") for _ in range(count)]

    async def _ask_origin(self, entry: _Pending) -> None:
        """Send ``question.request`` to the origin connection and record the answer."""
        request = entry.request
        started = asyncio.get_running_loop().time()
        try:
            reply = await entry.origin_conn.call(
                "question.request",
                request.model_dump(mode="json"),
                timeout=None if entry.no_limit else float(request.timeoutSec),
            )
            waited = asyncio.get_running_loop().time() - started
            if client_timed_out(reply, waited, entry.clock_sec, CLIENT_QUESTION_CARD_SEC):
                # 0.2.91: the asking client's card gave up (the desktop app
                # sends {answers: []}): nobody answered, nobody declined.
                log.warning("question %s: %s sent no answers after %.1f s: its own timeout",
                            request.requestId, surface_name(entry.origin_conn), waited)
                if not entry.future.done():
                    entry.future.set_result([
                        Answer(timed_out=True, by="origin-timeout")
                        for _ in range(len(request.questions))
                    ])
                return
            answers = _answers_from(reply.get("answers"), by="origin")
            if request.personOnly:
                actor = reply.get("actor") if isinstance(reply, dict) else None
                why = person_refusal(entry.origin_conn, entry.session,
                                     actor if isinstance(actor, str) else None, by="origin")
                if why is not None:
                    # Not a person's answer: the question stays open and a person
                    # answers it with question.respond.  The origin has already
                    # closed its card, so it is told why and shown it again.
                    log.warning("question %s: origin answer refused (%s)", request.requestId, why)
                    await self._refused_to_origin(entry, why)
                    return
                from snowpea_core.server.person_gate import connection_kind

                for answer in answers:
                    answer.person = True
                    answer.person_kind = connection_kind(entry.origin_conn)
        except TimeoutError:
            answers = [
                Answer(timed_out=True, by="timeout") for _ in range(len(request.questions))
            ]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a client that cannot ask declines
            # This is the headless and IDE-less path: an unknown s2c method
            # comes back as a JSON-RPC error, which is a "no", never a hang.
            log.debug("question %s could not reach its origin: %s", request.requestId, exc)
            answers = _declined(len(request.questions), by="origin-unreachable")
        if not entry.future.done():
            entry.future.set_result(_padded(answers, len(request.questions)))

    def _lookup(self) -> Any:
        """Session id -> live session (``asking_conn`` walks a child's parents)."""
        sessions = getattr(self.hub, "sessions", None)
        lookup = getattr(sessions, "get", None)
        return lookup if callable(lookup) else None

    async def follow_asker(self) -> int:
        """Move waiting questions to their session's new asker (0.2.91); how many moved.

        As ``ApprovalQueue.follow_asker``: asked again on the surface the
        person now types on; the old surface's card is withdrawn
        (``question.resolved {outcome: "withdrawn"}``).
        """
        moved = 0
        lookup = self._lookup()
        for entry in list(self._pending.values()):
            if entry.origin_conn is None or entry.future.done() or entry.request.route == "jarvis":
                continue
            target = asking_conn(entry.session, lookup)
            if target is None or target is entry.origin_conn:
                continue
            old, task = entry.origin_conn, entry.task
            entry.origin_conn = target
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
            if task is not None and not task.done():
                task.cancel()
            await withdraw(old, "question", entry.request.requestId)
            moved += 1
        return moved

    async def _refused_to_origin(self, entry: _Pending, why: str) -> None:
        """Re-announce a still-open question to the origin whose reply was refused (1.55.0)."""
        request = entry.request
        params = {
            "request": request.model_dump(mode="json"),
            "hostToolsFrom": getattr(entry.session, "host_tools_from", None),
            "refused": {
                "code": errors.JARVIS_PERSON_ONLY,
                "reason": "mode_raise" if request.kind == "mode" else "person_only",
                "message": f"only a person may answer this: {why}",
            },
        }
        try:
            await entry.origin_conn.notify("question.pending", params)
        except Exception:  # noqa: BLE001 - a closed origin: other surfaces still see it
            log.debug("could not re-announce question %s", request.requestId, exc_info=True)

    # -- the answer ----------------------------------------------------
    async def respond(
        self,
        request_id: str,
        answers: list[dict[str, Any]] | None = None,
        by: str = "client",
        *,
        conn: Any = None,
        actor: str | None = None,
    ) -> bool:
        """Resolve a pending question (``question.respond``); False when it already was.

        Any authenticated surface may answer an ordinary question: unlike an
        approval, it carries no authority.  A ``personOnly`` one (a mode picker)
        only a person: refused with ``jarvis_person_only`` (``details.reason:
        "mode_raise"`` for ``kind: "mode"``), and the card stays.
        """
        entry = self._pending.get(request_id)
        if entry is None:
            raise RpcError(errors.NOT_FOUND, f"no pending question {request_id}")
        person = False
        if entry.request.personOnly:
            # A messenger is not a person here: the gateway lets anyone in a chat
            # answer that chat's own session's questions (no bound-user check),
            # so a mode picker is answered in the app, the TUI or the web page.
            why = person_refusal(conn, entry.session, actor, by=by)
            if why is not None:
                raise RpcError(
                    errors.JARVIS_PERSON_ONLY, f"only a person may answer this: {why}",
                    {"reason": "mode_raise" if entry.request.kind == "mode" else "person_only",
                     "kind": entry.request.kind},
                )
            person = True
        if entry.future.done():
            return False
        count = len(entry.request.questions)
        parsed = _padded(_answers_from(answers, by=by), count)
        from snowpea_core.server.person_gate import connection_kind

        for answer in parsed:
            answer.person = person
            answer.person_kind = connection_kind(conn) if person else None
        entry.future.set_result(parsed)
        return True

    async def _resolve(self, request: QuestionRequest, answers: list[Answer]) -> None:
        """Tell every surface the batch is over, so nobody keeps showing it."""
        if self.hub is None:
            return
        by = answers[0].by if answers else "unknown"
        await self.hub.notify("question.resolved", {"requestId": request.requestId, "by": by})
        tell = getattr(self.hub, "tell_observers", None)
        if tell is not None:
            await tell("question.resolved", {"requestId": request.requestId, "by": by})


def person_refusal(conn: Any, session: Any, actor: str | None, *, by: str = "") -> str | None:
    """Why the answerer of a ``personOnly`` question is not its person; ``None`` if it is.

    A local person or a remote web login (the person, for running work); the
    browser that hosts the session (or opened it) with ``actor: "person"`` (a
    legacy browser as before); never a connector, Jarvis, the gateway or any
    in-process caller.
    """
    if conn is None:
        return f"{by or 'an in-process caller'} is not a person"
    from snowpea_core.server.person_gate import connection_kind
    from snowpea_core.server.settings_gate import acts_for_person

    kind = connection_kind(conn)
    if kind in ("person", "remote_login"):
        return None
    if kind == "browser":
        if not acts_for_person(conn, actor):
            return "a browser answers this only from a person's click (actor: \"person\")"
        mine = getattr(conn, "client_id", None)
        host = getattr(session, "host_tools_from", None) if session is not None else None
        if host is not None and host != mine:
            return "another browser's session"
        if host is None and session is not None and conn not in (
                getattr(session, "origin_conn", None), getattr(session, "asker_conn", None)):
            # (0.2.91: the browser the person is prompting from counts as well.)
            return "a session this browser did not open"
        return None
    return f"a {getattr(conn, 'client_kind', None) or kind} connection is not a person"


def _answer_from(raw: Any, *, by: str) -> Answer:
    """One answer entry, treating anything empty as a decline of that question."""
    entry = raw if isinstance(raw, dict) else {}
    picked = entry.get("selected")
    selected = [str(label) for label in picked] if isinstance(picked, list) else []
    text = entry.get("text")
    body = None if text is None else str(text)
    return Answer(
        selected=selected,
        text=body,
        declined=not selected and not (body or "").strip(),
        by=by,
    )


def _answers_from(raw: Any, *, by: str) -> list[Answer]:
    """The answer list off the wire; anything unusable is no answer at all."""
    if not isinstance(raw, list):
        return []
    return [_answer_from(entry, by=by) for entry in raw]


def _declined(count: int, *, by: str) -> list[Answer]:
    return [Answer(declined=True, by=by) for _ in range(count)]


def _padded(answers: list[Answer], count: int) -> list[Answer]:
    """Exactly ``count`` answers.

    A surface that submits fewer than it was asked — Esc on the second tab of
    three — has declined the rest, and the caller should be told that rather
    than have to reason about a short list.
    """
    if len(answers) >= count:
        return answers[:count]
    by = answers[-1].by if answers else "unknown"
    return answers + _declined(count - len(answers), by=by)


__all__ = ["GRACE_SECONDS", "Answer", "QuestionQueue", "QuestionRequests"]
