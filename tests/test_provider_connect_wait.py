"""A model call that never reached the server waits for the network to come back."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent

TIMEOUT = 20.0
DNS = "snowpeallm (qwen): ConnectError: [Errno 8] nodename nor servname provided, or not known"


def _connect_error(text: str = DNS) -> ProviderError:
    try:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    except httpx.ConnectError as cause:
        try:
            raise ProviderError("internal", text) from cause
        except ProviderError as exc:
            return exc


class _Scripted:
    """Raises the scripted errors in turn, then answers."""

    vendor = "test-scripted"

    def __init__(self, failures: list[ProviderError | None], forever: bool = False) -> None:
        self.failures = list(failures)
        self.forever = forever
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        elif self.forever:
            raise _connect_error()
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    base: float = 0.001,
    **limits: float,
) -> tuple[Any, Any, Recorder]:
    monkeypatch.setattr(agent_loop, "PROVIDER_RETRY_BASE_SEC", base)
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
    return daemon, session, recorder


async def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Any, **limits: float
) -> tuple[Any, Recorder]:
    daemon, session, recorder = await _start(tmp_path, monkeypatch, provider, **limits)
    await asyncio.wait_for(session.turn_task, timeout=TIMEOUT)
    return daemon, recorder


def _reason(recorder: Recorder) -> str:
    return str(recorder.of_kind("turn.done")[-1]["payload"]["reason"])


def _errors(recorder: Recorder) -> str:
    return "\n".join(str(e["payload"].get("message", "")) for e in recorder.of_kind("error"))


def test_connection_failures_are_told_apart() -> None:
    assert agent_loop.connection_failure(_connect_error())
    by_text = [
        "openai: ConnectError: [Errno -2] Name or service not known",
        "x: ConnectTimeout: ",
        "x: APIConnectionError: Temporary failure in name resolution",
        "x: getaddrinfo failed",
        "x: [Errno 101] Network is unreachable",
        "x: [Errno 113] No route to host",
        "x: [Errno 111] Connection refused",
    ]
    for text in by_text:
        assert agent_loop.connection_failure(ProviderError("internal", text)), text
    for text in ["x: ReadTimeout: ", "x: HTTP 503: busy", "x: RemoteProtocolError: closed"]:
        assert not agent_loop.connection_failure(ProviderError("internal", text)), text
    assert not agent_loop.connection_failure(ValueError(DNS))


async def test_more_connect_failures_than_retries_still_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([_connect_error() for _ in range(6)])
    daemon, recorder = await _run(tmp_path, monkeypatch, provider)
    try:
        assert provider.calls == 7
        assert _reason(recorder) == "complete"
        retries = [e["payload"] for e in recorder.of_kind("provider.retry")]
        assert len(retries) == 6 and {r["error"] for r in retries} == {"ConnectError"}
        notices = [e["payload"]["reason"] for e in recorder.of_kind("hook.continue")]
        assert any("unreachable" in n and "for up to 5 min" in n for n in notices), notices
    finally:
        await daemon.stop()


async def test_the_connect_budget_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([], forever=True)
    daemon, recorder = await _run(
        tmp_path, monkeypatch, provider, provider_connect_wait_sec=0.3
    )
    try:
        assert _reason(recorder) == "error"
        assert provider.calls > 4, "more tries than provider_retries"
        assert "unreachable for 0 s" in _errors(recorder)
    finally:
        await daemon.stop()


def test_the_outage_is_named_in_minutes() -> None:
    assert agent_loop._duration(45) == "45 s"
    assert agent_loop._duration(300) == "5 min"


async def test_zero_wait_keeps_the_old_retry_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([], forever=True)
    daemon, recorder = await _run(
        tmp_path, monkeypatch, provider, provider_connect_wait_sec=0
    )
    try:
        assert _reason(recorder) == "error"
        assert provider.calls == 4
    finally:
        await daemon.stop()


async def test_other_transient_errors_keep_provider_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy = [ProviderError("internal", "x: HTTP 503: busy") for _ in range(10)]
    provider = _Scripted(busy)
    daemon, recorder = await _run(tmp_path, monkeypatch, provider)
    try:
        assert _reason(recorder) == "error"
        assert provider.calls == 4
    finally:
        await daemon.stop()


async def test_a_read_timeout_then_dns_failures_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The log from the Mac that idle-slept: a ReadTimeout, then the network
    # was gone for a while, then it came back.
    read_timeout = ProviderError("internal", "snowpeallm (qwen): ReadTimeout: ")
    busy = ProviderError("internal", "snowpeallm (qwen): HTTP 503: busy")
    provider = _Scripted([read_timeout] + [_connect_error() for _ in range(5)] + [busy])
    # A step budget already used up by the time the network is back: the
    # connect failures do not count against it.  Every failed call "takes"
    # two minutes.
    real = agent_loop.monotonic
    clock = {"skew": 0.0}
    inner = provider.stream

    async def stream(*args: Any, **kwargs: Any) -> Any:
        try:
            async for event in inner(*args, **kwargs):
                yield event
        except ProviderError:
            clock["skew"] += 120.0
            raise

    provider.stream = stream  # type: ignore[method-assign]
    monkeypatch.setattr(agent_loop, "monotonic", lambda: real() + clock["skew"])
    daemon, session, recorder = await _start(
        tmp_path,
        monkeypatch,
        provider,
        provider_connect_wait_sec=3600,
        provider_step_budget_sec=300,
    )
    try:
        await asyncio.wait_for(session.turn_task, timeout=TIMEOUT)
        # 4 min of server time against a 5 min budget; the 10 min of outage
        # in between is not counted.
        assert _reason(recorder) == "complete", _errors(recorder)
        assert provider.calls == 8
    finally:
        await daemon.stop()


async def test_stop_ends_the_wait_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Scripted([], forever=True)
    daemon, session, recorder = await _start(tmp_path, monkeypatch, provider, base=30.0)
    try:
        for _ in range(200):
            if recorder.of_kind("provider.retry"):
                break
            await asyncio.sleep(0.01)
        assert recorder.of_kind("provider.retry")
        session.interrupt.set()
        await asyncio.wait_for(session.turn_task, timeout=5.0)
        assert provider.calls == 1
        assert _reason(recorder) != "complete"
    finally:
        await daemon.stop()
