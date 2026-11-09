"""A long turn full of large tool results never meets a context-overflow 400.

The provider below is a strict server: it counts every character at three per
token (our estimate uses four, so we run 25% low, as the field report did),
refuses prompt + max_tokens over a 262144 window, and never truncates.
"""

from __future__ import annotations

import asyncio
from typing import Any

from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text
from snowpea_core.tools.registry import Tool, ToolResult

WINDOW = 262144
MAX_TOKENS = 16384
ROUNDS = 12


class _Strict:
    vendor = "strict"

    def __init__(self) -> None:
        self.calls = 0
        self.refusals = 0
        self.summaries = 0
        self.largest = 0

    @staticmethod
    def _tokens(messages: list[ChatMessage]) -> int:
        return sum(len(message_text(m)) // 3 + 4 for m in messages)

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        used = self._tokens(messages)
        self.largest = max(self.largest, used)
        if used + MAX_TOKENS > WINDOW:
            self.refusals += 1
            raise ProviderError(
                "internal",
                f"HTTP 400: prompt ({used}) + max tokens ({MAX_TOKENS}) exceeds the context "
                f"({WINDOW})",
            )
        if not tools:
            self.summaries += 1
            yield StreamEvent(kind="text_delta", text="summary of the work so far")
            yield StreamEvent(kind="usage", usage=Usage(input_tokens=used, output_tokens=10))
            yield StreamEvent(kind="done", stop_reason="end_turn")
            return
        self.calls += 1
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=used, output_tokens=10))
        if self.calls <= ROUNDS:
            call = ToolCall(id=f"c{self.calls}", name="big_read", arguments={"n": self.calls})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="finished")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _big(ctx: Any, args: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, output=("line %d of a log\n" % args.get("n", 0)) * 12_000)


async def test_a_long_turn_of_big_reads_never_overflows(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        provider = _Strict()
        core.providers.get = lambda _p, _m: provider
        core.settings.providers["local"] = {"context_window": WINDOW}
        core.tools.register(
            Tool(
                name="big_read",
                category="test",
                description="read a big log",
                input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                permission="read",
                run=_big,
            )
        )
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        session.provider = "local"
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        agent_loop.start_turn(core, session, "read the log again and again")
        await asyncio.wait_for(session.turn_task, timeout=120)
        assert provider.refusals == 0, "the server refused a prompt: compaction fired too late"
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
        assert provider.summaries >= 1, "a turn this long must have been compacted"
    finally:
        await daemon.stop()
