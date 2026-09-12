"""Named persistent agents (M7 contract §6, AC-17).

A *named agent* is an agent definition that has been given a body: one
long-lived :class:`~snowpea_core.session.session.Session`, its own memory
namespace ``agent:<name>``, the gateway channels that reach it and the
scheduled jobs that run inside it.  Everything that makes it durable lives in
one row of ``named_agents`` in ``$SNOWPEA_HOME/state.db``, so a daemon restart
is a :meth:`NamedAgentRegistry.restore` away from the same agents with the same
session ids.

Three collaborations are worth spelling out:

*gateway*
    :meth:`NamedAgentRegistry.bind_channel` binds with the target
    ``{"agent": <name>, "session": <its session id>}``.  The ``agent`` key is
    what the contract asks for and what ``gateway.list`` reports; the
    ``session`` key is what makes the router deliver into the agent's *existing*
    session instead of opening a fresh one per conversation.  Because the
    session id survives a restart, the persisted binding keeps working without
    the router knowing that named agents exist.

*scheduler*
    A job carries ``agent``; :func:`session_for_job` hands the scheduler that
    agent's session so the run happens in its namespace instead of a throwaway
    one.

*lifecycle*
    The ``named_agents`` counter is the number of rows, which is what keeps the
    daemon alive for an agent nobody is currently talking to (plan §2.6).

Like :class:`~snowpea_core.gateway.router.BindingStore`, the table gets its own
connection: restoring runs before the session store is interesting, and this
story adds no method to a file another story is editing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.definition import (
    PROJECT_DIRS,
    AgentDefinition,
    DefinitionError,
    parse_agent_md,
    validate_name,
)
from snowpea_core.config.paths import utc_now
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    AgentBindChannelParams,
    AgentDeleteParams,
    AgentInfo,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.named")

#: Permission modes a definition may pin; anything else means "inherit".
MODES: tuple[str, ...] = ("plan", "accept", "auto")

SCHEMA = """
CREATE TABLE IF NOT EXISTS named_agents (
    name             TEXT PRIMARY KEY,
    definition_path  TEXT,
    session_id       TEXT NOT NULL,
    memory_namespace TEXT NOT NULL,
    channel_bindings TEXT NOT NULL DEFAULT '[]',
    schedule_ids     TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL
);
"""


#: Aliases so the annotations below still mean the builtin ``list`` even though
#: :class:`NamedAgentRegistry` defines a method called ``list``.
JobIds = list[str]
NamedAgents = list["NamedAgent"]


class NamedAgentError(ValueError):
    """A named agent could not be created, bound or deleted."""


def namespace_for(name: str) -> str:
    """The memory namespace of a named agent (M5 contract §1)."""
    return f"agent:{name}"



def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


@dataclass
class NamedAgent:
    """One row of ``named_agents``: the persistent instance of a definition."""

    name: str
    session_id: str
    memory_namespace: str = ""
    definition_path: str | None = None
    #: ``[{"channel": "telegram:111", "bindingId": "gw-…", "credentialsRef": …}]``
    channel_bindings: list[dict[str, str]] = field(default_factory=list)
    schedule_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    #: Filled in at runtime from the definition file, when there is one.
    description: str = ""

    def __post_init__(self) -> None:
        if not self.memory_namespace:
            self.memory_namespace = namespace_for(self.name)

    @property
    def channels(self) -> list[str]:
        return [str(entry.get("channel", "")) for entry in self.channel_bindings]

    @property
    def binding_ids(self) -> list[str]:
        return [str(entry.get("bindingId", "")) for entry in self.channel_bindings]

    def row(self) -> tuple[Any, ...]:
        return (
            self.name,
            self.definition_path,
            self.session_id,
            self.memory_namespace,
            json.dumps(self.channel_bindings),
            json.dumps(self.schedule_ids),
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> NamedAgent:
        bindings = [
            entry for entry in _json_list(row["channel_bindings"]) if isinstance(entry, dict)
        ]
        return cls(
            name=row["name"],
            session_id=row["session_id"],
            memory_namespace=row["memory_namespace"],
            definition_path=row["definition_path"],
            channel_bindings=[{str(k): str(v) for k, v in entry.items()} for entry in bindings],
            schedule_ids=[str(item) for item in _json_list(row["schedule_ids"])],
            created_at=row["created_at"],
        )


class NamedAgentStore:
    """The ``named_agents`` table in ``$SNOWPEA_HOME/state.db``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def upsert(self, agent: NamedAgent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO named_agents"
                " (name, definition_path, session_id, memory_namespace, channel_bindings,"
                "  schedule_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                agent.row(),
            )
            self._conn.commit()

    def delete(self, name: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM named_agents WHERE name = ?", (name,))
            self._conn.commit()

    def all(self) -> list[NamedAgent]:
        with self._lock:
            rows = list(self._conn.execute("SELECT * FROM named_agents ORDER BY created_at"))
        return [NamedAgent.from_row(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            rows = list(self._conn.execute("SELECT COUNT(*) AS n FROM named_agents"))
        return int(rows[0]["n"]) if rows else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class NamedAgentRegistry:
    """Creates, restores and deletes the daemon's named agents."""

    def __init__(self, core: Core | None = None) -> None:
        self.core = core
        self._agents: dict[str, NamedAgent] = {}
        self._store: NamedAgentStore | None = None
        if core is not None:
            self.bind_core(core)

    # -- wiring --------------------------------------------------------
    def bind_core(self, core: Core) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.core = core
        self._store = NamedAgentStore(core.paths.state_db)

    def _require_core(self) -> Core:
        if self.core is None:
            raise NamedAgentError("the named agent registry is not wired to a daemon")
        return self.core

    def _require_store(self) -> NamedAgentStore:
        if self._store is None:
            raise NamedAgentError("the named agent registry is not wired to a home directory")
        return self._store

    # -- reads ---------------------------------------------------------
    def list(self) -> NamedAgents:
        return list(self._agents.values())

    def get(self, name: str) -> NamedAgent | None:
        return self._agents.get(name)

    def _require(self, name: str) -> NamedAgent:
        agent = self._agents.get(name)
        if agent is None:
            raise NamedAgentError(f"no named agent {name!r}")
        return agent

    def session_of(self, name: str) -> Session | None:
        """The live session of ``name``, or ``None`` when it is gone."""
        agent = self._agents.get(name)
        if agent is None or self.core is None:
            return None
        return self.core.sessions.get(agent.session_id)

    # -- create --------------------------------------------------------
    async def create(
        self,
        name: str,
        definition: AgentDefinition | None = None,
        *,
        workdir: Path | str | None = None,
    ) -> NamedAgent:
        """Register ``name`` as a persistent instance and open its session."""
        try:
            slug = validate_name(name)
        except DefinitionError as exc:
            raise NamedAgentError(str(exc)) from exc
        if slug in self._agents:
            raise NamedAgentError(f"a named agent called {slug!r} already exists")
        session = await self._open_session(slug, definition, workdir=workdir)
        agent = NamedAgent(
            name=slug,
            session_id=session.id,
            memory_namespace=namespace_for(slug),
            definition_path=str(definition.path) if definition and definition.path else None,
            description=definition.description if definition else "",
        )
        self._agents[slug] = agent
        self._require_store().upsert(agent)
        self._count()
        log.info(
            "named agent %s created (session %s, %s)", slug, session.id, agent.memory_namespace
        )
        return agent

    async def _open_session(
        self,
        name: str,
        definition: AgentDefinition | None,
        *,
        workdir: Path | str | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Open the agent's unattended session, keeping ``session_id`` if given."""
        core = self._require_core()
        resolved = Path(workdir) if workdir else self._workdir_for(definition)
        mode = None
        if definition is not None and definition.permission in MODES:
            mode = definition.permission
        definition_model = (
            definition.model
            if definition is not None and definition.model and definition.model != "inherit"
            else None
        )
        session = await core.sessions.create(
            workdir=resolved,
            mode=mode,  # type: ignore[arg-type]
            agent=name,
            definition_model=definition_model,
            origin_surface=namespace_for(name),
            origin_conn=None,
            session_id=session_id,
        )
        session.unattended = True
        session.memory_namespace = namespace_for(name)
        if definition is not None and definition.prompt.strip():
            session.system_prompt = definition.prompt
        core.lifecycle.set_counter("sessions", len(core.sessions))
        return session

    def _workdir_for(self, definition: AgentDefinition | None) -> Path:
        """The project the definition belongs to, else ``$SNOWPEA_HOME``.

        ``settings.json`` has no default working directory, so a definition at
        ``<project>/.snowpea/agents/<name>.md`` gives ``<project>``.
        """
        core = self._require_core()
        if definition is not None and definition.path is not None:
            path = Path(definition.path)
            for relative in PROJECT_DIRS:
                parts = Path(relative).parts
                if path.parent.parts[-len(parts) :] == parts and len(path.parents) > len(parts):
                    return path.parents[len(parts)]
            return path.parent
        return Path(core.paths.home)

    def _definition_of(self, agent: NamedAgent) -> AgentDefinition | None:
        if not agent.definition_path:
            return None
        path = Path(agent.definition_path)
        if not path.is_file():
            return None
        try:
            return parse_agent_md(path)
        except DefinitionError:  # pragma: no cover - a broken file on disk
            log.warning("named agent %s has an unreadable definition at %s", agent.name, path)
            return None

    # -- channels ------------------------------------------------------
    async def bind_channel(
        self, name: str, channel: str, *, credentials_ref: str | None = None
    ) -> str:
        """Route ``"<platform>:<channel_id>"`` to this agent; returns the binding id."""
        core = self._require_core()
        agent = self._require(name)
        platform, _, channel_id = channel.partition(":")
        if not platform or not channel_id:
            raise NamedAgentError(
                f"channel {channel!r} must look like '<platform>:<channel id>', e.g. telegram:123"
            )
        if core.gateway is None:
            raise NamedAgentError("the gateway router is not running")
        await self._ensure_session(agent)
        reference = credentials_ref or platform
        binding = await core.gateway.bind(
            platform,
            reference,
            {"agent": agent.name, "session": agent.session_id},
            channel_id=channel_id,
        )
        agent.channel_bindings = [
            entry for entry in agent.channel_bindings if entry.get("channel") != channel
        ]
        agent.channel_bindings.append(
            {"channel": channel, "bindingId": binding.id, "credentialsRef": reference}
        )
        self._require_store().upsert(agent)
        log.info("named agent %s now answers %s (binding %s)", agent.name, channel, binding.id)
        return str(binding.id)

    async def _ensure_session(self, agent: NamedAgent) -> Session:
        """Re-open the agent's session if something closed it."""
        core = self._require_core()
        session = core.sessions.get(agent.session_id)
        if session is not None and session.closed_at is None:
            return session
        session = await self._open_session(
            agent.name,
            self._definition_of(agent),
            session_id=agent.session_id,
        )
        agent.session_id = session.id
        self._require_store().upsert(agent)
        return session

    # -- jobs ----------------------------------------------------------
    async def job_ids(self, name: str) -> JobIds:
        """Ids of the enabled scheduled jobs that run as ``name``."""
        core = self.core
        scheduler = getattr(core, "scheduler", None) if core is not None else None
        if scheduler is None:
            return []
        try:
            jobs = await scheduler.list()
        except Exception:  # noqa: BLE001 - a broken scheduler must not break a listing
            log.debug("could not list jobs for named agent %s", name, exc_info=True)
            return []
        return [job.id for job in jobs if job.agent == name and job.enabled]

    async def refresh_jobs(self, name: str) -> JobIds:
        """Re-read the agent's jobs from the scheduler and persist the ids."""
        agent = self._agents.get(name)
        if agent is None:
            return []
        agent.schedule_ids = await self.job_ids(name)
        self._require_store().upsert(agent)
        return list(agent.schedule_ids)

    # -- restore / delete ----------------------------------------------
    async def restore(self) -> int:
        """Re-open every persisted agent's session, keeping its id (AC-17).

        Runs *before* :meth:`~snowpea_core.gateway.router.GatewayRouter.restore`
        so a binding that targets one of these sessions finds it already open.
        """
        store = self._require_store()
        restored = 0
        for agent in store.all():
            definition = self._definition_of(agent)
            agent.description = definition.description if definition else ""
            try:
                session = await self._open_session(
                    agent.name, definition, session_id=agent.session_id
                )
            except Exception:  # noqa: BLE001 - one bad row must not stop the daemon
                log.exception("could not restore named agent %s", agent.name)
                continue
            agent.session_id = session.id
            self._agents[agent.name] = agent
            agent.schedule_ids = await self.job_ids(agent.name)
            store.upsert(agent)
            restored += 1
        self._count()
        if restored:
            log.info("restored %s named agent(s)", restored)
        return restored

    async def delete(self, name: str) -> bool:
        """Unbind the channels, cancel the jobs, close the session, drop the row."""
        agent = self._agents.pop(name, None)
        if agent is None:
            return False
        core = self.core
        if core is not None and core.gateway is not None:
            for binding_id in agent.binding_ids:
                with contextlib.suppress(Exception):
                    await core.gateway.unbind(binding_id)
        scheduler = getattr(core, "scheduler", None) if core is not None else None
        if scheduler is not None:
            for job_id in await self.job_ids(name):
                with contextlib.suppress(Exception):
                    await scheduler.cancel(job_id)
        if core is not None:
            with contextlib.suppress(Exception):
                await core.sessions.close(agent.session_id)
            core.lifecycle.set_counter("sessions", len(core.sessions))
        self._require_store().delete(name)
        self._count()
        log.info("named agent %s deleted", name)
        return True

    # -- bookkeeping ---------------------------------------------------
    def _count(self) -> None:
        core = self.core
        if core is not None:
            core.lifecycle.set_counter("named_agents", len(self._agents))

    def close(self) -> None:
        if self._store is not None:
            with contextlib.suppress(Exception):
                self._store.close()
            self._store = None


#: ``AgentInfo.kind`` for a persistent instance (M7 contract §6).
NAMED_KIND = "named"


def registry(core: Core) -> NamedAgentRegistry:
    """The daemon's registry, built on first use so any ``Core`` has one."""
    existing = getattr(core, "named_agents", None)
    if existing is None:
        existing = NamedAgentRegistry(core)
        core.named_agents = existing
    return existing  # type: ignore[no-any-return]


def session_for_job(core: Core, agent_name: str | None) -> Session | None:
    """The named agent's session a job should run in, when it names one."""
    if not agent_name:
        return None
    existing = getattr(core, "named_agents", None)
    if existing is None:
        return None
    session = existing.session_of(agent_name)
    if session is None or session.closed_at is not None:
        return None
    return session  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# RPC: the ``kind:"named"`` half of agent.list, agent.bindChannel, agent.delete
# ---------------------------------------------------------------------------


async def list_infos(core: Core) -> list[AgentInfo]:
    """The ``kind:"named"`` entries ``agent.list`` appends to its definitions."""
    entries = registry(core)
    out: list[AgentInfo] = []
    for agent in entries.list():
        jobs = await entries.job_ids(agent.name)
        channels = [channel for channel in agent.channels if channel]
        out.append(
            AgentInfo(
                name=agent.name,
                description=agent.description,
                kind=NAMED_KIND,
                source="named",
                path=agent.definition_path,
                channel=channels[0] if channels else None,
                channels=channels,
                bindings=[binding for binding in agent.binding_ids if binding],
                jobs=jobs,
                sessionId=agent.session_id,
                namespace=agent.memory_namespace,
            )
        )
    return out


async def rename_definition(
    core: Core, session: Session, defn: AgentDefinition, name: str
) -> AgentDefinition:
    """Rewrite a freshly generated definition under the caller's chosen name."""
    from snowpea_core.agent.definition import write_definition
    from snowpea_core.commands.agent_cmd import register_and_reload

    try:
        slug = validate_name(name)
    except DefinitionError as exc:
        raise NamedAgentError(str(exc)) from exc
    if slug == defn.name:
        return defn
    previous = defn.path
    defn.name = slug
    path = write_definition(defn, session.workdir)
    await register_and_reload(core, path)
    if previous is not None and Path(previous) != path:
        with contextlib.suppress(OSError):
            Path(previous).unlink()
    return defn


async def agent_bind_channel_handler(
    _conn: RpcConnection, params: AgentBindChannelParams, core: Core
) -> Ok:
    """``agent.bindChannel`` — route a gateway channel to a named agent."""
    try:
        await registry(core).bind_channel(
            params.name, params.channel, credentials_ref=params.credentialsRef
        )
    except NamedAgentError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad credential or dead platform
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return Ok(ok=True)


async def agent_delete_handler(_conn: RpcConnection, params: AgentDeleteParams, core: Core) -> Ok:
    """``agent.delete`` — unbind, cancel, close and forget a named agent."""
    if not await registry(core).delete(params.name):
        raise RpcError(errors.NOT_FOUND, f"no named agent {params.name}")
    return Ok(ok=True)


__all__ = [
    "MODES",
    "JobIds",
    "NamedAgents",
    "NAMED_KIND",
    "SCHEMA",
    "agent_bind_channel_handler",
    "agent_delete_handler",
    "list_infos",
    "rename_definition",
    "NamedAgent",
    "NamedAgentError",
    "NamedAgentRegistry",
    "NamedAgentStore",
    "namespace_for",
    "registry",
    "session_for_job",
]
