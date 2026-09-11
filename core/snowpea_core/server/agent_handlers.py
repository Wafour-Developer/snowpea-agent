"""``agent.create`` and ``agent.list`` (M6 contract §2, US-018).

Both mirror the ``/agent`` slash command exactly: ``agent.create`` asks the
provider for a definition and writes ``<workdir>/.snowpea/agents/<name>.md``,
``agent.list`` reports the definitions the daemon can see.

US-019 and US-021 extend the listing additively with ``kind:"subagent"``
(running children) and ``kind:"named"`` (persistent instances); everything this
module returns is ``kind:"definition"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent.definition import DefinitionError
from snowpea_core.commands.agent_cmd import create_definition, definitions_for
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    AgentCreateParams,
    AgentCreateResult,
    AgentInfo,
    AgentListResult,
    Empty,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.server.agent")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("agent.create", "agent.list")

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


async def agent_list_handler(conn: RpcConnection, _params: Empty, core: Core) -> AgentListResult:
    """``agent.list`` — the agent definitions visible from the caller's project."""
    definitions = definitions_for(core, _workdir(core, conn))
    return AgentListResult(
        agents=[
            AgentInfo(
                name=defn.name,
                description=defn.description,
                source=defn.source,
                kind=DEFINITION_KIND,
                path=str(defn.path) if defn.path else None,
            )
            for defn in definitions
        ]
    )


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
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return AgentCreateResult(name=defn.name, path=str(defn.path) if defn.path else None)


def register_agent_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register the two ``agent.*`` methods this story implements."""
    dispatcher.register("agent.list", agent_list_handler)
    dispatcher.register("agent.create", agent_create_handler)
    return dispatcher


__all__ = [
    "DEFINITION_KIND",
    "HANDLED_METHODS",
    "agent_create_handler",
    "agent_list_handler",
    "register_agent_handlers",
]
