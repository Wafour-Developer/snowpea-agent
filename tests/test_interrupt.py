"""Interrupting an in-flight tool stops the work, not just the next model call."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import StreamEvent, ToolCall

pytestmark = pytest.mark.asyncio

TIMEOUT = 10.0


class SleepToolProvider:
    vendor = "test-interrupt"

    async def stream(
        self, messages: list[Any], tools: list[Any], **kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        del messages, tools, kwargs
        yield StreamEvent(
            kind="tool_call",
            tool_call=ToolCall(
                id="call_sleep",
                name="shell",
                arguments={"command": "sleep 30", "timeout": 60},
            ),
        )
        yield StreamEvent(kind="done", stop_reason="tool_use")


async def test_interrupt_kills_an_inflight_shell_and_records_interrupted_result(
    tmp_path: Path,
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        assert core is not None
        core.providers.get = lambda _provider, _model: SleepToolProvider()  # type: ignore[assignment]
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)

        runner = asyncio.create_task(
            agent_loop.run_turn(core, session, "run the slow command", turn_id="t-shell")
        )
        deadline = monotonic() + TIMEOUT
        while monotonic() < deadline:
            if recorder.of_kind("tool.call"):
                break
            await asyncio.sleep(0.01)
        assert recorder.of_kind("tool.call")

        started = monotonic()
        session.interrupt_user_requested = True
        session.interrupt.set()
        await asyncio.wait_for(runner, timeout=1.5)
        elapsed = monotonic() - started

        result = recorder.of_kind("tool.result")[-1]["payload"]
        assert elapsed < 1.5
        assert result["name"] == "shell"
        assert result["ok"] is False
        assert result["error"] == "interrupted"
        done = recorder.of_kind("turn.done")[-1]["payload"]
        assert done["turnId"] == "t-shell"
        assert done["reason"] == "interrupted"
    finally:
        await daemon.stop()
