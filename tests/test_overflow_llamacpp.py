"""Context-overflow wordings of llama.cpp and vLLM, and a window too small to use.

A llama.cpp server with a 4,096-token window answered every request with a raw
HTTP 400 nobody recognised as an overflow; the turn just failed, every time.
Now its wording is an overflow, its numbers are learned, and a window that
cannot hold snowpea's own system prompt and tool schemas ends the turn once
with ``context_window_too_small`` naming both numbers, instead of retrying.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.session import Session

LLAMA_CPP = (
    'HTTP 400 {"error":{"code":400,"message":"request (31724 tokens) exceeds the available '
    'context size (4096 tokens), try increasing it","type":"exceed_context_size_error",'
    '"n_prompt_tokens":31724,"n_ctx":4096}}'
)
VLLM = "prompt (246410 tokens) + max tokens (16384) exceeds the context (262144)"


def test_llama_cpp_wording_is_an_overflow() -> None:
    assert agent_loop.context_overflow(ProviderError("provider_error", LLAMA_CPP))
    # Each phrase on its own is enough.
    assert agent_loop.context_overflow(
        ProviderError("x", "request exceeds the available context size")
    )
    assert agent_loop.context_overflow(ProviderError("x", '{"type":"exceed_context_size_error"}'))


def test_llama_cpp_numbers_come_from_the_body_fields() -> None:
    assert agent_loop.overflow_numbers(LLAMA_CPP) == (31724, 4096)


def test_vllm_prompt_and_window_are_both_parsed() -> None:
    assert agent_loop.overflow_numbers(VLLM) == (246410, 262144)


def _core(learned: list[tuple[str, int]]) -> Any:
    providers = SimpleNamespace(
        default_vendor=lambda: "local",
        learn_context_window=lambda vendor, model, window: learned.append((vendor, window)),
        note_prompt_refused=lambda *a, **k: None,
    )
    return SimpleNamespace(providers=providers)


def test_vllm_refusal_teaches_the_prompt_size_too(tmp_path: Path) -> None:
    learned: list[tuple[str, int]] = []
    session = Session(id="s-vllm", workdir=tmp_path)
    sent = [ChatMessage(role="user", content="y" * 4 * 100_000)]
    error = agent_loop._learn_overflow(  # type: ignore[arg-type]
        _core(learned), session, ProviderError("x", VLLM), sent
    )
    assert error is None
    assert learned == [("local", 262144)]
    # The server counted 246410 where we estimated ~100000: the ratio moved.
    assert session.context_ratio > 1.0


def test_a_window_below_the_fixed_parts_gives_the_plain_error(tmp_path: Path) -> None:
    learned: list[tuple[str, int]] = []
    session = Session(id="s-small", workdir=tmp_path)
    sent = [
        ChatMessage(role="system", content="s" * 4 * 6000),  # ~6000 tokens of prompt
        ChatMessage(role="user", content="hi"),
    ]
    error = agent_loop._learn_overflow(  # type: ignore[arg-type]
        _core(learned), session, ProviderError("x", LLAMA_CPP), sent
    )
    assert learned == [("local", 4096)]
    assert error is not None
    assert error.code == agent_loop.CONTEXT_WINDOW_TOO_SMALL == "context_window_too_small"
    details = error.details  # type: ignore[attr-defined]
    assert details["windowTokens"] == 4096
    assert details["neededTokens"] >= 6000
    assert "4096" in str(error) and str(details["neededTokens"]) in str(error)


def test_a_window_that_holds_the_fixed_parts_is_not_too_small() -> None:
    assert agent_loop.window_too_small(262144, 9000) is None
    assert agent_loop.window_too_small(0, 9000) is None


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


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append((params["kind"], params["payload"]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


class _TinyWindow:
    """A llama.cpp server with n_ctx 4096: refuses every request."""

    vendor = "local"
    model = "tiny"
    supports_thinking_option = True
    preset = None

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[Any], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        raise ProviderError("provider_error", LLAMA_CPP)
        yield  # pragma: no cover - an async generator that never yields


async def test_a_tiny_window_ends_the_turn_once_with_the_plain_error(
    daemon: Daemon, tmp_path: Path, monkeypatch: Any
) -> None:
    provider = _TinyWindow()
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(core.providers, "get", lambda *a, **k: provider)
    work = tmp_path / "w"
    work.mkdir()
    session = await core.sessions.create(work, mode="auto", provider="local")
    sink = _Events()
    core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(core, session, "hello")

    codes = [p["code"] for p in sink.of("error")]
    assert codes == ["context_window_too_small"]
    assert "4096" in sink.of("error")[0]["message"]
    assert [p["reason"] for p in sink.of("turn.done")] == ["error"]
    assert provider.calls == 1  # no retry loop
