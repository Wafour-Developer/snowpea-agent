"""Protocol single source of truth.

Every JSON-RPC method and notification the daemon speaks is declared here as a
pydantic v2 model.  ``scripts/gen_protocol.py`` turns :func:`dump_schema` into
``sdk/src/protocol.ts`` and ``docs/protocol.md``, so nothing else in the tree
may invent a method name or a payload field.

Field ``description``s and per-method ``summary``s are load-bearing: they are
the text of the generated TypeScript JSDoc and of the markdown field tables.

See ``docs/design/m1-core-contract.md`` §1 and plan §3.5.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from snowpea_core import __version__ as _core_version
from snowpea_core.server.errors import ERROR_CODES

PROTOCOL_VERSION = "0.1.0"
SERVER_VERSION = _core_version

Mode = Literal["plan", "accept", "auto"]
PermissionTag = Literal["read", "write", "exec", "network", "send"]
ToolState = Literal["active", "inactive"]
CommandSource = Literal["builtin", "skill", "plugin"]
Decision = Literal["allow", "deny"]
ApprovalScope = Literal["once", "session", "project", "always"]
AllowlistScope = Literal["session", "project", "always"]
BackendKind = Literal["local", "docker", "ssh"]
TurnReason = Literal["complete", "interrupted", "error", "denied", "timeout"]
TaskState = Literal["pending", "running", "done", "failed"]
Direction = Literal["c2s", "s2c"]


class Payload(BaseModel):
    """Base for every wire model: JSON objects, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class Empty(Payload):
    """A method that takes no parameters (``params`` is still an object)."""


class Ok(Payload):
    ok: bool = Field(default=True, description="True when the call succeeded.")


# --------------------------------------------------------------------------
# system.*
# --------------------------------------------------------------------------


class HelloParams(Payload):
    token: str = Field(description="Shared secret read from $SNOWPEA_HOME/token or daemon.json.")
    clientVersion: str = Field(description="Version string of the connecting client.")
    protocolVersion: str = Field(description="Protocol semver the client speaks; major must match.")


class HelloResult(Payload):
    protocolVersion: str = Field(description="Protocol semver the daemon speaks.")
    serverVersion: str = Field(description="Version of the running daemon.")
    capabilities: list[str] = Field(
        default_factory=list, description="Feature flags this daemon supports."
    )


class LifecycleStatus(Payload):
    """Idle-shutdown snapshot: ``Lifecycle.status()`` without the counters."""

    willExit: bool = Field(default=False, description="True while the idle timer is running.")
    reason: str = Field(default="busy", description="Why the daemon will or will not exit.")
    secondsUntilExit: float | None = Field(
        None, description="Seconds left before the idle shutdown, null while busy."
    )


class InfoResult(Payload):
    version: str = Field(description="Daemon version.")
    protocolVersion: str = Field(description="Protocol semver the daemon speaks.")
    pid: int = Field(description="Process id of the daemon.")
    port: int = Field(description="TCP port the daemon is listening on (127.0.0.1 only).")
    startedAt: str = Field(description="UTC ISO-8601 timestamp of daemon start.")
    home: str = Field(description="Resolved SNOWPEA_HOME directory.")
    counters: dict[str, int] = Field(
        default_factory=dict,
        description="Lifecycle counters: sessions, jobs, gateway_bindings, named_agents.",
    )
    lifecycle: LifecycleStatus | None = Field(
        None, description="Idle-shutdown status, omitted by older daemons."
    )


class HealthResult(Payload):
    status: Literal["ok"] = Field(default="ok", description="Always 'ok' when the daemon answers.")


# --------------------------------------------------------------------------
# session.*
# --------------------------------------------------------------------------


class Attachment(Payload):
    """A file, image or inline text sent along with a prompt."""

    kind: Literal["file", "image", "text"] = Field(
        default="file", description="Attachment flavour."
    )
    path: str | None = Field(default=None, description="Absolute path, for 'file' and 'image'.")
    mimeType: str | None = Field(default=None, description="Media type when known.")
    text: str | None = Field(default=None, description="Inline content, for 'text'.")


class SessionCreateParams(Payload):
    workdir: str = Field(description="Absolute path the session operates in.")
    mode: Mode | None = Field(
        default=None, description="Starting mode; defaults to the project setting."
    )
    provider: str | None = Field(
        default=None, description="Chat provider vendor; defaults to configured."
    )
    model: str | None = Field(
        default=None, description="Model id; defaults to the provider's default."
    )
    agent: str | None = Field(default=None, description="Named agent whose persona to load.")
    maxConcurrent: int | None = Field(
        default=None, description="Override for concurrent subagents."
    )
    originSurface: str | None = Field(
        None, description="Surface that owns approvals for this session (TUI, gateway, ...)."
    )


class SessionCreateResult(Payload):
    sessionId: str = Field(description="Id of the new session.")


class SessionEvent(Payload):
    """One entry of a session's ordered event log."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number; resume replays after it.")
    kind: str = Field(description="Event kind; see sessionEventKinds in the schema dump.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")


class SessionResumeParams(Payload):
    sessionId: str = Field(description="Session to resume.")
    afterSeq: int | None = Field(default=None, description="Replay only events with a greater seq.")


class SessionResumeResult(Payload):
    sessionId: str = Field(description="Session that was resumed.")
    events: list[SessionEvent] = Field(
        default_factory=list, description="Missed events in seq order."
    )


class SessionSummary(Payload):
    """One row of ``session.list``."""

    sessionId: str = Field(description="Session id.")
    workdir: str = Field(description="Absolute working directory.")
    mode: Mode = Field(description="Current permission mode.")
    provider: str | None = Field(default=None, description="Chat provider vendor in use.")
    model: str | None = Field(default=None, description="Model id in use.")
    originSurface: str | None = Field(default=None, description="Surface that owns approvals.")
    createdAt: str = Field(description="UTC ISO-8601 creation timestamp.")
    seq: int = Field(default=0, description="Sequence number of the latest event.")


class SessionListResult(Payload):
    sessions: list[SessionSummary] = Field(default_factory=list, description="Every live session.")


class SessionIdParams(Payload):
    sessionId: str = Field(description="Target session.")


class SessionPromptParams(Payload):
    sessionId: str = Field(description="Session to prompt.")
    text: str = Field(description="User text; a leading '/' is parsed as a slash command.")
    attachments: list[Attachment] | None = Field(
        default=None, description="Files or images to include."
    )


class TurnResult(Payload):
    turnId: str = Field(description="Id of the started turn; turn.done carries it back.")


class SessionSetModeParams(Payload):
    sessionId: str = Field(description="Session to change.")
    mode: Mode = Field(description="New permission mode.")


class SessionSetModeResult(Payload):
    mode: Mode = Field(description="Mode now in effect.")


# --------------------------------------------------------------------------
# command.* / tool.*
# --------------------------------------------------------------------------


class OptionalSessionParams(Payload):
    sessionId: str | None = Field(
        None, description="Scope the listing to one session; omit for the global set."
    )


class CommandInfo(Payload):
    """One slash command."""

    name: str = Field(description="Command name without the leading slash.")
    summary: str = Field(description="One-line description shown in /help.")
    argsSchema: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema for the argument string."
    )
    source: CommandSource = Field(default="builtin", description="Where the command came from.")


class CommandListResult(Payload):
    commands: list[CommandInfo] = Field(
        default_factory=list, description="Available slash commands."
    )


class CommandRunParams(Payload):
    sessionId: str = Field(description="Session to run the command in.")
    name: str = Field(description="Command name without the leading slash.")
    args: str = Field(default="", description="Raw argument string, as typed after the name.")


class ToolInfo(Payload):
    """One registered tool."""

    name: str = Field(description="Tool name as the model calls it.")
    category: str = Field(description="Grouping used by the UI.")
    permissionTag: PermissionTag = Field(description="Permission class checked against the mode.")
    state: ToolState = Field(
        default="active", description="Inactive tools are hidden from the model."
    )
    source: str = Field(default="builtin", description="builtin, skill, plugin or MCP server name.")
    description: str = Field(default="", description="Text shown to the model.")


class ToolListResult(Payload):
    tools: list[ToolInfo] = Field(default_factory=list, description="Registered tools.")


# --------------------------------------------------------------------------
# approval.* / permission.*
# --------------------------------------------------------------------------


class ApprovalRequest(Payload):
    """A tool call waiting for a human decision."""

    requestId: str = Field(description="Id to answer with approval.respond.")
    sessionId: str = Field(description="Session whose turn is blocked.")
    tool: str = Field(description="Tool the model wants to run.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments it wants to use.")
    risk: str = Field(default="low", description="Risk hint for the UI.")
    timeoutSec: int = Field(default=300, description="Seconds before the request auto-denies.")
    scopeHint: ApprovalScope = Field(default="once", description="Scope the UI should preselect.")


class ApprovalListResult(Payload):
    requests: list[ApprovalRequest] = Field(
        default_factory=list, description="Approvals still pending."
    )


class ApprovalRespondParams(Payload):
    requestId: str = Field(description="Request being answered.")
    decision: Decision = Field(description="allow runs the tool, deny ends the turn.")
    scope: ApprovalScope = Field(default="once", description="How long the decision applies.")


class ApprovalAnswer(Payload):
    """Result of the server-initiated ``approval.request``."""

    decision: Decision = Field(description="The human's decision.")
    scope: ApprovalScope = Field(default="once", description="How long the decision applies.")


class AllowlistAddParams(Payload):
    pattern: str = Field(description="Glob or command prefix promoted from ask to allow.")
    scope: AllowlistScope = Field(default="project", description="Where the pattern is stored.")


class AllowlistAddResult(Payload):
    patternId: str = Field(description="Id used to remove the pattern later.")


class AllowlistListParams(Payload):
    scope: AllowlistScope | None = Field(default=None, description="Filter by scope; omit for all.")


class AllowlistPattern(Payload):
    """One stored allowlist entry."""

    patternId: str = Field(description="Stable id of the pattern.")
    pattern: str = Field(description="The glob or command prefix.")
    scope: AllowlistScope = Field(description="Where it is stored.")


class AllowlistListResult(Payload):
    patterns: list[AllowlistPattern] = Field(
        default_factory=list, description="Stored allowlist entries."
    )


class AllowlistRemoveParams(Payload):
    patternId: str = Field(description="Pattern to delete.")


# --------------------------------------------------------------------------
# provider.* / backend.*
# --------------------------------------------------------------------------


class ProviderInfo(Payload):
    """One chat provider."""

    vendor: str = Field(description="Vendor key, e.g. 'anthropic'.")
    models: list[str] = Field(default_factory=list, description="Model ids this vendor offers.")
    configured: bool = Field(default=False, description="True when credentials are present.")
    default: bool = Field(default=False, description="True for the vendor used when none is named.")
    authMethods: list[str] = Field(
        default_factory=lambda: ["api_key"],
        description=(
            "Login flows the vendor supports: 'api_key' everywhere, plus 'device_code' "
            "(OpenAI) or 'oauth_pkce' (OpenRouter)."
        ),
    )


class ProviderListResult(Payload):
    providers: list[ProviderInfo] = Field(default_factory=list, description="Known chat providers.")


class ProviderConfigureParams(Payload):
    vendor: str = Field(description="Vendor to configure.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Vendor-specific settings, including credentials."
    )


class ProviderLoginWebParams(Payload):
    vendor: str = Field(description="Vendor to log into.")
    method: str = Field(description="Login flow to start, e.g. 'oauth'.")


class BackendSetParams(Payload):
    sessionId: str = Field(description="Session whose execution backend changes.")
    kind: BackendKind = Field(description="Where tools execute.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Backend settings, e.g. container or SSH target."
    )


# --------------------------------------------------------------------------
# agent.* / team.*
# --------------------------------------------------------------------------


class AgentInfo(Payload):
    """One named agent."""

    name: str = Field(description="Agent name used by agent.spawn.")
    description: str = Field(default="", description="What the agent is for.")
    channel: str | None = Field(default=None, description="Gateway channel bound to the agent.")
    source: str = Field(default="user", description="Where the definition came from.")


class AgentListResult(Payload):
    agents: list[AgentInfo] = Field(default_factory=list, description="Defined named agents.")


class AgentCreateParams(Payload):
    description: str = Field(description="Natural-language brief the daemon turns into an agent.")


class AgentCreateResult(Payload):
    name: str = Field(description="Name assigned to the new agent.")


class AgentSpawnParams(Payload):
    name: str = Field(description="Agent to run.")
    task: str = Field(description="Task handed to the agent.")
    sessionId: str | None = Field(
        default=None, description="Parent session, when spawned from one."
    )


class AgentSpawnResult(Payload):
    agentId: str = Field(description="Id correlating the subagent.* events.")


class AgentBindChannelParams(Payload):
    name: str = Field(description="Agent to bind.")
    channel: str = Field(description="Gateway channel that will reach the agent.")


class AgentDeleteParams(Payload):
    name: str = Field(description="Agent to delete.")


class TeamStartParams(Payload):
    sessionId: str = Field(description="Session the team works under.")
    n: int = Field(description="Number of workers to run in parallel.")
    task: str = Field(description="Task the team splits between workers.")


class TeamStartResult(Payload):
    teamId: str = Field(description="Id to poll with team.status.")


class TeamStatusParams(Payload):
    teamId: str = Field(description="Team to inspect.")


class TeamTask(Payload):
    """One unit of work inside a team run."""

    taskId: str = Field(description="Task id, stable for the run.")
    title: str = Field(description="Short task description.")
    status: TaskState = Field(default="pending", description="Current state.")
    assignee: str | None = Field(default=None, description="Worker that owns the task.")


class TeamStatusResult(Payload):
    teamId: str = Field(description="Team that was inspected.")
    state: Literal["running", "done", "failed"] = Field(
        default="running", description="Overall state."
    )
    tasks: list[TeamTask] = Field(default_factory=list, description="Task board contents.")


# --------------------------------------------------------------------------
# job.*
# --------------------------------------------------------------------------


class JobScheduleParams(Payload):
    spec: str = Field(description="Cron expression or natural-language schedule.")
    task: str = Field(description="Prompt run on each firing.")
    mode: Mode = Field(default="accept", description="Permission mode for the unattended run.")
    channel: str | None = Field(
        default=None, description="Gateway channel that receives the output."
    )


class JobScheduleResult(Payload):
    jobId: str = Field(description="Id of the scheduled job.")


class JobInfo(Payload):
    """One scheduled job."""

    jobId: str = Field(description="Job id.")
    spec: str = Field(description="Schedule as given.")
    task: str = Field(description="Prompt run on each firing.")
    mode: Mode = Field(default="accept", description="Permission mode for the run.")
    channel: str | None = Field(default=None, description="Channel that receives the output.")
    nextRunAt: str | None = Field(default=None, description="UTC ISO-8601 time of the next firing.")
    state: Literal["scheduled", "running", "cancelled"] = Field(
        "scheduled", description="Current job state."
    )


class JobListResult(Payload):
    jobs: list[JobInfo] = Field(default_factory=list, description="Known jobs.")


class JobIdParams(Payload):
    jobId: str = Field(description="Target job.")


# --------------------------------------------------------------------------
# gateway.*
# --------------------------------------------------------------------------


class GatewayBindParams(Payload):
    platform: str = Field(description="Chat platform key, e.g. 'slack'.")
    credentialsRef: str = Field(description="Name of the stored credential to use.")
    target: str = Field(description="Channel, room or chat id to attach to.")


class GatewayBindResult(Payload):
    bindingId: str = Field(description="Id used to unbind later.")


class GatewayBinding(Payload):
    """One live gateway attachment."""

    bindingId: str = Field(description="Binding id.")
    platform: str = Field(description="Chat platform key.")
    target: str = Field(description="Channel, room or chat id.")
    state: Literal["active", "inactive"] = Field(
        default="active", description="Whether it is listening."
    )


class GatewayListResult(Payload):
    bindings: list[GatewayBinding] = Field(
        default_factory=list, description="Live gateway bindings."
    )


class GatewayUnbindParams(Payload):
    bindingId: str = Field(description="Binding to remove.")


# --------------------------------------------------------------------------
# memory.* / skill.*
# --------------------------------------------------------------------------


class MemorySearchParams(Payload):
    query: str = Field(description="Free-text query.")
    limit: int = Field(default=10, description="Maximum number of hits.")


class MemoryHit(Payload):
    """One recalled memory."""

    id: str = Field(description="Memory id.")
    text: str = Field(description="Stored text.")
    tags: list[str] = Field(default_factory=list, description="Tags attached at write time.")
    score: float = Field(default=0.0, description="Relevance score; higher is closer.")


class MemorySearchResult(Payload):
    hits: list[MemoryHit] = Field(default_factory=list, description="Matches, best first.")


class MemoryWriteParams(Payload):
    text: str = Field(description="Text to remember.")
    tags: list[str] = Field(default_factory=list, description="Tags for later filtering.")


class MemoryWriteResult(Payload):
    id: str = Field(description="Id of the stored memory.")


class SkillSearchParams(Payload):
    query: str = Field(description="Free-text query over skill names and summaries.")


class SkillInfo(Payload):
    """One skill."""

    name: str = Field(description="Skill name.")
    summary: str = Field(default="", description="What the skill does.")
    source: str = Field(default="builtin", description="Where the skill came from.")
    installed: bool = Field(default=False, description="True when present locally.")


class SkillSearchResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Matching skills.")


class SkillInstallParams(Payload):
    source: str = Field(description="Path, URL or registry name to install from.")


class SkillListResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Installed skills.")


# --------------------------------------------------------------------------
# session.event kinds
# --------------------------------------------------------------------------


class MessageDelta(Payload):
    """Streaming assistant text."""

    kind: Literal["message.delta"] = "message.delta"
    text: str = Field(description="Text fragment to append to the current message.")


class MessageDone(Payload):
    """A completed message."""

    kind: Literal["message.done"] = "message.done"
    text: str = Field(description="Full message text.")
    role: Literal["assistant", "user", "system"] = Field(
        "assistant", description="Who produced the message."
    )


class ToolCallEvent(Payload):
    """The model asked to run a tool."""

    kind: Literal["tool.call"] = "tool.call"
    callId: str = Field(description="Id pairing this call with its tool.result.")
    name: str = Field(description="Tool being called.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments supplied.")


class ToolResultEvent(Payload):
    """A tool finished."""

    kind: Literal["tool.result"] = "tool.result"
    callId: str = Field(description="Id of the matching tool.call.")
    name: str = Field(description="Tool that ran.")
    ok: bool = Field(description="False when the tool failed.")
    output: str = Field(default="", description="Output handed back to the model.")
    error: str | None = Field(default=None, description="Failure detail when ok is false.")


class DiffEvent(Payload):
    """A file was edited."""

    kind: Literal["diff"] = "diff"
    path: str = Field(description="File that changed.")
    patch: str = Field(description="Unified diff of the change.")


class SubagentSpawn(Payload):
    """A subagent started."""

    kind: Literal["subagent.spawn"] = "subagent.spawn"
    agentId: str = Field(description="Id correlating this subagent's events.")
    name: str = Field(default="", description="Named agent that was spawned.")
    task: str = Field(default="", description="Task it was given.")


class SubagentUpdate(Payload):
    """Progress from a running subagent."""

    kind: Literal["subagent.update"] = "subagent.update"
    agentId: str = Field(description="Subagent reporting progress.")
    status: str = Field(default="", description="Short status label.")
    text: str = Field(default="", description="Human-readable progress text.")


class SubagentDone(Payload):
    """A subagent finished."""

    kind: Literal["subagent.done"] = "subagent.done"
    agentId: str = Field(description="Subagent that finished.")
    ok: bool = Field(default=True, description="False when it failed.")
    result: str = Field(default="", description="Final report.")


class TeamTaskUpdate(Payload):
    """A team task changed state."""

    kind: Literal["team.task.update"] = "team.task.update"
    teamId: str = Field(description="Team the task belongs to.")
    taskId: str = Field(description="Task that changed.")
    status: TaskState = Field(default="pending", description="New state.")
    assignee: str | None = Field(default=None, description="Worker that owns the task.")


class ModeChanged(Payload):
    """The session's permission mode changed."""

    kind: Literal["mode.changed"] = "mode.changed"
    mode: Mode = Field(description="Mode now in effect.")


class UsageEvent(Payload):
    """Token usage for the turn."""

    kind: Literal["usage"] = "usage"
    inputTokens: int = Field(default=0, description="Prompt tokens consumed.")
    outputTokens: int = Field(default=0, description="Completion tokens produced.")


class ErrorEvent(Payload):
    """Something went wrong inside a turn."""

    kind: Literal["error"] = "error"
    code: str = Field(description="One of the protocol error codes.")
    message: str = Field(description="Human-readable detail.")


class TurnDone(Payload):
    """A turn ended, for any reason."""

    kind: Literal["turn.done"] = "turn.done"
    turnId: str = Field(description="Turn that ended.")
    reason: TurnReason = Field(default="complete", description="Why the turn ended.")


SessionEventPayload = Annotated[
    MessageDelta
    | MessageDone
    | ToolCallEvent
    | ToolResultEvent
    | DiffEvent
    | SubagentSpawn
    | SubagentUpdate
    | SubagentDone
    | TeamTaskUpdate
    | ModeChanged
    | UsageEvent
    | ErrorEvent
    | TurnDone,
    Field(discriminator="kind"),
]

#: ``session.event`` payload model per ``kind`` (contract §1).
SESSION_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "message.delta": MessageDelta,
    "message.done": MessageDone,
    "tool.call": ToolCallEvent,
    "tool.result": ToolResultEvent,
    "diff": DiffEvent,
    "subagent.spawn": SubagentSpawn,
    "subagent.update": SubagentUpdate,
    "subagent.done": SubagentDone,
    "team.task.update": TeamTaskUpdate,
    "mode.changed": ModeChanged,
    "usage": UsageEvent,
    "error": ErrorEvent,
    "turn.done": TurnDone,
}

SESSION_EVENT_KINDS: tuple[str, ...] = tuple(SESSION_EVENT_MODELS)


class SessionEventNotification(Payload):
    """Server notification carrying one session event."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number.")
    kind: str = Field(description="Event kind; see sessionEventKinds for the payload schema.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")


class SessionEventKindEnvelope(Payload):
    """Typed view of a ``session.event`` payload, discriminated by ``kind``."""

    event: SessionEventPayload


class ApprovalResolvedNotification(Payload):
    """An approval was answered elsewhere; stop showing it."""

    requestId: str = Field(description="Request that was resolved.")
    decision: Decision = Field(description="The decision that was recorded.")
    by: str = Field(description="Surface or user that answered.")


class JobEventNotification(Payload):
    """Progress from a scheduled job."""

    jobId: str = Field(description="Job the event belongs to.")
    kind: str = Field(description="Event kind, e.g. 'started' or 'finished'.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")


class GatewayEventNotification(Payload):
    """Activity on a gateway binding."""

    bindingId: str = Field(description="Binding the event belongs to.")
    kind: str = Field(description="Event kind, e.g. 'message' or 'error'.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")


# --------------------------------------------------------------------------
# registries
# --------------------------------------------------------------------------


class RpcMethod(BaseModel):
    """One registry entry, used for dispatch validation and schema dumps."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    params: type[BaseModel]
    result: type[BaseModel]
    direction: Direction = "c2s"
    summary: str = ""


def _m(
    name: str,
    params: type[BaseModel],
    result: type[BaseModel],
    summary: str,
    direction: Direction = "c2s",
) -> RpcMethod:
    return RpcMethod(name=name, params=params, result=result, direction=direction, summary=summary)


METHODS: dict[str, RpcMethod] = {
    m.name: m
    for m in (
        _m(
            "system.hello",
            HelloParams,
            HelloResult,
            "Authenticate a connection and agree on the protocol version.",
        ),
        _m(
            "system.info",
            Empty,
            InfoResult,
            "Report the daemon's version, pid, port, start time and home.",
        ),
        _m(
            "system.health",
            Empty,
            HealthResult,
            "Liveness probe; answers as long as the daemon serves requests.",
        ),
        _m("system.shutdown", Empty, Ok, "Ask the daemon to shut down gracefully."),
        _m(
            "session.create",
            SessionCreateParams,
            SessionCreateResult,
            "Open a session rooted at a working directory.",
        ),
        _m(
            "session.resume",
            SessionResumeParams,
            SessionResumeResult,
            "Replay the events a disconnected client missed.",
        ),
        _m("session.list", Empty, SessionListResult, "List every live session."),
        _m("session.close", SessionIdParams, Ok, "Close a session and release its resources."),
        _m(
            "session.prompt",
            SessionPromptParams,
            TurnResult,
            "Send user text to a session and start a turn.",
        ),
        _m("session.interrupt", SessionIdParams, Ok, "Stop the running turn as soon as possible."),
        _m(
            "session.setMode",
            SessionSetModeParams,
            SessionSetModeResult,
            "Switch a session between plan, accept and auto.",
        ),
        _m(
            "command.list",
            OptionalSessionParams,
            CommandListResult,
            "List the slash commands available to a session.",
        ),
        _m(
            "command.run",
            CommandRunParams,
            TurnResult,
            "Run a slash command; the only execution path for them.",
        ),
        _m(
            "tool.list",
            OptionalSessionParams,
            ToolListResult,
            "List the tools registered for a session.",
        ),
        _m(
            "approval.list",
            OptionalSessionParams,
            ApprovalListResult,
            "List tool calls still waiting for a decision.",
        ),
        _m(
            "approval.respond",
            ApprovalRespondParams,
            Ok,
            "Answer a pending approval and unblock the turn.",
        ),
        _m(
            "permission.allowlist.add",
            AllowlistAddParams,
            AllowlistAddResult,
            "Promote a pattern from ask to allow.",
        ),
        _m(
            "permission.allowlist.list",
            AllowlistListParams,
            AllowlistListResult,
            "List stored allowlist patterns.",
        ),
        _m(
            "permission.allowlist.remove",
            AllowlistRemoveParams,
            Ok,
            "Delete an allowlist pattern by id.",
        ),
        _m(
            "provider.list",
            Empty,
            ProviderListResult,
            "List chat providers and whether they are configured.",
        ),
        _m(
            "provider.configure",
            ProviderConfigureParams,
            Ok,
            "Store settings and credentials for a provider.",
        ),
        _m(
            "provider.loginWeb",
            ProviderLoginWebParams,
            Ok,
            "Start a browser-based login flow for a provider.",
        ),
        _m(
            "backend.set",
            BackendSetParams,
            Ok,
            "Choose where a session's tools execute: local, docker or ssh.",
        ),
        _m("agent.list", Empty, AgentListResult, "List the named agents that are defined."),
        _m(
            "agent.create",
            AgentCreateParams,
            AgentCreateResult,
            "Define a named agent from a description.",
        ),
        _m("agent.spawn", AgentSpawnParams, AgentSpawnResult, "Run a named agent on a task."),
        _m(
            "agent.bindChannel",
            AgentBindChannelParams,
            Ok,
            "Route a gateway channel to a named agent.",
        ),
        _m("agent.delete", AgentDeleteParams, Ok, "Delete a named agent."),
        _m("team.start", TeamStartParams, TeamStartResult, "Split a task across parallel workers."),
        _m("team.status", TeamStatusParams, TeamStatusResult, "Inspect a team's task board."),
        _m(
            "job.schedule",
            JobScheduleParams,
            JobScheduleResult,
            "Schedule a prompt to run unattended.",
        ),
        _m("job.list", Empty, JobListResult, "List scheduled jobs and their next run times."),
        _m("job.cancel", JobIdParams, Ok, "Cancel a scheduled job."),
        _m("job.runNow", JobIdParams, Ok, "Fire a scheduled job immediately."),
        _m(
            "gateway.bind",
            GatewayBindParams,
            GatewayBindResult,
            "Attach the daemon to a chat platform channel.",
        ),
        _m("gateway.list", Empty, GatewayListResult, "List live gateway bindings."),
        _m("gateway.unbind", GatewayUnbindParams, Ok, "Detach a gateway binding."),
        _m(
            "memory.search",
            MemorySearchParams,
            MemorySearchResult,
            "Recall stored memories matching a query.",
        ),
        _m("memory.write", MemoryWriteParams, MemoryWriteResult, "Store a memory with tags."),
        _m("skill.search", SkillSearchParams, SkillSearchResult, "Search available skills."),
        _m(
            "skill.install", SkillInstallParams, Ok, "Install a skill from a path, URL or registry."
        ),
        _m("skill.list", Empty, SkillListResult, "List installed skills."),
        _m("skill.reload", Empty, Ok, "Reload skills from disk without restarting."),
        _m(
            "approval.request",
            ApprovalRequest,
            ApprovalAnswer,
            "Ask the client to approve a tool call.",
            "s2c",
        ),
    )
}

EVENTS: dict[str, type[BaseModel]] = {
    "session.event": SessionEventNotification,
    "approval.resolved": ApprovalResolvedNotification,
    "job.event": JobEventNotification,
    "gateway.event": GatewayEventNotification,
}

CAPABILITIES: list[str] = ["sessions", "approvals", "commands", "tools"]

#: Where the daemon listens; mirrored into the schema dump for the SDK.
TRANSPORT: dict[str, Any] = {
    "ws": "/ws",
    "http": {"health": "/health", "version": "/version", "schema": "/protocol.json"},
}

#: Methods implemented at M1; everything else answers ``not_implemented``.
IMPLEMENTED_METHODS: frozenset[str] = frozenset(
    {
        "system.hello",
        "system.info",
        "system.health",
        "system.shutdown",
        "session.create",
        "session.resume",
        "session.list",
        "session.close",
        "session.prompt",
        "session.interrupt",
        "session.setMode",
        "command.list",
        "command.run",
        "tool.list",
        "approval.list",
        "approval.respond",
        "provider.list",
    }
)


def protocol_major(version: str) -> str:
    """Major component of a semver string (``"0.1.0"`` -> ``"0"``)."""
    return version.split(".", 1)[0]


def dump_schema() -> dict[str, Any]:
    """Machine-readable description of the whole protocol.

    This is exactly what ``GET /protocol.json`` returns and what
    ``scripts/gen_protocol.py`` consumes.  Schemas are plain
    ``model_json_schema()`` output, so local ``#/$defs/...`` references are
    resolved by the consumer.  ``version`` and ``protocolVersion`` are aliases
    of each other and always carry the same value.
    """
    methods: dict[str, Any] = {}
    for name, method in METHODS.items():
        entry: dict[str, Any] = {
            "direction": method.direction,
            "params": method.params.model_json_schema(),
            "result": method.result.model_json_schema(),
        }
        if method.summary:
            entry["summary"] = method.summary
        methods[name] = entry
    return {
        "version": PROTOCOL_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "serverVersion": SERVER_VERSION,
        "methods": methods,
        "events": {name: model.model_json_schema() for name, model in EVENTS.items()},
        "sessionEventKinds": {
            kind: model.model_json_schema() for kind, model in SESSION_EVENT_MODELS.items()
        },
        "errorCodes": list(ERROR_CODES),
        "capabilities": list(CAPABILITIES),
        "transport": TRANSPORT,
    }
