// GENERATED — do not edit.
// Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py.
// Re-run `uv run python scripts/gen_protocol.py` after changing the protocol.

export const PROTOCOL_VERSION = "1.3.0";
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
  /** Credential entry the platform account uses; defaults to the platform name, e.g. 'telegram' for channel 'telegram:123'. */
  credentialsRef?: string | null;
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
  /** Name for the agent; when omitted the daemon takes the generated one. */
  name?: string | null;
  /** Also register a persistent named instance: its own session, the memory namespace agent:<name>, and rows that survive a daemon restart. */
  named?: boolean;
}

/** `agent.create` result. */
export interface AgentCreateResult {
  /** Name assigned to the new agent. */
  name: string;
  /** Where the definition was written. */
  path?: string | null;
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
    /** For kind='subagent': the id its subagent.* events carry. */
    agentId?: string | null;
    /** For kind='named': the gateway binding ids serving those channels. */
    bindings?: string[];
    /** Gateway channel bound to the agent. */
    channel?: string | null;
    /** For kind='named': every gateway channel bound to the agent. */
    channels?: string[];
    /** What the agent is for. */
    description?: string;
    /** For kind='named': ids of the scheduled jobs that run as this agent. */
    jobs?: string[];
    /** definition = an agents/<name>.md file, subagent = a running child, named = a persistent named instance. */
    kind?: string;
    /** Agent name used by agent.spawn. */
    name: string;
    /** For kind='named': the agent's memory namespace, agent:<name>. */
    namespace?: string | null;
    /** For kind='subagent': the session that delegated the task. */
    parentSessionId?: string | null;
    /** Definition file, when there is one. */
    path?: string | null;
    /** Session the agent runs in. */
    sessionId?: string | null;
    /** Where the definition came from. */
    source?: string;
    /** For kind='subagent': queued, running, done or error. */
    status?: string | null;
    /** For kind='subagent': the task it was given. */
    task?: string | null;
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
    /** Extra warning shown with the prompt, e.g. "modifies snowpea configuration". */
    note?: string;
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
  /** Extra warning shown with the prompt, e.g. "modifies snowpea configuration". */
  note?: string;
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

/** `audio.capabilities` params. Report what voice input and output can do on this machine. */
export type AudioCapabilitiesParams = Record<string, unknown>;

/** `audio.capabilities` result. */
export interface AudioCapabilitiesResult {
  /** True when replies are spoken without being asked. */
  autoSpeak?: boolean;
  /** True when the daemon can play audio itself. */
  play?: boolean;
  /** Audio players found on PATH. */
  players?: string[];
  /** Per-capability explanation of why it is off. */
  reasons?: Record<string, string>;
  /** True when the microphone can be recorded. */
  record?: boolean;
  /** Recorders found on PATH. */
  recorders?: string[];
  /** Transcription backend in use, or null when there is none. */
  stt?: string | null;
  /** Every usable transcription backend, preferred first. */
  sttProviders?: string[];
  /** True when speech synthesis is available. */
  tts?: boolean;
  /** Speech backend in use. */
  ttsProvider?: string | null;
  /** Every usable speech backend, preferred first. */
  ttsProviders?: string[];
  /** Configured voice, when one is set. */
  voice?: string | null;
}

/** `audio.record.start` params. Start recording the microphone. */
export interface AudioRecordStartParams {
  /** Session the recording belongs to. */
  sessionId?: string | null;
}

/** `audio.record.start` result. */
export interface AudioRecordStartResult {
  /** Media type of the recording. */
  mime?: string;
  /** Wav file being written, or the finished recording. */
  path: string;
  /** Backend that transcribed it. */
  provider?: string | null;
  /** True while capture is still running. */
  recording: boolean;
  /** Transcript, when 'stop' was asked to transcribe. */
  text?: string | null;
}

/** `audio.record.stop` params. Stop the recording and return the wav it wrote. */
export interface AudioRecordStopParams {
  /** Session that is recording. */
  sessionId?: string | null;
  /** Also transcribe the recording and return its text. */
  transcribe?: boolean;
}

/** `audio.record.stop` result. */
export interface AudioRecordStopResult {
  /** Media type of the recording. */
  mime?: string;
  /** Wav file being written, or the finished recording. */
  path: string;
  /** Backend that transcribed it. */
  provider?: string | null;
  /** True while capture is still running. */
  recording: boolean;
  /** Transcript, when 'stop' was asked to transcribe. */
  text?: string | null;
}

/** `audio.speak` params. Synthesise speech, optionally playing it on the daemon's machine. */
export interface AudioSpeakParams {
  /** Play on the daemon's machine instead of returning only a path. */
  play?: boolean;
  /** Session the audio belongs to. */
  sessionId?: string | null;
  /** What to say. */
  text: string;
  /** Voice id; defaults to the setting. */
  voice?: string | null;
}

/** `audio.speak` result. */
export interface AudioSpeakResult {
  /** Media type of that file. */
  mime: string;
  /** Audio file the speech was written to. */
  path: string;
  /** True when the daemon played it. */
  played?: boolean;
  /** Backend that synthesised it. */
  provider: string;
  /** Voice that was used. */
  voice?: string | null;
}

/** `audio.transcribe` params. Transcribe recorded audio to text. */
export interface AudioTranscribeParams {
  /** Base64 (or data-URI) audio. */
  data?: string | null;
  /** BCP-47 hint for the backend. */
  language?: string | null;
  /** Media type of the audio, e.g. audio/wav. */
  mime?: string | null;
  /** Audio file on the daemon's machine. */
  path?: string | null;
  /** Session the audio belongs to. */
  sessionId?: string | null;
}

/** `audio.transcribe` result. */
export interface AudioTranscribeResult {
  /** Backend that produced the transcript. */
  provider: string;
  /** What the backend heard. */
  text: string;
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
    source?: string;
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
  /** Restrict the binding to one chat, channel or room. */
  channelId?: string | null;
  /** Name of the credential to use: a key in credentials.json or an env var. */
  credentialsRef: string;
  /** Chat platform key: telegram, discord or slack. */
  platform: string;
  /** What the conversation talks to: {'agent': name}, {'session': id} or {'new_session': {'workdir': path, 'mode': mode}}. */
  target: Record<string, unknown>;
  /** Platform user allowed to answer approvals from chat. */
  userId?: string | null;
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
    /** Chat it is limited to, if any. */
    channelId?: string | null;
    /** Credential name; never the secret. */
    credentialsRef?: string;
    /** Chat platform key. */
    platform: string;
    /** 'manual' for a gateway.bind call, 'settings' for the catch-all binding the setup wizard's settings.gateway entry keeps in sync. */
    source?: string;
    /** Whether it is listening. */
    state?: "active" | "inactive";
    /** Target it routes to, e.g. 'agent:ops' or 'new_session'. */
    target: string;
    /** User allowed to answer approvals. */
    userId?: string | null;
  })[];
}

/** `gateway.sync` params. Reconcile the messenger bindings with settings.gateway. */
export type GatewaySyncParams = Record<string, unknown>;

/** `gateway.sync` result. */
export interface GatewaySyncResult {
  /** Platforms that started listening. */
  added?: string[];
  /** Platforms that were already listening. */
  kept?: string[];
  /** Platforms whose auto binding was dropped. */
  removed?: string[];
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
    /** False once the job is cancelled or has run out. */
    enabled?: boolean;
    /** Job id. */
    jobId: string;
    /** How the spec repeats: a cron rule, a one-shot, or a fixed interval. */
    kind?: "cron" | "once" | "interval";
    /** UTC ISO-8601 time of the last firing, null before the first. */
    lastRunAt?: string | null;
    /** How the last run ended. */
    lastStatus?: "ok" | "error" | "denied_by_timeout" | null;
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
  /** Named agent that runs the task. */
  agent?: string | null;
  /** Gateway channel that receives the output. */
  channel?: string | null;
  /** Permission mode for the unattended run. */
  mode?: "plan" | "accept" | "auto";
  /** Cron expression or natural-language schedule. */
  spec: string;
  /** Prompt run on each firing. */
  task: string;
  /** Working directory for the run; defaults to the daemon's home. */
  workdir?: string | null;
}

/** `job.schedule` result. */
export interface JobScheduleResult {
  /** Id of the scheduled job. */
  jobId: string;
  /** UTC ISO-8601 time of the first firing. */
  nextRunAt?: string | null;
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
  /** Seconds until the code or session expires, when known. */
  expiresInSec?: number | null;
  /** True when the call succeeded. */
  ok?: boolean;
  /** 'await_user' once userCode/verificationUri are ready and polling has started in the background; 'done' or 'failed' if the flow finished synchronously before this response was sent. */
  status?: "await_user" | "done" | "failed";
  /** Short code the user types in, for device-code flows. */
  userCode?: string | null;
  /** URL to open to approve the login. */
  verificationUri?: string | null;
  /** verificationUri with the code already embedded, when known. */
  verificationUriComplete?: string | null;
}

/** `provider.models` params. Ask a vendor's endpoint which models it serves. */
export interface ProviderModelsParams {
  /** Vendor to query; defaults to the configured one. */
  vendor?: string | null;
}

/** `provider.models` result. */
export interface ProviderModelsResult {
  /** Model this vendor uses today. */
  current?: string;
  /** Model ids the vendor's endpoint reports. */
  models?: string[];
  /** Vendor the listing came from. */
  vendor: string;
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

/** `session.compact` params. Summarise the conversation so far and replace the history with it. */
export interface SessionCompactParams {
  /** Extra guidance for the summary, e.g. 'keep the API design decisions'. */
  instructions?: string | null;
  /** Session whose history to compact. */
  sessionId: string;
}

/** `session.compact` result. */
export interface SessionCompactResult {
  /** Estimated tokens the history holds now. */
  after?: number;
  /** Estimated tokens the history held before. */
  before?: number;
  /** Length of the summary in characters. */
  summaryChars?: number;
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

/** `session.list` params. List live sessions, optionally including saved sessions. */
export interface SessionListParams {
  includeClosed?: boolean;
  workdir?: string | null;
}

/** `session.list` result. */
export interface SessionListResult {
  /** Every live session. */
  sessions?: ({
    /** Tokens the session's current prompt occupies (CORE-context). */
    contextUsed?: number;
    /** Context window of the session's model; null when unknown. */
    contextWindow?: number | null;
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
    /** Base64 (or data-URI) content, for a pasted image. */
    data?: string | null;
    /** Attachment flavour. */
    kind?: "file" | "image" | "text";
    /** Media type when known. */
    mimeType?: string | null;
    /** Display name; defaults to the file's basename. */
    name?: string | null;
    /** Absolute path, for 'file' and 'image'. */
    path?: string | null;
    /** Byte size the client measured. */
    size?: number | null;
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

/** `settings.get` params. Read global or project settings, with secrets masked. */
export interface SettingsGetParams {
  /** "global" reads $SNOWPEA_HOME/settings.json; "project" reads <workdir>/.snowpea/settings.json. */
  scope?: "global" | "project";
  /** Project root; required when scope is "project". */
  workdir?: string | null;
}

/** `settings.get` result. */
export interface SettingsGetResult {
  /** The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. */
  settings: Record<string, unknown>;
}

/** `settings.set` params. Deep-merge a patch into global or project settings and persist it. */
export interface SettingsSetParams {
  /** Fields to deep-merge into the existing settings. */
  patch: Record<string, unknown>;
  /** "global" writes $SNOWPEA_HOME/settings.json; "project" writes <workdir>/.snowpea/settings.json. */
  scope?: "global" | "project";
  /** Project root; required when scope is "project". */
  workdir?: string | null;
}

/** `settings.set` result. */
export interface SettingsSetResult {
  /** The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. */
  settings: Record<string, unknown>;
}

/** `setup.catalog` params. The setup wizard's vendor, search, browser, tools and gateway catalogs. */
export type SetupCatalogParams = Record<string, unknown>;

/** `setup.catalog` result. */
export interface SetupCatalogResult {
  /** Browser-control providers. */
  browser?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** Chat gateways (telegram, discord, slack), all off. */
  gateway?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** Web-search providers, ddgs first. */
  search?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** Tool categories and their default on/off state. */
  tools?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** LLM vendors. */
  vendors?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
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
    /** Registry id; empty for local entries. */
    id?: string;
    /** What to pass to skill.install to get this entry. */
    installSpec?: string;
    /** True when present locally. */
    installed?: boolean;
    /** What kind of entry this is. */
    kind?: "skill" | "agent" | "command" | "plugin";
    /** Skill name. */
    name: string;
    /** Where it came from: builtin, global, project, plugin:<name> for an installed entry, or the marketplace that offered it. */
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

/** `skill.remove` params. Delete an installed skill or plugin. */
export interface SkillRemoveParams {
  /** Installed plugin or skill to delete. */
  name: string;
}

/** `skill.remove` result. */
export interface SkillRemoveResult {
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
    /** Registry id; empty for local entries. */
    id?: string;
    /** What to pass to skill.install to get this entry. */
    installSpec?: string;
    /** True when present locally. */
    installed?: boolean;
    /** What kind of entry this is. */
    kind?: "skill" | "agent" | "command" | "plugin";
    /** Skill name. */
    name: string;
    /** Where it came from: builtin, global, project, plugin:<name> for an installed entry, or the marketplace that offered it. */
    source?: string;
    /** What the skill does. */
    summary?: string;
  })[];
  /** Sources that could not be reached, as '<source>: <reason>'. Empty skills with a non-empty list means offline, not no match. */
  unavailable?: string[];
}

/** `system.checkUpdate` params. Report whether a newer snowpea release exists; cached for 24h. */
export interface SystemCheckUpdateParams {
  /** Ignore the 24h cache and ask the network right now. */
  force?: boolean;
}

/** `system.checkUpdate` result. */
export interface SystemCheckUpdateResult {
  /** True when latest is strictly newer than current. */
  available: boolean;
  /** True when this came from $SNOWPEA_HOME/update-check.json. */
  cached?: boolean;
  /** Where the answer came from: 'pypi' or 'git'. */
  channel: "git" | "pypi";
  /** UTC ISO-8601 timestamp of the answer. */
  checkedAt: string;
  /** Version of the running daemon (snowpea_core.__version__). */
  current: string;
  /** Why the check could not complete; available is false whenever it is set. */
  error?: string | null;
  /** Newest version found; equal to current when nothing is known. */
  latest: string;
  /** Human page for the release, when one exists. */
  releaseUrl?: string | null;
  /** What an installer would be handed to get 'latest'. */
  source: string;
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
    /** Why the daemon is staying up, e.g. ['2 gateway bindings', '1 job']. */
    reasons?: string[];
    /** Seconds left before the idle shutdown, null while busy. */
    secondsUntilExit?: number | null;
    /** One line for humans: 'will exit in 120s' or 'will not exit: 1 job'. */
    summary?: string | null;
    /** True while the idle timer is running. */
    willExit?: boolean;
  } | null;
  /** Process id of the daemon. */
  pid: number;
  /** TCP port the daemon is listening on (127.0.0.1 only). */
  port: number;
  /** Protocol semver the daemon speaks. */
  protocolVersion: string;
  /** True once system.update finished; the daemon runs the old code until restarted. */
  restartRequired?: boolean;
  /** UTC ISO-8601 timestamp of daemon start. */
  startedAt: string;
  /** Daemon version. */
  version: string;
}

/** `system.reloadSettings` params. Re-read settings.json and rebind the daemon's in-memory state. */
export type SystemReloadSettingsParams = Record<string, unknown>;

/** `system.reloadSettings` result. */
export interface SystemReloadSettingsResult {
  /** Top-level settings sections that changed, e.g. ['providers']. */
  changedKeys?: string[];
  /** True when settings.json differed from what the daemon held and the in-memory state was rebound; false when it was already current. */
  reloaded: boolean;
}

/** `system.restart` params. Shut the daemon down so the next launch runs the newly installed version. */
export type SystemRestartParams = Record<string, unknown>;

/** `system.restart` result. */
export interface SystemRestartResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `system.shutdown` params. Ask the daemon to shut down gracefully. */
export type SystemShutdownParams = Record<string, unknown>;

/** `system.shutdown` result. */
export interface SystemShutdownResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `system.update` params. Upgrade snowpea in a detached subprocess and report progress. */
export type SystemUpdateParams = Record<string, unknown>;

/** `system.update` result. */
export interface SystemUpdateResult {
  /** The command line that runs, or that has to be run by hand. */
  command: string;
  /** Why nothing was started; null on the happy path. */
  error?: string | null;
  /** Absolute path of the file the upgrade writes its output to. */
  log: string;
  /** True when the upgrade subprocess was spawned. */
  started: boolean;
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
  /** Team to inspect; empty means the most recent one. */
  teamId?: string;
}

/** `team.status` result. */
export interface TeamStatusResult {
  /** Overall state. */
  state?: "running" | "done" | "failed";
  /** The task the team was given. */
  task?: string;
  /** Task board contents. */
  tasks?: ({
    /** 1-based worker index that owns the task. */
    agentN?: number | null;
    /** Worker that owns the task. */
    assignee?: string | null;
    /** Branch the worker commits the task on. */
    branch?: string;
    /** Conflicted diff captured before git merge --abort. */
    conflictHunks?: string;
    /** One line naming the conflicted files. */
    conflictSummary?: string;
    /** Task ids that must merge before this one runs. */
    dependsOn?: string[];
    /** Why the task is in this state. */
    note?: string;
    /** How many times a merge conflict re-queued it. */
    retries?: number;
    /** Current state. */
    status?: "pending" | "queued" | "claimed" | "running" | "done" | "conflict" | "merged" | "failed";
    /** Task id, stable for the run. */
    taskId: string;
    /** Short task description. */
    title: string;
  })[];
  /** Team that was inspected. */
  teamId: string;
  /** How many workers the run was started with. */
  workers?: number;
  /** Worker worktrees that exist right now. */
  worktrees?: string[];
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
    permissionTag: "read" | "write" | "exec" | "network" | "send" | "config";
    /** Backing provider for tools that have one, e.g. the web-search provider id; reads "configured → answering" when the configured one cannot run. */
    provider?: string;
    /** builtin, skill, plugin or MCP server name. */
    source?: string;
    /** Inactive tools are hidden from the model. */
    state?: "active" | "inactive";
  })[];
}

// ---------------------------------------------------------------------------
// Event payloads
// ---------------------------------------------------------------------------

/** `approval.pending` notification payload. */
export interface ApprovalPendingPayload {
  /** The request now in the shared queue. */
  request: {
    /** Arguments it wants to use. */
    args?: Record<string, unknown>;
    /** Extra warning shown with the prompt, e.g. "modifies snowpea configuration". */
    note?: string;
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
  };
}

/** `approval.resolved` notification payload. */
export interface ApprovalResolvedPayload {
  /** Surface or user that answered. */
  by: string;
  /** The decision that was recorded. */
  decision: "allow" | "deny";
  /** Request that was resolved. */
  requestId: string;
}

/** `commands.changed` notification payload. */
export interface CommandsChangedPayload {
  /** The command table as it stands now. */
  commands?: ({
    /** JSON Schema for the argument string. */
    argsSchema?: Record<string, unknown>;
    /** Command name without the leading slash. */
    name: string;
    /** Where the command came from. */
    source?: string;
    /** One-line description shown in /help. */
    summary: string;
  })[];
  /** Why the table changed. */
  reason?: string;
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
  /** Where the run got to. */
  kind: "started" | "finished" | "failed" | "denied";
  /** Kind-specific body. */
  payload?: Record<string, unknown>;
}

/** `provider.loginProgress` notification payload. */
export interface ProviderLoginProgressPayload {
  /** Seconds until the code or session expires, when known. */
  expiresInSec?: number | null;
  /** One line for humans. */
  message?: string | null;
  /** Login flow in progress, e.g. 'device_code' or 'oauth_pkce'. */
  method: string;
  /** Where the login got to. */
  phase: "started" | "await_user" | "polling" | "done" | "failed";
  /** Short code the user types in, for device-code flows. */
  userCode?: string | null;
  /** Vendor being logged into. */
  vendor: string;
  /** URL to open to approve the login. */
  verificationUri?: string | null;
  /** verificationUri with the code already embedded, when known. */
  verificationUriComplete?: string | null;
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

/** `settings.changed` notification payload. */
export interface SettingsChangedPayload {
  /** Top-level settings sections that changed, e.g. ['providers']. */
  keys?: string[];
  /** Which document was reloaded; "global" for $SNOWPEA_HOME/settings.json. */
  scope?: "global" | "project";
}

/** `system.updateProgress` notification payload. */
export interface SystemUpdateProgressPayload {
  /** One line for humans. */
  message?: string;
  /** Where the upgrade got to. */
  phase: "started" | "done" | "failed";
}

// ---------------------------------------------------------------------------
// session.event payloads by kind
// ---------------------------------------------------------------------------

/** Payload of `session.event` with kind `audio.spoken`. */
export interface AudioSpokenEventPayload {
  kind?: "audio.spoken";
  /** Media type of that file. */
  mime?: string;
  /** Audio file the speech was written to. */
  path: string;
  /** True when the daemon played it. */
  played?: boolean;
  /** Backend that synthesised it. */
  provider?: string;
  /** Voice that was used. */
  voice?: string | null;
}

/** Payload of `session.event` with kind `backend.changed`. */
export interface BackendChangedEventPayload {
  /** Where tools now execute. */
  backend: "local" | "docker" | "ssh";
  kind?: "backend.changed";
}

/** Payload of `session.event` with kind `compaction`. */
export interface CompactionEventPayload {
  /** Estimated tokens the history holds now. */
  after?: number;
  /** True when the auto-compaction threshold triggered it. */
  auto?: boolean;
  /** Estimated tokens the history held before. */
  before?: number;
  /** Messages kept verbatim after the summary. */
  kept?: number;
  kind?: "compaction";
  /** Length of the summary in characters. */
  summaryChars?: number;
}

/** Payload of `session.event` with kind `context`. */
export interface ContextEventPayload {
  /** True while 'used' is a local estimate; false once the provider reported it. */
  estimated?: boolean;
  kind?: "context";
  /** Model the window belongs to. */
  model?: string | null;
  /** used/window as a percentage, null when the window is unknown. */
  percent?: number | null;
  /** Vendor serving that model. */
  provider?: string | null;
  /** Tokens the current prompt occupies. */
  used?: number;
  /** Context window of the model in tokens; null when unknown. */
  window?: number | null;
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
  /** Named agent that ran, when there was one. */
  name?: string;
  /** False when it failed. */
  ok?: boolean;
  /** Final report. */
  result?: string;
  /** The subagent's own session. */
  sessionId?: string | null;
  /** Terminal state: done or error. */
  status?: "queued" | "running" | "done" | "error";
  /** The subagent's final answer. */
  summary?: string;
  /** Tokens the subagent consumed. */
  usage?: {
    /** Prompt tokens the subagent used. */
    inputTokens?: number;
    /** Completion tokens the subagent used. */
    outputTokens?: number;
  };
}

/** Payload of `session.event` with kind `subagent.spawn`. */
export interface SubagentSpawnEventPayload {
  /** Id correlating this subagent's events. */
  agentId: string;
  kind?: "subagent.spawn";
  /** Named agent that was spawned. */
  name?: string;
  /** The subagent's own session, once it has one. */
  sessionId?: string | null;
  /** State at spawn: queued until a concurrency slot frees up. */
  status?: "queued" | "running" | "done" | "error";
  /** Task it was given. */
  task?: string;
}

/** Payload of `session.event` with kind `subagent.update`. */
export interface SubagentUpdateEventPayload {
  /** Subagent reporting progress. */
  agentId: string;
  kind?: "subagent.update";
  /** Most recent text the subagent produced. */
  lastText?: string;
  /** Named agent that is running, when there is one. */
  name?: string;
  /** The subagent's own session. */
  sessionId?: string | null;
  /** Lifecycle state. */
  status?: "queued" | "running" | "done" | "error";
  /** Human-readable progress text. */
  text?: string;
}

/** Payload of `session.event` with kind `team.task.update`. */
export interface TeamTaskUpdateEventPayload {
  /** 1-based worker index that owns the task. */
  agentN?: number | null;
  /** Worker that owns the task. */
  assignee?: string | null;
  kind?: "team.task.update";
  /** How many times a merge conflict re-queued it. */
  retries?: number;
  /** New state. */
  status?: "pending" | "queued" | "claimed" | "running" | "done" | "conflict" | "merged" | "failed";
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
  "audio.spoken": AudioSpokenEventPayload;
  "backend.changed": BackendChangedEventPayload;
  "compaction": CompactionEventPayload;
  "context": ContextEventPayload;
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
  "audio.spoken",
  "backend.changed",
  "compaction",
  "context",
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
  "audio.capabilities": { params: AudioCapabilitiesParams; result: AudioCapabilitiesResult };
  "audio.record.start": { params: AudioRecordStartParams; result: AudioRecordStartResult };
  "audio.record.stop": { params: AudioRecordStopParams; result: AudioRecordStopResult };
  "audio.speak": { params: AudioSpeakParams; result: AudioSpeakResult };
  "audio.transcribe": { params: AudioTranscribeParams; result: AudioTranscribeResult };
  "backend.set": { params: BackendSetParams; result: BackendSetResult };
  "command.list": { params: CommandListParams; result: CommandListResult };
  "command.run": { params: CommandRunParams; result: CommandRunResult };
  "gateway.bind": { params: GatewayBindParams; result: GatewayBindResult };
  "gateway.list": { params: GatewayListParams; result: GatewayListResult };
  "gateway.sync": { params: GatewaySyncParams; result: GatewaySyncResult };
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
  "provider.models": { params: ProviderModelsParams; result: ProviderModelsResult };
  "session.close": { params: SessionCloseParams; result: SessionCloseResult };
  "session.compact": { params: SessionCompactParams; result: SessionCompactResult };
  "session.create": { params: SessionCreateParams; result: SessionCreateResult };
  "session.interrupt": { params: SessionInterruptParams; result: SessionInterruptResult };
  "session.list": { params: SessionListParams; result: SessionListResult };
  "session.prompt": { params: SessionPromptParams; result: SessionPromptResult };
  "session.resume": { params: SessionResumeParams; result: SessionResumeResult };
  "session.setMode": { params: SessionSetModeParams; result: SessionSetModeResult };
  "settings.get": { params: SettingsGetParams; result: SettingsGetResult };
  "settings.set": { params: SettingsSetParams; result: SettingsSetResult };
  "setup.catalog": { params: SetupCatalogParams; result: SetupCatalogResult };
  "skill.install": { params: SkillInstallParams; result: SkillInstallResult };
  "skill.list": { params: SkillListParams; result: SkillListResult };
  "skill.reload": { params: SkillReloadParams; result: SkillReloadResult };
  "skill.remove": { params: SkillRemoveParams; result: SkillRemoveResult };
  "skill.search": { params: SkillSearchParams; result: SkillSearchResult };
  "system.checkUpdate": { params: SystemCheckUpdateParams; result: SystemCheckUpdateResult };
  "system.health": { params: SystemHealthParams; result: SystemHealthResult };
  "system.hello": { params: SystemHelloParams; result: SystemHelloResult };
  "system.info": { params: SystemInfoParams; result: SystemInfoResult };
  "system.reloadSettings": { params: SystemReloadSettingsParams; result: SystemReloadSettingsResult };
  "system.restart": { params: SystemRestartParams; result: SystemRestartResult };
  "system.shutdown": { params: SystemShutdownParams; result: SystemShutdownResult };
  "system.update": { params: SystemUpdateParams; result: SystemUpdateResult };
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
  | "audio.capabilities"
  | "audio.record.start"
  | "audio.record.stop"
  | "audio.speak"
  | "audio.transcribe"
  | "backend.set"
  | "command.list"
  | "command.run"
  | "gateway.bind"
  | "gateway.list"
  | "gateway.sync"
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
  | "provider.models"
  | "session.close"
  | "session.compact"
  | "session.create"
  | "session.interrupt"
  | "session.list"
  | "session.prompt"
  | "session.resume"
  | "session.setMode"
  | "settings.get"
  | "settings.set"
  | "setup.catalog"
  | "skill.install"
  | "skill.list"
  | "skill.reload"
  | "skill.remove"
  | "skill.search"
  | "system.checkUpdate"
  | "system.health"
  | "system.hello"
  | "system.info"
  | "system.reloadSettings"
  | "system.restart"
  | "system.shutdown"
  | "system.update"
  | "team.start"
  | "team.status"
  | "tool.list";
/** Methods the server calls on the client (bidirectional JSON-RPC). */
export type ServerMethod =
  | "approval.request";

/** Every server→client notification, with its payload type. */
export interface EventMap {
  "approval.pending": ApprovalPendingPayload;
  "approval.resolved": ApprovalResolvedPayload;
  "commands.changed": CommandsChangedPayload;
  "gateway.event": GatewayEventPayload;
  "job.event": JobEventPayload;
  "provider.loginProgress": ProviderLoginProgressPayload;
  "session.event": SessionEventPayload;
  "settings.changed": SettingsChangedPayload;
  "system.updateProgress": SystemUpdateProgressPayload;
}

export type EventName = keyof EventMap;
export const EVENT_NAMES: readonly EventName[] = [
  "approval.pending",
  "approval.resolved",
  "commands.changed",
  "gateway.event",
  "job.event",
  "provider.loginProgress",
  "session.event",
  "settings.changed",
  "system.updateProgress",
];
