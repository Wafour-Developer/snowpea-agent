"""R4 (1.37.0): the model server's silence and retries are visible, and bounded on request."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent

TIMEOUT = 20.0


class _Stalling:
    """Stalls before its first event (and, optionally, between events)."""

    vendor = "test-stall"

    def __init__(self, stalls: list[float], mid_stall: float = 0.0) -> None:
        self.stalls = list(stalls)
        self.mid_stall = mid_stall
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(self.stalls.pop(0) if self.stalls else 0)
        yield StreamEvent(kind="text_delta", text="one")
        if self.mid_stall:
            await asyncio.sleep(self.mid_stall)
        yield StreamEvent(kind="text_delta", text=" two")
        yield StreamEvent(kind="done", stop_reason="end_turn")


class _Failing:
    vendor = "test-fail"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("internal", "snowpeallm (qwen): ReadTimeout: ")
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    **limits: float,
) -> tuple[Any, Recorder]:
    monkeypatch.setattr(agent_loop, "PROVIDER_RETRY_BASE_SEC", 0.001)
    monkeypatch.setattr(agent_loop, "PROVIDER_WAIT_TICK", 0.02)
    monkeypatch.setattr(agent_loop, "PROVIDER_WAIT_NOTICE_AFTER", 0.1)
    monkeypatch.setattr(agent_loop, "PROVIDER_WAIT_NOTICE_EVERY", 0.1)
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    assert core is not None
    for name, value in limits.items():
        setattr(core.settings.agent, name, value)
    core.providers.get = lambda _p, _m: provider  # type: ignore[assignment]
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    agent_loop.start_turn(core, session, "hello")
    assert session.turn_task is not None
    await asyncio.wait_for(session.turn_task, timeout=TIMEOUT)
    return daemon, recorder


def test_the_failure_is_named_by_its_class() -> None:
    def named(text: str, code: str = "internal") -> str:
        return agent_loop._error_class(ProviderError(code, text))

    assert named("snowpeallm: ReadTimeout: ") == "ReadTimeout"
    assert named("openai: HTTP 503: busy") == "HTTP 503"
    assert named("no first token for 9s (timed out)", "timeout") == "timeout"


async def test_a_retry_says_which_attempt_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, recorder = await _run(tmp_path, monkeypatch, _Failing())
    try:
        (retry,) = [e["payload"] for e in recorder.of_kind("provider.retry")]
        assert (retry["attempt"], retry["max"], retry["error"]) == (1, 3, "ReadTimeout")
        assert retry["delaySec"] > 0
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
    finally:
        await daemon.stop()


async def test_a_silent_server_is_reported_while_it_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, recorder = await _run(tmp_path, monkeypatch, _Stalling([0.45]))
    try:
        waiting = [e["payload"] for e in recorder.of_kind("provider.waiting")]
        assert len(waiting) >= 2
        assert {w["phase"] for w in waiting} == {"first_token"}
        assert waiting[0]["sinceSec"] <= waiting[-1]["sinceSec"]
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
        assert not recorder.of_kind("provider.retry")  # no limit set: it was only waited for
    finally:
        await daemon.stop()


async def test_a_first_token_limit_turns_a_stall_into_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Stalling([5.0, 0.0])
    daemon, recorder = await _run(tmp_path, monkeypatch, provider, provider_first_token_sec=0.2)
    try:
        assert provider.calls == 2
        (retry,) = [e["payload"] for e in recorder.of_kind("provider.retry")]
        assert retry["error"] == "timeout"
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
    finally:
        await daemon.stop()


async def test_an_idle_limit_applies_between_events_not_before_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Stalling([0.3, 0.0], mid_stall=5.0)
    daemon, recorder = await _run(tmp_path, monkeypatch, provider, provider_idle_sec=0.2)
    try:
        # The 0.3 s before the first event is not "idle"; the stall after it is.
        waiting = [e["payload"]["phase"] for e in recorder.of_kind("provider.waiting")]
        assert "first_token" in waiting
        assert provider.calls >= 2
        assert recorder.of_kind("provider.retry")
    finally:
        await daemon.stop()
