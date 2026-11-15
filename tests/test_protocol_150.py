"""Protocol 1.50.0: the browser client's security-review batch.

Archive disarms a goal, partial text on an errored step, ``lastUserAt``,
workspace adoption that respects ``workspace.remove``, forks in rows and
lineage, goal round numbers, ``fork_*`` refusal codes and untitled forks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import goals
from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, Usage
from snowpea_core.server.errors import RpcError
from snowpea_core.server.fork_handlers import session_fork_handler
from snowpea_core.server.goal_handlers import goal_set_handler
from snowpea_core.server.lineage_handlers import session_lineage_handler
from snowpea_core.server.protocol import (
    Empty,
    GoalSetParams,
    SessionFlagParams,
    SessionForkParams,
    SessionLineageParams,
    SessionListParams,
    WorkspaceIdParams,
)
from snowpea_core.server.session_handlers import session_list_handler
from snowpea_core.server.workspace_handlers import (
    session_archive_handler,
    workspace_list_handler,
    workspace_remove_handler,
)
from snowpea_core.session import workspaces as ws
from snowpea_core.session.history import message_text


class _Echo:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        last = message_text(messages[-1]).splitlines()[0]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        yield StreamEvent(kind="text_delta", text=f"echo {last}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


class _Breaks:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        yield StreamEvent(kind="reasoning_delta", text="thinking it over")
        yield StreamEvent(kind="text_delta", text="half an ans")
        raise ProviderError("invalid_request", "the model server refused the request")


async def _until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


async def _turns(core: Any, session: Any, *texts: str) -> list[str]:
    ids = []
    for text in texts:
        ids.append(agent_loop.start_turn(core, session, text))
        await asyncio.wait_for(session.turn_task, timeout=60)
    return ids


async def _row(core: Any, session_id: str, **params: Any) -> Any:
    result = await session_list_handler(None, SessionListParams(**params), core)
    return next(r for r in result.sessions if r.sessionId == session_id)


# 1 ------------------------------------------------------------------------


async def test_archiving_disarms_an_armed_goal(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        session = await core.sessions.create(tmp_path, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        goals.start(session, "keep going")
        session.turn_task = asyncio.ensure_future(asyncio.sleep(30))
        with pytest.raises(RpcError):  # still refused while a turn runs
            await session_archive_handler(
                None, SessionFlagParams(sessionId=session.id, value=True), core
            )
        assert session.goal_armed is True
        await session_archive_handler(
            None, SessionFlagParams(sessionId=session.id, value=True, stop=True), core
        )
        session.turn_task.cancel()
        assert session.goal_armed is False and session.goal.phase == "active"
        changed = recorder.of_kind("goal.changed")
        assert changed and changed[-1]["payload"]["goal"]["armed"] is False
    finally:
        await daemon.stop()


# 2 ------------------------------------------------------------------------


async def test_an_errored_step_carries_what_streamed(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Breaks()
        session = await core.sessions.create(tmp_path, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        await _turns(core, session, "go")
        done = recorder.of_kind("step.done")
        assert done and done[-1]["payload"]["outcome"] == "error"
        assert done[-1]["payload"]["text"] == "half an ans"
        assert done[-1]["payload"]["reasoning"] == "thinking it over"
    finally:
        await daemon.stop()


# 3 ------------------------------------------------------------------------


async def test_rows_carry_the_time_of_the_last_prompt(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        quiet = await core.sessions.create(tmp_path, mode="auto")
        session = await core.sessions.create(tmp_path, mode="auto")
        assert (await _row(core, quiet.id)).lastUserAt is None
        await _turns(core, session, "hello")
        live = await _row(core, session.id)
        assert live.lastUserAt and live.lastUserAt == session.last_user_at
        # A closed session reads it from the stored events.
        await core.sessions.close(session.id)
        closed = await _row(core, session.id, includeClosed=True)
        assert closed.lastUserAt == live.lastUserAt
    finally:
        await daemon.stop()


# 4 ------------------------------------------------------------------------


async def test_a_removed_workspace_stays_removed_until_a_new_chat(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        folder = tmp_path / "app"
        folder.mkdir()
        await core.sessions.create(folder, mode="auto")
        listing = await workspace_list_handler(None, Empty(), core)
        workspace = next(w for w in listing.workspaces if w.path == ws.canonical(folder))
        await workspace_remove_handler(None, WorkspaceIdParams(id=workspace.id), core)
        for _ in range(2):
            listing = await workspace_list_handler(None, Empty(), core)
            assert all(w.path != ws.canonical(folder) for w in listing.workspaces)
            await session_list_handler(None, SessionListParams(includeClosed=True), core)
        # A new conversation in the folder brings it back, once.
        await core.sessions.create(folder, mode="auto")
        listing = await workspace_list_handler(None, Empty(), core)
        again = [w for w in listing.workspaces if w.path == ws.canonical(folder)]
        assert len(again) == 1 and again[0].id != workspace.id and again[0].sessions == 2
    finally:
        await daemon.stop()


async def test_the_first_open_fills_the_registry_once(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        store = daemon.core.store
        folder = tmp_path / "old"
        folder.mkdir()
        await store.insert_session(
            "s-old", str(folder), "auto", None, None, None, "2026-01-01T00:00:00.000Z"
        )
        ws._READY.pop(id(store), None)
        await ws.ensure(store)  # the tables exist; now forget the fill ever ran
        ws._READY.pop(id(store), None)
        await asyncio.to_thread(store._execute, "DELETE FROM workspace_meta")
        await asyncio.to_thread(store._execute, "DELETE FROM workspaces")
        items = await ws.list_all(store)
        found = [i for i in items if i["path"] == ws.canonical(folder)]
        assert len(found) == 1
        assert await ws.remove(store, found[0]["id"])
        ws._READY.pop(id(store), None)  # a later open does not fill it again
        assert all(i["path"] != ws.canonical(folder) for i in await ws.list_all(store))
    finally:
        await daemon.stop()


# 5, 7, 8 ------------------------------------------------------------------


async def test_forks_in_rows_and_lineage(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        lead = await core.sessions.create(tmp_path, mode="auto")
        turns = await _turns(core, lead, "one", "two")
        lead.title = None  # an untitled source
        first = await session_fork_handler(
            None, SessionForkParams(sessionId=lead.id, atTurnId=turns[0]), core
        )
        fork = core.sessions.get(first.sessionId)
        assert fork.title is None
        assert (fork.forked_from, fork.forked_at_turn_id) == (lead.id, turns[0])
        stored = await core.store.session(fork.id)
        assert (stored["forked_from"], stored["forked_at_turn_id"]) == (lead.id, turns[0])
        row = await _row(core, fork.id)
        assert (row.forkedFrom, row.forkedAtTurnId, row.title) == (lead.id, turns[0], None)
        await core.sessions.close(fork.id)
        closed = await _row(core, fork.id, includeClosed=True)
        assert (closed.forkedFrom, closed.forkedAtTurnId) == (lead.id, turns[0])

        lead.title = "release work"
        second = await session_fork_handler(None, SessionForkParams(sessionId=lead.id), core)
        assert core.sessions.get(second.sessionId).title == "release work"  # 1.53.0: no suffix

        # A delegated child of the second fork: delegation, then fork, up to the lead.
        child = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=second.sessionId, kind="subagent"
        )
        deep = await session_lineage_handler(None, SessionLineageParams(sessionId=child.id), core)
        assert [(n.sessionId, n.kind) for n in deep.ancestors] == [
            (lead.id, "fork"),
            (second.sessionId, "delegation"),
        ]
        assert deep.ancestors[0].sessionKind == "chat"
        top = await session_lineage_handler(None, SessionLineageParams(sessionId=lead.id), core)
        assert [(f.sessionId, f.atTurnId) for f in top.forks] == [
            (first.sessionId, turns[0]),
            (second.sessionId, turns[1]),
        ]
        assert top.forks[0].title is None and top.forks[1].title == "release work"
    finally:
        await daemon.stop()


def _code(exc: pytest.ExceptionInfo[RpcError]) -> str:
    return exc.value.to_jsonrpc()["data"]["code"]


async def test_fork_refusals_have_their_own_codes(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        with pytest.raises(RpcError) as none:
            await session_fork_handler(None, SessionForkParams(sessionId=session.id), core)
        assert _code(none) == "fork_no_finished_turn"
        turns = await _turns(core, session, "one", "two")
        with pytest.raises(RpcError) as missing:
            await session_fork_handler(
                None, SessionForkParams(sessionId=session.id, atTurnId="t-nope"), core
            )
        assert _code(missing) == "fork_turn_not_found"
        assert missing.value.to_jsonrpc()["code"] == -32004
        session.history.replace(session.history.snapshot()[-2:])  # compacted
        with pytest.raises(RpcError) as compacted:
            await session_fork_handler(
                None, SessionForkParams(sessionId=session.id, atTurnId=turns[0]), core
            )
        assert _code(compacted) == "fork_after_compaction"
        assert compacted.value.to_jsonrpc()["code"] == -32602
        session.turn_task = asyncio.ensure_future(asyncio.sleep(30))
        try:
            with pytest.raises(RpcError) as running:
                await session_fork_handler(None, SessionForkParams(sessionId=session.id), core)
            assert _code(running) == "fork_running"
        finally:
            session.turn_task.cancel()
        child = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=session.id, kind="subagent"
        )
        child.is_subagent = True
        with pytest.raises(RpcError) as delegated:
            await session_fork_handler(None, SessionForkParams(sessionId=child.id), core)
        assert _code(delegated) == "fork_child_session"
    finally:
        await daemon.stop()


# 6 ------------------------------------------------------------------------


async def test_a_goal_round_is_numbered_on_turn_started(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        await _turns(core, session, "plain")
        await goal_set_handler(
            None, GoalSetParams(sessionId=session.id, objective="green", maxRounds=2), core
        )
        await _until(lambda: session.goal.phase == "blocked")
        await _until(lambda: session.turn_task is None or session.turn_task.done())
        started = recorder.of_kind("turn.started")
        assert "goalRound" not in started[0]["payload"] and "goalId" not in started[0]["payload"]
        rounds = [e["payload"] for e in started[1:]]
        assert [p["goalRound"] for p in rounds] == [1, 2]
        assert {p["goalId"] for p in rounds} == {session.goal.id}
        # goal.changed went out before the first round began.
        kinds = [e["kind"] for e in recorder.events]
        first_round = next(
            i for i, e in enumerate(recorder.events)
            if e["kind"] == "turn.started" and "goalRound" in e["payload"]
        )
        assert "goal.changed" in kinds[:first_round]
    finally:
        await daemon.stop()


# 9 ------------------------------------------------------------------------


@pytest.mark.parametrize("lead", ["　", "\x1c", "  \t"])
async def test_a_steer_never_runs_a_slash_command(tmp_path: Any, lead: str) -> None:
    from snowpea_core.server.host_handlers import session_steer_handler
    from snowpea_core.server.protocol import SessionSteerParams

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        text = f"{lead}/goal ship it --rounds 256"
        # Idle: it would have become a prompt, parsed as a command.
        with pytest.raises(RpcError) as idle:
            await session_steer_handler(
                None, SessionSteerParams(sessionId=session.id, text=text), core
            )
        assert _code(idle) == "steer_slash_command"
        assert idle.value.to_jsonrpc()["code"] == -32602
        assert session.goal is None and session.turn_task is None
        # Running: refused as well, nothing queued for the turn.
        session.turn_task = asyncio.ensure_future(asyncio.sleep(30))
        try:
            with pytest.raises(RpcError) as running:
                await session_steer_handler(
                    None, SessionSteerParams(sessionId=session.id, text=text), core
                )
            assert _code(running) == "steer_slash_command"
            assert session.rpc_steers == []
            # Plain text still steers.
            result = await session_steer_handler(
                None, SessionSteerParams(sessionId=session.id, text=f"{lead}also this"), core
            )
            assert result.started is False and session.rpc_steers == ["also this"]
        finally:
            session.turn_task.cancel()
    finally:
        await daemon.stop()


# 10 -----------------------------------------------------------------------


async def test_goals_are_refused_on_delegated_and_scheduled_sessions(tmp_path: Any) -> None:
    from snowpea_core.server.goal_handlers import goal_resume_handler
    from snowpea_core.server.protocol import GoalControlParams

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        lead = await core.sessions.create(tmp_path, mode="auto")
        child = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=lead.id, kind="subagent"
        )
        child.is_subagent = True
        job = await core.sessions.create(tmp_path, mode="auto", kind="scheduled", job_id="j-1")
        for session in (child, job):
            with pytest.raises(RpcError) as set_:
                await goal_set_handler(
                    None, GoalSetParams(sessionId=session.id, objective="x", start=False), core
                )
            assert _code(set_) == "goal_not_allowed"
            goals.start(session, "smuggled in")
            goals.pause(session)
            with pytest.raises(RpcError) as resume:
                await goal_resume_handler(None, GoalControlParams(sessionId=session.id), core)
            assert _code(resume) == "goal_not_allowed"
            assert session.goal_armed is False
        result = await goal_set_handler(
            None, GoalSetParams(sessionId=lead.id, objective="fine", start=False), core
        )
        assert result.goal is not None
    finally:
        await daemon.stop()
