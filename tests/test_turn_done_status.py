"""A session row read in reaction to ``turn.done`` no longer says "running".

Clients refresh their session list when a turn ends. ``turn.done`` goes out a
moment before the turn task clears ``current_turn``, so a row read in that gap
used to report the finished turn as still running, and the sidebar spun on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _support import make_daemon

from snowpea_core.agent import loop as agent_loop

pytestmark = pytest.mark.asyncio


class RowAtTurnDone:
    """Reads the session's summary the instant ``turn.done`` arrives."""

    closed = False

    def __init__(self, session: Any) -> None:
        self.session = session
        self.rows: list[Any] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event" and params.get("kind") == "turn.done":
            self.rows.append(self.session.summary())


async def test_row_is_not_running_once_turn_done_is_out(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        assert core is not None
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        watcher = RowAtTurnDone(session)
        core.hub.subscribe(watcher, session.id)

        session.current_turn = "t-1"
        await agent_loop.run_turn(core, session, "hello", turn_id="t-1")

        assert watcher.rows, "turn.done was not emitted"
        row = watcher.rows[-1]
        assert row.status != "running"
        assert row.running is False
        # A later turn is running again as soon as it starts.
        session.current_turn = "t-2"
        assert session.summary().status == "running"
    finally:
        await daemon.stop()
