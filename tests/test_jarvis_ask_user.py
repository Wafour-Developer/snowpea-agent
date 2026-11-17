"""The Jarvis session's own ``ask_user`` reaches the person (owner report 2026-11-17).

The owner saw Jarvis's ``ask_user`` card running with no dialog to answer it.
The daemon side is pinned here on a real daemon with a scripted provider: the
question is routed to the person, the client that opened the Jarvis session
gets ``question.request``, every other client ``question.pending``, the
question stays listed (``question.list``) for a window that comes up later,
and an answer from a client that is not the origin finishes the tool call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiohttp
from _support import connect, make_daemon

from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text

TIMEOUT = 15.0
QUESTION = "Delete all 38 projects or keep one?"


class Brain:
    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.tool_results: list[str] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        said = [m for m in messages if m.role != "system"]
        last = said[-1]
        text = message_text(last)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        if last.role == "user" and "ASK" in text:
            yield StreamEvent(kind="text_delta", text="I counted 38 projects.")
            yield StreamEvent(kind="tool_call", tool_call=ToolCall(
                id="ask1", name="ask_user",
                arguments={"questions": [{"question": QUESTION, "header": "Projects",
                                          "options": [{"label": "Delete all"},
                                                      {"label": "Keep one"}]}]}))
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        if last.role == "tool":
            self.tool_results.append(text)
        yield StreamEvent(kind="text_delta", text="Done." if last.role == "tool" else "noted")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _wait(predicate: Any, what: str) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} not reached")


async def test_jarvis_ask_user_reaches_every_person_surface_and_an_answer_finishes_it(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home", {"jarvis": {"folder": str(tmp_path / "J")}})
    brain = Brain()
    daemon.core.providers.get = lambda _p, _m: brain
    opener = await connect(http, daemon, question_answer=None)  # opened Jarvis, never answers
    other = await connect(http, daemon, question_answer=None)  # e.g. a second window
    try:
        await opener.ok("jarvis.enable", {})
        jarvis = daemon.core.jarvis.session()
        assert jarvis is not None
        await opener.ok("session.resume", {"sessionId": jarvis.id, "afterSeq": 0})
        await opener.ok("session.prompt", {"sessionId": jarvis.id, "text": "ASK please"})
        await _wait(lambda: opener.questions, "question.request to the opener")

        asked = opener.questions[0]
        assert asked["sessionId"] == jarvis.id
        assert asked["route"] == "person"
        pending = await other.wait_notification("question.pending")
        assert pending["request"]["requestId"] == asked["requestId"]
        assert pending["request"]["route"] == "person"
        # A window that comes up later still finds it.
        listed = await other.ok("question.list", {})
        assert [r["requestId"] for r in listed["requests"]] == [asked["requestId"]]

        # The second window answers; the tool call finishes with that answer.
        await other.ok("question.respond", {"requestId": asked["requestId"],
                                           "answers": [{"selected": ["Keep one"]}]})
        await _wait(lambda: brain.tool_results, "the ask_user result")
        assert "Keep one" in brain.tool_results[0]
        resolved = await other.wait_notification("question.resolved")
        assert resolved["requestId"] == asked["requestId"]
        assert (await other.ok("question.list", {}))["requests"] == []
    finally:
        await opener.stop()
        await other.stop()
        await daemon.stop()


def test_jarvis_prompt_asks_for_an_explanation_when_it_leaves_a_decision() -> None:
    from snowpea_core.jarvis import tools

    prompt = next(value for name, value in vars(tools).items()
                  if isinstance(value, str) and "How to work" in value and "Jarvis" in value)
    assert "When you leave a decision to the person" in prompt
    assert "what you recommend" in prompt
