"""RPC handlers for ``team.guide.*``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent.definition import DefinitionError
from snowpea_core.agent.team_guide import (
    TeamGuide,
    delete_guide,
    guide_for_session,
    list_guides,
    load_guide,
    save_guide,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Ok,
    TeamGuideDeleteParams,
    TeamGuideGetParams,
    TeamGuideGetResult,
    TeamGuideInfo,
    TeamGuideListParams,
    TeamGuideListResult,
    TeamGuideSetParams,
    TeamGuideSetResult,
    TeamGuideWorkdirParams,
    TeamRoutingRuleInfo,
    TeamsChangedNotification,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.server.team_guide")

HANDLED_METHODS: tuple[str, ...] = (
    "team.guide.get",
    "team.guide.set",
    "team.guide.delete",
    "team.guide.list",
)


def _session_for(core: Core, conn: RpcConnection, session_id: str | None) -> Session | None:
    if session_id:
        return core.sessions.get(session_id)
    sessions = [
        session
        for session in (core.sessions.get(row.sessionId) for row in core.sessions.list())
        if session is not None
    ]
    mine = [session for session in sessions if session.origin_conn is conn]
    if mine:
        return mine[-1]
    return sessions[0] if len(sessions) == 1 else None


def _workdir(core: Core, conn: RpcConnection, params: TeamGuideWorkdirParams) -> Path:
    if params.workdir:
        return Path(params.workdir).expanduser().resolve()
    session = _session_for(core, conn, params.sessionId)
    if session is not None:
        return Path(session.workdir).resolve()
    return Path.cwd()


def _to_info(guide: TeamGuide) -> TeamGuideInfo:
    return TeamGuideInfo(
        team=guide.team,
        description=guide.description,
        persona=guide.persona,
        routing=[
            TeamRoutingRuleInfo(when=rule.when, agent=rule.agent, known=rule.known)
            for rule in guide.routing
        ],
        source=guide.source,
        path=str(guide.path) if guide.path else None,
    )


async def _notify_changed(core: Core, *, reason: str, team: str | None = None) -> None:
    hub = getattr(core, "hub", None)
    if hub is None:  # pragma: no cover - test doubles
        return
    payload = TeamsChangedNotification(reason=reason, team=team)
    try:
        await hub.notify("teams.changed", payload.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 - a dead socket must not break a write
        return


def _effective_team(core: Core, session: Session | None, workdir: Path, team: str | None) -> str:
    if team:
        return team
    if session is not None:
        guide = guide_for_session(core, session)
        if guide is not None:
            return guide.team
        from snowpea_core.agent.team_config import active_team

        selected = active_team(core.settings, workdir)
        return selected.name if selected is not None else "default"
    return "default"


async def team_guide_get_handler(
    conn: RpcConnection, params: TeamGuideGetParams, core: Core
) -> TeamGuideGetResult:
    workdir = _workdir(core, conn, params)
    session = _session_for(core, conn, params.sessionId)
    effective = _effective_team(core, session, workdir, params.team)
    guide = load_guide(core.paths.home, workdir, effective, core=core)
    return TeamGuideGetResult(
        guide=_to_info(guide) if guide is not None else None,
        effectiveTeam=effective,
    )


async def team_guide_set_handler(
    conn: RpcConnection, params: TeamGuideSetParams, core: Core
) -> TeamGuideSetResult:
    workdir = _workdir(core, conn, params)
    routing = [(row.when, row.agent) for row in params.routing]
    try:
        path = save_guide(
            core.paths.home,
            workdir,
            params.team,
            scope=params.scope,
            description=params.description or "",
            persona=params.persona,
            routing=routing,
        )
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    guide = load_guide(core.paths.home, workdir, params.team, core=core)
    if guide is None:
        raise RpcError(errors.INTERNAL, "team guide was written but could not be read back")
    from dataclasses import replace

    guide = replace(guide, path=path)
    await _notify_changed(core, reason="set", team=guide.team)
    return TeamGuideSetResult(guide=_to_info(guide))


async def team_guide_delete_handler(
    conn: RpcConnection, params: TeamGuideDeleteParams, core: Core
) -> Ok:
    workdir = _workdir(core, conn, params)
    try:
        deleted = delete_guide(
            core.paths.home, workdir, params.team, scope=params.scope
        )
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if not deleted:
        raise RpcError(errors.NOT_FOUND, f"no team guide for {params.team!r}")
    await _notify_changed(core, reason="delete", team=params.team)
    return Ok(ok=True)


async def team_guide_list_handler(
    conn: RpcConnection, params: TeamGuideListParams, core: Core
) -> TeamGuideListResult:
    workdir = _workdir(core, conn, params)
    guides = [_to_info(guide) for guide in list_guides(core.paths.home, workdir, core=core)]
    return TeamGuideListResult(guides=guides)


def register_team_guide_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("team.guide.get", team_guide_get_handler)
    dispatcher.register("team.guide.set", team_guide_set_handler)
    dispatcher.register("team.guide.delete", team_guide_delete_handler)
    dispatcher.register("team.guide.list", team_guide_list_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "register_team_guide_handlers",
    "team_guide_delete_handler",
    "team_guide_get_handler",
    "team_guide_list_handler",
    "team_guide_set_handler",
]
