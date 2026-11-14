"""The tool-result contract (M16 P1.4): post-execute listeners, contexts, ending a turn."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text
from snowpea_core.tools import result_hooks
from snowpea_core.tools.registry import Tool, ToolResult
from snowpea_core.tools.result_hooks import PostDecision, PostExecuteChain

# ---- the chain ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_can_replace_the_text_and_add_context() -> None:
    chain = PostExecuteChain()
    chain.register("a", lambda ctx, name, args, r: PostDecision.accept("short", "note one"))
    out = await chain.run(None, "t", {}, ToolResult(ok=True, output="long"))  # type: ignore[arg-type]
    assert (out.ok, out.output, out.contexts) == (True, "short", ["note one"])


@pytest.mark.asyncio
async def test_block_turns_the_call_into_feedback_and_stops_the_chain() -> None:
    chain = PostExecuteChain()
    seen: list[str] = []
    chain.register("guard", lambda c, n, a, r: PostDecision.block("Use patch, not sed."), order=1)
    chain.register("later", lambda c, n, a, r: seen.append("later"), order=2)  # type: ignore[arg-type,func-returns-value]
    out = await chain.run(None, "shell", {}, ToolResult(ok=True, output="done"))  # type: ignore[arg-type]
    assert (out.ok, out.error) == (False, "Use patch, not sed.")
    assert seen == []


@pytest.mark.asyncio
async def test_a_broken_listener_is_skipped_and_a_disposed_one_is_gone() -> None:
    chain = PostExecuteChain()

    def boom(*_: Any) -> None:
        raise RuntimeError("no")

    async def tag(ctx: Any, name: str, args: dict, r: ToolResult) -> PostDecision:
        return PostDecision.accept(r.output + "!")

    chain.register("boom", boom)
    dispose = chain.register("tag", tag)
    out = await chain.run(None, "t", {}, ToolResult(ok=True, output="x"))  # type: ignore[arg-type]
    assert out.output == "x!"
    dispose()
    out = await chain.run(None, "t", {}, ToolResult(ok=True, output="x"))  # type: ignore[arg-type]
    assert out.output == "x"


# ---- through a real turn -------------------------------------------------------


class _Scripted:
    """Calls ``probe`` once, then answers; records what each request carried."""

    vendor = "fake"

    def __init__(self, tool: str = "probe") -> None:
        self.tool = tool
        self.requests: list[list[ChatMessage]] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=5))
        if len(self.requests) == 1:
            call = ToolCall(id="c1", name=self.tool, arguments={})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="all done")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _turn(tmp_path: Any, result: ToolResult, provider: _Scripted) -> tuple[Any, Recorder]:
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    core.providers.get = lambda _p, _m: provider

    async def run(ctx: Any, args: dict[str, Any]) -> ToolResult:
        return result

    core.tools.register(
        Tool(
            name="probe",
            category="test",
            description="probe",
            input_schema={"type": "object", "properties": {}},
            permission="read",
            run=run,
        )
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    agent_loop.start_turn(core, session, "go")
    await asyncio.wait_for(session.turn_task, timeout=60)
    return daemon, recorder


async def test_a_results_context_reaches_the_next_call_after_the_result(tmp_path: Any) -> None:
    provider = _Scripted()
    daemon, _ = await _turn(
        tmp_path, ToolResult(ok=True, output="42", contexts=["The cache is stale."]), provider
    )
    try:
        second = provider.requests[1]
        tool_at = next(i for i, m in enumerate(second) if m.role == "tool")
        note_at = next(
            i for i, m in enumerate(second) if "The cache is stale." in message_text(m)
        )
        assert note_at > tool_at
        assert str(second[tool_at].content).startswith("42")
        assert "The cache is stale." not in str(second[tool_at].content)
    finally:
        await daemon.stop()


async def test_a_result_that_concludes_the_turn_ends_it_without_another_call(
    tmp_path: Any,
) -> None:
    provider = _Scripted()
    daemon, recorder = await _turn(
        tmp_path, ToolResult(ok=True, output="saved", concludes_turn=True), provider
    )
    try:
        assert len(provider.requests) == 1
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
    finally:
        await daemon.stop()


async def test_a_blocking_listener_changes_what_the_model_reads(tmp_path: Any) -> None:
    dispose = result_hooks.POST_EXECUTE.register(
        "test-guard",
        lambda c, name, a, r: PostDecision.block("not like that") if name == "probe" else None,
    )
    provider = _Scripted()
    try:
        daemon, _ = await _turn(tmp_path, ToolResult(ok=True, output="raw"), provider)
        try:
            tool = next(m for m in provider.requests[1] if m.role == "tool")
            assert "not like that" in message_text(tool) and "raw" not in message_text(tool)
        finally:
            await daemon.stop()
    finally:
        dispose()


async def test_any_tools_huge_output_keeps_a_head_and_a_tail(tmp_path: Any) -> None:
    provider = _Scripted()
    huge = "A" * 150_000 + "TAIL-MARKER"
    daemon, _ = await _turn(tmp_path, ToolResult(ok=True, output=huge), provider)
    try:
        tool = next(m for m in provider.requests[1] if m.role == "tool")
        text = str(tool.content)
        assert len(text) < 110_000 and "TAIL-MARKER" in text[-200:]
        assert "tool-output" in text  # the pointer to the whole text on disk
    finally:
        await daemon.stop()
