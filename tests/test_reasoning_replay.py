"""A turn's thinking goes back to a vendor that reads it, for that turn only.

Qwen3.8's chat template renders ``reasoning_content`` on every assistant
message it is given, and DeepSeek/Kimi thinking-with-tools want the turn's
reasoning back.  Without it each tool round started its thinking from scratch.
The field is generic: ``ChatMessage.reasoning`` holds it, the preset names the
wire field (``reasoning_replay``), and an adapter whose vendor takes none
(Anthropic, Gemini, OpenAI…) never keeps it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent.loop import run_turn
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.normalize import build_openai_request, messages_to_openai
from snowpea_core.providers.presets import (
    LOCAL_VARIANTS,
    PRESETS,
    resolve_reasoning_replay,
    synthesize_local_preset,
)
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.history import History, estimate_message, message_to_json

CALL = ToolCall(id="c1", name="shell", arguments={"command": "ls"})


def _assistant(reasoning: str | None) -> ChatMessage:
    return ChatMessage(role="assistant", content="", tool_calls=[CALL], reasoning=reasoning)


# ---------------------------------------------------------------------------
# which vendors read it
# ---------------------------------------------------------------------------


def test_self_hosted_servers_and_thinking_apis_read_reasoning_content() -> None:
    readers = (PRESETS["local"], *LOCAL_VARIANTS.values(), PRESETS["deepseek"], PRESETS["kimi"])
    for preset in readers:
        assert preset.reasoning_replay == "reasoning_content", preset.id
    named = synthesize_local_preset("hon2", {"preset": "local", "base_url": "http://hon2:8001/v1"})
    assert named.reasoning_replay == "reasoning_content"


def test_vendors_that_take_no_such_field_are_never_sent_one() -> None:
    for vendor in ("openai", "anthropic", "gemini", "openrouter", "xai", "mistral"):
        if vendor in PRESETS:
            assert PRESETS[vendor].reasoning_replay is None, vendor


def test_config_turns_it_off_or_on() -> None:
    local = PRESETS["local"]
    assert resolve_reasoning_replay(local, vendor_config={"reasoningReplay": False}) is None
    assert resolve_reasoning_replay(local, profile={"reasoningReplay": False}) is None
    assert resolve_reasoning_replay(local, vendor_config={}) == "reasoning_content"
    openai = PRESETS["openai"]
    assert resolve_reasoning_replay(openai) is None
    assert resolve_reasoning_replay(openai, vendor_config={"reasoningReplay": True}) == (
        "reasoning_content"
    )


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------


def test_reasoning_rides_on_the_assistant_message_when_the_vendor_reads_it() -> None:
    messages = [ChatMessage(role="user", content="go"), _assistant("check the dir first")]
    [_, sent] = messages_to_openai(messages, reasoning_field="reasoning_content")
    assert sent["reasoning_content"] == "check the dir first"
    assert sent["tool_calls"][0]["function"]["name"] == "shell"
    [_, plain] = messages_to_openai(messages)
    assert "reasoning_content" not in plain


def test_the_request_uses_the_preset_s_field() -> None:
    messages = [ChatMessage(role="user", content="go"), _assistant("why")]
    local = build_openai_request(PRESETS["local"], "m", messages, [], max_tokens=64)
    assert local["messages"][1]["reasoning_content"] == "why"
    openai = build_openai_request(PRESETS["openai"], "gpt-x", messages, [], max_tokens=64)
    assert "reasoning_content" not in openai["messages"][1]


def test_an_empty_reasoning_sends_no_field() -> None:
    body = build_openai_request(
        PRESETS["local"], "m", [ChatMessage(role="user", content="go"), _assistant(None)], [],
        max_tokens=64,
    )
    assert "reasoning_content" not in body["messages"][1]


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_reasoning_is_counted_never_persisted_and_forgotten_by_a_new_turn() -> None:
    thinking = "x" * 4000
    message = _assistant(thinking)
    assert estimate_message(message) > estimate_message(_assistant(None)) + 500
    assert "reasoning" not in message_to_json(message)
    assert thinking not in str(message_to_json(message))
    history = History(messages=[ChatMessage(role="user", content="go"), message])
    history.forget_reasoning()
    assert history.messages[1].reasoning is None


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


class RecordingFake(FakeProvider):
    """The scripted fake, declaring replay and keeping every request."""

    def __init__(self, script: dict[str, Any], *, replays: bool) -> None:
        super().__init__(script)
        self.replays_reasoning = replays
        self.requests: list[list[ChatMessage]] = []

    def stream(self, messages: list[ChatMessage], tools: list[Any], **kwargs: Any):  # type: ignore[override]
        self.requests.append([ChatMessage(**vars(m)) for m in messages])
        return super().stream(messages, tools, **kwargs)


SCRIPT = {
    "steps": [
        {
            "match": "list it",
            "reasoning": "look at the files before answering",
            "tool_calls": [{"name": "shell", "arguments": {"command": "true"}}],
            "text": "",
        },
        {"after_tool": "shell", "reasoning": "that is enough", "text": "all done"},
        {"match": "again", "text": "ok"},
    ],
    "default": {"text": ""},
}


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


async def _run(daemon: Daemon, tmp_path: Path, *, replays: bool) -> tuple[RecordingFake, Any]:
    core = daemon.core
    assert core is not None
    workdir = tmp_path / "project"
    workdir.mkdir(parents=True, exist_ok=True)
    fake = RecordingFake(SCRIPT, replays=replays)
    core.providers.register("local", fake)
    session = await core.sessions.create(workdir, mode="auto", provider="local")
    await run_turn(core, session, "list it")
    return fake, session


def _tool_call_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.role == "assistant" and m.tool_calls]


async def test_the_next_round_sees_the_thinking_behind_the_call(
    daemon: Daemon, tmp_path: Path
) -> None:
    fake, session = await _run(daemon, tmp_path, replays=True)
    [second] = [r for r in fake.requests if _tool_call_messages(r)][:1]
    [call] = _tool_call_messages(second)
    assert call.reasoning == "look at the files before answering"

    # It stays for later turns — dropping it would rewrite a cached message —
    # until a compaction rewrites the conversation anyway.
    await run_turn(daemon.core, session, "again")
    [old] = _tool_call_messages(fake.requests[-1])
    assert old.reasoning == "look at the files before answering"
    session.history.replace(session.history.snapshot())
    assert all(m.reasoning is None for m in session.history.snapshot())


async def test_an_adapter_that_does_not_replay_keeps_nothing(
    daemon: Daemon, tmp_path: Path
) -> None:
    fake, session = await _run(daemon, tmp_path, replays=False)
    assert all(m.reasoning is None for r in fake.requests for m in r)
    assert all(m.reasoning is None for m in session.history.snapshot())


async def test_the_timing_line_estimates_unreported_reasoning(
    daemon: Daemon, tmp_path: Path, caplog: Any
) -> None:
    # vLLM folds reasoning into completion_tokens: the line said reasoning=0.
    import logging

    with caplog.at_level(logging.INFO, logger="snowpea.llm"):
        await _run(daemon, tmp_path, replays=True)
    lines = [r.getMessage() for r in caplog.records if r.name == "snowpea.llm"]
    assert lines and "reasoning=0 " not in lines[0], lines[0]
