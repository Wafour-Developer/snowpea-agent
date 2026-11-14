"""Goal state rules (M16 P1.6): evidence before complete, blocked after a streak, a cap."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from snowpea_core.agent import goals


def _session() -> SimpleNamespace:
    return SimpleNamespace(goal=None, goal_armed=False, is_subagent=False)


def test_a_new_goal_is_active_and_armed() -> None:
    session = _session()
    goal = goals.start(session, "  make   the build green ", max_rounds=5)
    assert (goal.objective, goal.phase, goal.max_rounds, goal.revision) == (
        "make the build green",
        "active",
        5,
        1,
    )
    assert goals.due(session)


def test_an_empty_objective_is_refused() -> None:
    with pytest.raises(ValueError):
        goals.start(_session(), "   ")


def test_complete_needs_evidence_and_the_current_revision() -> None:
    session = _session()
    goal = goals.start(session, "ship")
    changed, text = goals.update_from_model(session, revision=goal.revision, phase="complete")
    assert not changed and "evidence" in text
    changed, text = goals.update_from_model(
        session, revision=goal.revision - 1, phase="complete", evidence="x" * 40
    )
    assert not changed and "Stale goal revision" in text
    changed, _ = goals.update_from_model(
        session,
        revision=goal.revision,
        phase="complete",
        evidence="ran npm test: 42 passed, 0 failed; opened /login and signed in",
    )
    assert changed and goal.phase == "complete" and not goals.due(session)


def test_blocked_only_after_the_same_condition_three_rounds_running() -> None:
    session = _session()
    goal = goals.start(session, "deploy")
    for expected in (1, 2):
        changed, text = goals.update_from_model(
            session, revision=goal.revision, phase="blocked", code="no-credentials"
        )
        assert changed and goal.phase == "active" and f"{expected}/3" in text
    changed, _ = goals.update_from_model(
        session,
        revision=goal.revision,
        phase="blocked",
        code="no-credentials",
        message="needs the AWS key",
    )
    assert goal.phase == "blocked" and goal.blocked_message == "needs the AWS key"
    assert not goals.due(session)


def test_a_different_condition_or_progress_restarts_the_streak() -> None:
    session = _session()
    goal = goals.start(session, "deploy")
    goals.update_from_model(session, revision=goal.revision, phase="blocked", code="a")
    goals.update_from_model(session, revision=goal.revision, phase="blocked", code="a")
    goals.update_from_model(session, revision=goal.revision, phase="active")
    goals.update_from_model(session, revision=goal.revision, phase="blocked", code="a")
    assert goal.phase == "active" and goal.streak_count == 1


def test_rounds_stop_at_the_cap_as_blocked() -> None:
    session = _session()
    goals.start(session, "loop", max_rounds=2)
    assert goals.begin_round(session) is not None
    goal, text = goals.begin_round(session)  # type: ignore[misc]
    assert "Round 2/2" in text and "Objective: loop" in text
    assert goals.begin_round(session) is None
    assert goal.phase == "blocked" and goal.blocked_code == goals.ROUND_LIMIT
    assert not goals.due(session)


def test_a_resumed_goal_is_rearmed_with_room_to_run() -> None:
    session = _session()
    goals.start(session, "loop", max_rounds=1)
    goals.begin_round(session)
    goals.begin_round(session)  # hits the cap
    goal = goals.resume(session)
    assert goal is not None and goal.phase == "active" and goal.max_rounds > goal.rounds
    assert goals.due(session)


def test_a_restored_goal_is_not_armed() -> None:
    session = _session()
    goal = goals.start(session, "persist me")
    restored = goals.Goal.from_json(goal.to_json())
    assert restored is not None and restored.objective == "persist me"
    later = _session()
    later.goal = restored
    assert not goals.due(later)  # nobody re-armed it


def test_a_subagent_never_drives_rounds() -> None:
    session = _session()
    session.is_subagent = True
    goals.start(session, "x")
    assert not goals.due(session)


# ---- through a running daemon ------------------------------------------------

import asyncio  # noqa: E402
from typing import Any  # noqa: E402

from _support import Recorder, make_daemon  # noqa: E402

from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage  # noqa: E402
from snowpea_core.server.goal_handlers import (  # noqa: E402
    goal_clear_handler,
    goal_get_handler,
    goal_set_handler,
)
from snowpea_core.server.protocol import (  # noqa: E402
    GoalControlParams,
    GoalGetParams,
    GoalSetParams,
)
from snowpea_core.session.history import message_text  # noqa: E402


class _Worker:
    """Works one round without finishing, then reports complete with evidence."""

    vendor = "fake"

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []
        self.rounds = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        last = message_text(messages[-1])
        if "<goal_round>" in last:
            self.rounds += 1
            if self.rounds >= 2:
                revision = int(last.split("(revision ", 1)[1].split(")", 1)[0])
                call = ToolCall(
                    id=f"g{self.rounds}",
                    name="goal_update",
                    arguments={
                        "revision": revision,
                        "phase": "complete",
                        "evidence": "ran pytest: 12 passed, 0 failed; read build.log, no errors",
                    },
                )
                yield StreamEvent(kind="tool_call", tool_call=call)
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
        yield StreamEvent(kind="text_delta", text=f"worked (round {self.rounds})")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


async def test_a_goal_runs_rounds_until_the_model_proves_it_done(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        worker = _Worker()
        core.providers.get = lambda _p, _m: worker
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        result = await goal_set_handler(
            None, GoalSetParams(sessionId=session.id, objective="green build", maxRounds=5), core
        )
        assert result.goal is not None and result.goal.armed
        await _until(lambda: session.goal is not None and session.goal.phase == "complete")
        await _until(lambda: session.turn_task is None or session.turn_task.done())
        assert worker.rounds == 2
        assert "pytest: 12 passed" in session.goal.evidence
        seen = [e["payload"]["goal"] for e in recorder.of_kind("goal.changed")]
        assert seen[-1]["phase"] == "complete" and seen[-1]["armed"] is False
        # The model saw the goal in its runtime context, with the revision to name.
        first = "\n".join(message_text(m) for m in worker.requests[0])
        assert "Goal (revision" in first and "green build" in first
        # Stored with the session row, so a resume shows it (unarmed).
        row = await core.store.session(session.id)
        assert "green build" in (row or {}).get("goal", "")
        got = await goal_get_handler(None, GoalGetParams(sessionId=session.id), core)
        assert got.goal is not None and got.goal.phase == "complete"
        cleared = await goal_clear_handler(None, GoalControlParams(sessionId=session.id), core)
        assert cleared.goal is None
    finally:
        await daemon.stop()


async def test_stopping_a_round_disarms_the_goal(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        session = await core.sessions.create(tmp_path, mode="auto")
        goals.start(session, "keep going")
        session.last_turn_reason = "interrupted"
        from snowpea_core.agent import goal_driver

        assert await goal_driver.after_turn(core, session) is None
        assert session.goal_armed is False and session.goal.phase == "active"
    finally:
        await daemon.stop()


# ---- /goal --------------------------------------------------------------------


async def test_the_goal_command_sets_runs_and_controls_a_goal(tmp_path: Any) -> None:
    from snowpea_core.commands.goal_cmd import describe

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        worker = _Worker()
        core.providers.get = lambda _p, _m: worker
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        core.commands.start(core, session, "goal", "make it green --rounds 4", None)
        await _until(lambda: session.goal is not None and session.goal.phase == "complete")
        await _until(lambda: session.turn_task is None or session.turn_task.done())
        assert session.goal.max_rounds == 4 and session.goal.objective == "make it green"
        assert worker.rounds == 2
        assert "Phase: complete" in describe(session)
        core.commands.start(core, session, "goal", "clear", None)
        await _until(lambda: session.goal is None)
    finally:
        await daemon.stop()


async def test_pause_disarms_and_the_command_shows_it(tmp_path: Any) -> None:
    from snowpea_core.commands.goal_cmd import describe

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        session = await core.sessions.create(tmp_path, mode="auto")
        goals.start(session, "long job")
        core.commands.start(core, session, "goal", "pause", None)
        await _until(lambda: session.goal.phase == "paused")
        assert session.goal_armed is False
        assert "Phase: paused" in describe(session) and "(armed)" not in describe(session)
    finally:
        await daemon.stop()
