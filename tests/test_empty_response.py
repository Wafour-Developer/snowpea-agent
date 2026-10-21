"""An answer with no text and no tool call is not a completion.

A reasoning model cut off mid-thought (reasoning only, out=171 of which 170
reasoning) used to end the turn ``complete`` with an empty message.  It is
asked once more with a short nudge appended — the history before it is left
as sent, so the server's prefix cache still matches — and a second empty
answer ends the turn ``empty_response`` with an error a surface can show.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.server.app_server import Daemon

THOUGHT = {"reasoning": "The user wants a plan. Project", "reasoningTokens": 170}


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    with env_vars(SNOWPEA_PROVIDER=None):
        instance = Daemon(port=0, home=home)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.stop()


async def _session(daemon: Daemon, tmp_path: Path, script: dict[str, Any]) -> Any:
    core = daemon.core
    assert core is not None
    core.providers.register("local", FakeProvider(script))
    work = tmp_path / "w"
    work.mkdir()
    return await core.sessions.create(work, mode="auto", provider="local")


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append((params["kind"], params["payload"]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


async def test_an_empty_answer_is_nudged_once_and_the_prefix_is_kept(
    daemon: Daemon, tmp_path: Path
) -> None:
    script = {
        "steps": [
            {"match": agent_loop.EMPTY_NUDGE, "text": "Here is the plan."},
            {"match": "plan my trip", **THOUGHT},
        ]
    }
    session = await _session(daemon, tmp_path, script)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    reason = await agent_loop.run_turn(daemon.core, session, "plan my trip")
    turn_id = session.turn_origins and next(reversed(session.turn_origins))
    assert turn_id
    roles = [(m.role, m.content) for m in session.history.messages]
    # Appended after the prompt, nothing rewritten, and no empty assistant turn.
    assert roles[-3:] == [
        ("user", "plan my trip"),
        ("user", agent_loop.EMPTY_NUDGE),
        ("assistant", "Here is the plan."),
    ]
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert [p["text"] for p in sink.of("message.done")] == ["Here is the plan."]
    assert reason


async def test_a_second_empty_answer_ends_the_turn_empty_response(
    daemon: Daemon, tmp_path: Path
) -> None:
    session = await _session(daemon, tmp_path, {"steps": [], "default": THOUGHT})
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, "plan my trip")
    assert [p["reason"] for p in sink.of("turn.done")] == ["empty_response"]
    assert [p["code"] for p in sink.of("error")] == ["empty_response"]
    assert sink.of("message.done") == []
    assert [m.content for m in session.history.messages if m.role == "assistant"] == []


class _OverflowOnce:
    """Refuses the first call for its length (as vLLM does), then answers."""

    vendor = "local"
    model = "qwen38"
    supports_thinking_option = True
    preset = None

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[Any], tools: list[Any], **_: Any) -> Any:
        from snowpea_core.providers.base import ProviderError, StreamEvent, Usage

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


async def test_a_context_overflow_is_summarised_and_retried_once(
    daemon: Daemon, tmp_path: Path, monkeypatch: Any
) -> None:
    """HTTP 400 "maximum context length": the turn summarises and tries again
    instead of failing, and a long session goes on without the person's help."""
    from snowpea_core.providers.base import ChatMessage

    provider = _OverflowOnce()
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(core.providers, "get", lambda *a, **k: provider)
    work = tmp_path / "w"
    work.mkdir()
    session = await core.sessions.create(work, mode="auto", provider="local")
    for n in range(30):
        session.history.append(ChatMessage(role="user", content=f"question {n}"))
        session.history.append(ChatMessage(role="assistant", content=f"reply {n}"))
    sink = _Events()
    core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(core, session, "one more")
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sink.of("compaction") and provider.calls >= 3  # refused, summary, answer
    assert sink.of("message.done")[-1]["text"].startswith("answer")


def test_the_summary_request_fits_the_window_it_summarises_for() -> None:
    """A history that overflowed the model must not overflow the summary call:
    tool output is clipped and the middle gives way to a marker."""
    from types import SimpleNamespace

    from snowpea_core.providers.base import ChatMessage
    from snowpea_core.session.compaction import render_conversation, summary_budget

    messages = [ChatMessage(role="user", content="build the login page")]
    for n in range(200):
        messages.append(ChatMessage(role="tool", name="browser_snapshot", content="x" * 50_000))
        messages.append(ChatMessage(role="assistant", content=f"step {n} done"))
    budget = summary_budget(SimpleNamespace(context_window=60_000))
    text = render_conversation(messages, budget=budget)
    assert len(text) <= budget + 200
    assert text.startswith("USER: build the login page")
    assert "step 199 done" in text and "characters of the middle omitted" in text
    assert "chars cut]" in text
