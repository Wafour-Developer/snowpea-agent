"""The :class:`Session` entity (contract §4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.protocol import Mode, SessionKind, SessionSummary
from snowpea_core.session.history import History


@dataclass
class Session:
    """One conversation: a workdir, a mode, a message history and an event seq."""

    id: str
    workdir: Path
    mode: Mode = "accept"
    provider: str | None = None
    model: str | None = None
    agent: str | None = None
    team: str | None = None
    team_agents: tuple[str, ...] = ()
    origin_surface: str | None = None
    #: Set on a child session created by ``delegate_task`` / ``agent.spawn``
    #: (M7 contract §3); ``None`` for a session a human opened.
    parent_session_id: str | None = None
    #: What opened this session (CORE-session-kind): ``"chat"`` for a thread a
    #: human started, ``"scheduled"`` for a job run, ``"subagent"`` for a
    #: spawned child, ``"agent"`` for a persistent named agent's own session.
    kind: SessionKind = "chat"
    #: Scheduled job this session was opened to run; ``None`` otherwise.
    job_id: str | None = None
    #: An ``AgentDefinition``'s own prompt, composed after the role file so a
    #: subagent speaks with its definition's voice (M7 §3) *and* keeps the
    #: coding discipline every other agent has (CORE-prompts).
    system_prompt: str | None = None
    #: Name of a file in ``prompts/roles/`` to compose into a child's prompt;
    #: ``None`` when the definition has no matching built-in role.
    prompt_role: str | None = None
    #: True for a session a parent agent spawned: it gets the subagent preamble
    #: (no user is watching; the final message is the whole report).
    is_subagent: bool = False
    #: True once this turn's opening acknowledgement has been spoken, so
    #: only the first line before a tool call is read aloud.
    ack_spoken: bool = False
    #: ``"on"`` | ``"off"`` | ``"auto"`` for this session only — an agent
    #: definition's ``thinking:``.  ``None`` falls back to the vendor block and
    #: then ``agent.thinking`` (CORE-reasoning-budget).
    thinking: str | None = None
    #: ``"low"`` | ``"medium"`` | ``"high"`` | ``"max"`` for this session only
    #: — what ``/effort`` and ``session.setEffort`` pin.  ``None`` falls back
    #: to ``agent.effortBy`` and then ``agent.effort`` (CORE-effort).
    effort: str | None = None
    #: ``True``/``False`` for this session only — what ``/delegation on|off``
    #: pins.  ``None`` follows ``agents.delegateByDefault``.  On, the lead
    #: hands implementation, tests and verification to the team's agents
    #: instead of doing them itself.
    delegation: bool | None = None
    #: Tool-round budget for this session only — an agent definition's
    #: ``tool_rounds:``.  ``None`` falls back to ``agents.toolRounds`` and then
    #: to the default for the session's kind (CORE-subagent-budget).
    tool_rounds: int | None = None
    #: Tool rounds the current (or last) turn has used.  Reset at the top of
    #: every turn; read by ``subagent.py`` to report ``roundsUsed``.
    rounds_used: int = 0
    #: Long-term memory namespace (M5 contract §1): ``"default"`` for
    #: interactive sessions, ``"agent:<name>"`` for named agents.
    memory_namespace: str = "default"
    #: Project memory namespace (M5 contract §1b): ``"project:<realpath>"`` of
    #: the workdir's git root, or of the workdir itself when it is not a
    #: checkout.  ``""`` when the session is not in a project at all (its
    #: workdir is the user's home, or ``$SNOWPEA_HOME``), in which case there
    #: is nothing to scope a memory to and everything is global.  Derived once
    #: in :meth:`__post_init__`, so create and restore both get it.
    project_namespace: str = ""
    #: Language tag detected from the user's own words, cached so a turn that
    #: delegates four times detects once.  Refreshed by
    #: ``tools.delegate.detected_language`` whenever a newer user message is in
    #: the history; empty until something has asked.
    detected_language: str = ""
    #: The user text :attr:`detected_language` was derived from, so a new user
    #: message invalidates the cache without a hook in the turn path.
    detected_language_source: str = ""
    created_at: str = ""
    closed_at: str | None = None
    max_concurrent: int = 3
    history: History = field(default_factory=History)
    seq: int = 0
    #: True for sessions nobody is watching — a scheduled job (M5 contract §2)
    #: or a gateway message.  Their approvals go to the shared queue.
    unattended: bool = False
    #: When true, ``exec``-tagged tools (e.g. ``shell``) are refused without an
    #: approval prompt — for headless CI runs that should not fail on exit 4.
    deny_exec: bool = False
    #: The connection that created (or last resumed) the session; interactive
    #: ``approval.request`` calls go only here (contract §7).
    origin_conn: Any = None
    #: Turn ids already closed out of band — ``SessionManager.finish_open_turns``
    #: writes ``turn.done`` for the turn in flight at shutdown, and then
    #: ``close_all`` sets :attr:`interrupt`, which would otherwise make the
    #: turn's own interrupt path report the same turn done a second time
    #: (CORE-dangling-turns).  ``finish_turn`` skips an id listed here.
    finished_turns: set[str] = field(default_factory=set)
    #: Nested instruction files (relative POSIX paths) already shown to the
    #: model in this session, so ``src/AGENTS.md`` is attached to the first
    #: tool result that touches ``src/`` and not to every one after it
    #: (CORE-context-files).
    seen_context_files: set[str] = field(default_factory=set)
    #: Instruction files already quoted in the system prompt — the discovery
    #: chain and whatever nested files fitted the budget.  The on-demand
    #: attachment fires only for the ones that did *not* fit.
    loaded_context_files: set[str] = field(default_factory=set)
    #: Content hashes of the instruction files already attached, so a symlink
    #: or a copy of a file under another path is not shown twice (M15 §D4).
    context_file_hashes: set[str] = field(default_factory=set)
    #: ``skill name -> hash of the body last served by skill_view``.  A repeat
    #: view of an unchanged skill returns a one-line stub rather than the body
    #: again (M15 §B2).
    skill_views: dict[str, str] = field(default_factory=dict)
    #: Tool names this agent is narrowed to (``delegate_task(tools=...)`` or an
    #: agent definition); ``None`` means every tool.
    allowed_tools: set[str] | None = None
    #: Tools a running skill command may call without asking: its front
    #: matter's ``allowed-tools``, which is a pre-approval as in Claude Code,
    #: not a restriction.
    skill_approved_tools: set[str] | None = None
    #: Deferred tools whose schemas this session has already loaded, by
    #: ``tool_search`` or by calling one of them outright.  They join the tool
    #: list from the next round on and stay there (CORE-round-cost).
    loaded_tools: set[str] = field(default_factory=set)
    #: Set by ``session.interrupt``; the agent loop checks it between steps.
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    #: True when the interrupt came from a user-facing stop request. Shutdown
    #: and close paths also set :attr:`interrupt`, but they must not produce the
    #: "tell me what to change" invitation.
    interrupt_user_requested: bool = False
    #: Steer prompts already propagated from an ancestor session, keyed by the
    #: source prompt turn id so a child sees each one once.
    propagated_steers: set[str] = field(default_factory=set)
    turn_task: asyncio.Task[Any] | None = None
    current_turn: str | None = None
    #: Prompts submitted while a turn is running.  They are drained FIFO by
    #: the same task so two turns never mutate one history concurrently.
    queued_turns: list[Any] = field(default_factory=list)
    #: Images from tool results in the current tool round, flushed after the last call.
    pending_tool_images: list[tuple[str, Any]] = field(default_factory=list)
    #: Steering prompts inherited from a parent turn.  They are injected at the
    #: same point as local busy-steer prompts, but have no queued turn id of
    #: their own.
    steered_prompts: list[tuple[str, str]] = field(default_factory=list)
    #: ``session.steer`` texts waiting for the running turn's next tool round;
    #: injected whatever ``agent.busy`` says (1.6.0).
    rpc_steers: list[str] = field(default_factory=list)
    #: Recall also searches pages a browser host ingested (1.7.0).
    browser_memory: bool = False
    #: ``session.notice`` lines waiting for the next model call (1.7.0).
    pending_notices: list[str] = field(default_factory=list)
    #: ``write_todos`` list: ``[{id, content, status}]`` (1.7.0).
    todos: list[dict[str, str]] = field(default_factory=list)
    #: Short label: the first prompt's opening words, or ``session.rename``.
    title: str | None = None
    #: UTC ISO times for the task list (1.7.0): last prompt/turn end, and the
    #: start of the running turn.
    last_activity_at: str | None = None
    turn_started_at: str | None = None
    #: ``turn.done`` reason of the last finished turn (``error`` shows as such).
    last_turn_reason: str | None = None
    #: ``$SNOWPEA_HOME/sessions/<date>_<id>`` with ``tmp/`` and ``artifacts/``.
    workspace_dir: str | None = None
    #: Skills ``autoInject`` already brought into this session.
    auto_injected_skills: set[str] = field(default_factory=set)
    #: Whose host tools this session sees: a clientId or surface id from
    #: ``session.create {hostToolsFrom}``; ``None`` means the origin connection.
    host_tools_from: str | None = None
    #: ``system.hello`` clientId of the connection that created the session, so
    #: a restarted client re-binds as its origin (1.6.0).
    origin_client_id: str | None = None
    #: Tokens the current prompt occupies, provider-reported when
    #: :attr:`context_estimated` is False (CORE-context).
    context_used: int = 0
    #: True while :attr:`context_used` is a local estimate rather than a count
    #: the provider itself reported.
    context_estimated: bool = True
    #: Context window of :attr:`model` in tokens; ``None`` when unknown.
    context_window: int | None = None
    #: Where this session's tools run; ``backend.set`` / ``/backend`` swaps it
    #: (US-010).  Defaults to the local machine, rooted at :attr:`workdir`.
    backend: ExecutionBackend = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.backend is None:  # type: ignore[unreachable]
            self.backend = LocalBackend(self.workdir)  # type: ignore[unreachable]
        if not self.project_namespace:
            from snowpea_core.memory.scopes import project_namespace

            self.project_namespace = project_namespace(self.workdir)

    @property
    def project_root(self) -> str:
        """The project root this session's memories belong to, or ``""``."""
        from snowpea_core.memory.scopes import project_of

        return project_of(self.project_namespace)

    @property
    def max_tool_rounds(self) -> int | None:
        return self.tool_rounds

    @max_tool_rounds.setter
    def max_tool_rounds(self, value: int | None) -> None:
        self.tool_rounds = value

    async def set_backend(self, backend: ExecutionBackend) -> None:
        """Replace the backend, closing whatever it was using before."""
        previous = self.backend
        self.backend = backend
        if previous is not None and previous is not backend:
            await previous.close()

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def summary(self) -> SessionSummary:
        return SessionSummary(
            sessionId=self.id,
            workdir=str(self.workdir),
            mode=self.mode,
            provider=self.provider,
            model=self.model,
            originSurface=self.origin_surface,
            createdAt=self.created_at,
            seq=self.seq,
            contextUsed=self.context_used,
            contextWindow=self.context_window,
            effort=self.effort,  # type: ignore[arg-type]
            kind=self.kind,
            parentSessionId=self.parent_session_id,
            jobId=self.job_id,
            agent=self.agent,
            running=self.current_turn is not None,
            workspaceDir=self.workspace_dir,
            title=self.title,
            status="running" if self.current_turn is not None else (
                "error" if self.last_turn_reason == "error" else "idle"
            ),
            lastActivityAt=self.last_activity_at,
            turnStartedAt=self.turn_started_at,
        )


__all__ = ["Session"]
