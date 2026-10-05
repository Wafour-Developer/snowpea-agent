"""A transient model-server failure is retried; a refused request is not."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent

TIMEOUT = 20.0


def test_transient_errors_are_recognised() -> None:
    transient = [
        "snowpeallm (qwen38-flash-next): ReadTimeout: ",
        "openai: HTTP 503: service unavailable",
        "x: HTTP 429: rate limit reached",
        "x: ConnectError: [Errno 111] Connection refused",
        "x: RemoteProtocolError: Server disconnected without sending a response.",
    ]
    for text in transient:
        assert agent_loop.transient_provider_error(ProviderError("internal", text)), text
    for text in ["x: HTTP 400: invalid tool schema", "x: HTTP 401: bad key"]:
        assert not agent_loop.transient_provider_error(ProviderError("internal", text)), text


class _FlakyProvider:
    vendor = "test-flaky"

    def __init__(self, failures: list[str]) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.failures:
            raise ProviderError("internal", self.failures.pop(0))
        yield StreamEvent(kind="text_delta", text="answered")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: list[str]) -> Any:
    monkeypatch.setattr(agent_loop, "PROVIDER_RETRY_BASE_SEC", 0.001)
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    assert core is not None
    provider = _FlakyProvider(failures)
    core.providers.get = lambda _p, _m: provider  # type: ignore[assignment]
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    agent_loop.start_turn(core, session, "hello")
    assert session.turn_task is not None
    await asyncio.wait_for(session.turn_task, timeout=TIMEOUT)
    return daemon, provider, recorder


async def test_a_read_timeout_is_retried_and_the_turn_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, provider, recorder = await _run(
        tmp_path, monkeypatch, ["snowpeallm (qwen): ReadTimeout: ", "x: HTTP 503: busy"]
    )
    try:
        assert provider.calls == 3
        done = recorder.of_kind("turn.done")[-1]["payload"]
        assert done["reason"] == "complete"
        notes = [e["payload"]["reason"] for e in recorder.of_kind("hook.continue")]
        assert any("retrying 1/3" in n for n in notes)
    finally:
        await daemon.stop()


async def test_a_refused_request_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, provider, recorder = await _run(
        tmp_path, monkeypatch, ["x: HTTP 400: invalid tool schema"]
    )
    try:
        assert provider.calls == 1
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "error"
    finally:
        await daemon.stop()
