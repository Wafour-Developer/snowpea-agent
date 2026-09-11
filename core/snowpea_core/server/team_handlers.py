"""RPC handlers for ``team.*`` (M7 contract §5, US-020).

Its own module for the same reason ``job_handlers`` is one: team mode is a
subsystem, and ``build_dispatcher`` only needs the single
``register_team_handlers`` line.

``team.start`` answers as soon as the worktrees exist and the board is written,
because the run itself takes as long as the work does; the caller follows it
through ``team.task.update`` events on the lead session and ``team.status``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.agent.team import TeamError, get_manager_for
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    TeamStartParams,
    TeamStartResult,
    TeamStatusParams,
    TeamStatusResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.team")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("team.start", "team.status")


async def team_start_handler(
    _conn: RpcConnection, params: TeamStartParams, core: Core
) -> TeamStartResult:
    """``team.start`` — plan the work, create the worktrees, run the team."""
    session = core.sessions.get(params.sessionId)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    try:
        team_id = await get_manager_for(core).start(session, params.n, params.task)
    except TeamError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return TeamStartResult(teamId=team_id)


async def team_status_handler(
    _conn: RpcConnection, params: TeamStatusParams, core: Core
) -> TeamStatusResult:
    """``team.status`` — the board with states, retries, hunks and worktrees."""
    try:
        return await get_manager_for(core).status(params.teamId or None)
    except TeamError as exc:
        raise RpcError(errors.NOT_FOUND, str(exc)) from exc


def register_team_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("team.start", team_start_handler)
    dispatcher.register("team.status", team_status_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "register_team_handlers",
    "team_start_handler",
    "team_status_handler",
]
