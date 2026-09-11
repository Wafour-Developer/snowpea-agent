"""Protocol single source of truth.

Every JSON-RPC method and notification the daemon speaks is declared here as a
pydantic v2 model.  ``scripts/gen_protocol.py`` (M1, US-008) turns
:func:`dump_schema` into ``sdk/src/protocol.ts`` and ``docs/protocol.md``, so
nothing else in the tree may invent a method name or a payload field.

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
BackendKind = Literal["local", "docker", "ssh"]
TurnReason = Literal["complete", "interrupted", "error", "denied", "timeout"]
Direction = Literal["c2s", "s2c"]


class Payload(BaseModel):
    """Base for every wire model: JSON objects, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class Empty(Payload):
    """A method that takes no parameters (``params`` is still an object)."""


class Ok(Payload):
    ok: bool = True


# --------------------------------------------------------------------------
# system.*
# --------------------------------------------------------------------------


class HelloParams(Payload):
    token: str
    clientVersion: str
    protocolVersion: str


class HelloResult(Payload):
    protocolVersion: str
    serverVersion: str
    capabilities: list[str] = Field(default_factory=list)


class InfoResult(Payload):
    version: str
    protocolVersion: str
    pid: int
    port: int
    startedAt: str
    home: str


class HealthResult(Payload):
    status: Literal["ok"] = "ok"


# --------------------------------------------------------------------------
# session.*
# --------------------------------------------------------------------------


class Attachment(Payload):
    kind: Literal["file", "image", "text"] = "file"
    path: str | None = None
    mimeType: str | None = None
    text: str | None = None


class SessionCreateParams(Payload):
    workdir: str
    mode: Mode | None = None
    provider: str | None = None
    model: str | None = None
    agent: str | None = None
    maxConcurrent: int | None = None
    originSurface: str | None = None


class SessionCreateResult(Payload):
    sessionId: str


class SessionEvent(Payload):
    """One entry of a session's ordered event log."""

    sessionId: str
    seq: int
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str


class SessionResumeParams(Payload):
    sessionId: str
    afterSeq: int | None = None


class SessionResumeResult(Payload):
    sessionId: str
    events: list[SessionEvent] = Field(default_factory=list)


class SessionSummary(Payload):
    sessionId: str
    workdir: str
    mode: Mode
    provider: str | None = None
    model: str | None = None
    originSurface: str | None = None
    createdAt: str
    seq: int = 0


class SessionListResult(Payload):
    sessions: list[SessionSummary] = Field(default_factory=list)


class SessionIdParams(Payload):
    sessionId: str


class SessionPromptParams(Payload):
    sessionId: str
    text: str
    attachments: list[Attachment] | None = None


class TurnResult(Payload):
    turnId: str


class SessionSetModeParams(Payload):
    sessionId: str
    mode: Mode


class SessionSetModeResult(Payload):
    mode: Mode


# --------------------------------------------------------------------------
# command.* / tool.*
# --------------------------------------------------------------------------


class OptionalSessionParams(Payload):
    sessionId: str | None = None


class CommandInfo(Payload):
    name: str
    summary: str
    argsSchema: dict[str, Any] = Field(default_factory=dict)
    source: CommandSource = "builtin"


class CommandListResult(Payload):
    commands: list[CommandInfo] = Field(default_factory=list)


class CommandRunParams(Payload):
    sessionId: str
    name: str
    args: str = ""


class ToolInfo(Payload):
    name: str
    category: str
    permissionTag: PermissionTag
    state: ToolState = "active"
    source: str = "builtin"
    description: str = ""


class ToolListResult(Payload):
    tools: list[ToolInfo] = Field(default_factory=list)


# --------------------------------------------------------------------------
# approval.* / permission.*
# --------------------------------------------------------------------------


class ApprovalRequest(Payload):
    requestId: str
    sessionId: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk: str = "low"
    timeoutSec: int = 300
    scopeHint: ApprovalScope = "once"


class ApprovalListResult(Payload):
    requests: list[ApprovalRequest] = Field(default_factory=list)


class ApprovalRespondParams(Payload):
    requestId: str
    decision: Decision
    scope: ApprovalScope = "once"


class ApprovalAnswer(Payload):
    """Result of the server->client ``approval.request``."""

    decision: Decision
    scope: ApprovalScope = "once"


class AllowlistAddParams(Payload):
    pattern: str
    scope: Literal["session", "project", "always"] = "project"


class AllowlistAddResult(Payload):
    patternId: str


class AllowlistListParams(Payload):
    scope: Literal["session", "project", "always"] | None = None


class AllowlistPattern(Payload):
    patternId: str
    pattern: str
    scope: Literal["session", "project", "always"]


class AllowlistListResult(Payload):
    patterns: list[AllowlistPattern] = Field(default_factory=list)


class AllowlistRemoveParams(Payload):
    patternId: str


# --------------------------------------------------------------------------
# provider.* / backend.*
# --------------------------------------------------------------------------


class ProviderInfo(Payload):
    vendor: str
    models: list[str] = Field(default_factory=list)
    configured: bool = False
    default: bool = False


class ProviderListResult(Payload):
    providers: list[ProviderInfo] = Field(default_factory=list)


class ProviderConfigureParams(Payload):
    vendor: str
    config: dict[str, Any] = Field(default_factory=dict)


class ProviderLoginWebParams(Payload):
    vendor: str
    method: str


class BackendSetParams(Payload):
    sessionId: str
    kind: BackendKind
    config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# agent.* / team.*
# --------------------------------------------------------------------------


class AgentInfo(Payload):
    name: str
    description: str = ""
    channel: str | None = None
    source: str = "user"


class AgentListResult(Payload):
    agents: list[AgentInfo] = Field(default_factory=list)


class AgentCreateParams(Payload):
    description: str


class AgentCreateResult(Payload):
    name: str


class AgentSpawnParams(Payload):
    name: str
    task: str
    sessionId: str | None = None


class AgentSpawnResult(Payload):
    agentId: str


class AgentBindChannelParams(Payload):
    name: str
    channel: str


class AgentDeleteParams(Payload):
    name: str


class TeamStartParams(Payload):
    sessionId: str
    n: int
    task: str


class TeamStartResult(Payload):
    teamId: str


class TeamStatusParams(Payload):
    teamId: str


class TeamTask(Payload):
    taskId: str
    title: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assignee: str | None = None


class TeamStatusResult(Payload):
    teamId: str
    state: Literal["running", "done", "failed"] = "running"
    tasks: list[TeamTask] = Field(default_factory=list)


# --------------------------------------------------------------------------
# job.*
# --------------------------------------------------------------------------


class JobScheduleParams(Payload):
    spec: str
    task: str
    mode: Mode = "accept"
    channel: str | None = None


class JobScheduleResult(Payload):
    jobId: str


class JobInfo(Payload):
    jobId: str
    spec: str
    task: str
    mode: Mode = "accept"
    channel: str | None = None
    nextRunAt: str | None = None
    state: Literal["scheduled", "running", "cancelled"] = "scheduled"


class JobListResult(Payload):
    jobs: list[JobInfo] = Field(default_factory=list)


class JobIdParams(Payload):
    jobId: str


# --------------------------------------------------------------------------
# gateway.*
# --------------------------------------------------------------------------


class GatewayBindParams(Payload):
    platform: str
    credentialsRef: str
    target: str


class GatewayBindResult(Payload):
    bindingId: str


class GatewayBinding(Payload):
    bindingId: str
    platform: str
    target: str
    state: Literal["active", "inactive"] = "active"


class GatewayListResult(Payload):
    bindings: list[GatewayBinding] = Field(default_factory=list)


class GatewayUnbindParams(Payload):
    bindingId: str


# --------------------------------------------------------------------------
# memory.* / skill.*
# --------------------------------------------------------------------------


class MemorySearchParams(Payload):
    query: str
    limit: int = 10


class MemoryHit(Payload):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0


class MemorySearchResult(Payload):
    hits: list[MemoryHit] = Field(default_factory=list)


class MemoryWriteParams(Payload):
    text: str
    tags: list[str] = Field(default_factory=list)


class MemoryWriteResult(Payload):
    id: str


class SkillSearchParams(Payload):
    query: str


class SkillInfo(Payload):
    name: str
    summary: str = ""
    source: str = "builtin"
    installed: bool = False


class SkillSearchResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list)


class SkillInstallParams(Payload):
    source: str


class SkillListResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list)


# --------------------------------------------------------------------------
# session.event kinds
# --------------------------------------------------------------------------


class MessageDelta(Payload):
    kind: Literal["message.delta"] = "message.delta"
    text: str


class MessageDone(Payload):
    kind: Literal["message.done"] = "message.done"
    text: str
    role: Literal["assistant", "user", "system"] = "assistant"


class ToolCallEvent(Payload):
    kind: Literal["tool.call"] = "tool.call"
    callId: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(Payload):
    kind: Literal["tool.result"] = "tool.result"
    callId: str
    name: str
    ok: bool
    output: str = ""
    error: str | None = None


class DiffEvent(Payload):
    kind: Literal["diff"] = "diff"
    path: str
    patch: str


class SubagentSpawn(Payload):
    kind: Literal["subagent.spawn"] = "subagent.spawn"
    agentId: str
    name: str = ""
    task: str = ""


class SubagentUpdate(Payload):
    kind: Literal["subagent.update"] = "subagent.update"
    agentId: str
    status: str = ""
    text: str = ""


class SubagentDone(Payload):
    kind: Literal["subagent.done"] = "subagent.done"
    agentId: str
    ok: bool = True
    result: str = ""


class TeamTaskUpdate(Payload):
    kind: Literal["team.task.update"] = "team.task.update"
    teamId: str
    taskId: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assignee: str | None = None


class ModeChanged(Payload):
    kind: Literal["mode.changed"] = "mode.changed"
    mode: Mode


class UsageEvent(Payload):
    kind: Literal["usage"] = "usage"
    inputTokens: int = 0
    outputTokens: int = 0


class ErrorEvent(Payload):
    kind: Literal["error"] = "error"
    code: str
    message: str


class TurnDone(Payload):
    kind: Literal["turn.done"] = "turn.done"
    turnId: str
    reason: TurnReason = "complete"


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

SESSION_EVENT_KINDS: tuple[str, ...] = (
    "message.delta",
    "message.done",
    "tool.call",
    "tool.result",
    "diff",
    "subagent.spawn",
    "subagent.update",
    "subagent.done",
    "team.task.update",
    "mode.changed",
    "usage",
    "error",
    "turn.done",
)


class SessionEventNotification(Payload):
    """``session.event`` notification body (payload discriminated by ``kind``)."""

    sessionId: str
    seq: int
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str


class SessionEventKindEnvelope(Payload):
    """Typed view of ``session.event`` used for schema generation."""

    event: SessionEventPayload


class ApprovalResolvedNotification(Payload):
    requestId: str
    decision: Decision
    by: str


class JobEventNotification(Payload):
    jobId: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class GatewayEventNotification(Payload):
    bindingId: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


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


def _m(
    name: str,
    params: type[BaseModel],
    result: type[BaseModel],
    direction: Direction = "c2s",
) -> RpcMethod:
    return RpcMethod(name=name, params=params, result=result, direction=direction)


METHODS: dict[str, RpcMethod] = {
    m.name: m
    for m in (
        _m("system.hello", HelloParams, HelloResult),
        _m("system.info", Empty, InfoResult),
        _m("system.health", Empty, HealthResult),
        _m("system.shutdown", Empty, Ok),
        _m("session.create", SessionCreateParams, SessionCreateResult),
        _m("session.resume", SessionResumeParams, SessionResumeResult),
        _m("session.list", Empty, SessionListResult),
        _m("session.close", SessionIdParams, Ok),
        _m("session.prompt", SessionPromptParams, TurnResult),
        _m("session.interrupt", SessionIdParams, Ok),
        _m("session.setMode", SessionSetModeParams, SessionSetModeResult),
        _m("command.list", OptionalSessionParams, CommandListResult),
        _m("command.run", CommandRunParams, TurnResult),
        _m("tool.list", OptionalSessionParams, ToolListResult),
        _m("approval.list", OptionalSessionParams, ApprovalListResult),
        _m("approval.respond", ApprovalRespondParams, Ok),
        _m("permission.allowlist.add", AllowlistAddParams, AllowlistAddResult),
        _m("permission.allowlist.list", AllowlistListParams, AllowlistListResult),
        _m("permission.allowlist.remove", AllowlistRemoveParams, Ok),
        _m("provider.list", Empty, ProviderListResult),
        _m("provider.configure", ProviderConfigureParams, Ok),
        _m("provider.loginWeb", ProviderLoginWebParams, Ok),
        _m("backend.set", BackendSetParams, Ok),
        _m("agent.list", Empty, AgentListResult),
        _m("agent.create", AgentCreateParams, AgentCreateResult),
        _m("agent.spawn", AgentSpawnParams, AgentSpawnResult),
        _m("agent.bindChannel", AgentBindChannelParams, Ok),
        _m("agent.delete", AgentDeleteParams, Ok),
        _m("team.start", TeamStartParams, TeamStartResult),
        _m("team.status", TeamStatusParams, TeamStatusResult),
        _m("job.schedule", JobScheduleParams, JobScheduleResult),
        _m("job.list", Empty, JobListResult),
        _m("job.cancel", JobIdParams, Ok),
        _m("job.runNow", JobIdParams, Ok),
        _m("gateway.bind", GatewayBindParams, GatewayBindResult),
        _m("gateway.list", Empty, GatewayListResult),
        _m("gateway.unbind", GatewayUnbindParams, Ok),
        _m("memory.search", MemorySearchParams, MemorySearchResult),
        _m("memory.write", MemoryWriteParams, MemoryWriteResult),
        _m("skill.search", SkillSearchParams, SkillSearchResult),
        _m("skill.install", SkillInstallParams, Ok),
        _m("skill.list", Empty, SkillListResult),
        _m("skill.reload", Empty, Ok),
        _m("approval.request", ApprovalRequest, ApprovalAnswer, "s2c"),
    )
}

EVENTS: dict[str, type[BaseModel]] = {
    "session.event": SessionEventNotification,
    "approval.resolved": ApprovalResolvedNotification,
    "job.event": JobEventNotification,
    "gateway.event": GatewayEventNotification,
}

CAPABILITIES: list[str] = ["sessions", "approvals", "commands", "tools"]

#: Methods implemented at M1; everything else answers ``not_implemented``.
IMPLEMENTED_METHODS: frozenset[str] = frozenset(
    {"system.hello", "system.info", "system.health", "system.shutdown"}
)


def protocol_major(version: str) -> str:
    """Major component of a semver string (``"0.1.0"`` -> ``"0"``)."""
    return version.split(".", 1)[0]


def dump_schema() -> dict[str, Any]:
    """Machine-readable description of the whole protocol."""
    methods: dict[str, Any] = {}
    for name, method in METHODS.items():
        methods[name] = {
            "direction": method.direction,
            "params": method.params.model_json_schema(),
            "result": method.result.model_json_schema(),
        }
    events: dict[str, Any] = {
        name: {"direction": "s2c", "params": model.model_json_schema()}
        for name, model in EVENTS.items()
    }
    return {
        "version": PROTOCOL_VERSION,
        "serverVersion": SERVER_VERSION,
        "methods": methods,
        "events": events,
        "sessionEventKinds": list(SESSION_EVENT_KINDS),
        "sessionEventPayloads": SessionEventKindEnvelope.model_json_schema(),
        "errorCodes": list(ERROR_CODES),
        "capabilities": list(CAPABILITIES),
    }
