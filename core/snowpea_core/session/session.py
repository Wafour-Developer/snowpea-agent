"""The :class:`Session` entity (contract §4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.protocol import Mode, SessionKind, SessionOrigin, SessionSummary
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
    #: ``"lean"`` | ``"full"`` for this child only — its agent definition's
    #: ``childContext:``.  ``None`` follows ``agents.childContext``.
    child_context: str | None = None
    #: ``delegate_task {readOnly: true}`` (addendum 23): the child runs in plan
    #: mode, every mutating tag is refused, and a call that would ask a person
    #: is refused instead of asked.  A grandchild inherits it.
    read_only: bool = False
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
    #: 0.2.91: the person's connection whose words started (or last steered)
    #: the running turn (``person_gate.asker_for``): that turn's
    #: ``approval.request`` / ``question.request`` go here, not to the origin.
    #: ``None`` (a Jarvis, scheduled or messenger turn): the origin, as before.
    asker_conn: Any = None
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
    #: ``allowed_tools`` is the whole list: the always-allowed reading tools
    #: (``read_file``, ``grep``...) and ``tool_search`` are not added to it.  Set on
    #: the isolated worker of a Jarvis mail task (M19 WP8).
    exclusive_tools: bool = False
    #: ``"jarvis"`` on the daemon's Jarvis session (M19, set by its service);
    #: ``None`` everywhere else.  Jarvis's own tools exist only on it.
    role: str | None = None
    #: Tools only this session sees, laid over the daemon's registry
    #: (Jarvis's tools, M19 WP4); ``name -> Tool``.
    session_tools: dict[str, Any] = field(default_factory=dict)
    #: The agent definition's prompt shape (``identity``, ``deferredTools``,
    #: ``skillsIndex``) and the plugin it came from; see
    #: :func:`snowpea_core.agent.session_agent.apply_prompt_shape`.
    #: The user's words that started the current turn of a root session, as
    #: ``{"messageId", "text", "steers"}`` (``tool.invoke`` ``userTurn``,
    #: 1.22.0); ``None`` for a turn the system started.
    user_turn: dict[str, Any] | None = None
    #: ``turn id -> origin`` (``agent.loop.ORIGINS``) for the latest turns, so a
    #: command's nested run inherits whose words started it.
    turn_origins: dict[str, str] = field(default_factory=dict)
    #: ``turn id -> caller snapshot`` (``person_gate.CallerSnapshot``, security
    #: review 7 H-1): who started each of the latest turns, as it was when the
    #: prompt or command was accepted (``agent.loop.note_caller``).
    turn_callers: dict[str, Any] = field(default_factory=dict)
    #: ``turn id -> {"kind", "surface"}``: the ``message.user`` origin
    #: (1.53.0) a turn's prompt is published with, kept beside
    #: :attr:`turn_origins` and trimmed with it.
    turn_message_origins: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``turn id -> job run`` (``tool.invoke`` ``job``, 1.23.0) for turns a
    #: scheduled run started; dropped when the run ends.
    turn_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The browser clientId whose owned job this run serves, set when the run's
    #: session is made and inherited by its children: such a session takes no
    #: outside input, and only that browser answers its approvals (1.23.0).
    owned_by: str | None = None
    #: ``session.setAgent {guardTurn}`` (addendum 25): non-read and MCP calls
    #: are always asked, here and in every session delegated from here, until
    #: the person's own next prompt starts a turn.
    agent_guard: bool = False
    #: The definition those came from; a delegated child inherits its own.
    agent_definition: str | None = None
    agent_identity: str | None = None
    agent_deferred: list[str] = field(default_factory=list)
    #: The definition's ``unavailable`` hints (1.55.0, ``tools/tool_hints``).
    agent_unavailable: dict[str, str] = field(default_factory=dict)
    agent_skills_index: str | None = None
    agent_plugin: str | None = None
    #: What the system prompt was built from, decided once and kept until the
    #: next compaction (:func:`snowpea_core.agent.agent.prompt_plan`), so a
    #: setting or a skill changed mid-session never rewrites the cached prefix.
    prompt_plan: Any = None
    #: Tools a running skill command may call without asking: its front
    #: matter's ``allowed-tools``, which is a pre-approval as in Claude Code,
    #: not a restriction.
    skill_approved_tools: set[str] | None = None
    #: Deferred tools whose schemas this session has already loaded, by
    #: ``tool_search`` or by calling one of them outright.  They join the tool
    #: list from the next round on and stay there (CORE-round-cost).
    loaded_tools: set[str] = field(default_factory=set)
    #: True while this session talks to a self-hosted server (set per turn by
    #: the loop): a loaded tool is then called by name from its tool_search
    #: schema and never joins the tool list, because a chat template renders
    #: the tools first and a changed list re-prefills the whole conversation.
    fixed_tool_list: bool = False
    #: ``((vendor, model), provider)`` built for a planning round on
    #: ``models.categories.deep`` (1.33.0), rebuilt only when the pair changes.
    deep_provider: Any = None
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
    #: The turn whose ``turn.done`` has gone out.  The turn task clears
    #: ``current_turn`` a moment later; until then a ``session.list`` row read
    #: by a client reacting to ``turn.done`` must not call the session running.
    done_turn: str | None = None
    #: Prompts submitted while a turn is running.  They are drained FIFO by
    #: the same task so two turns never mutate one history concurrently.
    queued_turns: list[Any] = field(default_factory=list)
    #: Images from tool results in the current tool round, flushed after the last call.
    pending_tool_images: list[tuple[str, Any]] = field(default_factory=list)
    #: Messages the last request carried, for ``compaction.persist_image_stubs``.
    images_seen_len: int = 0
    #: Skills the browser host's last note said it covers (``coversSkills``, 1.37.0).
    host_covered_skills: frozenset[str] = frozenset()
    #: Steering prompts inherited from a parent turn.  They are injected at the
    #: same point as local busy-steer prompts, but have no queued turn id of
    #: their own.
    steered_prompts: list[tuple[str, str]] = field(default_factory=list)
    #: ``session.steer`` texts waiting for the running turn's next tool round;
    #: injected whatever ``agent.busy`` says (1.6.0).
    rpc_steers: list[str | tuple[str, str]] = field(default_factory=list)
    #: Recall also searches pages a browser host ingested (1.7.0).
    browser_memory: bool = False
    #: ``session.create {memory, siteMemory}`` (addendum 27, C1): ``None``
    #: follows ``memory.enabled`` / ``browser.siteMemory``; ``False`` turns
    #: recall, the digest, the nudge and the memory tools (or ``site.*`` calls
    #: made for this session) off, ``True`` on, for this session only.  Stored
    #: with the session row and restored with it (addendum 30).
    memory_override: bool | None = None
    site_memory_override: bool | None = None
    #: Serialises history writes so an older persistence attempt cannot delete
    #: or overwrite rows written by a newer one for the same session.
    history_persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history_persisted_len: int = 0
    #: Serialises event emission: numbering, storing and broadcasting an event
    #: happen together, so concurrent emitters (team workers, subagents) can
    #: never deliver a later seq before an earlier one.
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history_persisted_version: int = 0
    history_persisted_rewrite_version: int = 0
    #: ``session.create {locale}``: the client's UI language (``ko-KR``), which
    #: ``agent.replyLanguage: auto`` prefers to a guess (1.7.0).
    locale: str | None = None
    #: ``session.notice`` lines waiting for the next model call (1.7.0).
    pending_notices: list[str] = field(default_factory=list)
    #: Recently queued child-attempt evidence markers.  This survives notice
    #: flushes so the same failed attempt is carried once, not every replay.
    reported_child_attempts: list[str] = field(default_factory=list)
    #: ``subagent_message`` texts from the delegating agent, injected at this
    #: child's next model round.
    lead_messages: list[str] = field(default_factory=list)
    #: Set when the user's words are waiting to be folded into the running
    #: turn.  A lead blocked on a delegation (``delegate_task``,
    #: ``subagent_wait``) wakes on it, so the user is answered mid-delegation
    #: instead of after it.  Cleared when the words are folded in.
    user_waiting: asyncio.Event = field(default_factory=asyncio.Event)
    #: This turn's edits and checks, for verify-on-stop (agent/verify_gate.py).
    verify_turn: Any = None
    #: This turn's answer-now note state (agent/answer_nudge.py).
    answer_nudge: Any = None
    #: ``session.continue`` (1.8.0): keys already honoured, and when the last
    #: system-initiated turn started (loop guard).
    continue_keys: set[str] = field(default_factory=set)
    last_system_start: float = 0.0
    #: ``write_todos`` list: ``[{id, content, status}]`` (1.7.0).
    todos: list[dict[str, str]] = field(default_factory=list)
    #: Plan documents written while in PLAN mode this session, newest last, and
    #: whether ``plan_save`` ran: leaving plan mode registers the newest file as
    #: the current plan only when nothing was saved (CORE-plan-continuity).
    plan_files: list[str] = field(default_factory=list)
    plan_saved: bool = False
    #: "Allow Everything" pressed while planning (1.32.0): the session stays
    #: in PLAN mode with every ``ask`` answered ``allow`` (``deny`` untouched),
    #: and ``set_mode`` offers auto first once the plan is done.  Meaningful
    #: only while ``mode == "plan"``; :meth:`SessionManager.set_mode` clears it
    #: on every way out of plan.  Stored with the session row.
    auto_after_plan: bool = False
    #: The flag went on mid-turn: the next tool result carries the turn note
    #: the turn's own message went out without (runtime only).
    plan_auto_note_pending: bool = False
    #: The runtime-context snapshot last sent, and the compaction count it was
    #: sent under (M16 P1.2); ``None`` until one goes out.  Runtime only: a
    #: resumed session sends it again.
    context_snapshot: tuple[tuple[str, str], ...] | None = None
    context_snapshot_epoch: int = -1
    #: A tool result asked to end the turn after its batch (M16 P1.4; runtime).
    conclude_turn: bool = False
    #: Model calls made in the current turn (M17 2a); stamped on every event.
    current_step: int = 0
    #: The session's goal (M16 P1.6, ``agent.goals.Goal``); stored with the row.
    goal: Any = None
    #: Rounds start by themselves only while armed; runtime only, so a restart
    #: or a resume never starts one unasked.
    goal_armed: bool = False
    #: Short label: the first prompt's opening words, or ``session.rename``.
    title: str | None = None
    #: UTC ISO times for the task list (1.7.0): last prompt/turn end, and the
    #: start of the running turn.
    last_activity_at: str | None = None
    turn_started_at: str | None = None
    #: UTC ISO time of the latest prompt a person sent (1.50.0); a goal round or
    #: another system turn does not move it.
    last_user_at: str | None = None
    #: The session this one was forked from and the turn it was forked at
    #: (``session.fork``, 1.50.0); stored on the row.
    forked_from: str | None = None
    forked_at_turn_id: str | None = None
    #: ``turn.done`` reason of the last finished turn (``error`` shows as such).
    last_turn_reason: str | None = None
    #: ``$SNOWPEA_HOME/sessions/<date>_<id>`` with ``tmp/`` and ``artifacts/``.
    workspace_dir: str | None = None
    #: Skills ``autoInject`` already brought into this session.
    auto_injected_skills: set[str] = field(default_factory=set)
    #: Bytes of skill text ``autoInject`` brought in so far (the session cap, 1.55.0).
    auto_injected_bytes: int = 0
    #: ``(tool, section)`` manual sections already appended to a failed host
    #: result (``tools/host_tools.with_help_section``, 1.55.0).
    help_sections_shown: set[tuple[str, str]] = field(default_factory=set)
    #: Whose host tools this session sees: a clientId or surface id from
    #: ``session.create {hostToolsFrom}``; ``None`` means the origin connection.
    host_tools_from: str | None = None
    #: Which browser a browser session's ``browser_*`` tools drive (addendum 9):
    #: ``None``/``"host"`` means only the Snowpea browser, never core's own;
    #: ``"local"`` is the user's explicit opt-in (``session.setBrowserProvider``).
    browser_provider: str | None = None
    #: True once :mod:`agent.session_agent` applied ``agent``'s definition
    #: (prompt, tools, rounds) to this human session (addendum 17).
    agent_applied: bool = False
    #: Write/exec calls the lead made itself this turn in delegation mode.
    lead_direct_calls: int = 0
    #: ``system.hello`` clientId of the connection that created the session, so
    #: a restarted client re-binds as its origin (1.6.0).
    origin_client_id: str | None = None
    #: Tokens the current prompt occupies, provider-reported when
    #: :attr:`context_estimated` is False (CORE-context).
    context_used: int = 0
    #: True while :attr:`context_used` is a local estimate rather than a count
    #: the provider itself reported.
    context_estimated: bool = True
    #: ``(turn id, reasons)`` already reported by ``vision.unavailable`` this turn.
    vision_notified: tuple[str | None, set[str]] | None = None
    #: ``len(history)`` when :attr:`context_used` was reported; messages after
    #: that index (tool results, the reply) are not in the count and are
    #: estimated on top.  ``-1``: unknown.
    context_used_len: int = -1
    #: Provider-reported tokens over our estimate for the same messages
    #: (1.0 to 2.0): how far the chars/4 guess runs low for this model.
    context_ratio: float = 1.0
    #: Context window of :attr:`model` in tokens; ``None`` when unknown.
    context_window: int | None = None
    #: Where ``context_window`` came from (see ``ProviderRegistry.window_info``).
    context_window_source: str | None = None
    #: Where this session's tools run
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
    def turn_live(self) -> bool:
        """A turn is running and has not yet announced ``turn.done``."""
        return self.current_turn is not None and self.current_turn != self.done_turn

    @property
    def max_tool_rounds(self) -> int | None:
        return self.tool_rounds

    @max_tool_rounds.setter
    def max_tool_rounds(self, value: int | None) -> None:
        self.tool_rounds = value

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def summary(self) -> SessionSummary:
        return SessionSummary(
            sessionId=self.id,
            workdir=str(self.workdir),
            mode=self.mode,
            autoAfterPlan=self.mode == "plan" and self.auto_after_plan,
            provider=self.provider,
            model=self.model,
            originSurface=self.origin_surface,
            createdAt=self.created_at,
            seq=self.seq,
            contextUsed=self.context_used,
            contextWindow=self.context_window,
            contextWindowSource=self.context_window_source,  # type: ignore[arg-type]
            effort=self.effort,  # type: ignore[arg-type]
            kind=self.kind,
            parentSessionId=self.parent_session_id,
            jobId=self.job_id,
            agent=self.agent,
            running=self.turn_live,
            workspaceDir=self.workspace_dir,
            title=self.title,
            status="running" if self.turn_live else (
                "error" if self.last_turn_reason == "error" else "idle"
            ),
            lastActivityAt=self.last_activity_at,
            lastUserAt=self.last_user_at,
            forkedFrom=self.forked_from,
            forkedAtTurnId=self.forked_at_turn_id,
            turnStartedAt=self.turn_started_at,
            hostToolsFrom=self.host_tools_from,
            browserProvider=browser_provider_of(self),  # type: ignore[arg-type]
            origin=origin_of(self),
        )



def shown_host(host: str | None, caller: Any) -> str | None:
    """``hostToolsFrom`` as ``caller`` may see it (m27 fix, M-2): a concrete
    clientId only to that client (its clientId or surface id), as
    :func:`origin_of` shows the origin's; to anyone else ``"browser"`` ("a
    browser's tools").  In-process listeners (no ``client_kind``) see it all."""
    if not host or host == "browser" or not hasattr(caller, "client_kind"):
        return host
    if host in (getattr(caller, "client_id", None), getattr(caller, "surface_id", None)):
        return host
    return "browser"


def origin_of(session: Session, caller: Any = None) -> SessionOrigin | None:
    """Who answers ``session``'s approvals, and whether that client is still there.

    The origin's clientId is shown only to that same client (``mine``): it is
    what a browser-owned job's approvals are matched on, so it is not handed
    to every other connection.
    """
    conn = getattr(session, "origin_conn", None)
    if conn is None:
        return None
    client_id = getattr(conn, "client_id", None)
    caller_id = getattr(caller, "client_id", None)
    same_client = client_id is not None and client_id == caller_id
    mine = caller is not None and (conn is caller or same_client)
    return SessionOrigin(
        clientKind=getattr(conn, "client_kind", None),
        clientId=client_id if mine else None,
        live=not getattr(conn, "closed", False),
        mine=mine,
    )


def _live(conn: Any) -> bool:
    return conn is not None and not getattr(conn, "closed", False)


def asking_conn(session: Any, lookup: Any = None) -> Any:
    """Where ``session``'s approvals and questions are asked now (0.2.91), or ``None``.

    The running turn's asker (:attr:`Session.asker_conn`) — a delegated child
    has none of its own and follows the nearest one up the
    ``parent_session_id`` chain (``lookup``: session id -> session) — while it
    is open; else the session's origin while it is open; else ``None``: the
    request goes to the shared queue, where any surface may answer it.
    """
    current = session
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        asker = getattr(current, "asker_conn", None)
        if _live(asker):
            return asker
        parent = getattr(current, "parent_session_id", None)
        current = lookup(parent) if parent and callable(lookup) else None
    origin = getattr(session, "origin_conn", None)
    return origin if _live(origin) else None


#: How long the desktop app shows a relayed card before it answers for nobody
#: (snowpea-ide ``APPROVAL_TIMEOUT_MS`` / ``QUESTION_TIMEOUT_MS``): an approval
#: gets ``{decision: "deny", scope: "once"}``, a question ``{answers: []}``.
CLIENT_APPROVAL_CARD_SEC = 300.0
CLIENT_QUESTION_CARD_SEC = 600.0
#: A client's timer and ours start a moment apart.
CLIENT_CLOCK_SLACK_SEC = 1.0


def client_timed_out(reply: Any, waited: float, clock_sec: float, card_sec: float) -> bool:
    """True when ``reply`` is the asking client's own card timing out (0.2.91).

    The shape a client sends when nobody answered its card — an approval's bare
    deny (no reason, no ``actor``), a question's empty ``answers`` — arriving
    no sooner than the core's own clock (``clock_sec``) or the desktop app's
    card window (``card_sec``), whichever is shorter.  A person's quick "no"
    is a deny, as before.
    """
    if not isinstance(reply, dict):
        return False
    if "answers" in reply or "decision" not in reply:
        answers = reply.get("answers")
        bare = isinstance(answers, list) and not any(
            isinstance(a, dict) and (a.get("selected") or str(a.get("text") or "").strip())
            for a in answers)
    else:
        bare = (reply.get("decision") == "deny" and not str(reply.get("reason") or "").strip()
                and not reply.get("actor"))
    limit = min(clock_sec, card_sec) if clock_sec > 0 else card_sec
    return bare and waited >= limit - CLIENT_CLOCK_SLACK_SEC


def surface_name(conn: Any) -> str:
    """A connection as a person would name it: "the desktop app", "the browser", ..."""
    if getattr(conn, "remote_login", False):
        return "the remote web page"
    kind = str(getattr(conn, "client_kind", None) or "").lower()
    return {
        "ide": "the desktop app", "desktop": "the desktop app", "app": "the desktop app",
        "tui": "the terminal (TUI)",
        "cli": "the command line", "web": "the web page", "browser": "the browser",
    }.get(kind, f"the {kind} client" if kind else "the client that was asked")


async def withdraw(conn: Any, prefix: str, request_id: str) -> None:
    """Tell ``conn`` its card for ``request_id`` is no longer its to show (0.2.91).

    ``{approval|question}.resolved {by: "rerouted", outcome: "withdrawn"}``
    (1.53.0's shape; ``decision`` null for an approval): the request moved to
    the surface the person is typing on.  A closed connection is skipped.
    """
    if not _live(conn):
        return
    body: dict[str, Any] = {"requestId": request_id, "by": "rerouted", "outcome": "withdrawn"}
    if prefix == "approval":
        body["decision"] = None
    try:
        await conn.notify(f"{prefix}.resolved", body)
    except Exception:  # noqa: BLE001 - a dying connection: its card dies with it
        pass


def inherited_override(core: Any, session: Any, attr: str) -> bool | None:
    """``session``'s ``memory_override`` / ``site_memory_override``, or its root's.

    ``session.create {memory, siteMemory}`` sets the override on the session
    a client opened; a delegated child has none of its own and follows the
    conversation above it (addendum 30, C4).  The nearest one set up the
    ``parent_session_id`` chain wins.  A scheduled run is a root of its own,
    as in ``host_tools.root_session``; a lost parent or a cycle ends the walk
    with ``None``, which leaves the user's setting in charge.
    """
    seen: set[str] = set()
    sessions = getattr(core, "sessions", None)
    current = session
    while current is not None:
        value = getattr(current, attr, None)
        if value is not None:
            return bool(value)
        parent_id = getattr(current, "parent_session_id", None)
        if (
            not parent_id
            or getattr(current, "kind", None) == "scheduled"
            or sessions is None
            or str(parent_id) in seen
        ):
            return None
        seen.add(str(parent_id))
        current = sessions.get(str(parent_id))
    return None


def browser_provider_of(session: Session) -> str | None:
    from snowpea_core.tools.browser_providers import browser_provider_of as lookup

    return lookup(session)


__all__ = ["Session", "inherited_override"]
