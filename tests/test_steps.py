"""Turn and step on every event, timing per model call (M17 2a, protocol 1.44.0)."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.tools.registry import Tool, ToolResult


class _TwoCalls:
    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.n = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.n += 1
        await asyncio.sleep(0.01)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=100 * self.n, output_tokens=7))
        if self.n == 1:
            call = ToolCall(id="c1", name="ping", arguments={})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="pong")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _ping(ctx: Any, args: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, output="pong")


async def test_a_turn_is_numbered_in_steps_with_timing(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
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
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        turn_id = agent_loop.start_turn(core, session, "ping it")
        await asyncio.wait_for(session.turn_task, timeout=60)

        started = recorder.of_kind("step.started")
        done = recorder.of_kind("step.done")
        assert [e["payload"]["step"] for e in started] == [1, 2]
        assert [e["payload"]["outcome"] for e in done] == ["tool_use", "end_turn"]
        assert done[1]["payload"]["inputTokens"] == 200
        assert done[1]["payload"]["text"] == "pong" and done[0]["payload"]["toolCalls"] == 1
        assert done[0]["payload"]["totalMs"] >= 10
        assert all(e["payload"]["turnId"] == turn_id for e in started + done)

        # Every event inside the turn carries the turn and the step it happened in.
        call = recorder.of_kind("tool.call")[0]
        assert (call["turnId"], call["step"]) == (turn_id, 1)
        reply = recorder.of_kind("message.done")[-1]
        assert (reply["turnId"], reply["step"]) == (turn_id, 2)

        # And they are stored: a resume gets them back.
        stored = await core.store.events_after(session.id, 0)
        tool_row = next(e for e in stored if e["kind"] == "tool.call")
        assert (tool_row["turnId"], tool_row["step"]) == (turn_id, 1)
    finally:
        await daemon.stop()


async def test_an_older_database_gains_the_columns(tmp_path: Any) -> None:
    from snowpea_core.session.store import Store

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,"
        " payload_json TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (session_id, seq))"
    )
    conn.execute(
        "INSERT INTO events VALUES ('s-old', 1, 'message.user', '{\"text\": \"hi\"}', 'T')"
    )
    conn.commit()
    conn.close()
    store = Store(db)
    try:
        rows = await store.events_after("s-old", 0)
        assert rows[0]["kind"] == "message.user" and "turnId" not in rows[0]
        await store.append_event("s-old", 2, "message.done", {"text": "x"}, "T", "t-1", 3)
        rows = await store.events_after("s-old", 1)
        assert (rows[0]["turnId"], rows[0]["step"]) == ("t-1", 3)
    finally:
        store.close()
