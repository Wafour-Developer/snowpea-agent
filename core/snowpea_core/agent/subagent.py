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
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.definition import AgentDefinition
from snowpea_core.config.model_routing import ModelRoute, model_config_for, resolve_reference
from snowpea_core.config.settings import THINKING_CHOICES
from snowpea_core.prompts.loader import PromptNotFound, load
from snowpea_core.server.protocol import AgentInfo
from snowpea_core.session import events
from snowpea_core.tools.registry import ProgressEmitter

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

#: Appended to a summary the child could not finish inside the output budget.
#: A caller reading a review has to be told it is incomplete, or it reads as a
#: short review rather than a cut-off one (CORE-reasoning-budget).
TRUNCATED_MARK = "[truncated at max_tokens after {count} continuations]"

#: Attribute the manager is cached under on :class:`Core`.
CORE_ATTR = "_subagents"

#: Tool calls kept from the end of a child's run.  A parent deciding what to do
#: with a child that ran out of budget (or time) needs to see where it was, not
#: a transcript (CORE-subagent-budget).
LAST_CALLS = 3

#: Characters of one remembered call's arguments.
CALL_ARG_CHARS = 120

#: Why the child's turn ended, as reported to the caller.  Mirrors
#: ``turn.done.reason`` with ``"complete"`` as the default.
COMPLETE = "complete"
BUDGET = "budget"
TIMEOUT = "timeout"

#: Appended to every brief so the child knows what it has to spend.
BUDGET_LINE = (
    "You have {n} tool rounds for this task. Leave enough of them to write your "
    "report: if you run out, the report is written for you and the work stops."
)


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
    #: One-line, user-language label the caller wrote for this delegation
    #: (``delegate_task(title=…)``); ``""`` when it did not write one, and the
    #: surface falls back to its own wording.
    title: str = ""
    status: str = QUEUED
    session_id: str | None = None
    summary: str = ""
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: Why the child's turn ended: ``complete`` | ``budget`` | ``error`` |
    #: ``timeout`` | ``interrupted`` | ``denied`` (CORE-subagent-budget).
    reason: str = ""
    #: Tool rounds the child's turn used, as the loop counted them.
    rounds_used: int = 0
    #: The last :data:`LAST_CALLS` tool calls the child made, newest last.
    last_calls: list[str] = field(default_factory=list)
    #: True when the child's last answer still ended at the output limit.
    truncated: bool = False
    #: How many times that answer was resumed before it was given up on.
    continuations: int = 0
    #: Caller's explicit model override for this one delegation, resolved from
    #: ``delegate_task(model=…)`` / ``agent.spawn(model=…)``.  Highest rung of
    #: the precedence chain (CORE-model-assignment).
    provider_override: str | None = None
    model_override: str | None = None
    #: Where this child's progress is republished as ``tool.progress`` on the
    #: delegating tool call; set by ``delegate_task`` when a surface is
    #: listening, ``None`` otherwise (IDE-PROGRESS D2).
    progress: ProgressEmitter | None = field(default=None, repr=False, compare=False)

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
    #: ``complete`` | ``budget`` | ``error`` | ``timeout`` | ``interrupted``.
    reason: str = COMPLETE
    #: Tool rounds the child used, and the last few calls it made.
    rounds_used: int = 0
    last_calls: list[str] = field(default_factory=list)


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
            record.truncated = bool(payload.get("truncated"))
            record.continuations = int(payload.get("continuations") or 0)
            if not text:
                return
            record.summary = text
            await self._manager.emit_update(record, last_text=text)
            return
        if kind == "turn.done":
            record.reason = str(payload.get("reason") or COMPLETE)
            return
        if kind == "tool.call":
            # Whatever the child said before reaching for a tool was not its
            # answer — the turn went on after it.  Anything kept from an
            # earlier ``message.done`` is dropped here, so the summary can
            # only ever be text that nothing followed (CORE-subagent-budget).
            record.summary = ""
            record.last_calls.append(_call_summary(payload))
            del record.last_calls[:-LAST_CALLS]
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
                    "title": record.title,
                    "status": record.status,
                    "sessionId": record.session_id,
                },
            ),
        )

    async def emit_update(self, record: SubagentRecord, *, last_text: str = "") -> None:
        # A delegation is a tool call that can run for minutes; the child's own
        # last line is the only progress it has (IDE-PROGRESS D2).  Advisory,
        # like every ``tool.progress``: the delegation's ``tool.result`` still
        # carries the answer.
        if record.progress is not None and last_text:
            await record.progress.emit("stdout", last_text)
        await self._emit(
            record.parent_session_id,
            (
                "subagent.update",
                {
                    "agentId": record.agent_id,
                    "status": record.status,
                    "title": record.title,
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
                    "title": record.title,
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
    def new_record(
        self, parent: Session, task: str, agent: str | None, title: str = ""
    ) -> SubagentRecord:
        """Register a queued record; its id is what ``agent.spawn`` answers."""
        record = SubagentRecord(
            agent_id=new_agent_id(),
            name=agent or "",
            task=(task or "").strip(),
            parent_session_id=parent.id,
            title=(title or "").strip(),
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
        model: str | None = None,
        title: str = "",
    ) -> tuple[str, asyncio.Task[SubagentResult]]:
        """Start a subagent in the background; returns its id and its task.

        ``agent.spawn`` answers the moment the id exists, because a client wants
        the correlation id now and the events later.
        """
        record = self.new_record(parent, task, agent, title)
        runner = asyncio.ensure_future(
            self.run(
                parent,
                task,
                agent=agent,
                tools=tools,
                timeout=timeout,
                record=record,
                model=model,
                title=title,
            )
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
        model: str | None = None,
        title: str = "",
        progress: ProgressEmitter | None = None,
    ) -> SubagentResult:
        """Delegate ``task`` to a child session and return its final answer.

        ``model`` is a one-shot override — a profile id, ``vendor:model`` or a
        bare vendor — and outranks everything else for this delegation only
        (CORE-model-assignment).  An unresolvable reference is refused rather
        than silently ignored: the caller asked for a specific model.
        """
        brief = (task or "").strip()
        if record is None:
            record = self.new_record(parent, task, agent, title)
        if progress is not None:
            record.progress = progress
        if model:
            route = resolve_reference(
                self.core.settings,
                model,
                config=model_config_for(self.core.settings, parent.workdir),
            )
            if not route.resolved():
                record.status = ERROR
                record.error = (
                    f"unknown model {model!r}: not a profile id, a 'vendor:model' pair "
                    "or a known vendor"
                )
                record.reason = ERROR
                await self.emit_spawn(record)
                await self.emit_done(record)
                return self._result(record)
            record.provider_override, record.model_override = route.provider, route.model
        if parent.team_agents and not agent:
            agent = "executor" if "executor" in parent.team_agents else parent.team_agents[0]
            record.name = agent
        await self.emit_spawn(record)
        if not brief:
            record.status = ERROR
            record.error = "delegate_task needs a non-empty task"
            record.reason = ERROR
            await self.emit_done(record)
            return self._result(record)

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
                record.reason = "interrupted"
                await self.emit_done(record)
                raise
            except Exception as exc:  # noqa: BLE001 - a broken child is a failed task
                log.exception("subagent %s failed", record.agent_id)
                record.status = ERROR
                record.error = f"{type(exc).__name__}: {exc}"
                record.reason = ERROR

        if record.status == RUNNING:
            record.status = DONE
        await self.emit_done(record)
        return self._result(record)

    def _result(self, record: SubagentRecord) -> SubagentResult:
        """One place that turns a record into what the caller reads."""
        reason = record.reason or (COMPLETE if record.ok else ERROR)
        return SubagentResult(
            agent_id=record.agent_id,
            ok=record.ok,
            summary=record.summary,
            status=record.status,
            error=record.error,
            session_id=record.session_id,
            usage=record.usage(),
            reason=reason,
            rounds_used=record.rounds_used,
            last_calls=list(record.last_calls),
        )

    async def _refuse(self, record: SubagentRecord, message: str) -> SubagentResult:
        record.status = ERROR
        record.error = message
        record.reason = ERROR
        await self.emit_done(record)
        return self._result(record)

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
            # The child is told its own budget, because it is the one that has
            # to spend it: a worker that knows it has N rounds reads what it
            # needs and reports, instead of being cut off mid-survey
            # (CORE-subagent-budget).
            rounds = agent_loop.tool_rounds_for(self.core, child)
            brief = f"{task}\n\n{BUDGET_LINE.format(n=rounds)}"
            coro = agent_loop.run_turn(self.core, child, brief, unattended=child.unattended)
            if timeout and timeout > 0:
                await asyncio.wait_for(coro, timeout=timeout)
            else:
                await coro
            record.rounds_used = int(getattr(child, "rounds_used", 0) or 0)
            if not record.summary:
                record.summary = _last_assistant_text(child)
            if record.truncated:
                record.summary = (
                    f"{record.summary}\n\n"
                    f"{TRUNCATED_MARK.format(count=record.continuations)}"
                ).strip()
        except TimeoutError:
            child.interrupt.set()
            record.status = ERROR
            record.reason = TIMEOUT
            record.rounds_used = int(getattr(child, "rounds_used", 0) or 0)
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
        # ``route_for`` owns the whole precedence chain — there is no second
        # copy of it here any more.  The old bypass resolved a definition's
        # ``model:`` through ``_split_model``, which never consulted
        # ``models.profiles``, so a *valid profile id* in an agent .md was read
        # as a vendor name and the child died at its first turn with
        # "unknown provider vendor: fast" (CORE-model-assignment B-P1-1).
        #
        # The parent's own route is passed as the session pin: it is what the
        # child inherits when neither an explicit override, an assignment, a
        # definition nor a default has anything to say.
        provider, model = (record.provider_override, record.model_override)
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
            session_pin=ModelRoute(parent.provider, parent.model),
            parent_session_id=parent.id,
            kind="subagent",
        )
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
            # ``tool_rounds:`` in the definition outranks ``agents.toolRounds``
            # for this child only (CORE-subagent-budget).
            if defn.tool_rounds:
                child.tool_rounds = defn.tool_rounds
            # A delegated turn does not think by default — its report is the
            # whole output — unless the definition asks for it by name.
            if defn.thinking in THINKING_CHOICES:
                child.thinking = defn.thinking
            from_defn = defn.tool_list()
            if from_defn is not None:
                allowed = set(from_defn)
            if defn.prompt.strip():
                child.system_prompt = defn.prompt.strip()
        if tools:
            explicit = {str(name) for name in tools if str(name).strip()}
            allowed = explicit if allowed is None else (allowed & explicit)
        child.allowed_tools = allowed


def _call_summary(payload: dict[str, Any]) -> str:
    """``"read_file {\"path\": \"x\"}"`` — one short line per remembered call."""
    name = str(payload.get("name") or "a tool")
    args = payload.get("args") or payload.get("arguments") or {}
    try:
        rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(args)
    if len(rendered) > CALL_ARG_CHARS:
        rendered = rendered[:CALL_ARG_CHARS] + "…"
    return f"{name} {rendered}".strip()


def _last_assistant_text(session: Session) -> str:
    """The child's final answer, or ``""`` when it never wrote one.

    An assistant message that carries ``tool_calls`` is the prose the model
    wrote *on its way* to a tool — "let me check the tests first" — and the
    turn continued after it.  Returning that as the delegation's summary is
    what made a parent read "That grep swept node_modules…" as a report
    (CORE-subagent-budget).  The scan therefore stops at the first assistant
    message from the end: if it reached for a tool, there is no final answer
    and the caller must say so rather than quote the muttering.
    """
    for message in reversed(session.history.snapshot()):
        if message.role != "assistant":
            continue
        if getattr(message, "tool_calls", None):
            return ""
        if isinstance(message.content, str) and message.content.strip():
            return message.content
        return ""
    return ""


def get_manager(core: Core) -> SubagentManager:
    """The daemon's one :class:`SubagentManager`, created on first use."""
    manager = getattr(core, CORE_ATTR, None)
    if manager is None:
        manager = SubagentManager(core)
        setattr(core, CORE_ATTR, manager)
    return manager


__all__ = [
    "BUDGET",
    "BUDGET_LINE",
    "COMPLETE",
    "DONE",
    "ERROR",
    "TIMEOUT",
    "QUEUED",
    "RUNNING",
    "SUBAGENT_KIND",
    "TRUNCATED_MARK",
    "SharedBackend",
    "SubagentManager",
    "SubagentRecord",
    "SubagentResult",
    "get_manager",
    "new_agent_id",
]
