"""A bare 400 on a long request is tested for length, then the history is compacted.

The server below has a hidden limit, no model-list entry, and answers anything
longer with ``invalid request to model`` (no numbers).  The window is unknown,
so it is assumed to be 131072 and compaction would not fire for a long time.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text
from snowpea_core.tools.registry import Tool, ToolResult

LIMIT = 60_000
ROUNDS = 8


class _Hidden:
    vendor = "hidden"

    def __init__(self) -> None:
        self.calls = 0
        self.refusals = 0
        self.trials = 0
        self.summaries = 0

    @staticmethod
    def _tokens(messages: list[ChatMessage]) -> int:
        return sum(len(message_text(m)) // 3 + 4 for m in messages)

    async def stream(
        self, messages: list[ChatMessage], tools: list[Any], max_tokens: int = 0, **_: Any
    ) -> Any:
        used = self._tokens(messages)
        if used > LIMIT:
            self.refusals += 1
            raise ProviderError(
                "invalid_request",
                'HTTP 400: {"error":{"message":"invalid request to model",'
                '"type":"invalid_request_error","param":null,"code":null}}',
            )
        if max_tokens == 1:
            self.trials += 1
            yield StreamEvent(kind="done", stop_reason="max_tokens")
            return
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
    return ToolResult(ok=True, output=f"line {args.get('n', 0)} of a log\n" * 2_000)


async def test_a_bare_400_on_a_long_request_is_length_so_it_compacts(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        provider = _Hidden()
        core.providers.get = lambda _p, _m: provider
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
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
        assert provider.refusals >= 1, "the scenario never hit the hidden limit"
        assert provider.trials >= 1, "a bare 400 on a long request was not tested for length"
        assert provider.summaries >= 1, "length means the history is compacted"
        info = core.providers.window_info("local", session.model)
        assert info.upper is not None and info.window < info.upper
    finally:
        await daemon.stop()


async def test_a_bare_400_that_is_not_length_leaves_the_history_alone(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core

        class _Broken(_Hidden):
            async def stream(self, messages: Any, tools: Any, max_tokens: int = 0, **_: Any) -> Any:
                self.refusals += 1
                if max_tokens == 1:
                    self.trials += 1
                raise ProviderError(
                    "invalid_request",
                    'HTTP 400: {"error":{"message":"invalid request to model"}}',
                )
                yield  # pragma: no cover

        provider = _Broken()
        core.providers.get = lambda _p, _m: provider
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        session.provider = "local"
        session.history.append(ChatMessage(role="user", content="x" * 400_000))
        before = len(session.history.messages)
        agent_loop.start_turn(core, session, "hello")
        await asyncio.wait_for(session.turn_task, timeout=120)
        assert provider.summaries == 0
        assert len(session.history.messages) >= before  # nothing was summarised away
        assert core.providers.window_info("local", session.model).upper is None
    finally:
        await daemon.stop()


async def test_a_summary_request_refused_for_length_is_retried_smaller(tmp_path: Any) -> None:
    from snowpea_core.session import compaction

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        asked: list[int] = []

        class _Summariser:
            vendor = "hidden"

            async def stream(self, messages: Any, tools: Any, **_: Any) -> Any:
                size = sum(len(message_text(m)) for m in messages)
                asked.append(size)
                if len(asked) == 1:
                    raise ProviderError(
                        "invalid_request",
                        'HTTP 400: {"error":{"message":"invalid request to model"}}',
                    )
                yield StreamEvent(kind="text_delta", text="a real summary")
                yield StreamEvent(kind="done", stop_reason="end_turn")

        core.providers.get = lambda _p, _m: _Summariser()
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        session.provider = "local"
        session.context_window = 300_000
        head = [
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content="word " * 20_000)
            for i in range(8)
        ]
        text = await compaction.summarise(core, session, head, None)
        assert text == "a real summary"
        assert len(asked) == 2 and asked[1] < asked[0] * 0.75
    finally:
        await daemon.stop()


def test_the_summary_budget_follows_the_cost_this_conversation_really_has() -> None:
    from snowpea_core.session.compaction import summary_budget

    plain = SimpleNamespace(context_window=16_384, context_ratio=1.0)
    dense = SimpleNamespace(context_window=16_384, context_ratio=2.0)
    assert summary_budget(dense) < summary_budget(plain)
    # Korean text at 2 tokens per estimated token: the request stays inside the window.
    assert summary_budget(dense) / 1.25 <= 16_384 - 2_048
