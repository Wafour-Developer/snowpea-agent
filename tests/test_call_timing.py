"""Side-call routing and per-call timing lines (snowpea-browser).

* ``llm.sample`` routes with ``<session>:sample``, not the session's own
  ``X-Session-ID``: keyed alike it queued behind the main turn's prefill.
* Every model call logs ``llm call … ttft= decode= total= in= out=`` to
  daemon.log, so waiting for the server and decoding can be told apart.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from test_host_tools import Daemon, daemon  # noqa: F401
from test_llm_sample import StubProvider, host_session

from snowpea_core.providers import routing
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.providers.call_timing import CallTiming


class KeyedStub(StubProvider):
    """Remembers which routing key each call was made under."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.keys: list[str | None] = []

    async def stream(
        self, messages: list[ChatMessage], tools: list[Any], **kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        self.keys.append(routing.current_session())
        yield StreamEvent(kind="text_delta", text=self.text)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=30, output_tokens=4))
        yield StreamEvent(kind="done", stop_reason="end_turn")


def test_a_side_key_is_the_session_plus_its_purpose() -> None:
    assert routing.side_key("s-1", "sample") == "s-1:sample"
    with routing.for_session(routing.side_key("s-1", "sample")):
        assert routing.session_headers() == {routing.SESSION_HEADER: "s-1:sample"}


def test_a_timing_line_splits_waiting_from_decoding(caplog: pytest.LogCaptureFixture) -> None:
    timing = CallTiming("s-1", "turn", "local", "qwen38")
    timing.first()
    with caplog.at_level(logging.INFO, logger="snowpea.llm"):
        timing.done(input_tokens=1200, output_tokens=40, reasoning_tokens=30)
    [line] = [r.getMessage() for r in caplog.records if r.name == "snowpea.llm"]
    assert line.startswith("llm call session=s-1 kind=turn vendor=local model=qwen38 ttft=")
    for part in ("decode=", "total=", "in=1200", "out=40", "reasoning=30", "outcome=ok"):
        assert part in line
    assert "(no output)" not in line


async def test_sample_uses_its_own_routing_key_and_logs_its_timing(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = KeyedStub("ok")
    monkeypatch.setattr(daemon.core.providers, "get", lambda vendor=None, model=None: provider)
    host, session_id = await host_session(http, daemon, tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="snowpea.llm"):
            await host.ok("llm.sample", {"sessionId": session_id, "input": "page"})
        assert provider.keys == [f"{session_id}:sample"]
        lines = [r.getMessage() for r in caplog.records if r.name == "snowpea.llm"]
        assert any(f"session={session_id} kind=sample" in line and "in=30" in line
                   for line in lines), lines
    finally:
        await host.stop()
