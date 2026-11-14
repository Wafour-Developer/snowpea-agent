"""``goal.*`` (1.43.0, capability ``goals``; M16 P1.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.agent import goal_driver, goals
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    GoalControlParams,
    GoalGetParams,
    GoalResult,
    GoalSetParams,
    GoalView,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
    from snowpea_core.session.session import Session

HANDLED_METHODS: tuple[str, ...] = (
    "goal.get",
    "goal.set",
    "goal.pause",
    "goal.resume",
    "goal.clear",
)


def _session(core: Core, session_id: str) -> Session:
    session = core.sessions.get(session_id)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
    if getattr(session, "is_subagent", False):
        raise RpcError(errors.INVALID_PARAMS, "a delegated child does not take a goal")
    return session


def _result(session: Session) -> GoalResult:
    data = goal_driver.view(session)
    return GoalResult(goal=GoalView(**data) if data else None)


def _idle(session: Session) -> bool:
    task = getattr(session, "turn_task", None)
    return task is None or task.done()


async def goal_get_handler(
    _conn: RpcConnection | None, params: GoalGetParams, core: Core
) -> GoalResult:
    return _result(_session(core, params.sessionId))


async def goal_set_handler(
    _conn: RpcConnection | None, params: GoalSetParams, core: Core
) -> GoalResult:
    session = _session(core, params.sessionId)
    try:
        goals.start(session, params.objective, params.maxRounds)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if params.start and _idle(session):
        goal_driver.start_round(core, session)
    await goal_driver.publish(core, session)
    return _result(session)


async def goal_pause_handler(
    _conn: RpcConnection | None, params: GoalControlParams, core: Core
) -> GoalResult:
    session = _session(core, params.sessionId)
    goals.pause(session)
    await goal_driver.publish(core, session)
    return _result(session)


async def goal_resume_handler(
    _conn: RpcConnection | None, params: GoalControlParams, core: Core
) -> GoalResult:
    session = _session(core, params.sessionId)
    if getattr(session, "goal", None) is None:
        raise RpcError(errors.NOT_FOUND, "this session has no goal")
    goal = goals.resume(session, max(0, int(params.extraRounds or 0)))
    if goal is not None and goal.phase == "active" and _idle(session):
        goal_driver.start_round(core, session)
    await goal_driver.publish(core, session)
    return _result(session)


async def goal_clear_handler(
    _conn: RpcConnection | None, params: GoalControlParams, core: Core
) -> GoalResult:
    session = _session(core, params.sessionId)
    goals.clear(session)
    await goal_driver.publish(core, session)
    return _result(session)


def register_goal_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("goal.get", goal_get_handler)
    dispatcher.register("goal.set", goal_set_handler)
    dispatcher.register("goal.pause", goal_pause_handler)
    dispatcher.register("goal.resume", goal_resume_handler)
    dispatcher.register("goal.clear", goal_clear_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_goal_handlers"]
