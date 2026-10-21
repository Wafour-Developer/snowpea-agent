"""A long shell command steps aside when the user speaks (Codex-style yield).

The turn used to wait out the whole command before a message typed meanwhile
was even read; now the command keeps running in the background, the tool
returns what it printed so far, and the model hears when it ends.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import process as process_tools
from snowpea_core.tools.registry import ToolContext
from snowpea_core.tools.shell import shell


def _ctx(tmp_path: Path) -> ToolContext:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    session = Session(id="s-yield", workdir=tmp_path)
    return ToolContext(session=session, core=core, backend=LocalBackend(tmp_path))


async def test_a_long_command_moves_to_the_background_when_the_user_speaks(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    started = time.monotonic()
    running = asyncio.ensure_future(
        shell(ctx, {"command": "echo first; sleep 3; echo second", "timeout": 30})
    )
    await asyncio.sleep(1.3)
    ctx.session.user_waiting.set()
    result = await asyncio.wait_for(running, 5)
    assert time.monotonic() - started < 2.5
    assert result.ok and "first" in result.output and "still running" in result.output
    [entry] = process_tools.REGISTRY.list("s-yield")
    assert entry.id in result.output

    await asyncio.wait_for(entry.detached.task, 10)
    await asyncio.sleep(0)
    later = await process_tools.process_output(ctx, {"id": entry.id})
    assert "second" in later.output and "exited(0)" in later.output
    assert any(entry.id in note and "exit 0" in note for note in ctx.session.pending_notices)
    process_tools.REGISTRY.clear()


async def test_a_command_nobody_talks_over_runs_to_the_end(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await shell(ctx, {"command": "sleep 1.5; echo done", "timeout": 30})
    assert result.ok and "done" in result.output and "still running" not in result.output
