"""The Trajectory projection (M17 2b, protocol 1.45.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from _support import make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.server.protocol import SessionTrajectoryParams
from snowpea_core.server.trajectory_handlers import session_trajectory_handler
from snowpea_core.session import trajectory
from snowpea_core.tools.registry import Tool, ToolResult


class _TwoCalls:
    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.n = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.n += 1
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=50, output_tokens=5))
        if self.n % 2 == 1:
            yield StreamEvent(kind="text_delta", text="Let me check.")
            call = ToolCall(id=f"c{self.n}", name="ping", arguments={"path": "a.txt"})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="All good.")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _ping(ctx: Any, args: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, output="pong")


async def _session(tmp_path: Any) -> tuple[Any, Any]:
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    core.providers.get = lambda _p, _m: _TwoCalls()
    core.tools.register(
        Tool(
            name="ping",
            category="test",
            description="ping",
            input_schema={"type": "object", "properties": {}},
            permission="read",
            run=_ping,
        )
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    return daemon, session


async def _turn(core: Any, session: Any, text: str) -> str:
    turn_id = agent_loop.start_turn(core, session, text)
    await asyncio.wait_for(session.turn_task, timeout=60)
    return turn_id


async def test_a_turn_becomes_turn_user_step_tool_step_rows(tmp_path: Any) -> None:
    daemon, session = await _session(tmp_path)
    core = daemon.core
    try:
        turn_id = await _turn(core, session, "check a.txt")
        result = await session_trajectory_handler(
            None, SessionTrajectoryParams(sessionId=session.id), core
        )
        rows = [r for r in result.rows if r.turnId == turn_id]
        assert [r.kind for r in rows] == ["turn", "user", "message", "tool", "message"]
        turn, user, first, tool, second = rows
        assert turn.ok is True and turn.data["outcome"] == "complete" and turn.endSeq
        assert user.text == "check a.txt"
        assert first.step == 1 and first.text == "Let me check." and first.data["toolCalls"] == 1
        assert first.data["totalMs"] is not None and first.data["inputTokens"] == 50
        assert tool.step == 1 and tool.title == "ping a.txt" and tool.ok
        assert tool.text.startswith("pong")
        assert tool.endSeq > tool.seq
        assert second.step == 2 and second.text == "All good."
    finally:
        await daemon.stop()


async def test_a_later_read_gets_only_what_changed(tmp_path: Any) -> None:
    daemon, session = await _session(tmp_path)
    core = daemon.core
    try:
        await _turn(core, session, "one")
        first = await session_trajectory_handler(
            None, SessionTrajectoryParams(sessionId=session.id), core
        )
        second_turn = await _turn(core, session, "two")
        later = await session_trajectory_handler(
            None, SessionTrajectoryParams(sessionId=session.id, afterSeq=first.lastSeq), core
        )
        assert later.lastSeq > first.lastSeq
        assert later.rows and all(r.turnId == second_turn for r in later.rows)
    finally:
        await daemon.stop()


def test_a_log_from_before_steps_uses_message_done() -> None:
    events = [
        {"seq": 1, "kind": "turn.started", "ts": "T1", "payload": {"turnId": "t1", "prompt": "x"}},
        {"seq": 2, "kind": "message.user", "ts": "T1", "payload": {"text": "hi"}},
        {"seq": 3, "kind": "message.delta", "ts": "T2", "payload": {"text": "he"}},
        {"seq": 4, "kind": "message.done", "ts": "T3", "payload": {"text": "hello"}},
        {"seq": 5, "kind": "turn.done", "ts": "T4", "payload": {"turnId": "t1", "reason": "x"}},
    ]
    rows = trajectory.build(events)
    assert [r["kind"] for r in rows] == ["turn", "user", "message"]
    assert rows[2]["text"] == "hello" and rows[0]["endSeq"] == 5 and rows[0]["ok"] is False


def test_a_delegation_points_at_the_child_session() -> None:
    events = [
        {
            "seq": 1,
            "kind": "subagent.spawn",
            "ts": "T",
            "payload": {
                "agentId": "a1",
                "name": "critic",
                "task": "review the plan",
                "sessionId": "s-child",
            },
        },
        {
            "seq": 2,
            "kind": "subagent.done",
            "ts": "T",
            "payload": {
                "agentId": "a1",
                "ok": True,
                "summary": "looks fine",
                "reason": "complete",
                "rounds": 7,
            },
        },
    ]
    [row] = trajectory.build(events)
    assert row["kind"] == "subagent" and row["data"]["childSessionId"] == "s-child"
    assert row["ok"] is True and row["text"] == "looks fine" and row["data"]["rounds"] == 7


async def test_an_unknown_session_is_not_found(tmp_path: Any) -> None:
    import pytest

    from snowpea_core.server.errors import RpcError

    daemon = await make_daemon(tmp_path / "home")
    try:
        with pytest.raises(RpcError):
            await session_trajectory_handler(
                None, SessionTrajectoryParams(sessionId="s-nope"), daemon.core
            )
    finally:
        await daemon.stop()


def test_a_failed_step_row_carries_the_error_code() -> None:
    events = [
        {"seq": 1, "kind": "turn.started", "ts": "T1", "payload": {"turnId": "t1", "prompt": "x"}},
        {"seq": 2, "kind": "step.started", "ts": "T1", "payload": {"turnId": "t1", "step": 1}},
        {
            "seq": 3,
            "kind": "step.done",
            "ts": "T2",
            "payload": {
                "turnId": "t1",
                "step": 1,
                "outcome": "error",
                "error": "server said no",
                "errorCode": "rate_limited",
            },
        },
        {"seq": 4, "kind": "turn.done", "ts": "T3", "payload": {"turnId": "t1", "reason": "error"}},
    ]
    rows = trajectory.build(events)
    failed = [r for r in rows if (r.get("data") or {}).get("error")]
    assert failed and failed[0]["data"]["errorCode"] == "rate_limited"
