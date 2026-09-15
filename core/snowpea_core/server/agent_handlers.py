"""``agent.create`` and ``agent.list`` (M6 contract §2, US-018).

Both mirror the ``/agent`` slash command exactly: ``agent.create`` asks the
provider for a definition and writes ``<workdir>/.snowpea/agents/<name>.md``,
``agent.list`` reports the definitions the daemon can see.

US-019 adds ``agent.spawn`` and lists the subagents that are queued or running
right now as ``kind:"subagent"``; US-021 adds ``kind:"named"`` for persistent
instances.  File-backed definitions keep ``kind:"definition"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent import named as named_agents
from snowpea_core.agent.definition import DefinitionError
from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands.agent_cmd import create_definition, definitions_for
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    AgentCreateParams,
    AgentCreateResult,
    AgentInfo,
    AgentListResult,
    AgentSpawnParams,
    AgentSpawnResult,
    Empty,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.server.agent")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "agent.create",
    "agent.list",
    "agent.bindChannel",
    "agent.delete",
    "agent.spawn",
)

#: Kind reported for file-backed definitions.
DEFINITION_KIND = "definition"


def _session_for(core: Core, conn: RpcConnection) -> Session | None:
    """The session this connection means.

    ``agent.*`` carries no session id, so the caller's own session is used;
    failing that, the only open one.
    """
    sessions = [
        session
        for session in (core.sessions.get(row.sessionId) for row in core.sessions.list())
        if session is not None
    ]
    mine = [session for session in sessions if session.origin_conn is conn]
    if mine:
        return mine[-1]
    return sessions[0] if len(sessions) == 1 else None


def _workdir(core: Core, conn: RpcConnection) -> Path:
    session = _session_for(core, conn)
    return Path(session.workdir) if session is not None else Path.cwd()


def _team_rows(core: Core, workdir: Path, session: Session | None) -> list[AgentInfo]:
    """One ``kind:"team"`` row per team the user could pick here.

    A client that offers "assign a team to this project" needs the whole list,
    not only the one in force: every global and project team, each with its
    members, where it came from, whether it is the active one, and which stage
    of ``/team "<task>"`` each member fills.  A team with no implementer gets
    an empty ``stages`` rather than being hidden — the user still has to see it
    to understand why it is not offered.
    """
    from snowpea_core.agent.team_config import teams_with_source
    from snowpea_core.agent.team_pipeline import stages_map

    active = session.team if session is not None else None
    rows: list[AgentInfo] = []
    for name, (members, origin) in sorted(teams_with_source(core.settings, workdir).items()):
        rows.append(
            AgentInfo(
                name=name,
                description=(
                    "Active project team" if name == active else f"{origin.capitalize()} team"
                ),
                source=origin,
                kind="team",
                active=name == active,
                agents=list(members),
                stages=stages_map(core, workdir, members),
            )
        )
    # The active team is what a client renders first, as it did before.
    rows.sort(key=lambda row: (not row.active, row.name))
    return rows


async def agent_list_handler(conn: RpcConnection, _params: Empty, core: Core) -> AgentListResult:
    """``agent.list`` — definitions on disk, named instances, subagents running now.

    The subagent rows are a snapshot of what the daemon is doing this instant,
    which is what ``snowpea agents --json`` polls during a ``/ralph`` run to see
    two children running at once (AC-04).
    """
    session = _session_for(core, conn)
    definitions = definitions_for(core, _workdir(core, conn))
    if session is not None and session.team_agents:
        allowed = set(session.team_agents)
        registry = getattr(core, "named_agents", None)
        definitions = [
            definition
            for definition in definitions
            if definition.name in allowed
            or (registry is not None and registry.get(definition.name) is not None)
        ]
    agents = [
        AgentInfo(
            name=defn.name,
            description=defn.description,
            source=defn.source,
            kind=DEFINITION_KIND,
            path=str(defn.path) if defn.path else None,
        )
        for defn in definitions
    ]
    agents[:0] = _team_rows(core, _workdir(core, conn), session)
    # US-021: the persistent instances, listed alongside their definitions.
    agents.extend(await named_agents.list_infos(core))
    # US-019: the children this daemon is running for somebody right now.
    agents.extend(get_manager(core).infos())
    return AgentListResult(agents=agents)


async def agent_spawn_handler(
    conn: RpcConnection, params: AgentSpawnParams, core: Core
) -> AgentSpawnResult:
    """``agent.spawn`` — run a task as a subagent of the caller's session.

    The same path the ``delegate_task`` tool takes, entered from a client
    instead of from the model.  It answers as soon as the child has an id: the
    run itself continues in the background and reports through the
    ``subagent.*`` events on the parent session.
    """
    session = core.sessions.get(params.sessionId) if params.sessionId else _session_for(core, conn)
    if session is None:
        raise RpcError(
            errors.INVALID_PARAMS,
            "agent.spawn needs a session: pass sessionId, or open one first",
        )
    task = params.task.strip()
    if not task:
        raise RpcError(errors.INVALID_PARAMS, "agent.spawn needs a non-empty task")
    agent_id, _runner = get_manager(core).spawn(
        session,
        task,
        agent=params.name.strip() or None,
        model=(params.model or "").strip() or None,
    )
    return AgentSpawnResult(agentId=agent_id)


async def agent_create_handler(
    conn: RpcConnection, params: AgentCreateParams, core: Core
) -> AgentCreateResult:
    """``agent.create`` — generate a definition and write it to the project."""
    session = _session_for(core, conn)
    if session is None:
        raise RpcError(
            errors.INVALID_PARAMS,
            "agent.create needs an open session to locate the project directory",
        )
    try:
        defn = await create_definition(core, session, params.description)
        if params.name:
            defn = await named_agents.rename_definition(core, session, defn, params.name)
        if params.named:
            await named_agents.registry(core).create(defn.name, defn)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    except named_agents.NamedAgentError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return AgentCreateResult(name=defn.name, path=str(defn.path) if defn.path else None)


def register_agent_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register the ``agent.*`` methods US-018 and US-021 implement."""
    dispatcher.register("agent.list", agent_list_handler)
    dispatcher.register("agent.create", agent_create_handler)
    dispatcher.register("agent.bindChannel", named_agents.agent_bind_channel_handler)
    dispatcher.register("agent.delete", named_agents.agent_delete_handler)
    dispatcher.register("agent.spawn", agent_spawn_handler)
    return dispatcher


__all__ = [
    "DEFINITION_KIND",
    "HANDLED_METHODS",
    "agent_create_handler",
    "agent_list_handler",
    "agent_spawn_handler",
    "register_agent_handlers",
]
