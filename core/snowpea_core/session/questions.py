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
from snowpea_core.server.protocol import QuestionOption, QuestionRequest

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

    @property
    def answered(self) -> bool:
        return bool(self.selected) or bool(self.text)


@dataclass
class _Pending:
    request: QuestionRequest
    future: asyncio.Future[Answer]
    origin_conn: Any = None
    task: asyncio.Task[None] | None = None


class QuestionQueue:
    """Questions waiting on a human."""

    def __init__(self, settings: Settings | None = None, hub: EventHub | None = None) -> None:
        self.settings = settings or Settings()
        self.hub = hub
        self._pending: dict[str, _Pending] = {}

    def bind(self, settings: Settings, hub: EventHub) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.settings = settings
        self.hub = hub

    # -- queries -------------------------------------------------------
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
        question: str,
        *,
        header: str = "",
        options: list[QuestionOption] | None = None,
        multi: bool = False,
        allow_other: bool = True,
        timeout_sec: int | None = None,
        cancel_event: asyncio.Event | None = None,
        index: int = 1,
        total: int = 1,
    ) -> Answer:
        """Put one question to the human; block until answered, refused or timed out."""
        timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        request = QuestionRequest(
            requestId=f"qu-{uuid.uuid4().hex[:12]}",
            sessionId=session.id,
            header=header,
            question=question,
            options=list(options or []),
            multi=multi,
            allowOther=allow_other,
            timeoutSec=timeout,
            index=index,
            total=total,
        )
        loop = asyncio.get_running_loop()
        origin = getattr(session, "origin_conn", None)
        entry = _Pending(request=request, future=loop.create_future(), origin_conn=origin)
        self._pending[request.requestId] = entry
        await self._broadcast_pending(request, exclude=origin)
        if origin is not None:
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
        outer = float(timeout) + (GRACE_SECONDS if entry.task is not None else 0.0)
        try:
            answer = await self._await_answer(entry, outer, cancel_event)
        except RpcError as exc:  # pragma: no cover - transport failure
            log.debug("question %s failed on the wire: %s", request.requestId, exc)
            answer = Answer(declined=True, by="error")
        finally:
            self._pending.pop(request.requestId, None)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.task
        await self._resolve(request, answer)
        return answer

    async def _broadcast_pending(self, request: QuestionRequest, exclude: Any = None) -> None:
        """Announce the question so a second surface can show it too."""
        if self.hub is None:
            return
        await self.hub.notify(
            "question.pending", {"request": request.model_dump(mode="json")}, exclude=exclude
        )

    async def _await_answer(
        self, entry: _Pending, timeout: float, cancel_event: asyncio.Event | None
    ) -> Answer:
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
        if entry.future.done():
            return entry.future.result()
        if cancel_event is not None and cancel_event.is_set():
            return Answer(declined=True, by="interrupted")
        return Answer(timed_out=True, by="timeout")

    async def _ask_origin(self, entry: _Pending) -> None:
        """Send ``question.request`` to the origin connection and record the answer."""
        request = entry.request
        try:
            reply = await entry.origin_conn.call(
                "question.request",
                request.model_dump(mode="json"),
                timeout=float(request.timeoutSec),
            )
            answer = _answer_from(reply, by="origin")
        except TimeoutError:
            answer = Answer(timed_out=True, by="timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a client that cannot ask declines
            # This is the headless and IDE-less path: an unknown s2c method
            # comes back as a JSON-RPC error, which is a "no", never a hang.
            log.debug("question %s could not reach its origin: %s", request.requestId, exc)
            answer = Answer(declined=True, by="origin-unreachable")
        if not entry.future.done():
            entry.future.set_result(answer)

    # -- the answer ----------------------------------------------------
    async def respond(
        self,
        request_id: str,
        selected: list[str] | None = None,
        text: str | None = None,
        by: str = "client",
    ) -> None:
        """Resolve a pending question (``question.respond``).

        Any authenticated surface may answer: unlike an approval, a question
        carries no authority, so whoever is looking at it may say the word.
        """
        entry = self._pending.get(request_id)
        if entry is None:
            raise RpcError(errors.NOT_FOUND, f"no pending question {request_id}")
        if entry.future.done():
            return
        picked = [str(label) for label in (selected or [])]
        body = text if text is None else str(text)
        entry.future.set_result(
            Answer(
                selected=picked,
                text=body,
                declined=not picked and not (body or "").strip(),
                by=by,
            )
        )

    async def _resolve(self, request: QuestionRequest, answer: Answer) -> None:
        """Tell every surface the question is over, so nobody keeps showing it."""
        if self.hub is None:
            return
        await self.hub.notify(
            "question.resolved", {"requestId": request.requestId, "by": answer.by}
        )


def _answer_from(reply: dict[str, Any], *, by: str) -> Answer:
    """Read a ``question.request`` result, treating anything empty as a decline."""
    raw = reply.get("selected")
    selected = [str(label) for label in raw] if isinstance(raw, list) else []
    text = reply.get("text")
    body = None if text is None else str(text)
    return Answer(
        selected=selected,
        text=body,
        declined=not selected and not (body or "").strip(),
        by=by,
    )


__all__ = ["GRACE_SECONDS", "Answer", "QuestionQueue", "QuestionRequests"]
