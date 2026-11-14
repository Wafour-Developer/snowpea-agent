"""session.fork (M17 2c, protocol 1.46.0): branch at a finished turn into a new session."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _support import make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.server.errors import RpcError
from snowpea_core.server.fork_handlers import session_fork_handler
from snowpea_core.server.protocol import SessionForkParams, SessionTrajectoryParams
from snowpea_core.server.trajectory_handlers import session_trajectory_handler
from snowpea_core.session.history import message_text


class _Echo:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        last = message_text(messages[-1]).splitlines()[0]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        yield StreamEvent(kind="text_delta", text=f"echo {last}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _setup(tmp_path: Any) -> tuple[Any, Any, list[str]]:
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    core.providers.get = lambda _p, _m: _Echo()
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    session.title = "release work"
    turns = []
    for text in ("first", "second", "third"):
        turns.append(agent_loop.start_turn(core, session, text))
        await asyncio.wait_for(session.turn_task, timeout=60)
    return daemon, session, turns


def _words(session: Any) -> list[str]:
    return [m.content for m in session.history.messages if m.role == "user"]


async def test_fork_at_a_middle_turn_keeps_history_up_to_it(tmp_path: Any) -> None:
    daemon, source, turns = await _setup(tmp_path)
    core = daemon.core
    try:
        result = await session_fork_handler(
            None, SessionForkParams(sessionId=source.id, atTurnId=turns[1]), core
        )
        fork = core.sessions.get(result.sessionId)
        assert fork is not None and fork.id != source.id
        assert _words(fork) == ["first", "second"]
        assert _words(source) == ["first", "second", "third"]  # untouched
        assert fork.workdir == source.workdir and fork.mode == source.mode
        assert fork.title == "release work (fork)"
        # Its log holds the inherited turns, then the fork marker.
        events = await core.store.events_after(fork.id, 0)
        assert events[-1]["kind"] == "session.forked"
        started = [e["payload"]["turnId"] for e in events if e["kind"] == "turn.started"]
        assert started == turns[:2]
        rows = await session_trajectory_handler(
            None, SessionTrajectoryParams(sessionId=fork.id), core
        )
        assert rows.rows[-1].title == "history above is inherited"
        # The fork goes on independently.
        agent_loop.start_turn(core, fork, "branch")
        await asyncio.wait_for(fork.turn_task, timeout=60)
        assert _words(fork)[-1] == "branch" and "branch" not in _words(source)
    finally:
        await daemon.stop()


async def test_fork_without_a_turn_takes_the_latest(tmp_path: Any) -> None:
    daemon, source, turns = await _setup(tmp_path)
    try:
        result = await session_fork_handler(
            None, SessionForkParams(sessionId=source.id), daemon.core
        )
        assert result.atTurnId == turns[-1]
        assert result.historyLen == len(source.history)
    finally:
        await daemon.stop()


async def test_a_turn_before_the_last_compaction_cannot_be_cut_at(tmp_path: Any) -> None:
    daemon, source, turns = await _setup(tmp_path)
    try:
        source.history.replace(source.history.snapshot()[-2:])  # a compaction happened
        with pytest.raises(RpcError):
            await session_fork_handler(
                None, SessionForkParams(sessionId=source.id, atTurnId=turns[0]), daemon.core
            )
        # The latest turn still forks: the whole current history.
        result = await session_fork_handler(
            None, SessionForkParams(sessionId=source.id), daemon.core
        )
        assert result.historyLen == 2
    finally:
        await daemon.stop()
