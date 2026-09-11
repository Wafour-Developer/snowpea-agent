// GENERATED — do not edit.
// Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py.
// Re-run `uv run python scripts/gen_protocol.py` after changing the protocol.

export const PROTOCOL_VERSION = "0.1.0";
export const WS_PATH = "/ws";
export const HTTP_ENDPOINTS = {
  health: "/health",
  schema: "/protocol.json",
  version: "/version",
} as const;

/** Values of JSON-RPC `error.data.code`. */
export type ErrorCode =
  | "approval_denied"
  | "approval_timeout"
  | "internal"
  | "invalid_params"
  | "login_unsupported"
  | "mode_denied"
  | "not_found"
  | "not_implemented"
  | "protocol_incompatible"
  | "tool_inactive"
  | "unauthorized";
export const ERROR_CODES: readonly ErrorCode[] = [
  "approval_denied",
  "approval_timeout",
  "internal",
  "invalid_params",
  "login_unsupported",
  "mode_denied",
  "not_found",
  "not_implemented",
  "protocol_incompatible",
  "tool_inactive",
  "unauthorized",
];

// ---------------------------------------------------------------------------
// Method params / results
// ---------------------------------------------------------------------------

/** `agent.bindChannel` params. */
export interface AgentBindChannelParams {
  channel: string;
  name: string;
}

/** `agent.bindChannel` result. */
export interface AgentBindChannelResult {
  ok?: boolean;
}

/** `agent.create` params. */
export interface AgentCreateParams {
  description: string;
}

/** `agent.create` result. */
export interface AgentCreateResult {
  name: string;
}

/** `agent.delete` params. */
export interface AgentDeleteParams {
  name: string;
}

/** `agent.delete` result. */
export interface AgentDeleteResult {
  ok?: boolean;
}

/** `agent.list` params. */
export type AgentListParams = Record<string, unknown>;

/** `agent.list` result. */
export interface AgentListResult {
  agents?: ({
    channel?: string | null;
    description?: string;
    name: string;
    source?: string;
  })[];
}

/** `agent.spawn` params. */
export interface AgentSpawnParams {
  name: string;
  sessionId?: string | null;
  task: string;
}

/** `agent.spawn` result. */
export interface AgentSpawnResult {
  agentId: string;
}

/** `approval.list` params. */
export interface ApprovalListParams {
  sessionId?: string | null;
}

/** `approval.list` result. */
export interface ApprovalListResult {
  requests?: ({
    args?: Record<string, unknown>;
    requestId: string;
    risk?: string;
    scopeHint?: "once" | "session" | "project" | "always";
    sessionId: string;
    timeoutSec?: number;
    tool: string;
  })[];
}

/** `approval.request` params. */
export interface ApprovalRequestParams {
  args?: Record<string, unknown>;
  requestId: string;
  risk?: string;
  scopeHint?: "once" | "session" | "project" | "always";
  sessionId: string;
  timeoutSec?: number;
  tool: string;
}

/** `approval.request` result. */
export interface ApprovalRequestResult {
  decision: "allow" | "deny";
  scope?: "once" | "session" | "project" | "always";
}

/** `approval.respond` params. */
export interface ApprovalRespondParams {
  decision: "allow" | "deny";
  requestId: string;
  scope?: "once" | "session" | "project" | "always";
}

/** `approval.respond` result. */
export interface ApprovalRespondResult {
  ok?: boolean;
}

/** `backend.set` params. */
export interface BackendSetParams {
  config?: Record<string, unknown>;
  kind: "local" | "docker" | "ssh";
  sessionId: string;
}

/** `backend.set` result. */
export interface BackendSetResult {
  ok?: boolean;
}

/** `command.list` params. */
export interface CommandListParams {
  sessionId?: string | null;
}

/** `command.list` result. */
export interface CommandListResult {
  commands?: ({
    argsSchema?: Record<string, unknown>;
    name: string;
    source?: "builtin" | "skill" | "plugin";
    summary: string;
  })[];
}

/** `command.run` params. */
export interface CommandRunParams {
  args?: string;
  name: string;
  sessionId: string;
}

/** `command.run` result. */
export interface CommandRunResult {
  turnId: string;
}

/** `gateway.bind` params. */
export interface GatewayBindParams {
  credentialsRef: string;
  platform: string;
  target: string;
}

/** `gateway.bind` result. */
export interface GatewayBindResult {
  bindingId: string;
}

/** `gateway.list` params. */
export type GatewayListParams = Record<string, unknown>;

/** `gateway.list` result. */
export interface GatewayListResult {
  bindings?: ({
    bindingId: string;
    platform: string;
    state?: "active" | "inactive";
    target: string;
  })[];
}

/** `gateway.unbind` params. */
export interface GatewayUnbindParams {
  bindingId: string;
}

/** `gateway.unbind` result. */
export interface GatewayUnbindResult {
  ok?: boolean;
}

/** `job.cancel` params. */
export interface JobCancelParams {
  jobId: string;
}

/** `job.cancel` result. */
export interface JobCancelResult {
  ok?: boolean;
}

/** `job.list` params. */
export type JobListParams = Record<string, unknown>;

/** `job.list` result. */
export interface JobListResult {
  jobs?: ({
    channel?: string | null;
    jobId: string;
    mode?: "plan" | "accept" | "auto";
    nextRunAt?: string | null;
    spec: string;
    state?: "scheduled" | "running" | "cancelled";
    task: string;
  })[];
}

/** `job.runNow` params. */
export interface JobRunNowParams {
  jobId: string;
}

/** `job.runNow` result. */
export interface JobRunNowResult {
  ok?: boolean;
}

/** `job.schedule` params. */
export interface JobScheduleParams {
  channel?: string | null;
  mode?: "plan" | "accept" | "auto";
  spec: string;
  task: string;
}

/** `job.schedule` result. */
export interface JobScheduleResult {
  jobId: string;
}

/** `memory.search` params. */
export interface MemorySearchParams {
  limit?: number;
  query: string;
}

/** `memory.search` result. */
export interface MemorySearchResult {
  hits?: ({
    id: string;
    score?: number;
    tags?: string[];
    text: string;
  })[];
}

/** `memory.write` params. */
export interface MemoryWriteParams {
  tags?: string[];
  text: string;
}

/** `memory.write` result. */
export interface MemoryWriteResult {
  id: string;
}

/** `permission.allowlist.add` params. */
export interface PermissionAllowlistAddParams {
  pattern: string;
  scope?: "session" | "project" | "always";
}

/** `permission.allowlist.add` result. */
export interface PermissionAllowlistAddResult {
  patternId: string;
}

/** `permission.allowlist.list` params. */
export interface PermissionAllowlistListParams {
  scope?: "session" | "project" | "always" | null;
}

/** `permission.allowlist.list` result. */
export interface PermissionAllowlistListResult {
  patterns?: ({
    pattern: string;
    patternId: string;
    scope: "session" | "project" | "always";
  })[];
}

/** `permission.allowlist.remove` params. */
export interface PermissionAllowlistRemoveParams {
  patternId: string;
}

/** `permission.allowlist.remove` result. */
export interface PermissionAllowlistRemoveResult {
  ok?: boolean;
}

/** `provider.configure` params. */
export interface ProviderConfigureParams {
  config?: Record<string, unknown>;
  vendor: string;
}

/** `provider.configure` result. */
export interface ProviderConfigureResult {
  ok?: boolean;
}

/** `provider.list` params. */
export type ProviderListParams = Record<string, unknown>;

/** `provider.list` result. */
export interface ProviderListResult {
  providers?: ({
    configured?: boolean;
    default?: boolean;
    models?: string[];
    vendor: string;
  })[];
}

/** `provider.loginWeb` params. */
export interface ProviderLoginWebParams {
  method: string;
  vendor: string;
}

/** `provider.loginWeb` result. */
export interface ProviderLoginWebResult {
  ok?: boolean;
}

/** `session.close` params. */
export interface SessionCloseParams {
  sessionId: string;
}

/** `session.close` result. */
export interface SessionCloseResult {
  ok?: boolean;
}

/** `session.create` params. */
export interface SessionCreateParams {
  agent?: string | null;
  maxConcurrent?: number | null;
  mode?: "plan" | "accept" | "auto" | null;
  model?: string | null;
  originSurface?: string | null;
  provider?: string | null;
  workdir: string;
}

/** `session.create` result. */
export interface SessionCreateResult {
  sessionId: string;
}

/** `session.interrupt` params. */
export interface SessionInterruptParams {
  sessionId: string;
}

/** `session.interrupt` result. */
export interface SessionInterruptResult {
  ok?: boolean;
}

/** `session.list` params. */
export type SessionListParams = Record<string, unknown>;

/** `session.list` result. */
export interface SessionListResult {
  sessions?: ({
    createdAt: string;
    mode: "plan" | "accept" | "auto";
    model?: string | null;
    originSurface?: string | null;
    provider?: string | null;
    seq?: number;
    sessionId: string;
    workdir: string;
  })[];
}

/** `session.prompt` params. */
export interface SessionPromptParams {
  attachments?: ({
    kind?: "file" | "image" | "text";
    mimeType?: string | null;
    path?: string | null;
    text?: string | null;
  })[] | null;
  sessionId: string;
  text: string;
}

/** `session.prompt` result. */
export interface SessionPromptResult {
  turnId: string;
}

/** `session.resume` params. */
export interface SessionResumeParams {
  afterSeq?: number | null;
  sessionId: string;
}

/** `session.resume` result. */
export interface SessionResumeResult {
  events?: ({
    kind: string;
    payload?: Record<string, unknown>;
    seq: number;
    sessionId: string;
    ts: string;
  })[];
  sessionId: string;
}

/** `session.setMode` params. */
export interface SessionSetModeParams {
  mode: "plan" | "accept" | "auto";
  sessionId: string;
}

/** `session.setMode` result. */
export interface SessionSetModeResult {
  mode: "plan" | "accept" | "auto";
}

/** `skill.install` params. */
export interface SkillInstallParams {
  source: string;
}

/** `skill.install` result. */
export interface SkillInstallResult {
  ok?: boolean;
}

/** `skill.list` params. */
export type SkillListParams = Record<string, unknown>;

/** `skill.list` result. */
export interface SkillListResult {
  skills?: ({
    installed?: boolean;
    name: string;
    source?: string;
    summary?: string;
  })[];
}

/** `skill.reload` params. */
export type SkillReloadParams = Record<string, unknown>;

/** `skill.reload` result. */
export interface SkillReloadResult {
  ok?: boolean;
}

/** `skill.search` params. */
export interface SkillSearchParams {
  query: string;
}

/** `skill.search` result. */
export interface SkillSearchResult {
  skills?: ({
    installed?: boolean;
    name: string;
    source?: string;
    summary?: string;
  })[];
}

/** `system.health` params. */
export type SystemHealthParams = Record<string, unknown>;

/** `system.health` result. */
export interface SystemHealthResult {
  status?: "ok";
}

/** `system.hello` params. */
export interface SystemHelloParams {
  clientVersion: string;
  protocolVersion: string;
  token: string;
}

/** `system.hello` result. */
export interface SystemHelloResult {
  capabilities?: string[];
  protocolVersion: string;
  serverVersion: string;
}

/** `system.info` params. */
export type SystemInfoParams = Record<string, unknown>;

/** `system.info` result. */
export interface SystemInfoResult {
  counters?: Record<string, number>;
  home: string;
  lifecycle?: {
    reason?: string;
    secondsUntilExit?: number | null;
    willExit?: boolean;
  } | null;
  pid: number;
  port: number;
  protocolVersion: string;
  startedAt: string;
  version: string;
}

/** `system.shutdown` params. */
export type SystemShutdownParams = Record<string, unknown>;

/** `system.shutdown` result. */
export interface SystemShutdownResult {
  ok?: boolean;
}

/** `team.start` params. */
export interface TeamStartParams {
  n: number;
  sessionId: string;
  task: string;
}

/** `team.start` result. */
export interface TeamStartResult {
  teamId: string;
}

/** `team.status` params. */
export interface TeamStatusParams {
  teamId: string;
}

/** `team.status` result. */
export interface TeamStatusResult {
  state?: "running" | "done" | "failed";
  tasks?: ({
    assignee?: string | null;
    status?: "pending" | "running" | "done" | "failed";
    taskId: string;
    title: string;
  })[];
  teamId: string;
}

/** `tool.list` params. */
export interface ToolListParams {
  sessionId?: string | null;
}

/** `tool.list` result. */
export interface ToolListResult {
  tools?: ({
    category: string;
    description?: string;
    name: string;
    permissionTag: "read" | "write" | "exec" | "network" | "send";
    source?: string;
    state?: "active" | "inactive";
  })[];
}

// ---------------------------------------------------------------------------
// Event payloads
// ---------------------------------------------------------------------------

/** `approval.resolved` notification payload. */
export interface ApprovalResolvedPayload {
  by: string;
  decision: "allow" | "deny";
  requestId: string;
}

/** `gateway.event` notification payload. */
export interface GatewayEventPayload {
  bindingId: string;
  kind: string;
  payload?: Record<string, unknown>;
}

/** `job.event` notification payload. */
export interface JobEventPayload {
  jobId: string;
  kind: string;
  payload?: Record<string, unknown>;
}

/** `session.event` notification payload. */
export interface SessionEventPayload {
  kind: string;
  payload?: Record<string, unknown>;
  seq: number;
  sessionId: string;
  ts: string;
}

// ---------------------------------------------------------------------------
// session.event payloads by kind
// ---------------------------------------------------------------------------

/** Payload of `session.event` with kind `diff`. */
export interface DiffEventPayload {
  kind?: "diff";
  patch: string;
  path: string;
}

/** Payload of `session.event` with kind `error`. */
export interface ErrorEventPayload {
  code: string;
  kind?: "error";
  message: string;
}

/** Payload of `session.event` with kind `message.delta`. */
export interface MessageDeltaEventPayload {
  kind?: "message.delta";
  text: string;
}

/** Payload of `session.event` with kind `message.done`. */
export interface MessageDoneEventPayload {
  kind?: "message.done";
  role?: "assistant" | "user" | "system";
  text: string;
}

/** Payload of `session.event` with kind `mode.changed`. */
export interface ModeChangedEventPayload {
  kind?: "mode.changed";
  mode: "plan" | "accept" | "auto";
}

/** Payload of `session.event` with kind `subagent.done`. */
export interface SubagentDoneEventPayload {
  agentId: string;
  kind?: "subagent.done";
  ok?: boolean;
  result?: string;
}

/** Payload of `session.event` with kind `subagent.spawn`. */
export interface SubagentSpawnEventPayload {
  agentId: string;
  kind?: "subagent.spawn";
  name?: string;
  task?: string;
}

/** Payload of `session.event` with kind `subagent.update`. */
export interface SubagentUpdateEventPayload {
  agentId: string;
  kind?: "subagent.update";
  status?: string;
  text?: string;
}

/** Payload of `session.event` with kind `team.task.update`. */
export interface TeamTaskUpdateEventPayload {
  assignee?: string | null;
  kind?: "team.task.update";
  status?: "pending" | "running" | "done" | "failed";
  taskId: string;
  teamId: string;
}

/** Payload of `session.event` with kind `tool.call`. */
export interface ToolCallEventPayload {
  args?: Record<string, unknown>;
  callId: string;
  kind?: "tool.call";
  name: string;
}

/** Payload of `session.event` with kind `tool.result`. */
export interface ToolResultEventPayload {
  callId: string;
  error?: string | null;
  kind?: "tool.result";
  name: string;
  ok: boolean;
  output?: string;
}

/** Payload of `session.event` with kind `turn.done`. */
export interface TurnDoneEventPayload {
  kind?: "turn.done";
  reason?: "complete" | "interrupted" | "error" | "denied" | "timeout";
  turnId: string;
}

/** Payload of `session.event` with kind `usage`. */
export interface UsageEventPayload {
  inputTokens?: number;
  kind?: "usage";
  outputTokens?: number;
}

/** Maps every `session.event` kind to its payload type. */
export interface SessionEventKindMap {
  "diff": DiffEventPayload;
  "error": ErrorEventPayload;
  "message.delta": MessageDeltaEventPayload;
  "message.done": MessageDoneEventPayload;
  "mode.changed": ModeChangedEventPayload;
  "subagent.done": SubagentDoneEventPayload;
  "subagent.spawn": SubagentSpawnEventPayload;
  "subagent.update": SubagentUpdateEventPayload;
  "team.task.update": TeamTaskUpdateEventPayload;
  "tool.call": ToolCallEventPayload;
  "tool.result": ToolResultEventPayload;
  "turn.done": TurnDoneEventPayload;
  "usage": UsageEventPayload;
}

export type SessionEventKind = keyof SessionEventKindMap;
export const SESSION_EVENT_KINDS: readonly SessionEventKind[] = [
  "diff",
  "error",
  "message.delta",
  "message.done",
  "mode.changed",
  "subagent.done",
  "subagent.spawn",
  "subagent.update",
  "team.task.update",
  "tool.call",
  "tool.result",
  "turn.done",
  "usage",
];

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/** Every JSON-RPC method, with its params and result types. */
export interface MethodMap {
  "agent.bindChannel": { params: AgentBindChannelParams; result: AgentBindChannelResult };
  "agent.create": { params: AgentCreateParams; result: AgentCreateResult };
  "agent.delete": { params: AgentDeleteParams; result: AgentDeleteResult };
  "agent.list": { params: AgentListParams; result: AgentListResult };
  "agent.spawn": { params: AgentSpawnParams; result: AgentSpawnResult };
  "approval.list": { params: ApprovalListParams; result: ApprovalListResult };
  "approval.request": { params: ApprovalRequestParams; result: ApprovalRequestResult };
  "approval.respond": { params: ApprovalRespondParams; result: ApprovalRespondResult };
  "backend.set": { params: BackendSetParams; result: BackendSetResult };
  "command.list": { params: CommandListParams; result: CommandListResult };
  "command.run": { params: CommandRunParams; result: CommandRunResult };
  "gateway.bind": { params: GatewayBindParams; result: GatewayBindResult };
  "gateway.list": { params: GatewayListParams; result: GatewayListResult };
  "gateway.unbind": { params: GatewayUnbindParams; result: GatewayUnbindResult };
  "job.cancel": { params: JobCancelParams; result: JobCancelResult };
  "job.list": { params: JobListParams; result: JobListResult };
  "job.runNow": { params: JobRunNowParams; result: JobRunNowResult };
  "job.schedule": { params: JobScheduleParams; result: JobScheduleResult };
  "memory.search": { params: MemorySearchParams; result: MemorySearchResult };
  "memory.write": { params: MemoryWriteParams; result: MemoryWriteResult };
  "permission.allowlist.add": { params: PermissionAllowlistAddParams; result: PermissionAllowlistAddResult };
  "permission.allowlist.list": { params: PermissionAllowlistListParams; result: PermissionAllowlistListResult };
  "permission.allowlist.remove": { params: PermissionAllowlistRemoveParams; result: PermissionAllowlistRemoveResult };
  "provider.configure": { params: ProviderConfigureParams; result: ProviderConfigureResult };
  "provider.list": { params: ProviderListParams; result: ProviderListResult };
  "provider.loginWeb": { params: ProviderLoginWebParams; result: ProviderLoginWebResult };
  "session.close": { params: SessionCloseParams; result: SessionCloseResult };
  "session.create": { params: SessionCreateParams; result: SessionCreateResult };
  "session.interrupt": { params: SessionInterruptParams; result: SessionInterruptResult };
  "session.list": { params: SessionListParams; result: SessionListResult };
  "session.prompt": { params: SessionPromptParams; result: SessionPromptResult };
  "session.resume": { params: SessionResumeParams; result: SessionResumeResult };
  "session.setMode": { params: SessionSetModeParams; result: SessionSetModeResult };
  "skill.install": { params: SkillInstallParams; result: SkillInstallResult };
  "skill.list": { params: SkillListParams; result: SkillListResult };
  "skill.reload": { params: SkillReloadParams; result: SkillReloadResult };
  "skill.search": { params: SkillSearchParams; result: SkillSearchResult };
  "system.health": { params: SystemHealthParams; result: SystemHealthResult };
  "system.hello": { params: SystemHelloParams; result: SystemHelloResult };
  "system.info": { params: SystemInfoParams; result: SystemInfoResult };
  "system.shutdown": { params: SystemShutdownParams; result: SystemShutdownResult };
  "team.start": { params: TeamStartParams; result: TeamStartResult };
  "team.status": { params: TeamStatusParams; result: TeamStatusResult };
  "tool.list": { params: ToolListParams; result: ToolListResult };
}

export type MethodName = keyof MethodMap;
export type MethodParams<M extends MethodName> = MethodMap[M]["params"];
export type MethodResult<M extends MethodName> = MethodMap[M]["result"];

/** Methods the client calls on the server. */
export type ClientMethod =
  | "agent.bindChannel"
  | "agent.create"
  | "agent.delete"
  | "agent.list"
  | "agent.spawn"
  | "approval.list"
  | "approval.respond"
  | "backend.set"
  | "command.list"
  | "command.run"
  | "gateway.bind"
  | "gateway.list"
  | "gateway.unbind"
  | "job.cancel"
  | "job.list"
  | "job.runNow"
  | "job.schedule"
  | "memory.search"
  | "memory.write"
  | "permission.allowlist.add"
  | "permission.allowlist.list"
  | "permission.allowlist.remove"
  | "provider.configure"
  | "provider.list"
  | "provider.loginWeb"
  | "session.close"
  | "session.create"
  | "session.interrupt"
  | "session.list"
  | "session.prompt"
  | "session.resume"
  | "session.setMode"
  | "skill.install"
  | "skill.list"
  | "skill.reload"
  | "skill.search"
  | "system.health"
  | "system.hello"
  | "system.info"
  | "system.shutdown"
  | "team.start"
  | "team.status"
  | "tool.list";
/** Methods the server calls on the client (bidirectional JSON-RPC). */
export type ServerMethod =
  | "approval.request";

/** Every server→client notification, with its payload type. */
export interface EventMap {
  "approval.resolved": ApprovalResolvedPayload;
  "gateway.event": GatewayEventPayload;
  "job.event": JobEventPayload;
  "session.event": SessionEventPayload;
}

export type EventName = keyof EventMap;
export const EVENT_NAMES: readonly EventName[] = [
  "approval.resolved",
  "gateway.event",
  "job.event",
  "session.event",
];
