"""`complete_text` asks again when a model answers with nothing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent.definition import EMPTY_REPLY_RETRIES, complete_text
from snowpea_core.providers.base import ChatMessage


class ScriptedProvider:
    """Streams one scripted reply per call; an empty string is a silent turn."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def stream(
        self, messages: list[ChatMessage], tools: list[Any], *, max_tokens: int
    ) -> AsyncIterator[Any]:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        # Reasoning arrives, the answer does not: what the local Qwen really does.
        yield SimpleNamespace(kind="reasoning_delta", text="thinking…")
        for start in range(0, len(reply), 8):
            yield SimpleNamespace(kind="text_delta", text=reply[start : start + 8])


MESSAGES = [ChatMessage(role="user", content="plan it")]


@pytest.mark.asyncio
async def test_an_empty_reply_is_asked_for_again() -> None:
    provider = ScriptedProvider(["", '{"tasks": []}'])
    assert await complete_text(provider, MESSAGES) == '{"tasks": []}'
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_whitespace_reply_counts_as_empty() -> None:
    provider = ScriptedProvider(["  \n", "ok"])
    assert await complete_text(provider, MESSAGES) == "ok"


@pytest.mark.asyncio
async def test_a_real_reply_is_never_repeated() -> None:
    provider = ScriptedProvider(["first", "second"])
    assert await complete_text(provider, MESSAGES) == "first"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_it_gives_up_after_the_retries() -> None:
    provider = ScriptedProvider([""])
    assert await complete_text(provider, MESSAGES) == ""
    assert provider.calls == 1 + EMPTY_REPLY_RETRIES
