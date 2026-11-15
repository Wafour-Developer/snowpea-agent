"""Protocol 1.51.0: codes next to the English text clients used to parse.

``provider.retry`` says why (``reason``, ``waitLeftSec``, ``max`` null for an
unreachable server), ``hook.continue`` says who (``kind``, ``count``,
``limit``), Trajectory rows carry ``data.kind``, a goal's ``blockedReason.code``
is a closed set and ``step.done`` carries ``errorCode``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import goals, verify_gate
from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent import turn_stopping as ts
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolCall, Usage
from snowpea_core.server.protocol import PROTOCOL_VERSION, GoalView
from snowpea_core.session import events, trajectory
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolResult

TIMEOUT = 30.0
DNS = "snowpeallm (qwen): ConnectError: [Errno 8] nodename nor servname provided, or not known"
CHECKPOINT = "Checkpoint.\n\n## Remaining (next turn)\n1. fix the router\n2. build again\n"


def test_the_version() -> None:
    # 1.51.0 or any later additive version.
    assert tuple(int(part) for part in PROTOCOL_VERSION.split(".")) >= (1, 51, 0)


# ---------------------------------------------------------------------------
# providers and turns
# ---------------------------------------------------------------------------


def _connect_error() -> ProviderError:
    try:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    except httpx.ConnectError as cause:
        try:
            raise ProviderError("internal", DNS) from cause
        except ProviderError as exc:
            return exc


class _Scripted:
    """Raises (or sleeps silent for) the scripted failures in turn, then answers."""

    vendor = "test-scripted"
    model = "m"

    def __init__(self, failures: list[Any]) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            if isinstance(failure, float):
                await asyncio.sleep(failure)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    *,
    setup: Any = None,
    **limits: Any,
) -> tuple[Any, Any, Recorder]:
    monkeypatch.setattr(agent_loop, "PROVIDER_RETRY_BASE_SEC", 0.001)
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    assert core is not None
    for name, value in limits.items():
        setattr(core.settings.agent, name, value)
    core.providers.get = lambda _p, _m: provider  # type: ignore[assignment]
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    if setup is not None:
        setup(session)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    try:
        agent_loop.start_turn(core, session, "hello")
        assert session.turn_task is not None
        await asyncio.wait_for(session.turn_task, timeout=TIMEOUT)
    except BaseException:
        await daemon.stop()
        raise
    return daemon, session, recorder


def _payloads(recorder: Recorder, kind: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in recorder.of_kind(kind)]


async def test_an_unreachable_retry_has_no_count_limit_and_says_how_long_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([_connect_error() for _ in range(5)])
    daemon, _, recorder = await _turn(
        tmp_path, monkeypatch, provider, provider_retries=3, provider_connect_wait_sec=300
    )
    try:
        retries = _payloads(recorder, "provider.retry")
        assert len(retries) == 5
        for number, retry in enumerate(retries, 1):
            assert retry["reason"] == "unreachable"
            assert retry["max"] is None, "a count limit would read 4/4, 5/5 …"
            assert retry["attempt"] == number
            assert isinstance(retry["waitLeftSec"], int) and 290 <= retry["waitLeftSec"] <= 300
        continues = _payloads(recorder, "hook.continue")
        assert [c["kind"] for c in continues] == ["provider_retry"] * 5
        assert [c["count"] for c in continues] == [1, 2, 3, 4, 5]
        assert {c["limit"] for c in continues} == {None}
    finally:
        await daemon.stop()


async def test_a_503_retry_is_transient_with_a_count_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([ProviderError("internal", "x: HTTP 503: busy")])
    daemon, _, recorder = await _turn(tmp_path, monkeypatch, provider, provider_retries=4)
    try:
        (retry,) = _payloads(recorder, "provider.retry")
        assert retry["reason"] == "transient"
        assert retry["max"] == 4 and retry["attempt"] == 1
        assert retry["waitLeftSec"] is None
        (cont,) = _payloads(recorder, "hook.continue")
        assert (cont["kind"], cont["count"], cont["limit"]) == ("provider_retry", 1, 4)
        assert "retrying 1/4" in cont["reason"]
        assert _payloads(recorder, "turn.done")[-1]["reason"] == "complete"
    finally:
        await daemon.stop()


async def test_a_silent_server_retry_says_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_loop, "PROVIDER_WAIT_TICK", 0.02)
    provider = _Scripted([2.0])
    daemon, _, recorder = await _turn(
        tmp_path, monkeypatch, provider, provider_first_token_sec=0.1
    )
    try:
        (retry,) = _payloads(recorder, "provider.retry")
        assert retry["reason"] == "silent" and isinstance(retry["max"], int)
        assert retry["waitLeftSec"] is None
    finally:
        await daemon.stop()


def test_retry_reasons_are_told_apart() -> None:
    silent = ProviderError("timeout", "the model server sent no first token for 3s (timed out)")
    idle = ProviderError("timeout", "the model server sent no idle for 90s (timed out)")
    assert agent_loop._retry_reason(silent) == "silent"
    assert agent_loop._retry_reason(idle) == "silent"
    for text in ("x: ReadTimeout: ", "x: HTTP 429: slow down", "x: connection reset"):
        assert agent_loop._retry_reason(ProviderError("internal", text)) == "transient"


async def test_step_done_carries_the_error_code_of_a_failed_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([ProviderError("invalid_params", "the server refused the request")])
    daemon, _, recorder = await _turn(tmp_path, monkeypatch, provider)
    try:
        (done,) = _payloads(recorder, "step.done")
        assert done["outcome"] == "error" and done["errorCode"] == "invalid_params"
        assert done["errorCode"] == _payloads(recorder, "error")[-1]["code"]
    finally:
        await daemon.stop()


async def test_step_done_says_internal_for_a_bug_and_null_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([RuntimeError("boom")])
    daemon, _, recorder = await _turn(tmp_path, monkeypatch, provider)
    try:
        (done,) = _payloads(recorder, "step.done")
        assert done["errorCode"] == "internal"
    finally:
        await daemon.stop()
    good = _Scripted([])
    daemon, _, recorder = await _turn(tmp_path / "ok", monkeypatch, good)
    try:
        (done,) = _payloads(recorder, "step.done")
        assert done["outcome"] == "end_turn" and done["errorCode"] is None
    finally:
        await daemon.stop()


class _OverflowOnce:
    vendor = "local"
    model = "qwen38"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[Any], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(
                "provider_error",
                "HTTP 400: This model's maximum context length is 262144 tokens. However, "
                "you requested 16384 output tokens and your prompt contains at least 245761 "
                "input tokens",
            )
        yield StreamEvent(kind="text_delta", text=f"answer {self.calls}")
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=5))
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_the_overflow_note_is_kind_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def history(session: Any) -> None:
        for n in range(30):
            session.history.append(ChatMessage(role="user", content=f"question {n}"))
            session.history.append(ChatMessage(role="assistant", content=f"reply {n}"))

    daemon, _, recorder = await _turn(tmp_path, monkeypatch, _OverflowOnce(), setup=history)
    try:
        continues = _payloads(recorder, "hook.continue")
        assert [(c["kind"], c["count"], c["limit"]) for c in continues] == [("overflow", 1, 1)]
    finally:
        await daemon.stop()


class _EmptyOnce:
    vendor = "fake"
    model = "m"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[Any], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.calls > 1:
            yield StreamEvent(kind="text_delta", text="here it is")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_the_empty_answer_nudge_is_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, _, recorder = await _turn(tmp_path, monkeypatch, _EmptyOnce())
    try:
        (cont,) = _payloads(recorder, "hook.continue")
        assert (cont["kind"], cont["count"], cont["limit"]) == (
            "other",
            1,
            agent_loop.EMPTY_RETRIES,
        )
    finally:
        await daemon.stop()


class _ToolsTwice:
    """Calls a tool while it has tools and has called fewer than two, then answers."""

    vendor = "fake"
    model = "m"

    def __init__(self) -> None:
        self.tool_calls = 0

    async def stream(self, messages: list[Any], tools: list[Any], **_: Any) -> Any:
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        if tools and self.tool_calls < 2:
            self.tool_calls += 1
            call = ToolCall(id=f"t{self.tool_calls}", name="read_file", arguments={"path": "a.txt"})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="done for now")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_a_budget_continuation_is_kind_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def small(session: Any) -> None:
        session.max_tool_rounds = 1
        (session.workdir / "a.txt").write_text("hi", encoding="utf-8")

    daemon, _, recorder = await _turn(
        tmp_path, monkeypatch, _ToolsTwice(), setup=small, auto_budget_continuations=3
    )
    try:
        budget = [c for c in _payloads(recorder, "hook.continue") if c["kind"] == "budget"]
        assert budget and (budget[0]["count"], budget[0]["limit"]) == (1, 3)
    finally:
        await daemon.stop()


async def test_a_plugin_decision_reaches_hook_continue_with_its_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def once(core: Any, session: Any, ctx: ts.StopContext) -> ts.StopDecision | None:
        if asked:
            return None
        asked.append(ctx.last_message)
        return ts.StopDecision("Check the changelog.", "plugin:changelog", count=1, limit=1)

    dispose = ts.CHAIN.register("plugin:changelog", once, order=150)
    try:
        daemon, _, recorder = await _turn(tmp_path, monkeypatch, _Scripted([]))
        try:
            (cont,) = _payloads(recorder, "hook.continue")
            assert (cont["kind"], cont["count"], cont["limit"]) == ("other", 1, 1)
            assert cont["reason"] == "Check the changelog."
        finally:
            await daemon.stop()
    finally:
        dispose()


# ---------------------------------------------------------------------------
# hook.continue: the stop chain's decisions and the wire shape
# ---------------------------------------------------------------------------


def test_decisions_take_their_kind_from_the_producer() -> None:
    assert ts.StopDecision("x", "verify-gate").kind == "verify_gate"
    assert ts.StopDecision("x", "unfinished-work").kind == "unfinished_work"
    assert ts.StopDecision("x", "stop-hook").kind == "stop_hook"
    assert ts.StopDecision("x", "plugin:mine").kind == "other"
    assert ts.StopDecision("x", "plugin:mine", kind="goal").kind == "goal"
    assert ts.StopDecision("x", "plugin:mine", kind="made-up").kind == "other"


def _gate_session(tmp_path: Path) -> Session:
    session = Session(id="s-151", workdir=tmp_path)
    verify_gate.begin_turn(session)
    return session


def _core(**agent: Any) -> SimpleNamespace:
    values = {"verifyOnStop": "auto", "unfinished_continue": 2, **agent}
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(**values)))


async def test_the_verify_gate_counts_its_nudges(tmp_path: Path) -> None:
    session = _gate_session(tmp_path)
    verify_gate.observe(session, "patch", {"path": "a.py"}, ToolResult(ok=True, path="a.py"))
    decision = ts._verify_gate(_core(), session, ts.StopContext("done"))
    assert decision is not None
    assert (decision.kind, decision.count, decision.limit) == (
        "verify_gate",
        1,
        verify_gate.MAX_NUDGES,
    )


async def test_unfinished_work_counts_its_continuations(tmp_path: Path) -> None:
    session = _gate_session(tmp_path)
    verify_gate.observe(session, "read_file", {"path": "a.txt"}, ToolResult(ok=True))
    decision = ts._unfinished_work(_core(), session, ts.StopContext(CHECKPOINT))
    assert decision is not None
    assert (decision.kind, decision.count, decision.limit) == ("unfinished_work", 1, 2)


async def test_stop_hooks_count_up_to_their_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.skills import hooks as plugin_hooks

    async def blocks(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(block=True, reason="keep going")

    monkeypatch.setattr(plugin_hooks, "stop", blocks)
    decision = await ts._stop_hooks(None, None, ts.StopContext("done", stop_continuations=2))
    assert decision is not None
    assert (decision.kind, decision.count, decision.limit) == (
        "stop_hook",
        3,
        plugin_hooks.MAX_STOP_CONTINUATIONS,
    )


def test_hook_continue_carries_kind_in_its_payload_and_survives_a_replay() -> None:
    kind, payload = events.hook_continue("go on", 2, "unfinished_work", 2)
    assert kind == "hook.continue"
    assert payload == {"reason": "go on", "count": 2, "kind": "unfinished_work", "limit": 2}
    assert events.validate(kind, payload) == payload
    # An event stored before 1.51.0 reads back as "other".
    old = events.validate(kind, {"reason": "go on", "count": 0})
    assert old["kind"] == "other" and old["limit"] is None


# ---------------------------------------------------------------------------
# Trajectory rows
# ---------------------------------------------------------------------------


def _ev(seq: int, event: tuple[str, dict[str, Any]], turn: str | None = "t1") -> dict[str, Any]:
    kind, payload = event
    return {"seq": seq, "kind": kind, "payload": payload, "ts": "2026-11-15T00:00:00Z",
            "turnId": turn}


def test_every_context_system_and_compacted_row_has_a_data_kind() -> None:
    goal = goals.Goal(objective="ship it", rounds=3, max_rounds=5)
    blocked = goals.Goal(objective="deploy", phase="blocked", blocked_code="no-credentials",
                         blocked_message="needs the AWS key")
    stream = [
        _ev(1, ("turn.started", {"prompt": "hi", "reason": "user"})),
        _ev(2, events.message_done("summary of before", role="system")),
        _ev(3, events.hook_continue("listed work", 1, "unfinished_work", 2)),
        _ev(4, ("context.host", {"chars": 120})),
        _ev(5, events.error("rate_limited", "slow down")),
        _ev(6, events.provider_retry(2, None, 4.0, "ConnectError", "unreachable", 240)),
        _ev(7, events.provider_retry(1, 3, 2.0, "HTTPStatusError")),
        _ev(8, ("model.changed", {"provider": "openai", "model": "gpt-x"})),
        _ev(9, ("mode.changed", {"mode": "plan"})),
        _ev(10, events.goal_changed(goal.view(armed=True))),
        _ev(11, events.goal_changed(blocked.view())),
        _ev(12, events.goal_changed(None)),
        _ev(13, ("compaction", {"before": 9000, "after": 1200, "summaryChars": 800,
                                "auto": True, "kept": 4})),
        _ev(14, ("session.forked", {"fromSessionId": "s-src", "atTurnId": "t0"}), turn=None),
    ]
    rows = trajectory.build(stream)
    data = {row["seq"]: row.get("data") or {} for row in rows}
    assert data[2] == {"kind": "summary", "chars": len("summary of before")}
    assert data[3] == {"kind": "continued", "hookKind": "unfinished_work", "count": 1, "limit": 2}
    assert data[4] == {"kind": "host_note", "chars": 120}
    assert data[5] == {"kind": "error", "code": "rate_limited"}
    assert data[6] == {
        "kind": "provider_retry",
        "attempt": 2,
        "max": None,
        "reason": "unreachable",
        "delaySec": 4.0,
        "waitLeftSec": 240,
        "error": "ConnectError",
    }
    assert data[7]["reason"] == "transient" and data[7]["max"] == 3
    assert data[8] == {"kind": "model_changed", "provider": "openai", "model": "gpt-x"}
    assert data[9] == {"kind": "mode_changed", "mode": "plan"}
    assert data[10] == {
        "kind": "goal",
        "goalId": goal.id,
        "phase": "active",
        "round": 3,
        "maxRounds": 5,
        "blockedCode": None,
        "cleared": False,
    }
    assert data[11]["blockedCode"] == "no-credentials" and data[11]["phase"] == "blocked"
    assert data[12]["cleared"] is True and data[12]["goalId"] is None
    assert data[13] == {"kind": "compaction", "before": 9000, "after": 1200,
                        "summaryChars": 800, "auto": True, "kept": 4}
    assert data[14] == {"kind": "forked", "fromSessionId": "s-src", "atTurnId": "t0"}
    for row in rows:
        if row["kind"] in ("context", "system", "compacted"):
            assert row["data"]["kind"], row
    titles = {row["seq"]: row["title"] for row in rows}
    assert titles[3] == "kept going" and titles[7] == "retry 1/3" and titles[6] == "retry 2"


def test_a_continue_stored_before_151_folds_to_other() -> None:
    rows = trajectory.build([_ev(1, ("hook.continue", {"reason": "x", "count": 1}))])
    assert rows[0]["data"] == {"kind": "continued", "hookKind": "other", "count": 1,
                               "limit": None}


# ---------------------------------------------------------------------------
# goal blocked codes
# ---------------------------------------------------------------------------


def test_blocked_codes_are_a_closed_set() -> None:
    assert goals.BLOCKED_CODES == (
        "round-limit",
        "needs-input",
        "no-credentials",
        "no-access",
        "external",
        "other",
    )
    for code in goals.BLOCKED_CODES:
        assert goals.blocked_code(code) == code
    free_form = {
        "No_Credentials": "no-credentials",
        "missing api key": "no-credentials",
        "auth-expired": "no-credentials",
        "waiting on user": "needs-input",
        "needs decision": "needs-input",
        "permission denied": "no-access",
        "server down": "external",
        "network unreachable": "external",
        "unspecified": "other",
        "": "other",
        "weird": "other",
    }
    for raw, expected in free_form.items():
        assert goals.blocked_code(raw) == expected, raw


def test_a_model_block_is_stored_with_a_code_from_the_set() -> None:
    session = SimpleNamespace(goal=None, goal_armed=False, is_subagent=False)
    goal = goals.start(session, "deploy")
    for _ in range(goals.BLOCKED_STREAK):
        goals.update_from_model(
            session, revision=goal.revision, phase="blocked", code="Waiting_on_user",
            message="which region?",
        )
    assert goal.phase == "blocked"
    view = GoalView(**goal.view())
    assert view.blockedReason is not None
    assert view.blockedReason.code == "needs-input"
    assert view.blockedReason.message == "which region?"


def test_the_round_cap_and_a_stored_free_form_code_validate() -> None:
    session = SimpleNamespace(goal=None, goal_armed=False, is_subagent=False)
    goal = goals.start(session, "ship", max_rounds=1)
    goals.begin_round(session)
    assert goals.begin_round(session) is None
    assert GoalView(**goal.view()).blockedReason.code == "round-limit"  # type: ignore[union-attr]
    # A goal saved before 1.51.0 with a code of the model's own.
    old = goals.Goal(objective="x", phase="blocked", blocked_code="flaky-ci", blocked_message="m")
    assert GoalView(**old.view()).blockedReason.code == "other"  # type: ignore[union-attr]


def test_goal_update_offers_the_model_the_codes_it_may_name() -> None:
    from snowpea_core.agent import goal_driver

    (tool,) = [t for t in goal_driver.TOOLS if t.name == "goal_update"]
    offered = tool.input_schema["properties"]["code"]["enum"]
    assert offered == ["needs-input", "no-credentials", "no-access", "external", "other"]
