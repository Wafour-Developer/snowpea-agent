"""M19 WP3: the Jarvis intake — digests, wake batching, budget, takeover, overview order.

The intake runs on a real daemon core (hub, approval and question queues) but
with a hand-moved clock and the delegation engine with a settable verdict, and
the service's wake delivery is recorded instead of starting a model turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest_asyncio

# Written when Jarvis supervised every session: every session is shared (1.56.0).
from _support import make_shared_daemon as make_daemon
from _support import share_everything

from snowpea_core.jarvis import overview
from snowpea_core.jarvis.classify import ClassifyRequest, Verdict
from snowpea_core.jarvis.clock import Clock, iso
from snowpea_core.jarvis.delegation import DelegationEngine, PauseState, RouteResult
from snowpea_core.jarvis.intake import (
    NOTICE_WINDOW_SEC,
    ROUTED_BATCH_SEC,
    Digest,
    Intake,
    PendingItem,
)
from snowpea_core.jarvis.service import JarvisService
from snowpea_core.server.app_server import Daemon
from snowpea_core.session import events
from snowpea_core.session.workspaces import canonical


class FakeClock(Clock):
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


class FakeEngine(DelegationEngine):
    """The real engine (store, pauses, ledger) with a settable verdict.

    ``route_to`` is what an exposed, unpaused request in budget gets; the
    engine's own pause, exposure and budget rules still apply first.
    """

    def __init__(self, home: Path, clock: FakeClock, settings: Any) -> None:
        super().__init__(home, lambda: settings, now=clock.now)
        self.route_to = "person"
        self.seen: list[ClassifyRequest] = []
        self.typed: list[str] = []

    def _route(  # type: ignore[override]
        self,
        request: ClassifyRequest,
        *,
        project: str | None,
        external: Any,
        exposed: bool,
        budget_exhausted: bool,
    ) -> RouteResult:
        self.seen.append(request)
        verdict = Verdict(
            "safe" if self.route_to == "auto" else "risky", rules=("test:rule",)
        )
        level = self.default_level
        if not exposed:
            return RouteResult("person", verdict, level, None, "not-exposed")
        if self.pause_state(request.session_id).paused:
            return RouteResult("person", verdict, level, None, "paused")
        if budget_exhausted:
            return RouteResult("person", verdict, level, None, "budget")
        return RouteResult(self.route_to, verdict, level, None, "test")  # type: ignore[arg-type]

    def note_human_input(self, session_id: Any = None, **kwargs: Any) -> PauseState:
        self.typed.append(session_id)
        return super().note_human_input(session_id, **kwargs)

    def route_question(self, session_id: Any = None, **kwargs: Any) -> Any:
        route = super().route_question(session_id, **kwargs)
        return "person" if self.route_to == "person" else route

    def is_paused(self, session_id: str) -> bool:
        return self.pause_state(session_id).paused

    def entries(self) -> list[Any]:
        return self.ledger.decisions()[0]


class Harness:
    def __init__(self, daemon: Daemon, tmp: Path) -> None:
        self.daemon = daemon
        self.core = daemon.core
        self.tmp = tmp
        self.clock = FakeClock()
        self.engine = FakeEngine(Path(self.core.paths.home), self.clock, self.core.settings)
        self.core.settings.jarvis.enabled = True
        self.core.settings.jarvis.folder = str(tmp / "JarvisFolder")
        self.service = JarvisService(self.core, clock=self.clock, engine=self.engine)
        share_everything(self.core, self.engine)
        self.core.jarvis = self.service
        self.wakes: list[str] = []
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.hooked: list[tuple[str, dict[str, Any]]] = []

        async def deliver(text: str, listener: Any = None, who: Any = None) -> bool:
            self.wakes.append(text)
            return True

        self.service.deliver_wake = deliver  # type: ignore[method-assign]
        self.service.hooks.notify.append(self._hook)
        self.core.hub.register_client(self)
        self.intake = Intake(self.service)
        self.service.intake = self.intake
        self.service.running = True
        self.service._session_id = "s-jarvis"

    closed = False

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method.startswith("jarvis."):
            self.notes.append((method, params))

    async def _hook(self, kind: str, params: dict[str, Any]) -> None:
        self.hooked.append((kind, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.notes if name == method]

    async def start(self) -> None:
        await self.intake.seed()
        self.intake.attach()

    async def session(self, name: str, *, hidden: bool = False, title: str | None = None) -> Any:
        folder = self.tmp / name
        folder.mkdir(exist_ok=True)
        if hidden:
            (folder / ".snowpea").mkdir(exist_ok=True)
            (folder / ".snowpea" / "settings.json").write_text('{"jarvis": {"expose": false}}')
        session = await self.core.sessions.create(folder, mode="auto")
        session.title = title
        return session

    async def emit(self, session: Any, event: Any) -> None:
        await self.core.hub.emit_event(session.id, event)

    async def tick(self, seconds: float = 0.0) -> None:
        self.clock.advance(seconds)
        await self.intake.tick()

    async def pending(self, session: Any, request_id: str, kind: str = "approval") -> None:
        method = "approval.pending" if kind == "approval" else "question.pending"
        request: dict[str, Any] = {"requestId": request_id, "sessionId": session.id}
        if kind == "approval":
            request.update(tool="shell", args={"command": f"run {request_id}"})
        else:
            request["questions"] = [{"question": f"Which way for {request_id}?"}]
        await self.intake.observe(method, {"request": request, "interactive": False})


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    daemon = await make_daemon(tmp_path / "home")
    h = Harness(daemon, tmp_path)
    try:
        yield h
    finally:
        h.intake.detach()
        await daemon.stop()


# -- digests ----------------------------------------------------------------------


async def test_digest_follows_a_synthetic_event_stream(harness: Harness) -> None:
    await harness.start()
    s = await harness.session("proj", title="Login bug")
    await harness.emit(s, events.turn_started("t1", "fix it"))
    digest = harness.intake.digests[s.id]
    assert digest.status == "running"
    assert digest.project == canonical(harness.tmp / "proj")
    await harness.emit(s, events.tool_call("c1", "edit_file", {"path": "a.py"}))
    assert digest.line().startswith("running: edit_file")
    await harness.emit(s, events.diff(str(harness.tmp / "proj" / "a.py"), "@@"))
    await harness.emit(s, events.diff(str(harness.tmp / "proj" / "b.py"), "@@"))
    await harness.emit(s, events.diff(str(harness.tmp / "proj" / "a.py"), "@@"))
    await harness.emit(s, events.message_delta("Fixed the "))
    await harness.emit(s, events.message_delta("redirect."))
    assert digest.partial == "Fixed the redirect."
    long_answer = "x" * 700 + " Fixed the redirect loop."
    await harness.emit(s, events.message_done(long_answer))
    await harness.emit(s, events.turn_done("t1", "complete"))
    assert digest.status == "idle" and digest.partial == ""
    assert len(digest.last_answer) == 600 and digest.last_answer.endswith("redirect loop.")
    assert [Path(p).name for p in digest.files] == ["b.py", "a.py"]
    assert digest.last_activity is not None
    line = digest.line()
    assert len(line) <= 200 and line.endswith("changed b.py, a.py")

    await harness.emit(s, events.turn_started("t2"))
    await harness.emit(s, events.error("provider_error", "the model went away"))
    await harness.emit(s, events.turn_done("t2", "error"))
    assert digest.status == "error" and digest.last_error == "the model went away"
    assert digest.line().startswith("error: the model went away")
    await harness.emit(s, events.turn_started("t3"))
    assert digest.last_error is None

    # Closed sessions stay listed as idle; deleted ones go.
    await harness.core.sessions.close(s.id)
    assert digest.closed and digest.status == "idle"
    await harness.intake.notify("sessions.changed", {"reason": "deleted", "sessionId": s.id})
    assert s.id not in harness.intake.digests


async def test_pending_requests_show_in_the_digest(harness: Harness) -> None:
    await harness.start()
    s = await harness.session("proj")
    await harness.pending(s, "ap-1")
    await harness.pending(s, "q-1", kind="question")
    digest = harness.intake.digests[s.id]
    assert set(digest.pending) == {"ap-1", "q-1"}
    assert "approve shell run ap-1" in digest.line()
    assert "asks: Which way for q-1?" in digest.line()
    await harness.intake.observe("approval.resolved", {"requestId": "ap-1", "decision": "allow"})
    assert set(digest.pending) == {"q-1"}


async def test_seed_covers_live_sessions_and_skips_jarvis_and_subagents(
    harness: Harness,
) -> None:
    s = await harness.session("proj")
    child = await harness.core.sessions.create(
        harness.tmp / "proj", mode="auto", parent_session_id=s.id, kind="subagent"
    )
    child.is_subagent = True
    harness.service._session_id = s.id  # pretend s is Jarvis
    other = await harness.session("other")
    await harness.start()
    assert set(harness.intake.digests) == {other.id}


# -- exposure -----------------------------------------------------------------------


async def test_unexposed_projects_never_reach_the_intake(harness: Harness) -> None:
    harness.engine.route_to = "jarvis"
    await harness.start()
    hidden = await harness.session("secret", hidden=True)
    await harness.emit(hidden, events.turn_started("t1"))
    await harness.emit(hidden, events.turn_done("t1", "error"))
    await harness.pending(hidden, "ap-h")
    assert hidden.id not in harness.intake.digests
    assert harness.engine.seen == []
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert harness.wakes == [] and harness.of("jarvis.escalation") == []
    rows = overview.build_rows(harness.service)
    assert hidden.id not in {row.sessionId for row in rows}


# -- wake batching ---------------------------------------------------------------------


async def test_routed_items_wake_once_after_two_seconds(harness: Harness) -> None:
    harness.engine.route_to = "jarvis"
    await harness.start()
    a = await harness.session("a")
    b = await harness.session("b")
    await harness.pending(a, "ap-a")
    await harness.tick(1.0)
    await harness.pending(b, "q-b", kind="question")
    await harness.tick(ROUTED_BATCH_SEC - 1.0 - 0.01)
    assert harness.wakes == []
    await harness.tick(0.02)
    assert len(harness.wakes) == 1
    text = harness.wakes[0]
    assert "approval ap-a" in text and "question q-b" in text
    assert "Waiting for your decision" in text
    await harness.tick(10)
    assert len(harness.wakes) == 1
    assert harness.intake.budget.wakes_this_hour() == 1
    # Routed to Jarvis: nothing waits on the person.
    rows = {row.sessionId: row for row in overview.build_rows(harness.service)}
    assert rows[a.id].waitingOnPerson == 0 or rows[a.id].pendingApprovals == 0


async def test_answered_before_the_wake_needs_no_wake(harness: Harness) -> None:
    harness.engine.route_to = "jarvis"
    await harness.start()
    a = await harness.session("a")
    await harness.pending(a, "ap-a")
    await harness.intake.observe("approval.resolved", {"requestId": "ap-a", "decision": "deny"})
    await harness.tick(ROUTED_BATCH_SEC + 1)
    assert harness.wakes == []


async def test_other_endings_coalesce_into_one_notice_per_30_seconds(
    harness: Harness,
) -> None:
    await harness.start()
    sessions = [await harness.session(f"p{i}", title=f"task {i}") for i in range(3)]
    for index, offset in enumerate((0.0, 10.0, 15.0)):
        await harness.tick(offset)
        s = sessions[index]
        await harness.emit(s, events.turn_started(f"t{index}"))
        await harness.emit(s, events.message_done(f"done {index}"))
        await harness.emit(s, events.turn_done(f"t{index}", "complete" if index else "error"))
    await harness.tick(NOTICE_WINDOW_SEC - 25.0 - 0.01)
    assert harness.wakes == []
    await harness.tick(0.02)
    assert len(harness.wakes) == 1
    text = harness.wakes[0]
    assert text.count("\n- ") == 3 and "Since the last notice" in text
    assert "turn error (error)" in text and "turn finished (complete)" in text
    # A new ending opens a new window.
    await harness.emit(sessions[0], events.turn_started("t9"))
    await harness.emit(sessions[0], events.turn_done("t9", "complete"))
    await harness.tick(NOTICE_WINDOW_SEC - 1)
    assert len(harness.wakes) == 1
    await harness.tick(1.01)
    assert len(harness.wakes) == 2
    # An interrupt is the person's own doing: no notice.
    await harness.emit(sessions[1], events.turn_started("t10"))
    await harness.emit(sessions[1], events.turn_done("t10", "interrupted"))
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert len(harness.wakes) == 2


async def test_proactive_switches_gate_the_notices(harness: Harness) -> None:
    harness.core.settings.jarvis.proactive.finished = False
    await harness.start()
    s = await harness.session("p")
    await harness.emit(s, events.turn_started("t1"))
    await harness.emit(s, events.turn_done("t1", "complete"))
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert harness.wakes == []
    await harness.emit(s, events.turn_started("t2"))
    await harness.emit(s, events.turn_done("t2", "error"))
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert len(harness.wakes) == 1


async def test_delegated_session_endings_wake_within_the_batch(harness: Harness) -> None:
    from snowpea_core.jarvis.delegation import Caller

    person = Caller(kind="connection", surface="ide")
    harness.engine.set_grant("global", None, "full", [], caller=person)
    await harness.start()
    s = await harness.session("p", title="nightly refactor")
    await harness.emit(s, events.turn_started("t1"))
    await harness.emit(s, events.turn_done("t1", "stalled"))
    await harness.tick(ROUTED_BATCH_SEC + 0.01)
    assert len(harness.wakes) == 1
    assert "Delegated sessions that stopped" in harness.wakes[0]
    assert "turn stalled (stalled)" in harness.wakes[0]


async def test_auto_decisions_wait_for_the_next_wake(harness: Harness) -> None:
    harness.engine.route_to = "auto"
    await harness.start()
    s = await harness.session("p")
    approval = asyncio.ensure_future(
        harness.core.approvals.request(
            s, "read_file", {"path": "README.md"}, unattended=True, timeout_sec=5
        )
    )
    decision = await approval
    assert decision.allowed and decision.by == "jarvis:auto"
    decided = harness.of("jarvis.decision")
    assert decided and decided[0]["entry"]["decidedBy"] == "auto"
    assert decided[0]["entry"]["rule"] == "test:rule"
    assert harness.engine.entries() and harness.engine.entries()[0].decision == "allow"
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert harness.wakes == []
    # The next real wake mentions it.
    await harness.emit(s, events.turn_started("t1"))
    await harness.emit(s, events.turn_done("t1", "complete"))
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert len(harness.wakes) == 1
    assert "auto-allowed" in harness.wakes[0] and "read_file README.md" in harness.wakes[0]


# -- the person ---------------------------------------------------------------------------


async def test_person_routed_requests_escalate(harness: Harness) -> None:
    await harness.start()
    s = await harness.session("p", title="deploy")
    await harness.pending(s, "ap-1")
    escalations = harness.of("jarvis.escalation")
    assert len(escalations) == 1
    first = escalations[0]
    assert first["reason"] == "risky" and first["importance"] == "high"
    assert first["requestId"] == "ap-1" and first["requestKind"] == "approval"
    assert first["sessionId"] == s.id and len(first["summary"]) <= 120
    assert harness.hooked and harness.hooked[0][0] == "escalation"
    await harness.pending(s, "q-1", kind="question")
    assert harness.of("jarvis.escalation")[-1]["reason"] == "question"
    rows = {row.sessionId: row for row in overview.build_rows(harness.service)}
    assert rows[s.id].digest.startswith("approve shell")


async def test_notify_settings_are_honoured(harness: Harness) -> None:
    notify = harness.core.settings.jarvis.notify
    notify.inApp = False
    notify.os = False
    notify.messenger = False
    await harness.start()
    s = await harness.session("p")
    await harness.pending(s, "ap-1")
    assert harness.of("jarvis.escalation") == [] and harness.hooked == []
    notify.messenger = True
    await harness.pending(s, "ap-2")
    assert harness.of("jarvis.escalation") == []
    assert [kind for kind, _ in harness.hooked] == ["escalation"]
    # Messenger only from importance high.
    await harness.service.escalate(reason="finished", summary="x", importance="normal")
    assert len(harness.hooked) == 1
    harness.core.settings.jarvis.proactive.needsYou = False
    await harness.pending(s, "ap-3")
    assert len(harness.hooked) == 1


async def test_takeover_is_detected_by_origin(harness: Harness) -> None:
    await harness.start()
    s = await harness.session("p")
    for kind in ("schedule", "system", "subagent", "gateway", "jarvis"):
        await harness.emit(s, events.message_user("not the person", origin={"kind": kind}))
    assert harness.engine.typed == []
    assert harness.engine.is_paused(s.id) is False
    await harness.emit(s, events.message_user("stop, I'll do it", origin={"kind": "human"}))
    assert harness.engine.typed == [s.id]
    assert harness.engine.is_paused(s.id)
    state = harness.service.session_state(s.id, canonical(s.workdir))
    assert state.pausedUntil == iso(harness.clock.now() + timedelta(minutes=10))
    # Stored before 1.53.0: no origin means the person.
    await harness.emit(s, events.message_user("again"))
    assert harness.engine.typed == [s.id, s.id]
    # A paused session's request is the person's even when Jarvis would get it.
    harness.engine.route_to = "jarvis"
    await harness.pending(s, "ap-1")
    await harness.tick(ROUTED_BATCH_SEC + 1)
    assert harness.wakes == []
    assert harness.of("jarvis.escalation")[-1]["requestId"] == "ap-1"
    # Quiet for jarvis.quietMinutes: authority returns.
    harness.clock.advance(10 * 60 + 1)
    assert harness.engine.is_paused(s.id) is False
    await harness.pending(s, "ap-2")
    await harness.tick(ROUTED_BATCH_SEC + 0.01)
    assert len(harness.wakes) == 1 and "ap-2" in harness.wakes[0]
    reasons = [p["reason"] for p in harness.of("jarvis.state")]
    assert "paused" in reasons


# -- budget -----------------------------------------------------------------------------


async def test_over_the_wake_budget_everything_goes_to_the_person(harness: Harness) -> None:
    harness.core.settings.jarvis.budget.wakesPerHour = 1
    harness.engine.route_to = "jarvis"
    await harness.start()
    s = await harness.session("p", title="build")
    await harness.pending(s, "ap-1")
    await harness.tick(ROUTED_BATCH_SEC + 0.01)
    assert len(harness.wakes) == 1
    assert harness.intake.budget.exhausted()
    budget = [p for p in harness.of("jarvis.escalation") if p["reason"] == "budget"]
    assert len(budget) == 1 and budget[0]["importance"] == "high"
    assert harness.of("jarvis.state")[-1]["reason"] == "budget"
    assert harness.of("jarvis.state")[-1]["status"]["budget"]["exhausted"] is True
    # Routed to Jarvis no longer: straight to the person.
    await harness.pending(s, "ap-2")
    await harness.tick(ROUTED_BATCH_SEC + 0.01)
    assert len(harness.wakes) == 1
    assert harness.of("jarvis.escalation")[-1]["requestId"] == "ap-2"
    rows = {row.sessionId: row for row in overview.build_rows(harness.service)}
    assert rows[s.id].waitingOnPerson == 0  # nothing is really pending in the queue
    # Endings become one escalation instead of a wake.
    await harness.emit(s, events.turn_started("t1"))
    await harness.emit(s, events.turn_done("t1", "error"))
    await harness.tick(NOTICE_WINDOW_SEC + 1)
    assert len(harness.wakes) == 1
    assert harness.of("jarvis.escalation")[-1]["reason"] == "error"
    # An hour later the allowance is back.
    await harness.tick(3600)
    assert not harness.intake.budget.exhausted()
    assert harness.of("jarvis.state")[-1]["status"]["budget"]["exhausted"] is False


async def test_daily_tokens_are_counted_from_jarvis_usage(harness: Harness) -> None:
    harness.core.settings.jarvis.budget.dailyTokens = 1000
    harness.engine.route_to = "jarvis"
    await harness.start()
    jarvis = await harness.session("jarvis-home")
    harness.service._session_id = jarvis.id
    await harness.emit(jarvis, events.usage(600, 300))
    assert harness.intake.budget.tokens_today() == 900
    assert not harness.intake.budget.exhausted()
    await harness.emit(jarvis, events.usage(80, 20))
    assert harness.intake.budget.exhausted()
    # Persisted per local day.
    assert (harness.service.home / "budget.json").is_file()
    worker = await harness.session("w")
    await harness.pending(worker, "ap-1")
    await harness.tick(ROUTED_BATCH_SEC + 1)
    assert harness.wakes == []
    assert harness.of("jarvis.escalation")[-1]["requestId"] == "ap-1"
    # A new day starts from zero.
    await harness.tick(24 * 3600)
    assert harness.intake.budget.tokens_today() == 0
    assert not harness.intake.budget.exhausted()


# -- overview ordering --------------------------------------------------------------------


async def test_overview_order_and_the_40_row_cap(harness: Harness) -> None:
    await harness.start()
    intake = harness.intake
    base = harness.clock.now()
    project = canonical(harness.tmp)
    for index in range(50):
        intake.digests[f"s-{index:02d}"] = Digest(
            session_id=f"s-{index:02d}",
            project=project,
            title=f"t{index}",
            status="idle",
            last_activity=iso(base + timedelta(minutes=index)),
        )
    intake.digests["s-03"].status = "running"
    intake.digests["s-04"].status = "error"
    # A digest's own pending item does not count without the live queue: the
    # session is not open, so nothing can be waiting in it.
    intake.digests["s-05"].pending["ap"] = PendingItem(
        "approval", "ap", "person", "approve shell ls"
    )
    intake.digests["s-06"].status = "running"
    rows = overview.build_rows(harness.service)
    assert len(rows) == 50
    ordered = [row.sessionId for row in rows]
    # Error, then the running ones newest first, then the rest newest first.
    assert ordered[:3] == ["s-04", "s-06", "s-03"]
    assert ordered[3:6] == ["s-49", "s-48", "s-47"]
    text = overview.render_table(harness.service, rows[: overview.CONTEXT_ROWS], len(rows))
    body = [line for line in text.splitlines() if line.startswith("s-")]
    assert len(body) == 40
    assert "50 supervised session(s), the 40 most relevant shown" in text
    assert overview.build_rows(harness.service, include_idle=False)[0].sessionId == "s-04"
    assert len(overview.build_rows(harness.service, include_idle=False)) == 3
    assert overview.build_rows(harness.service, project=str(harness.tmp / "nope")) == []


async def test_waiting_rows_rank_first(harness: Harness) -> None:
    harness.engine.route_to = "person"
    await harness.start()
    busy = await harness.session("busy")
    waiting = await harness.session("waiting")
    await harness.emit(busy, events.turn_started("t1"))
    busy.current_turn = "t1"  # the synthetic turn, as the loop would mark it
    ask = asyncio.ensure_future(
        harness.core.approvals.request(
            waiting, "shell", {"command": "rm -rf build"}, unattended=True, timeout_sec=30
        )
    )
    for _ in range(50):
        if harness.core.approvals.count(waiting.id):
            break
        await asyncio.sleep(0.01)
    rows = overview.build_rows(harness.service)
    assert [row.sessionId for row in rows][:2] == [waiting.id, busy.id]
    assert rows[0].status == "awaiting_approval" and rows[0].waitingOnPerson == 1
    assert rows[1].status == "running"
    request_id = harness.core.approvals.list()[0].requestId
    await harness.core.approvals.respond(request_id, "deny")
    await asyncio.wait_for(ask, 5)
    assert harness.intake.digests[waiting.id].pending == {}


async def test_nothing_is_decided_for_a_paused_session(harness: Harness) -> None:
    harness.engine.route_to = "auto"
    await harness.start()
    s = await harness.session("p")
    harness.engine.takeover(s.id, project=canonical(s.workdir))
    await harness.pending(s, "ap-1")
    assert harness.of("jarvis.decision") == []
    assert harness.intake.digests[s.id].pending["ap-1"].route == "person"
    assert harness.of("jarvis.escalation")[-1]["requestId"] == "ap-1"
