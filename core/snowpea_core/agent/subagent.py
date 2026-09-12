"""Subagents: one delegated task, one child session (M7 contract §3).

A subagent is an ordinary agent turn that runs in its own :class:`Session`.
The child inherits the parent's workdir, mode, provider, execution backend and
origin surface; an :class:`~snowpea_core.agent.definition.AgentDefinition` may
override the model, the tool set, the permission mode and the system prompt.

Concurrency is a semaphore per parent session sized from the resolved
``agents.max_concurrent`` (global settings < project settings <
``session.create(maxConcurrent)``), so a parent that delegates four tasks with
a limit of two never has more than two running at once.

Three events land on the **parent** session, which is what a TUI renders::

    subagent.spawn  {agentId, name, task, status}
    subagent.update {agentId, status, lastText}
    subagent.done   {agentId, status, summary, usage}

The child's own ``message.delta``, ``tool.call`` and ``turn.done`` events keep
flowing on the child session id, so a client that subscribes to everything sees
the whole tree.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.definition import AgentDefinition
from snowpea_core.prompts.loader import PromptNotFound, load
from snowpea_core.server.protocol import AgentInfo
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.subagent")


def role_file(name: str | None) -> str | None:
    """``name`` when ``prompts/roles/<name>.md`` exists, else ``None``.

    A project may ship its own role file and shadow the built-in one; a
    definition whose name matches no role simply composes without one.
    """
    if not name:
        return None
    try:
        load(f"roles/{name}")
    except PromptNotFound:
        return None
    return name


#: Statuses a record moves through, in order.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"

#: Kind reported by ``agent.list`` for a delegated child.
SUBAGENT_KIND = "subagent"

#: Records kept per parent session so ``agent.list`` stays bounded.
MAX_RECORDS = 200

#: Attribute the manager is cached under on :class:`Core`.
CORE_ATTR = "_subagents"


def new_agent_id() -> str:
    return f"a-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# backend sharing
# ---------------------------------------------------------------------------


class SharedBackend:
    """The parent's backend, minus the right to close it.

    A child session must run where its parent runs (``/backend docker`` has to
    move the whole tree, AC-18), but closing the child must not tear down a
    container the parent is still using.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def kind(self) -> str:
        return str(self._inner.kind)

    @property
    def cwd(self) -> Any:
        return self._inner.cwd

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def close(self) -> None:
        """No-op: the parent owns the real backend."""
        return None


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass
class SubagentRecord:
    """One delegated run, from ``queued`` to ``done``/``error``."""

    agent_id: str
    name: str
    task: str
    parent_session_id: str
    status: str = QUEUED
    session_id: str | None = None
    summary: str = ""
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.status == DONE

    def usage(self) -> dict[str, int]:
        return {"inputTokens": self.input_tokens, "outputTokens": self.output_tokens}

    def info(self) -> AgentInfo:
        return AgentInfo(
            name=self.name or self.agent_id,
            description=self.task,
            kind=SUBAGENT_KIND,
            source="subagent",
            status=self.status,
            task=self.task,
            agentId=self.agent_id,
            sessionId=self.session_id,
            parentSessionId=self.parent_session_id,
        )


@dataclass
class SubagentResult:
    """What :meth:`SubagentManager.run` hands back to its caller."""

    agent_id: str
    ok: bool
    summary: str
    status: str = DONE
    error: str | None = None
    session_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# the child's event watcher
# ---------------------------------------------------------------------------


class _ChildWatcher:
    """A pseudo-connection subscribed to the child session.

    It exists so the parent learns what the child is doing without the agent
    loop knowing anything about subagents: it counts usage, keeps the last text
    and republishes progress as ``subagent.update`` on the parent.
    """

    closed = False

    def __init__(self, manager: SubagentManager, record: SubagentRecord) -> None:
        self._manager = manager
        self._record = record

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method != "session.event":
            return
        kind = params.get("kind")
        payload = params.get("payload") or {}
        record = self._record
        if kind == "usage":
            record.input_tokens += int(payload.get("inputTokens") or 0)
            record.output_tokens += int(payload.get("outputTokens") or 0)
            return
        if kind == "message.done":
            text = str(payload.get("text") or "")
            if not text:
                return
            record.summary = text
            await self._manager.emit_update(record, last_text=text)
            return
        if kind == "tool.call":
            await self._manager.emit_update(
                record, last_text=f"calling {payload.get('name') or 'a tool'}"
            )


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class SubagentManager:
    """Spawns child agent runs under a per-parent concurrency limit."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self._records: dict[str, SubagentRecord] = {}
        self._order: list[str] = []
        self._semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}

    # -- concurrency ---------------------------------------------------
    def limit_for(self, parent: Session) -> int:
        """Resolved ``agents.max_concurrent`` for this parent session."""
        configured = getattr(parent, "max_concurrent", None)
        if configured:
            return max(1, int(configured))
        return max(1, self.core.sessions.max_concurrent(parent.workdir))

    def semaphore_for(self, parent: Session) -> asyncio.Semaphore:
        """One semaphore per parent, rebuilt if the limit changed."""
        limit = self.limit_for(parent)
        current = self._semaphores.get(parent.id)
        if current is None or current[0] != limit:
            current = (limit, asyncio.Semaphore(limit))
            self._semaphores[parent.id] = current
        return current[1]

    # -- records -------------------------------------------------------
    def _remember(self, record: SubagentRecord) -> None:
        self._records[record.agent_id] = record
        self._order.append(record.agent_id)
        while len(self._order) > MAX_RECORDS:
            self._records.pop(self._order.pop(0), None)

    def records(self) -> list[SubagentRecord]:
        return [self._records[key] for key in self._order if key in self._records]

    def active(self) -> list[SubagentRecord]:
        """Queued and running children — what ``agent.list`` reports."""
        return [record for record in self.records() if record.status in (QUEUED, RUNNING)]

    def infos(self, *, include_finished: bool = False) -> list[AgentInfo]:
        source = self.records() if include_finished else self.active()
        return [record.info() for record in source]

    def get(self, agent_id: str) -> SubagentRecord | None:
        return self._records.get(agent_id)

    # -- events --------------------------------------------------------
    async def _emit(self, parent_id: str, event: events.Event) -> None:
        try:
            await self.core.hub.emit_event(parent_id, event)
        except Exception:  # noqa: BLE001 - a dead parent must not kill the child
            log.debug("could not emit %s on %s", event[0], parent_id, exc_info=True)

    async def emit_spawn(self, record: SubagentRecord) -> None:
        await self._emit(
            record.parent_session_id,
            (
                "subagent.spawn",
                {
                    "agentId": record.agent_id,
                    "name": record.name,
                    "task": record.task,
                    "status": record.status,
                    "sessionId": record.session_id,
                },
            ),
        )

    async def emit_update(self, record: SubagentRecord, *, last_text: str = "") -> None:
        await self._emit(
            record.parent_session_id,
            (
                "subagent.update",
                {
                    "agentId": record.agent_id,
                    "status": record.status,
                    "text": last_text,
                    "lastText": last_text,
                    "name": record.name,
                    "sessionId": record.session_id,
                },
            ),
        )

    async def emit_done(self, record: SubagentRecord) -> None:
        await self._emit(
            record.parent_session_id,
            (
                "subagent.done",
                {
                    "agentId": record.agent_id,
                    "ok": record.ok,
                    "result": record.summary,
                    "status": record.status,
                    "summary": record.summary or (record.error or ""),
                    "usage": record.usage(),
                    "name": record.name,
                    "sessionId": record.session_id,
                },
            ),
        )

    # -- definitions ---------------------------------------------------
    def definition(self, parent: Session, name: str | None) -> AgentDefinition | None:
        """Look up ``name`` among the definitions visible from the parent."""
        if not name:
            return None
        from snowpea_core.commands.agent_cmd import definitions_for

        for defn in definitions_for(self.core, parent.workdir):
            if defn.name == name:
                return defn
        return None

    # -- the run --------------------------------------------------------
    def new_record(self, parent: Session, task: str, agent: str | None) -> SubagentRecord:
        """Register a queued record; its id is what ``agent.spawn`` answers."""
        record = SubagentRecord(
            agent_id=new_agent_id(),
            name=agent or "",
            task=(task or "").strip(),
            parent_session_id=parent.id,
        )
        self._remember(record)
        return record

    def spawn(
        self,
        parent: Session,
        task: str,
        *,
        agent: str | None = None,
        tools: list[str] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, asyncio.Task[SubagentResult]]:
        """Start a subagent in the background; returns its id and its task.

        ``agent.spawn`` answers the moment the id exists, because a client wants
        the correlation id now and the events later.
        """
        record = self.new_record(parent, task, agent)
        runner = asyncio.ensure_future(
            self.run(parent, task, agent=agent, tools=tools, timeout=timeout, record=record)
        )
        return record.agent_id, runner

    async def run(
        self,
        parent: Session,
        task: str,
        *,
        agent: str | None = None,
        tools: list[str] | None = None,
        timeout: float | None = None,
        record: SubagentRecord | None = None,
    ) -> SubagentResult:
        """Delegate ``task`` to a child session and return its final answer."""
        brief = (task or "").strip()
        if record is None:
            record = self.new_record(parent, task, agent)
        if parent.team_agents and not agent:
            agent = "executor" if "executor" in parent.team_agents else parent.team_agents[0]
            record.name = agent
        await self.emit_spawn(record)
        if not brief:
            record.status = ERROR
            record.error = "delegate_task needs a non-empty task"
            await self.emit_done(record)
            return SubagentResult(
                agent_id=record.agent_id,
                ok=False,
                summary="",
                status=ERROR,
                error=record.error,
                usage=record.usage(),
            )

        if parent.team_agents:
            if agent not in parent.team_agents:
                return await self._refuse(
                    record,
                    f"agent '{agent}' is not in active team '{parent.team}'; choose one of: "
                    + ", ".join(parent.team_agents),
                )
        defn = self.definition(parent, agent)
        if agent and defn is None:
            return await self._refuse(record, f"unknown agent '{agent}'")

        semaphore = self.semaphore_for(parent)
        async with semaphore:
            record.status = RUNNING
            await self.emit_update(record, last_text="started")
            try:
                await self._execute(parent, record, brief, defn, tools, timeout)
            except asyncio.CancelledError:
                record.status = ERROR
                record.error = "cancelled"
                await self.emit_done(record)
                raise
            except Exception as exc:  # noqa: BLE001 - a broken child is a failed task
                log.exception("subagent %s failed", record.agent_id)
                record.status = ERROR
                record.error = f"{type(exc).__name__}: {exc}"

        if record.status == RUNNING:
            record.status = DONE
        await self.emit_done(record)
        return SubagentResult(
            agent_id=record.agent_id,
            ok=record.ok,
            summary=record.summary,
            status=record.status,
            error=record.error,
            session_id=record.session_id,
            usage=record.usage(),
        )

    async def _refuse(self, record: SubagentRecord, message: str) -> SubagentResult:
        record.status = ERROR
        record.error = message
        await self.emit_done(record)
        return SubagentResult(
            agent_id=record.agent_id,
            ok=False,
            summary="",
            status=ERROR,
            error=message,
            session_id=record.session_id,
            usage=record.usage(),
        )

    async def _execute(
        self,
        parent: Session,
        record: SubagentRecord,
        task: str,
        defn: AgentDefinition | None,
        tools: list[str] | None,
        timeout: float | None,
    ) -> None:
        """Create the child session, run one turn, collect the answer."""
        from snowpea_core.agent import loop as agent_loop

        child = await self._child_session(parent, record, defn)
        record.session_id = child.id
        watcher = _ChildWatcher(self, record)
        self.core.hub.subscribe(watcher, child.id)
        try:
            self._apply_definition(child, defn, tools)
            coro = agent_loop.run_turn(self.core, child, task, unattended=child.unattended)
            if timeout and timeout > 0:
                await asyncio.wait_for(coro, timeout=timeout)
            else:
                await coro
            if not record.summary:
                record.summary = _last_assistant_text(child)
        except TimeoutError:
            child.interrupt.set()
            record.status = ERROR
            record.error = f"the subagent did not finish within {timeout:g}s"
        finally:
            self.core.hub.unsubscribe(watcher)
            try:
                await self.core.sessions.close(child.id)
            except Exception:  # noqa: BLE001 - closing is best effort
                log.debug("could not close child session %s", child.id, exc_info=True)

    async def _child_session(
        self, parent: Session, record: SubagentRecord, defn: AgentDefinition | None
    ) -> Session:
        """A fresh session that inherits the parent and obeys the definition."""
        mode = parent.mode
        if defn is not None and defn.permission not in ("", "inherit", None):
            mode = defn.permission  # type: ignore[assignment]
        definition_model = (
            defn.model if defn is not None and defn.model and defn.model != "inherit" else None
        )
        assigned = bool(record.name and record.name in self.core.settings.agents.models)
        has_model_routing = bool(self.core.settings.models.default or assigned)
        if has_model_routing or definition_model:
            provider, model = (None, None)
            if definition_model and not has_model_routing:
                provider, model = _split_model(definition_model, parent.provider)
                definition_model = None
        else:
            provider, model = parent.provider, parent.model
        child = await self.core.sessions.create(
            parent.workdir,
            mode=mode,
            provider=provider,
            model=model,
            agent=record.name or None,
            definition_model=definition_model,
            max_concurrent=self.limit_for(parent),
            origin_surface=parent.origin_surface,
            origin_conn=parent.origin_conn,
        )
        child.parent_session_id = parent.id
        child.unattended = parent.unattended
        child.memory_namespace = parent.memory_namespace
        child.backend = SharedBackend(parent.backend)  # type: ignore[assignment]
        return child

    def _apply_definition(
        self, child: Session, defn: AgentDefinition | None, tools: list[str] | None
    ) -> None:
        """Narrow the child's tools and give it its role and persona.

        Every child is marked a subagent, definition or not, so it always gets
        the subagent preamble — before CORE-prompts, ``/ralph``, ``/ultrawork``
        and ``/team`` workers ran on the bare base prompt with no report
        contract at all.  A definition's own prompt is composed *after* the role
        rather than replacing the rules.
        """
        child.is_subagent = True
        allowed: set[str] | None = None
        if defn is not None:
            child.prompt_role = role_file(defn.name)
            from_defn = defn.tool_list()
            if from_defn is not None:
                allowed = set(from_defn)
            if defn.prompt.strip():
                child.system_prompt = defn.prompt.strip()
        if tools:
            explicit = {str(name) for name in tools if str(name).strip()}
            allowed = explicit if allowed is None else (allowed & explicit)
        child.allowed_tools = allowed


def _split_model(value: str, fallback_vendor: str | None) -> tuple[str | None, str | None]:
    """``"anthropic:claude-sonnet-4"`` -> ``("anthropic", "claude-sonnet-4")``."""
    text = value.strip()
    if ":" in text:
        vendor, _, model = text.partition(":")
        return (vendor.strip() or fallback_vendor), (model.strip() or None)
    return (text or fallback_vendor), None


def _last_assistant_text(session: Session) -> str:
    for message in reversed(session.history.snapshot()):
        if message.role == "assistant" and isinstance(message.content, str) and message.content:
            return message.content
    return ""


def get_manager(core: Core) -> SubagentManager:
    """The daemon's one :class:`SubagentManager`, created on first use."""
    manager = getattr(core, CORE_ATTR, None)
    if manager is None:
        manager = SubagentManager(core)
        setattr(core, CORE_ATTR, manager)
    return manager


__all__ = [
    "DONE",
    "ERROR",
    "QUEUED",
    "RUNNING",
    "SUBAGENT_KIND",
    "SharedBackend",
    "SubagentManager",
    "SubagentRecord",
    "SubagentResult",
    "get_manager",
    "new_agent_id",
]
