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

/** `agent.bindChannel` params. Route a gateway channel to a named agent. */
export interface AgentBindChannelParams {
  /** Gateway channel that will reach the agent. */
  channel: string;
  /** Agent to bind. */
  name: string;
}

/** `agent.bindChannel` result. */
export interface AgentBindChannelResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `agent.create` params. Define a named agent from a description. */
export interface AgentCreateParams {
  /** Natural-language brief the daemon turns into an agent. */
  description: string;
}

/** `agent.create` result. */
export interface AgentCreateResult {
  /** Name assigned to the new agent. */
  name: string;
}

/** `agent.delete` params. Delete a named agent. */
export interface AgentDeleteParams {
  /** Agent to delete. */
  name: string;
}

/** `agent.delete` result. */
export interface AgentDeleteResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `agent.list` params. List the named agents that are defined. */
export type AgentListParams = Record<string, unknown>;

/** `agent.list` result. */
export interface AgentListResult {
  /** Defined named agents. */
  agents?: ({
    /** Gateway channel bound to the agent. */
    channel?: string | null;
    /** What the agent is for. */
    description?: string;
    /** Agent name used by agent.spawn. */
    name: string;
    /** Where the definition came from. */
    source?: string;
  })[];
}

/** `agent.spawn` params. Run a named agent on a task. */
export interface AgentSpawnParams {
  /** Agent to run. */
  name: string;
  /** Parent session, when spawned from one. */
  sessionId?: string | null;
  /** Task handed to the agent. */
  task: string;
}

/** `agent.spawn` result. */
export interface AgentSpawnResult {
  /** Id correlating the subagent.* events. */
  agentId: string;
}

/** `approval.list` params. List tool calls still waiting for a decision. */
export interface ApprovalListParams {
  /** Scope the listing to one session; omit for the global set. */
  sessionId?: string | null;
}

/** `approval.list` result. */
export interface ApprovalListResult {
  /** Approvals still pending. */
  requests?: ({
    /** Arguments it wants to use. */
    args?: Record<string, unknown>;
    /** Id to answer with approval.respond. */
    requestId: string;
    /** Risk hint for the UI. */
    risk?: string;
    /** Scope the UI should preselect. */
    scopeHint?: "once" | "session" | "project" | "always";
    /** Session whose turn is blocked. */
    sessionId: string;
    /** Seconds before the request auto-denies. */
    timeoutSec?: number;
    /** Tool the model wants to run. */
    tool: string;
  })[];
}

/** `approval.request` params. Ask the client to approve a tool call. */
export interface ApprovalRequestParams {
  /** Arguments it wants to use. */
  args?: Record<string, unknown>;
  /** Id to answer with approval.respond. */
  requestId: string;
  /** Risk hint for the UI. */
  risk?: string;
  /** Scope the UI should preselect. */
  scopeHint?: "once" | "session" | "project" | "always";
  /** Session whose turn is blocked. */
  sessionId: string;
  /** Seconds before the request auto-denies. */
  timeoutSec?: number;
  /** Tool the model wants to run. */
  tool: string;
}

/** `approval.request` result. */
export interface ApprovalRequestResult {
  /** The human's decision. */
  decision: "allow" | "deny";
  /** How long the decision applies. */
  scope?: "once" | "session" | "project" | "always";
}

/** `approval.respond` params. Answer a pending approval and unblock the turn. */
export interface ApprovalRespondParams {
  /** allow runs the tool, deny ends the turn. */
  decision: "allow" | "deny";
  /** Request being answered. */
  requestId: string;
  /** How long the decision applies. */
  scope?: "once" | "session" | "project" | "always";
}

/** `approval.respond` result. */
export interface ApprovalRespondResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `backend.set` params. Choose where a session's tools execute: local, docker or ssh. */
export interface BackendSetParams {
  /** Backend settings, e.g. container or SSH target. */
  config?: Record<string, unknown>;
  /** Where tools execute. */
  kind: "local" | "docker" | "ssh";
  /** Session whose execution backend changes. */
  sessionId: string;
}

/** `backend.set` result. */
export interface BackendSetResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `command.list` params. List the slash commands available to a session. */
export interface CommandListParams {
  /** Scope the listing to one session; omit for the global set. */
  sessionId?: string | null;
}

/** `command.list` result. */
export interface CommandListResult {
  /** Available slash commands. */
  commands?: ({
    /** JSON Schema for the argument string. */
    argsSchema?: Record<string, unknown>;
    /** Command name without the leading slash. */
    name: string;
    /** Where the command came from. */
    source?: "builtin" | "skill" | "plugin";
    /** One-line description shown in /help. */
    summary: string;
  })[];
}

/** `command.run` params. Run a slash command; the only execution path for them. */
export interface CommandRunParams {
  /** Raw argument string, as typed after the name. */
  args?: string;
  /** Command name without the leading slash. */
  name: string;
  /** Session to run the command in. */
  sessionId: string;
}

/** `command.run` result. */
export interface CommandRunResult {
  /** Id of the started turn; turn.done carries it back. */
  turnId: string;
}

/** `gateway.bind` params. Attach the daemon to a chat platform channel. */
export interface GatewayBindParams {
  /** Name of the stored credential to use. */
  credentialsRef: string;
  /** Chat platform key, e.g. 'slack'. */
  platform: string;
  /** Channel, room or chat id to attach to. */
  target: string;
}

/** `gateway.bind` result. */
export interface GatewayBindResult {
  /** Id used to unbind later. */
  bindingId: string;
}

/** `gateway.list` params. List live gateway bindings. */
export type GatewayListParams = Record<string, unknown>;

/** `gateway.list` result. */
export interface GatewayListResult {
  /** Live gateway bindings. */
  bindings?: ({
    /** Binding id. */
    bindingId: string;
    /** Chat platform key. */
    platform: string;
    /** Whether it is listening. */
    state?: "active" | "inactive";
    /** Channel, room or chat id. */
    target: string;
  })[];
}

/** `gateway.unbind` params. Detach a gateway binding. */
export interface GatewayUnbindParams {
  /** Binding to remove. */
  bindingId: string;
}

/** `gateway.unbind` result. */
export interface GatewayUnbindResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `job.cancel` params. Cancel a scheduled job. */
export interface JobCancelParams {
  /** Target job. */
  jobId: string;
}

/** `job.cancel` result. */
export interface JobCancelResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `job.list` params. List scheduled jobs and their next run times. */
export type JobListParams = Record<string, unknown>;

/** `job.list` result. */
export interface JobListResult {
  /** Known jobs. */
  jobs?: ({
    /** Channel that receives the output. */
    channel?: string | null;
    /** Job id. */
    jobId: string;
    /** Permission mode for the run. */
    mode?: "plan" | "accept" | "auto";
    /** UTC ISO-8601 time of the next firing. */
    nextRunAt?: string | null;
    /** Schedule as given. */
    spec: string;
    /** Current job state. */
    state?: "scheduled" | "running" | "cancelled";
    /** Prompt run on each firing. */
    task: string;
  })[];
}

/** `job.runNow` params. Fire a scheduled job immediately. */
export interface JobRunNowParams {
  /** Target job. */
  jobId: string;
}

/** `job.runNow` result. */
export interface JobRunNowResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `job.schedule` params. Schedule a prompt to run unattended. */
export interface JobScheduleParams {
  /** Gateway channel that receives the output. */
  channel?: string | null;
  /** Permission mode for the unattended run. */
  mode?: "plan" | "accept" | "auto";
  /** Cron expression or natural-language schedule. */
  spec: string;
  /** Prompt run on each firing. */
  task: string;
}

/** `job.schedule` result. */
export interface JobScheduleResult {
  /** Id of the scheduled job. */
  jobId: string;
}

/** `memory.search` params. Recall stored memories matching a query. */
export interface MemorySearchParams {
  /** Maximum number of hits. */
  limit?: number;
  /** Memory namespace to search; defaults to "default". Never crosses namespaces. */
  namespace?: string | null;
  /** Free-text query. */
  query: string;
}

/** `memory.search` result. */
export interface MemorySearchResult {
  /** Matches, best first. */
  hits?: ({
    /** Memory id. */
    id: string;
    /** Relevance score; higher is closer. */
    score?: number;
    /** Tags attached at write time. */
    tags?: string[];
    /** Stored text. */
    text: string;
  })[];
}

/** `memory.write` params. Store a memory with tags. */
export interface MemoryWriteParams {
  /** Memory namespace to write into; defaults to "default". */
  namespace?: string | null;
  /** Tags for later filtering. */
  tags?: string[];
  /** Text to remember. */
  text: string;
}

/** `memory.write` result. */
export interface MemoryWriteResult {
  /** Id of the stored memory. */
  id: string;
}

/** `permission.allowlist.add` params. Promote a pattern from ask to allow. */
export interface PermissionAllowlistAddParams {
  /** Glob or command prefix promoted from ask to allow. */
  pattern: string;
  /** Where the pattern is stored. */
  scope?: "session" | "project" | "always";
}

/** `permission.allowlist.add` result. */
export interface PermissionAllowlistAddResult {
  /** Id used to remove the pattern later. */
  patternId: string;
}

/** `permission.allowlist.list` params. List stored allowlist patterns. */
export interface PermissionAllowlistListParams {
  /** Filter by scope; omit for all. */
  scope?: "session" | "project" | "always" | null;
}

/** `permission.allowlist.list` result. */
export interface PermissionAllowlistListResult {
  /** Stored allowlist entries. */
  patterns?: ({
    /** The glob or command prefix. */
    pattern: string;
    /** Stable id of the pattern. */
    patternId: string;
    /** Where it is stored. */
    scope: "session" | "project" | "always";
  })[];
}

/** `permission.allowlist.remove` params. Delete an allowlist pattern by id. */
export interface PermissionAllowlistRemoveParams {
  /** Pattern to delete. */
  patternId: string;
}

/** `permission.allowlist.remove` result. */
export interface PermissionAllowlistRemoveResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `provider.configure` params. Store settings and credentials for a provider. */
export interface ProviderConfigureParams {
  /** Vendor-specific settings, including credentials. */
  config?: Record<string, unknown>;
  /** Vendor to configure. */
  vendor: string;
}

/** `provider.configure` result. */
export interface ProviderConfigureResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `provider.list` params. List chat providers and whether they are configured. */
export type ProviderListParams = Record<string, unknown>;

/** `provider.list` result. */
export interface ProviderListResult {
  /** Known chat providers. */
  providers?: ({
    /** Login flows the vendor supports: 'api_key' everywhere, plus 'device_code' (OpenAI) or 'oauth_pkce' (OpenRouter). */
    authMethods?: string[];
    /** True when credentials are present. */
    configured?: boolean;
    /** True for the vendor used when none is named. */
    default?: boolean;
    /** Model used when the caller names none. */
    defaultModel?: string;
    /** Human-readable vendor name for pickers. */
    label?: string;
    /** Model ids this vendor offers. */
    models?: string[];
    /** Vendor key, e.g. 'anthropic'. */
    vendor: string;
  })[];
}

/** `provider.loginWeb` params. Start a browser-based login flow for a provider. */
export interface ProviderLoginWebParams {
  /** Login flow to start, e.g. 'oauth'. */
  method: string;
  /** Vendor to log into. */
  vendor: string;
}

/** `provider.loginWeb` result. */
export interface ProviderLoginWebResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `session.close` params. Close a session and release its resources. */
export interface SessionCloseParams {
  /** Target session. */
  sessionId: string;
}

/** `session.close` result. */
export interface SessionCloseResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `session.create` params. Open a session rooted at a working directory. */
export interface SessionCreateParams {
  /** Named agent whose persona to load. */
  agent?: string | null;
  /** Override for concurrent subagents. */
  maxConcurrent?: number | null;
  /** Starting mode; defaults to the project setting. */
  mode?: "plan" | "accept" | "auto" | null;
  /** Model id; defaults to the provider's default. */
  model?: string | null;
  /** Surface that owns approvals for this session (TUI, gateway, ...). */
  originSurface?: string | null;
  /** Chat provider vendor; defaults to configured. */
  provider?: string | null;
  /** Absolute path the session operates in. */
  workdir: string;
}

/** `session.create` result. */
export interface SessionCreateResult {
  /** Id of the new session. */
  sessionId: string;
}

/** `session.interrupt` params. Stop the running turn as soon as possible. */
export interface SessionInterruptParams {
  /** Target session. */
  sessionId: string;
}

/** `session.interrupt` result. */
export interface SessionInterruptResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `session.list` params. List every live session. */
export type SessionListParams = Record<string, unknown>;

/** `session.list` result. */
export interface SessionListResult {
  /** Every live session. */
  sessions?: ({
    /** UTC ISO-8601 creation timestamp. */
    createdAt: string;
    /** Current permission mode. */
    mode: "plan" | "accept" | "auto";
    /** Model id in use. */
    model?: string | null;
    /** Surface that owns approvals. */
    originSurface?: string | null;
    /** Chat provider vendor in use. */
    provider?: string | null;
    /** Sequence number of the latest event. */
    seq?: number;
    /** Session id. */
    sessionId: string;
    /** Absolute working directory. */
    workdir: string;
  })[];
}

/** `session.prompt` params. Send user text to a session and start a turn. */
export interface SessionPromptParams {
  /** Files or images to include. */
  attachments?: ({
    /** Attachment flavour. */
    kind?: "file" | "image" | "text";
    /** Media type when known. */
    mimeType?: string | null;
    /** Absolute path, for 'file' and 'image'. */
    path?: string | null;
    /** Inline content, for 'text'. */
    text?: string | null;
  })[] | null;
  /** Session to prompt. */
  sessionId: string;
  /** User text; a leading '/' is parsed as a slash command. */
  text: string;
}

/** `session.prompt` result. */
export interface SessionPromptResult {
  /** Id of the started turn; turn.done carries it back. */
  turnId: string;
}

/** `session.resume` params. Replay the events a disconnected client missed. */
export interface SessionResumeParams {
  /** Replay only events with a greater seq. */
  afterSeq?: number | null;
  /** Session to resume. */
  sessionId: string;
}

/** `session.resume` result. */
export interface SessionResumeResult {
  /** Missed events in seq order. */
  events?: ({
    /** Event kind; see sessionEventKinds in the schema dump. */
    kind: string;
    /** Kind-specific body. */
    payload?: Record<string, unknown>;
    /** Monotonic per-session sequence number; resume replays after it. */
    seq: number;
    /** Session the event belongs to. */
    sessionId: string;
    /** UTC ISO-8601 timestamp. */
    ts: string;
  })[];
  /** Session that was resumed. */
  sessionId: string;
}

/** `session.setMode` params. Switch a session between plan, accept and auto. */
export interface SessionSetModeParams {
  /** New permission mode. */
  mode: "plan" | "accept" | "auto";
  /** Session to change. */
  sessionId: string;
}

/** `session.setMode` result. */
export interface SessionSetModeResult {
  /** Mode now in effect. */
  mode: "plan" | "accept" | "auto";
}

/** `skill.install` params. Install a skill from a path, URL or registry. */
export interface SkillInstallParams {
  /** Path, URL or registry name to install from. */
  source: string;
}

/** `skill.install` result. */
export interface SkillInstallResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `skill.list` params. List installed skills. */
export type SkillListParams = Record<string, unknown>;

/** `skill.list` result. */
export interface SkillListResult {
  /** Installed skills. */
  skills?: ({
    /** True when present locally. */
    installed?: boolean;
    /** Skill name. */
    name: string;
    /** Where the skill came from. */
    source?: string;
    /** What the skill does. */
    summary?: string;
  })[];
}

/** `skill.reload` params. Reload skills from disk without restarting. */
export type SkillReloadParams = Record<string, unknown>;

/** `skill.reload` result. */
export interface SkillReloadResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `skill.search` params. Search available skills. */
export interface SkillSearchParams {
  /** Free-text query over skill names and summaries. */
  query: string;
}

/** `skill.search` result. */
export interface SkillSearchResult {
  /** Matching skills. */
  skills?: ({
    /** True when present locally. */
    installed?: boolean;
    /** Skill name. */
    name: string;
    /** Where the skill came from. */
    source?: string;
    /** What the skill does. */
    summary?: string;
  })[];
}

/** `system.health` params. Liveness probe; answers as long as the daemon serves requests. */
export type SystemHealthParams = Record<string, unknown>;

/** `system.health` result. */
export interface SystemHealthResult {
  /** Always 'ok' when the daemon answers. */
  status?: "ok";
}

/** `system.hello` params. Authenticate a connection and agree on the protocol version. */
export interface SystemHelloParams {
  /** Version string of the connecting client. */
  clientVersion: string;
  /** Protocol semver the client speaks; major must match. */
  protocolVersion: string;
  /** Shared secret read from $SNOWPEA_HOME/token or daemon.json. */
  token: string;
}

/** `system.hello` result. */
export interface SystemHelloResult {
  /** Feature flags this daemon supports. */
  capabilities?: string[];
  /** Protocol semver the daemon speaks. */
  protocolVersion: string;
  /** Version of the running daemon. */
  serverVersion: string;
}

/** `system.info` params. Report the daemon's version, pid, port, start time and home. */
export type SystemInfoParams = Record<string, unknown>;

/** `system.info` result. */
export interface SystemInfoResult {
  /** Lifecycle counters: sessions, jobs, gateway_bindings, named_agents. */
  counters?: Record<string, number>;
  /** Resolved SNOWPEA_HOME directory. */
  home: string;
  /** Idle-shutdown status, omitted by older daemons. */
  lifecycle?: {
    /** Why the daemon will or will not exit. */
    reason?: string;
    /** Seconds left before the idle shutdown, null while busy. */
    secondsUntilExit?: number | null;
    /** True while the idle timer is running. */
    willExit?: boolean;
  } | null;
  /** Process id of the daemon. */
  pid: number;
  /** TCP port the daemon is listening on (127.0.0.1 only). */
  port: number;
  /** Protocol semver the daemon speaks. */
  protocolVersion: string;
  /** UTC ISO-8601 timestamp of daemon start. */
  startedAt: string;
  /** Daemon version. */
  version: string;
}

/** `system.shutdown` params. Ask the daemon to shut down gracefully. */
export type SystemShutdownParams = Record<string, unknown>;

/** `system.shutdown` result. */
export interface SystemShutdownResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `team.start` params. Split a task across parallel workers. */
export interface TeamStartParams {
  /** Number of workers to run in parallel. */
  n: number;
  /** Session the team works under. */
  sessionId: string;
  /** Task the team splits between workers. */
  task: string;
}

/** `team.start` result. */
export interface TeamStartResult {
  /** Id to poll with team.status. */
  teamId: string;
}

/** `team.status` params. Inspect a team's task board. */
export interface TeamStatusParams {
  /** Team to inspect. */
  teamId: string;
}

/** `team.status` result. */
export interface TeamStatusResult {
  /** Overall state. */
  state?: "running" | "done" | "failed";
  /** Task board contents. */
  tasks?: ({
    /** Worker that owns the task. */
    assignee?: string | null;
    /** Current state. */
    status?: "pending" | "running" | "done" | "failed";
    /** Task id, stable for the run. */
    taskId: string;
    /** Short task description. */
    title: string;
  })[];
  /** Team that was inspected. */
  teamId: string;
}

/** `tool.list` params. List the tools registered for a session. */
export interface ToolListParams {
  /** Scope the listing to one session; omit for the global set. */
  sessionId?: string | null;
}

/** `tool.list` result. */
export interface ToolListResult {
  /** Registered tools. */
  tools?: ({
    /** Grouping used by the UI. */
    category: string;
    /** Text shown to the model. */
    description?: string;
    /** Tool name as the model calls it. */
    name: string;
    /** Permission class checked against the mode. */
    permissionTag: "read" | "write" | "exec" | "network" | "send";
    /** builtin, skill, plugin or MCP server name. */
    source?: string;
    /** Inactive tools are hidden from the model. */
    state?: "active" | "inactive";
  })[];
}

// ---------------------------------------------------------------------------
// Event payloads
// ---------------------------------------------------------------------------

/** `approval.resolved` notification payload. */
export interface ApprovalResolvedPayload {
  /** Surface or user that answered. */
  by: string;
  /** The decision that was recorded. */
  decision: "allow" | "deny";
  /** Request that was resolved. */
  requestId: string;
}

/** `gateway.event` notification payload. */
export interface GatewayEventPayload {
  /** Binding the event belongs to. */
  bindingId: string;
  /** Event kind, e.g. 'message' or 'error'. */
  kind: string;
  /** Kind-specific body. */
  payload?: Record<string, unknown>;
}

/** `job.event` notification payload. */
export interface JobEventPayload {
  /** Job the event belongs to. */
  jobId: string;
  /** Event kind, e.g. 'started' or 'finished'. */
  kind: string;
  /** Kind-specific body. */
  payload?: Record<string, unknown>;
}

/** `session.event` notification payload. */
export interface SessionEventPayload {
  /** Event kind; see sessionEventKinds for the payload schema. */
  kind: string;
  /** Kind-specific body. */
  payload?: Record<string, unknown>;
  /** Monotonic per-session sequence number. */
  seq: number;
  /** Session the event belongs to. */
  sessionId: string;
  /** UTC ISO-8601 timestamp. */
  ts: string;
}

// ---------------------------------------------------------------------------
// session.event payloads by kind
// ---------------------------------------------------------------------------

/** Payload of `session.event` with kind `backend.changed`. */
export interface BackendChangedEventPayload {
  /** Where tools now execute. */
  backend: "local" | "docker" | "ssh";
  kind?: "backend.changed";
}

/** Payload of `session.event` with kind `diff`. */
export interface DiffEventPayload {
  kind?: "diff";
  /** Unified diff of the change. */
  patch: string;
  /** File that changed. */
  path: string;
}

/** Payload of `session.event` with kind `error`. */
export interface ErrorEventPayload {
  /** One of the protocol error codes. */
  code: string;
  kind?: "error";
  /** Human-readable detail. */
  message: string;
}

/** Payload of `session.event` with kind `message.delta`. */
export interface MessageDeltaEventPayload {
  kind?: "message.delta";
  /** Text fragment to append to the current message. */
  text: string;
}

/** Payload of `session.event` with kind `message.done`. */
export interface MessageDoneEventPayload {
  kind?: "message.done";
  /** Who produced the message. */
  role?: "assistant" | "user" | "system";
  /** Full message text. */
  text: string;
}

/** Payload of `session.event` with kind `mode.changed`. */
export interface ModeChangedEventPayload {
  kind?: "mode.changed";
  /** Mode now in effect. */
  mode: "plan" | "accept" | "auto";
}

/** Payload of `session.event` with kind `subagent.done`. */
export interface SubagentDoneEventPayload {
  /** Subagent that finished. */
  agentId: string;
  kind?: "subagent.done";
  /** False when it failed. */
  ok?: boolean;
  /** Final report. */
  result?: string;
}

/** Payload of `session.event` with kind `subagent.spawn`. */
export interface SubagentSpawnEventPayload {
  /** Id correlating this subagent's events. */
  agentId: string;
  kind?: "subagent.spawn";
  /** Named agent that was spawned. */
  name?: string;
  /** Task it was given. */
  task?: string;
}

/** Payload of `session.event` with kind `subagent.update`. */
export interface SubagentUpdateEventPayload {
  /** Subagent reporting progress. */
  agentId: string;
  kind?: "subagent.update";
  /** Short status label. */
  status?: string;
  /** Human-readable progress text. */
  text?: string;
}

/** Payload of `session.event` with kind `team.task.update`. */
export interface TeamTaskUpdateEventPayload {
  /** Worker that owns the task. */
  assignee?: string | null;
  kind?: "team.task.update";
  /** New state. */
  status?: "pending" | "running" | "done" | "failed";
  /** Task that changed. */
  taskId: string;
  /** Team the task belongs to. */
  teamId: string;
}

/** Payload of `session.event` with kind `tool.call`. */
export interface ToolCallEventPayload {
  /** Arguments supplied. */
  args?: Record<string, unknown>;
  /** Id pairing this call with its tool.result. */
  callId: string;
  kind?: "tool.call";
  /** Tool being called. */
  name: string;
}

/** Payload of `session.event` with kind `tool.result`. */
export interface ToolResultEventPayload {
  /** Id of the matching tool.call. */
  callId: string;
  /** Failure detail when ok is false. */
  error?: string | null;
  kind?: "tool.result";
  /** Tool that ran. */
  name: string;
  /** False when the tool failed. */
  ok: boolean;
  /** Output handed back to the model. */
  output?: string;
}

/** Payload of `session.event` with kind `turn.done`. */
export interface TurnDoneEventPayload {
  kind?: "turn.done";
  /** Why the turn ended. */
  reason?: "complete" | "interrupted" | "error" | "denied" | "timeout";
  /** Turn that ended. */
  turnId: string;
}

/** Payload of `session.event` with kind `usage`. */
export interface UsageEventPayload {
  /** Prompt tokens consumed. */
  inputTokens?: number;
  kind?: "usage";
  /** Completion tokens produced. */
  outputTokens?: number;
}

/** Maps every `session.event` kind to its payload type. */
export interface SessionEventKindMap {
  "backend.changed": BackendChangedEventPayload;
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
  "backend.changed",
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
