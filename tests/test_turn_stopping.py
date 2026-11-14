"""``turn.stopping`` (M16 P1.5): who may keep a turn going when the model stops."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent import turn_stopping as ts
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.session.history import message_text

_SESSION = SimpleNamespace(interrupt=SimpleNamespace(is_set=lambda: False))
_CTX = ts.StopContext("done")


@pytest.mark.asyncio
async def test_the_first_decision_in_order_wins() -> None:
    chain = ts.TurnStoppingChain()
    chain.register("late", lambda c, s, x: ts.StopDecision("late", "late"), order=200)
    chain.register("early", lambda c, s, x: ts.StopDecision("early", "early"), order=100)
    decision = await chain.run(None, _SESSION, _CTX)  # type: ignore[arg-type]
    assert decision is not None and decision.text == "early"


@pytest.mark.asyncio
async def test_none_and_broken_listeners_let_the_turn_end() -> None:
    chain = ts.TurnStoppingChain()

    def boom(*_: Any) -> None:
        raise RuntimeError("no")

    chain.register("boom", boom)
    chain.register("quiet", lambda c, s, x: None)
    assert await chain.run(None, _SESSION, _CTX) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_never_kept_going() -> None:
    chain = ts.TurnStoppingChain()
    chain.register("always", lambda c, s, x: ts.StopDecision("again", "x"))
    stopped = SimpleNamespace(interrupt=SimpleNamespace(is_set=lambda: True))
    assert await chain.run(None, stopped, _CTX) is None  # type: ignore[arg-type]


def test_the_built_ins_are_verify_then_stop_hooks() -> None:
    assert [entry.name for entry in ts.default_chain().listeners()] == ["verify-gate", "stop-hooks"]


class _TwoAnswers:
    vendor = "fake"

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=50, output_tokens=3))
        yield StreamEvent(kind="text_delta", text=f"answer {len(self.requests)}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_a_plugin_listener_keeps_a_real_turn_going_once(tmp_path: Any) -> None:
    asked: list[str] = []

    def once(core: Any, session: Any, ctx: ts.StopContext) -> ts.StopDecision | None:
        if asked:
            return None
        asked.append(ctx.last_message)
        return ts.StopDecision("Check the changelog before you finish.", "plugin:changelog")

    dispose = ts.CHAIN.register("plugin:changelog", once, order=150)
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        provider = _TwoAnswers()
        core.providers.get = lambda _p, _m: provider
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        agent_loop.start_turn(core, session, "write the release notes")
        await asyncio.wait_for(session.turn_task, timeout=60)
        assert asked == ["answer 1"]
        assert len(provider.requests) == 2
        steer = provider.requests[1][-1]
        assert "Check the changelog" in message_text(steer)
        stored = next(m for m in session.history.messages if "changelog" in message_text(m))
        assert stored.source == {"producer": "plugin:changelog", "form": "notice"}
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
    finally:
        dispose()
        await daemon.stop()
