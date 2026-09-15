// GENERATED — do not edit.
// Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py.
// Re-run `uv run python scripts/gen_protocol.py` after changing the protocol.

export const PROTOCOL_VERSION = "1.5.0";
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
  | "auth_expired"
  | "internal"
  | "invalid_params"
  | "login_unsupported"
  | "mcp_exists"
  | "mcp_invalid"
  | "mcp_not_found"
  | "mcp_read_only"
  | "mcp_start_failed"
  | "mcp_unsafe"
  | "mode_denied"
  | "not_found"
  | "not_implemented"
  | "protocol_incompatible"
  | "tool_inactive"
  | "unauthorized";
export const ERROR_CODES: readonly ErrorCode[] = [
  "approval_denied",
  "approval_timeout",
  "auth_expired",
  "internal",
  "invalid_params",
  "login_unsupported",
  "mcp_exists",
  "mcp_invalid",
  "mcp_not_found",
  "mcp_read_only",
  "mcp_start_failed",
  "mcp_unsafe",
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
    /** For kind='team': true for the project's active team. Exactly one team row is active, or none when the project has chosen no team. */
    active?: boolean | null;
    /** For kind='subagent': the id its subagent.* events carry. */
    agentId?: string | null;
    /** For kind='team': its member agent names, in roster order. */
    agents?: string[];
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
    /** definition = an agents/<name>.md file, subagent = a running child, named = a persistent named instance, team = the active project team (a label only - it is not spawnable). */
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
    /** For kind='team': which member fills each stage of `/team "<task>"` — keys explore, plan, implement, test, review, and a stage nobody fills is absent. Empty when the team has no implementer and so cannot run the pipeline at all. */
    stages?: Record<string, string>;
    /** For kind='subagent': queued, running, done or error. */
    status?: string | null;
    /** For kind='subagent': the task it was given. */
    task?: string | null;
  })[];
}

/** `agent.spawn` params. Run a named agent on a task. */
export interface AgentSpawnParams {
  /** Run this one spawn on a specific model: a models.profiles id, a 'vendor:model' pair, or a bare vendor. Outranks the agent's own assignment; null uses the configured routing. */
  model?: string | null;
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
  /** Why the human refused; quoted back to the model as the tool's refusal so the next turn can answer it (M15b §1). */
  reason?: string;
  /** How long the decision applies. */
  scope?: "once" | "session" | "project" | "always";
}

/** `approval.respond` params. Answer a pending approval and unblock the turn. */
export interface ApprovalRespondParams {
  /** allow runs the tool, deny ends the turn. */
  decision: "allow" | "deny";
  /** Why the human refused; quoted back to the model as the tool's refusal so the next turn can answer it (M15b §1). */
  reason?: string;
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

/** `audio.install` params. Install a local voice engine and re-run detection. */
export interface AudioInstallParams {
  /** Engine id from the setup catalog: faster-whisper (or local-whisper), piper, edge-tts. A system package (espeak-ng, say, powershell) answers ok=false with a hint instead. */
  engine: string;
}

/** `audio.install` result. */
export interface AudioInstallResult {
  /** Engine that was attempted, after id normalisation. */
  engine: string;
  /** What to do instead, when ok is false: the platform's own install command for a system package, or why the attempt could not run. */
  hint?: string | null;
  /** Tail of the installer's combined output, newest last; may be empty. */
  log?: string;
  /** True when the engine is installed and now detected. */
  ok: boolean;
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
    /** TUI/chat session that also receives every result. */
    originSessionId?: string | null;
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

/** `lsp.catalog` params. List every registered language server, regardless of whether it has started. */
export type LspCatalogParams = Record<string, unknown>;

/** `lsp.catalog` result. */
export interface LspCatalogResult {
  /** One row per registered server id. */
  servers?: ({
    /** True when settings (lsp.disabled, a default-off id, or an explicit lsp.servers override) keep this server from starting. */
    disabled: boolean;
    /** Extensions or whole filenames this server claims. */
    extensions?: string[];
    /** Server id, e.g. 'pyright' or 'gopls'. */
    id: string;
    /** A command that installs it manually, e.g. 'pip install ty'. */
    installHint?: string | null;
    /** True when lsp.autoInstall could obtain it (npm/pip/go); False means it must already be on PATH. */
    installable: boolean;
    /** LSP language ids this server's extensions map to. */
    languageIds?: string[];
  })[];
}

/** `lsp.status` params. Report every language server the daemon has started and its state. */
export type LspStatusParams = Record<string, unknown>;

/** `lsp.status` result. */
export interface LspStatusResult {
  /** One row per (server, root) pair. */
  servers?: ({
    /** Server id, e.g. 'pyright' or 'gopls'. */
    id: string;
    /** LSP language id the server's first extension maps to. */
    languageId?: string;
    /** Process id while it is running. */
    pid?: number | null;
    /** Project root the server was started in. */
    root: string;
    /** starting, ready, broken or stopped. */
    state: "starting" | "ready" | "broken" | "stopped";
  })[];
}

/** `mcp.add` params. Write an MCP server into the project or global .mcp.json and start it. */
export interface McpAddParams {
  /** Arguments, argv style; never a shell string. */
  args?: string[] | null;
  /** Executable for a stdio server. */
  command?: string | null;
  /** Working directory for a stdio server. */
  cwd?: string | null;
  /** Keep the entry but never start the server. */
  disabled?: boolean | null;
  /** Environment for the child process. */
  env?: Record<string, string> | null;
  /** Overwrite an existing entry and accept the security findings. */
  force?: boolean;
  /** HTTP headers sent with every request. */
  headers?: Record<string, string> | null;
  /** Server name; ^[a-zA-Z0-9_-]{1,64}$. */
  name: string;
  /** Permission tag for the server's tools; stored in settings. */
  permission?: "read" | "write" | "exec" | "network" | "send" | "config" | "delegate" | null;
  /** Catalog id copied before the explicit fields are applied. */
  preset?: string | null;
  /** Which file to write. */
  scope?: "project" | "global";
  /** Session whose workdir to use. */
  sessionId?: string | null;
  /** Probe the server before saving; nothing is written if it fails. */
  test?: boolean;
  /** Startup cap, in seconds. */
  timeoutSec?: number | null;
  /** Per-call cap, in seconds. */
  toolTimeoutSec?: number | null;
  /** Never register these tools of the server. */
  toolsExclude?: string[] | null;
  /** Register only these tools of the server. */
  toolsInclude?: string[] | null;
  /** Force a transport instead of inferring it. */
  type?: "stdio" | "http" | "sse" | null;
  /** Endpoint for an http or sse server. */
  url?: string | null;
  /** Project directory for scope=project. */
  workdir?: string | null;
}

/** `mcp.add` result. */
export interface McpAddResult {
  /** Why the server did not start. */
  error?: string | null;
  /** True when the entry was written. */
  ok?: boolean;
  /** File the entry was written to. */
  path: string;
  /** State of the server after the write. */
  state?: "stopped" | "starting" | "ready" | "error";
  /** Tools the probe found, so a client can offer a picker. */
  tools?: ({
    /** One-line description from the server. */
    description?: string;
    /** Tool name as the server reports it, without the mcp__ prefix. */
    name: string;
  })[];
  /** Security findings that --force accepted. */
  warnings?: string[];
}

/** `mcp.catalog` params. The curated MCP servers a client can offer as presets. */
export type McpCatalogParams = Record<string, unknown>;

/** `mcp.catalog` result. */
export interface McpCatalogResult {
  /** Curated servers, in display order. */
  entries?: ({
    /** What the server does. */
    description?: string;
    /** The .mcp.json entry this preset writes. */
    entry?: Record<string, unknown>;
    /** Where the server is documented. */
    homepage?: string;
    /** Catalog id, e.g. 'github'. */
    id: string;
    /** Human name. */
    label: string;
    /** Environment variables the user must supply. */
    needs?: string[];
    /** stdio, http or sse. */
    transport: "stdio" | "http" | "sse";
  })[];
}

/** `mcp.list` params. List every configured MCP server with its scope, state and tools. */
export interface McpListParams {
  /** Session whose workdir to read. */
  sessionId?: string | null;
  /** Project directory; defaults to the session's, then the daemon's. */
  workdir?: string | null;
}

/** `mcp.list` result. */
export interface McpListResult {
  /** One row per configured server, project entries winning. */
  servers?: ({
    /** Arguments, argv style. */
    args?: string[];
    /** Executable, for a stdio server. */
    command?: string | null;
    /** Working directory for a stdio server. */
    cwd?: string | null;
    /** True when the entry is kept but never started. */
    disabled?: boolean;
    /** Names of the environment variables set for the server; never their values. */
    envKeys?: string[];
    /** Why the last start failed. */
    error?: string | null;
    /** Names of the HTTP headers sent to the server; never their values. */
    headerKeys?: string[];
    /** Server name; the key under mcpServers. */
    name: string;
    /** Permission tag every tool of this server is judged by. */
    permission?: "read" | "write" | "exec" | "network" | "send" | "config" | "delegate";
    /** Plugin that brings a plugin-scoped entry. */
    plugin?: string | null;
    /** project, global, plugin or settings. */
    scope: "project" | "global" | "plugin" | "settings";
    /** stopped, starting, ready or error. */
    state?: "stopped" | "starting" | "ready" | "error";
    /** Startup cap override, in seconds. */
    timeoutSec?: number | null;
    /** Tools the server contributed after filtering. */
    toolCount?: number;
    /** Per-call cap override, in seconds. */
    toolTimeoutSec?: number | null;
    /** The tools themselves; filled once the server is ready. */
    tools?: ({
      /** One-line description from the server. */
      description?: string;
      /** Tool name as the server reports it, without the mcp__ prefix. */
      name: string;
    })[];
    /** These tools are never registered. */
    toolsExclude?: string[];
    /** Only these tools are registered, when set. */
    toolsInclude?: string[];
    /** stdio, http or sse. */
    transport: "stdio" | "http" | "sse";
    /** Endpoint, for an http or sse server. */
    url?: string | null;
  })[];
}

/** `mcp.reload` params. Restart one MCP server, or every configured one. */
export interface McpReloadParams {
  /** Server to restart; all of them when absent. */
  name?: string | null;
  /** Session whose workdir to use. */
  sessionId?: string | null;
  /** Project directory to rediscover from. */
  workdir?: string | null;
}

/** `mcp.reload` result. */
export interface McpReloadResult {
  /** True when the reload ran. */
  ok?: boolean;
  /** Servers that were restarted. */
  servers?: string[];
}

/** `mcp.remove` params. Delete an MCP server entry and stop the server. */
export interface McpRemoveParams {
  /** Server name. */
  name: string;
  /** Which file to rewrite. */
  scope?: "project" | "global";
  /** Session whose workdir to use. */
  sessionId?: string | null;
  /** Project directory for scope=project. */
  workdir?: string | null;
}

/** `mcp.remove` result. */
export interface McpRemoveResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `mcp.test` params. Probe a saved MCP server or an unsaved draft and report its tools. */
export interface McpTestParams {
  /** Arguments, argv style; never a shell string. */
  args?: string[] | null;
  /** Executable for a stdio server. */
  command?: string | null;
  /** Working directory for a stdio server. */
  cwd?: string | null;
  /** Keep the entry but never start the server. */
  disabled?: boolean | null;
  /** Environment for the child process. */
  env?: Record<string, string> | null;
  /** HTTP headers sent with every request. */
  headers?: Record<string, string> | null;
  /** Saved server to probe. */
  name?: string | null;
  /** Permission tag for the server's tools; stored in settings. */
  permission?: "read" | "write" | "exec" | "network" | "send" | "config" | "delegate" | null;
  /** Scope of the saved server. */
  scope?: "project" | "global" | "plugin" | "settings" | null;
  /** Session whose workdir to use. */
  sessionId?: string | null;
  /** Startup cap, in seconds. */
  timeoutSec?: number | null;
  /** Per-call cap, in seconds. */
  toolTimeoutSec?: number | null;
  /** Never register these tools of the server. */
  toolsExclude?: string[] | null;
  /** Register only these tools of the server. */
  toolsInclude?: string[] | null;
  /** Force a transport instead of inferring it. */
  type?: "stdio" | "http" | "sse" | null;
  /** Endpoint for an http or sse server. */
  url?: string | null;
  /** Project directory for scope=project. */
  workdir?: string | null;
}

/** `mcp.test` result. */
export interface McpTestResult {
  /** How long the probe took. */
  elapsedMs?: number;
  /** Spawn or protocol error, verbatim. */
  error?: string | null;
  /** True when the server answered tools/list. */
  ok: boolean;
  /** ready when the probe succeeded, error otherwise. */
  state: "stopped" | "starting" | "ready" | "error";
  /** What the server exposes. */
  tools?: ({
    /** One-line description from the server. */
    description?: string;
    /** Tool name as the server reports it, without the mcp__ prefix. */
    name: string;
  })[];
}

/** `mcp.update` params. Merge a patch into an existing MCP server entry. */
export interface McpUpdateParams {
  /** Server name. */
  name: string;
  /** Keys to change; anything absent is kept. */
  patch?: {
    /** Arguments, argv style; never a shell string. */
    args?: string[] | null;
    /** Executable for a stdio server. */
    command?: string | null;
    /** Working directory for a stdio server. */
    cwd?: string | null;
    /** Keep the entry but never start the server. */
    disabled?: boolean | null;
    /** Environment for the child process. */
    env?: Record<string, string> | null;
    /** HTTP headers sent with every request. */
    headers?: Record<string, string> | null;
    /** Permission tag for the server's tools; stored in settings. */
    permission?: "read" | "write" | "exec" | "network" | "send" | "config" | "delegate" | null;
    /** Startup cap, in seconds. */
    timeoutSec?: number | null;
    /** Per-call cap, in seconds. */
    toolTimeoutSec?: number | null;
    /** Never register these tools of the server. */
    toolsExclude?: string[] | null;
    /** Register only these tools of the server. */
    toolsInclude?: string[] | null;
    /** Force a transport instead of inferring it. */
    type?: "stdio" | "http" | "sse" | null;
    /** Endpoint for an http or sse server. */
    url?: string | null;
  };
  /** Which file to rewrite. */
  scope?: "project" | "global";
  /** Session whose workdir to use. */
  sessionId?: string | null;
  /** Project directory for scope=project. */
  workdir?: string | null;
}

/** `mcp.update` result. */
export interface McpUpdateResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `memory.delete` params. Forget one stored memory. */
export interface MemoryDeleteParams {
  /** Memory id to forget. */
  id: string;
}

/** `memory.delete` result. */
export interface MemoryDeleteResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `memory.list` params. List stored memories by scope, newest first. */
export interface MemoryListParams {
  /** Maximum number of entries. */
  limit?: number;
  /** Project root to use instead of a session's, so a CLI in a checkout can list that project's memories without opening a session. */
  project?: string | null;
  /** Free-text filter; omit to list newest first. */
  query?: string | null;
  /** Which scopes to list: "project", "global", "agent" or "all" (default). */
  scope?: "project" | "global" | "agent" | "all" | null;
  /** Session whose project and agent scopes to resolve; omit for global only. */
  sessionId?: string | null;
}

/** `memory.list` result. */
export interface MemoryListResult {
  /** Matching memories, newest or best first. */
  entries?: ({
    /** When it was written (ISO-8601, UTC). */
    createdAt?: string;
    /** Memory id. */
    id: string;
    /** Project root for a project memory; empty otherwise. */
    project?: string;
    /** Scope this memory is kept in. */
    scope?: "project" | "global" | "agent" | "all";
    /** Tags attached at write time. */
    tags?: string[];
    /** Stored text. */
    text: string;
  })[];
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
    /** Project root for a project memory; empty otherwise. */
    project?: string;
    /** Scope the hit came from. */
    scope?: "project" | "global" | "agent" | "all";
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
    /** Login flows the vendor supports: 'api_key' everywhere, plus provider-specific device-code, PKCE, Google ADC, or direct OAuth-token authentication. */
    authMethods?: string[];
    /** State of the stored credential: 'unconfigured', 'active', or 'expired' when an OAuth session is past its expiry and needs refreshing or a new login. */
    authStatus?: "unconfigured" | "active" | "expired";
    /** True when credentials are present. */
    configured?: boolean;
    /** True for a named OpenAI-compatible server the user added, not a built-in. */
    custom?: boolean;
    /** True for the vendor used when none is named. */
    default?: boolean;
    /** Model used when the caller names none. */
    defaultModel?: string;
    /** Human-readable vendor name for pickers. */
    label?: string;
    /** Model ids this vendor offers. */
    models?: string[];
    /** Preset this vendor follows: 'local' for the built-in local vendor and for every named OpenAI-compatible server, otherwise the vendor's own id. */
    preset?: string;
    /** True when this vendor accepts a reasoning-effort setting. False for a local-style server unless its block sets effort_param: true. */
    supportsEffort?: boolean;
    /** Vendor key, e.g. 'anthropic'. */
    vendor: string;
  })[];
}

/** `provider.loginWeb` params. Start a browser-based login flow for a provider. */
export interface ProviderLoginWebParams {
  /** Login flow to start: 'browser_pkce' or 'google_oauth' (browser), 'device_code' or 'google_adc' (headless), 'oauth_pkce' (OpenRouter). 'web' or an empty value picks the best flow this machine can complete. */
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
  /** One line naming the source, for a picker to show. */
  detail?: string;
  /** Model ids the vendor's endpoint reports. */
  models?: string[];
  /** Which rung answered: live (the vendor's endpoint), settings (providers.<vendor>.models), cache (the last good listing) or curated (this build's list, merged with models.dev). */
  source?: string;
  /** Vendor the listing came from. */
  vendor: string;
  /** Which of the listed models can be sent images, when that is known. A model missing from this map is unknown rather than text-only, so a picker draws no badge for it instead of a negative one. */
  vision?: Record<string, boolean>;
}

/** `provider.remove` params. Forget a configured provider, typically a named local server. */
export interface ProviderRemoveParams {
  /** Provider to forget: its credentials, its model profiles and the agent assignments that used them. */
  vendor: string;
}

/** `provider.remove` result. */
export interface ProviderRemoveResult {
  /** True when the call succeeded. */
  ok?: boolean;
}

/** `question.list` params. List questions the agent is still waiting on. */
export interface QuestionListParams {
  /** Scope the listing to one session; omit for the global set. */
  sessionId?: string | null;
}

/** `question.list` result. */
export interface QuestionListResult {
  /** Questions still waiting for an answer. */
  requests?: ({
    /** The questions, in the order they were asked. */
    questions?: ({
      /** Offer a free-text '기타 / Other' row alongside the options. */
      allowOther?: boolean;
      /** Short chip naming the question, e.g. "Auth method". */
      header?: string;
      /** More than one option may be picked. */
      multi?: boolean;
      /** Closed set of answers; empty means free text. */
      options?: ({
        /** One dim line under the label. */
        description?: string;
        /** What the row says, and what comes back in selected[]. */
        label: string;
        /** Monospace block beside the list, for an option easier shown than told. */
        preview?: string;
      })[];
      /** The question, including why the answer matters. */
      question: string;
      /** The free-text answer is a credential. A surface MUST mask it while it is typed, MUST NOT echo it into the transcript, and MUST NOT log it. Set for API keys and tokens; a URL or a project id is asked in the clear. */
      secret?: boolean;
    })[];
    /** Id to answer with question.respond. */
    requestId: string;
    /** Session whose turn is blocked. */
    sessionId: string;
    /** Seconds before the batch gives up. */
    timeoutSec?: number;
  })[];
}

/** `question.request` params. Ask the client to put a question to the human. */
export interface QuestionRequestParams {
  /** The questions, in the order they were asked. */
  questions?: ({
    /** Offer a free-text '기타 / Other' row alongside the options. */
    allowOther?: boolean;
    /** Short chip naming the question, e.g. "Auth method". */
    header?: string;
    /** More than one option may be picked. */
    multi?: boolean;
    /** Closed set of answers; empty means free text. */
    options?: ({
      /** One dim line under the label. */
      description?: string;
      /** What the row says, and what comes back in selected[]. */
      label: string;
      /** Monospace block beside the list, for an option easier shown than told. */
      preview?: string;
    })[];
    /** The question, including why the answer matters. */
    question: string;
    /** The free-text answer is a credential. A surface MUST mask it while it is typed, MUST NOT echo it into the transcript, and MUST NOT log it. Set for API keys and tokens; a URL or a project id is asked in the clear. */
    secret?: boolean;
  })[];
  /** Id to answer with question.respond. */
  requestId: string;
  /** Session whose turn is blocked. */
  sessionId: string;
  /** Seconds before the batch gives up. */
  timeoutSec?: number;
}

/** `question.request` result. */
export interface QuestionRequestResult {
  /** One entry per question, in question order. */
  answers?: ({
    /** Labels the human picked, in the order offered. */
    selected?: string[];
    /** Free text, for 'Other' or no options. */
    text?: string | null;
  })[];
}

/** `question.respond` params. Answer a pending question and unblock the turn. */
export interface QuestionRespondParams {
  /** One entry per question, in question order; empty declines the batch. */
  answers?: ({
    /** Labels the human picked, in the order offered. */
    selected?: string[];
    /** Free text, for 'Other' or no options. */
    text?: string | null;
  })[];
  /** Batch being answered. */
  requestId: string;
}

/** `question.respond` result. */
export interface QuestionRespondResult {
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
  /** Reasoning effort for this session; null follows the settings. */
  effort?: "low" | "medium" | "high" | "max" | null;
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

/** `session.deleteSaved` params. Delete saved sessions. */
export interface SessionDeleteSavedParams {
  /** Delete saved sessions from every directory. */
  all?: boolean;
  /** Delete one saved session. */
  sessionId?: string | null;
  /** Delete saved sessions rooted here. */
  workdir?: string | null;
}

/** `session.deleteSaved` result. */
export interface SessionDeleteSavedResult {
  deleted?: number;
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

/** `session.list` params. List live or saved sessions. */
export interface SessionListParams {
  /** Include persisted closed sessions. */
  includeClosed?: boolean;
  /** Only sessions of these kinds; omit for every kind. A surface that shows human threads asks for ["chat"] (CORE-session-kind). */
  kinds?: string[] | null;
  /** Only sessions rooted here. */
  workdir?: string | null;
}

/** `session.list` result. */
export interface SessionListResult {
  /** Every live session. */
  sessions?: ({
    /** Named agent the session belongs to, when it has one. */
    agent?: string | null;
    /** Tokens the session's current prompt occupies (CORE-context). */
    contextUsed?: number;
    /** Context window of the session's model; null when unknown. */
    contextWindow?: number | null;
    /** UTC ISO-8601 creation timestamp. */
    createdAt: string;
    /** Reasoning effort pinned to this session, or null when it follows agent.effortBy / agent.effort. */
    effort?: "low" | "medium" | "high" | "max" | null;
    /** Scheduled job this run belongs to; null for other kinds. */
    jobId?: string | null;
    /** What opened the session: a human (chat), a scheduled job, a spawned subagent, or a persistent named agent (CORE-session-kind). */
    kind?: "chat" | "scheduled" | "subagent" | "agent";
    /** Latest saved user input. */
    lastPrompt?: string | null;
    /** Current permission mode. */
    mode: "plan" | "accept" | "auto";
    /** Model id in use. */
    model?: string | null;
    /** Surface that owns approvals. */
    originSurface?: string | null;
    /** Session that caused this one: the thread that scheduled the job, or the parent that spawned the subagent. */
    parentSessionId?: string | null;
    /** Chat provider vendor in use. */
    provider?: string | null;
    /** True while the daemon has a turn in flight for this session. Always false for a stored row, which by definition has no live turn — a client should trust this rather than infer a running turn from a replay that ends on turn.started (CORE-dangling-turns). */
    running?: boolean;
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

/** `session.setEffort` params. Pin how hard a session's model may think, or clear the pin. */
export interface SessionSetEffortParams {
  /** One of 'low', 'medium', 'high', 'max'. Null clears the pin and lets agent.effortBy / agent.effort decide again. */
  effort?: "low" | "medium" | "high" | "max" | null;
  /** Session to pin. */
  sessionId: string;
}

/** `session.setEffort` result. */
export interface SessionSetEffortResult {
  /** Effective reasoning effort after the change. */
  effort: "low" | "medium" | "high" | "max";
  /** Rule that decided it: the session pin, a model or vendor rule, or the default. */
  effortSource: "session" | "model" | "vendor" | "default";
  /** The session's own pin; null when it follows the settings. */
  pinned?: "low" | "medium" | "high" | "max" | null;
  /** Session that was pinned. */
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

/** `session.setModel` params. Pin a session to a model profile, or clear the pin. */
export interface SessionSetModelParams {
  /** A models.profiles id, a 'vendor:model' pair, or a bare vendor name. Null or 'inherit' clears the pin and lets the configured routing decide. */
  model?: string | null;
  /** Session to pin. */
  sessionId: string;
}

/** `session.setModel` result. */
export interface SessionSetModelResult {
  /** Model id now in effect. */
  model?: string | null;
  /** False when the pin was cleared. */
  pinned?: boolean;
  /** Vendor now in effect. */
  provider?: string | null;
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
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
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
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
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
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** Speech-to-text choices; active reflects what is usable on this machine. */
  stt?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
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
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
  /** Text-to-speech choices; active reflects what is usable on this machine. */
  tts?: ({
    /** False for items listed but not usable yet. */
    active?: boolean;
    /** Whether this is the screen's default pick. */
    default?: boolean;
    /** One-line description. */
    description?: string;
    /** Stable id, e.g. a vendor or provider name. */
    id: string;
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
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
    /** A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. Set for a system package the daemon will not install, and as a fallback beside an Install button. */
    installHint?: string | null;
    /** True when audio.install can obtain this row without root, so a surface may draw an Install button next to it. Only the voice rows set it. */
    installable?: boolean;
    /** "no key", "key optional", "key required" or "self-hosted". */
    key: string;
    /** Display label. */
    label: string;
    /** True for the one row a screen should lead with and pre-select when nothing is configured yet. At most one row per list sets it. */
    recommended?: boolean;
    /** Display tags, e.g. ('free · no key', 'active'). */
    tags?: string[];
    /** "free", "paid" or "subscription". */
    tier: string;
  })[];
}

/** `skill.create` params. Write a new SKILL.md, generated from a brief or supplied verbatim. */
export interface SkillCreateParams {
  /** A complete SKILL.md body. When given, it is validated and written directly — no model turn runs. */
  content?: string | null;
  /** Natural-language brief. Used only when 'content' is omitted: the daemon starts the same generating turn '/skill create' runs and answers with a turnId rather than waiting for it. */
  description?: string | null;
  /** Overwrite an existing SKILL.md at the target. */
  force?: boolean;
  /** Skill name; also its directory and the future /<name>. */
  name: string;
  /** Where to write the skill. */
  scope?: "project" | "global";
  /** Draft mode only ('content' omitted): run the generating turn on this existing session instead of creating a headless one. Must be rooted at 'workdir'; invalid_params otherwise. Ignored when 'content' is given. */
  sessionId?: string | null;
  /** Project directory the skill is written under (or read/create a session from). */
  workdir: string;
}

/** `skill.create` result. */
export interface SkillCreateResult {
  /** Skill name, once known. */
  name?: string | null;
  /** Where the SKILL.md was written. */
  path?: string | null;
  /** Set alongside turnId: the session the turn ran on — the caller's own 'sessionId', or a new headless one created for 'workdir'. */
  sessionId?: string | null;
  /** Set instead of name/path when generation was started as a turn. */
  turnId?: string | null;
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
    /** Download count, when the source publishes one; 0 means unknown. */
    downloads?: number;
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
    /** Average rating, when the source publishes one; 0 means unrated. */
    rating?: number;
    /** Where it came from: builtin, global, project, plugin:<name> for an installed entry, or the marketplace that offered it. */
    source?: string;
    /** What the skill does. */
    summary?: string;
  })[];
}

/** `skill.read` params. Read a skill's SKILL.md. */
export interface SkillReadParams {
  /** Skill to read. */
  name: string;
  /** Project directory to resolve a project-scoped skill in. */
  workdir: string;
}

/** `skill.read` result. */
export interface SkillReadResult {
  /** Its full text. */
  content: string;
  /** Where the SKILL.md was found. */
  path: string;
  /** 'project' or 'global', wherever it was found. */
  scope: "project" | "global";
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
  /** Hubs the registry has deliberately switched off (not failures). Clients should mention them quietly, never as an outage. */
  notIncluded?: ({
    /** Human name of the hub, e.g. 'Hermes Hub'. */
    label: string;
    /** Why it is switched off on the registry. */
    reason?: string;
  })[];
  /** Matching skills. */
  skills?: ({
    /** Download count, when the source publishes one; 0 means unknown. */
    downloads?: number;
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
    /** Average rating, when the source publishes one; 0 means unrated. */
    rating?: number;
    /** Where it came from: builtin, global, project, plugin:<name> for an installed entry, or the marketplace that offered it. */
    source?: string;
    /** What the skill does. */
    summary?: string;
  })[];
  /** Sources that could not be reached, as '<source>: <reason>'. Empty skills with a non-empty list means offline, not no match. */
  unavailable?: string[];
}

/** `skill.write` params. Save a skill's SKILL.md verbatim. */
export interface SkillWriteParams {
  /** Full SKILL.md text to save. */
  content: string;
  /** Skill to write. */
  name: string;
  /** Where to write the skill. */
  scope?: "project" | "global";
  /** Project directory, when scope is 'project'. */
  workdir: string;
}

/** `skill.write` result. */
export interface SkillWriteResult {
  /** True when the call succeeded. */
  ok?: boolean;
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
    permissionTag: "read" | "write" | "exec" | "network" | "send" | "config" | "delegate";
    /** Backing provider for tools that have one, e.g. the web-search provider id; reads "configured → answering" when the configured one cannot run. */
    provider?: string;
    /** Why an inactive tool is inactive, e.g. "lsp.enabled is false". */
    reason?: string;
    /** MCP server this tool came from; empty for everything else (M14 §3). */
    server?: string;
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

/** `audio.install.progress` notification payload. */
export interface AudioInstallProgressPayload {
  /** Engine being installed. */
  engine: string;
  /** One line of the installer's output. */
  line: string;
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

/** `mcp.changed` notification payload. */
export interface McpChangedPayload {
  /** Why the server is in the error state. */
  error?: string | null;
  /** Server name. */
  name: string;
  /** True when the entry itself is gone. */
  removed?: boolean;
  /** Scope the server is declared in. */
  scope?: "project" | "global" | "plugin" | "settings";
  /** stopped, starting, ready or error. */
  state: "stopped" | "starting" | "ready" | "error";
  /** Tools the server currently contributes. */
  toolCount?: number;
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

/** `question.pending` notification payload. */
export interface QuestionPendingPayload {
  /** The question now in the shared queue. */
  request: {
    /** The questions, in the order they were asked. */
    questions?: ({
      /** Offer a free-text '기타 / Other' row alongside the options. */
      allowOther?: boolean;
      /** Short chip naming the question, e.g. "Auth method". */
      header?: string;
      /** More than one option may be picked. */
      multi?: boolean;
      /** Closed set of answers; empty means free text. */
      options?: ({
        /** One dim line under the label. */
        description?: string;
        /** What the row says, and what comes back in selected[]. */
        label: string;
        /** Monospace block beside the list, for an option easier shown than told. */
        preview?: string;
      })[];
      /** The question, including why the answer matters. */
      question: string;
      /** The free-text answer is a credential. A surface MUST mask it while it is typed, MUST NOT echo it into the transcript, and MUST NOT log it. Set for API keys and tokens; a URL or a project id is asked in the clear. */
      secret?: boolean;
    })[];
    /** Id to answer with question.respond. */
    requestId: string;
    /** Session whose turn is blocked. */
    sessionId: string;
    /** Seconds before the batch gives up. */
    timeoutSec?: number;
  };
}

/** `question.resolved` notification payload. */
export interface QuestionResolvedPayload {
  /** Surface or user that answered. */
  by: string;
  /** Question that was resolved. */
  requestId: string;
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

/** Payload of `session.event` with kind `compaction.started`. */
export interface CompactionStartedEventPayload {
  /** Estimated tokens the history holds now. */
  before?: number;
  kind?: "compaction.started";
  /** auto = the auto-compaction threshold triggered it. */
  reason?: "manual" | "auto";
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

/** Payload of `session.event` with kind `job.done`. */
export interface JobDoneEventPayload {
  /** Job that ran. */
  jobId: string;
  kind?: "job.done";
  /** Session the run used. */
  sessionId?: string | null;
  /** Job status as the scheduler recorded it. */
  status?: string;
  /** What the run reported. */
  text?: string;
}

/** Payload of `session.event` with kind `job.failed`. */
export interface JobFailedEventPayload {
  /** Job that ran. */
  jobId: string;
  kind?: "job.failed";
  /** Session the run used. */
  sessionId?: string | null;
  /** Job status as the scheduler recorded it. */
  status?: string;
  /** What the run reported, or the error. */
  text?: string;
}

/** Payload of `session.event` with kind `lsp.diagnostics`. */
export interface LspDiagnosticsEventPayload {
  /** Diagnostics of every severity. */
  count?: number;
  /** How many of them are errors. */
  errors?: number;
  kind?: "lsp.diagnostics";
  /** File the diagnostics are about. */
  path: string;
  /** How many of them are warnings. */
  warnings?: number;
}

/** Payload of `session.event` with kind `message.delta`. */
export interface MessageDeltaEventPayload {
  kind?: "message.delta";
  /** Text fragment to append to the current message. */
  text: string;
}

/** Payload of `session.event` with kind `message.done`. */
export interface MessageDoneEventPayload {
  /** How many times the turn was resumed after hitting the output limit. */
  continuations?: number;
  kind?: "message.done";
  /** Who produced the message. */
  role?: "assistant" | "user" | "system";
  /** Full message text. */
  text: string;
  /** The answer still hit the output limit and is incomplete. */
  truncated?: boolean;
}

/** Payload of `session.event` with kind `message.reasoning`. */
export interface MessageReasoningEventPayload {
  /** Characters of reasoning so far in this turn. */
  chars?: number;
  kind?: "message.reasoning";
  /** Reasoning fragment; not part of the answer. */
  text?: string;
}

/** Payload of `session.event` with kind `message.user`. */
export interface MessageUserEventPayload {
  /** Files sent along with the prompt. */
  attachments?: ({
    /** Attachment flavour. */
    kind?: "file" | "image" | "text";
    /** Display name shown under the prompt. */
    name?: string;
  })[];
  kind?: "message.user";
  /** Prompt text as the model received it. */
  text: string;
}

/** Payload of `session.event` with kind `mode.changed`. */
export interface ModeChangedEventPayload {
  kind?: "mode.changed";
  /** Mode now in effect. */
  mode: "plan" | "accept" | "auto";
}

/** Payload of `session.event` with kind `model.changed`. */
export interface ModelChangedEventPayload {
  /** Effective reasoning effort for the session (CORE-effort). */
  effort?: "low" | "medium" | "high" | "max" | null;
  /** Rule that decided the effort: 'session', 'model', 'vendor' or 'default'. */
  effortSource?: "session" | "model" | "vendor" | "default" | null;
  kind?: "model.changed";
  /** Model id now in effect. */
  model?: string | null;
  /** Vendor now in effect. */
  provider?: string | null;
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
  /** One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. */
  title?: string;
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
  /** One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. */
  title?: string;
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
  /** One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. */
  title?: string;
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

/** Payload of `session.event` with kind `tool.progress`. */
export interface ToolProgressEventPayload {
  /** Id of the tool.call this output belongs to. */
  callId: string;
  /** Raw output fragment, in order. */
  chunk?: string;
  kind?: "tool.progress";
  /** Tool that is running. */
  name: string;
  /** 0-based index of this progress within the call. */
  seq?: number;
  /** Which stream the chunk came from. */
  stream?: "stdout" | "stderr";
  /** True on the final progress when the byte cap stopped the tail. */
  truncated?: boolean;
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

/** Payload of `session.event` with kind `turn.dequeued`. */
export interface TurnDequeuedEventPayload {
  kind?: "turn.dequeued";
  /** Prompts still waiting after this one left. */
  queued?: number;
  /** started = it is now running, dropped = it was discarded. */
  reason?: "started" | "dropped";
  /** Turn id that left the queue. */
  turnId: string;
}

/** Payload of `session.event` with kind `turn.done`. */
export interface TurnDoneEventPayload {
  kind?: "turn.done";
  /** Why the turn ended. budget = the tool-round budget ran out; the turn reported what it had done before ending. */
  reason?: "complete" | "interrupted" | "error" | "denied" | "timeout" | "budget";
  /** True when the daemon wrote this event itself to close a turn a crash left open, rather than the turn reporting its own end (CORE-dangling-turns). The turn produced no further output after the events already stored. */
  synthetic?: boolean;
  /** Turn that ended. */
  turnId: string;
}

/** Payload of `session.event` with kind `turn.queued`. */
export interface TurnQueuedEventPayload {
  kind?: "turn.queued";
  /** 1-based place in the queue behind the running turn. */
  position: number;
  /** Prompts waiting in the queue after this one was added. */
  queued: number;
  /** Turn id assigned to the queued prompt. */
  turnId: string;
}

/** Payload of `session.event` with kind `turn.started`. */
export interface TurnStartedEventPayload {
  kind?: "turn.started";
  /** The prompt that opened it; null when there is none. */
  prompt?: string | null;
  /** True when this turn waited in the prompt queue first. */
  queued?: boolean;
  /** Turn that is now running. */
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
  "compaction.started": CompactionStartedEventPayload;
  "context": ContextEventPayload;
  "diff": DiffEventPayload;
  "error": ErrorEventPayload;
  "job.done": JobDoneEventPayload;
  "job.failed": JobFailedEventPayload;
  "lsp.diagnostics": LspDiagnosticsEventPayload;
  "message.delta": MessageDeltaEventPayload;
  "message.done": MessageDoneEventPayload;
  "message.reasoning": MessageReasoningEventPayload;
  "message.user": MessageUserEventPayload;
  "mode.changed": ModeChangedEventPayload;
  "model.changed": ModelChangedEventPayload;
  "subagent.done": SubagentDoneEventPayload;
  "subagent.spawn": SubagentSpawnEventPayload;
  "subagent.update": SubagentUpdateEventPayload;
  "team.task.update": TeamTaskUpdateEventPayload;
  "tool.call": ToolCallEventPayload;
  "tool.progress": ToolProgressEventPayload;
  "tool.result": ToolResultEventPayload;
  "turn.dequeued": TurnDequeuedEventPayload;
  "turn.done": TurnDoneEventPayload;
  "turn.queued": TurnQueuedEventPayload;
  "turn.started": TurnStartedEventPayload;
  "usage": UsageEventPayload;
}

export type SessionEventKind = keyof SessionEventKindMap;
export const SESSION_EVENT_KINDS: readonly SessionEventKind[] = [
  "audio.spoken",
  "backend.changed",
  "compaction",
  "compaction.started",
  "context",
  "diff",
  "error",
  "job.done",
  "job.failed",
  "lsp.diagnostics",
  "message.delta",
  "message.done",
  "message.reasoning",
  "message.user",
  "mode.changed",
  "model.changed",
  "subagent.done",
  "subagent.spawn",
  "subagent.update",
  "team.task.update",
  "tool.call",
  "tool.progress",
  "tool.result",
  "turn.dequeued",
  "turn.done",
  "turn.queued",
  "turn.started",
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
  "audio.install": { params: AudioInstallParams; result: AudioInstallResult };
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
  "lsp.catalog": { params: LspCatalogParams; result: LspCatalogResult };
  "lsp.status": { params: LspStatusParams; result: LspStatusResult };
  "mcp.add": { params: McpAddParams; result: McpAddResult };
  "mcp.catalog": { params: McpCatalogParams; result: McpCatalogResult };
  "mcp.list": { params: McpListParams; result: McpListResult };
  "mcp.reload": { params: McpReloadParams; result: McpReloadResult };
  "mcp.remove": { params: McpRemoveParams; result: McpRemoveResult };
  "mcp.test": { params: McpTestParams; result: McpTestResult };
  "mcp.update": { params: McpUpdateParams; result: McpUpdateResult };
  "memory.delete": { params: MemoryDeleteParams; result: MemoryDeleteResult };
  "memory.list": { params: MemoryListParams; result: MemoryListResult };
  "memory.search": { params: MemorySearchParams; result: MemorySearchResult };
  "memory.write": { params: MemoryWriteParams; result: MemoryWriteResult };
  "permission.allowlist.add": { params: PermissionAllowlistAddParams; result: PermissionAllowlistAddResult };
  "permission.allowlist.list": { params: PermissionAllowlistListParams; result: PermissionAllowlistListResult };
  "permission.allowlist.remove": { params: PermissionAllowlistRemoveParams; result: PermissionAllowlistRemoveResult };
  "provider.configure": { params: ProviderConfigureParams; result: ProviderConfigureResult };
  "provider.list": { params: ProviderListParams; result: ProviderListResult };
  "provider.loginWeb": { params: ProviderLoginWebParams; result: ProviderLoginWebResult };
  "provider.models": { params: ProviderModelsParams; result: ProviderModelsResult };
  "provider.remove": { params: ProviderRemoveParams; result: ProviderRemoveResult };
  "question.list": { params: QuestionListParams; result: QuestionListResult };
  "question.request": { params: QuestionRequestParams; result: QuestionRequestResult };
  "question.respond": { params: QuestionRespondParams; result: QuestionRespondResult };
  "session.close": { params: SessionCloseParams; result: SessionCloseResult };
  "session.compact": { params: SessionCompactParams; result: SessionCompactResult };
  "session.create": { params: SessionCreateParams; result: SessionCreateResult };
  "session.deleteSaved": { params: SessionDeleteSavedParams; result: SessionDeleteSavedResult };
  "session.interrupt": { params: SessionInterruptParams; result: SessionInterruptResult };
  "session.list": { params: SessionListParams; result: SessionListResult };
  "session.prompt": { params: SessionPromptParams; result: SessionPromptResult };
  "session.resume": { params: SessionResumeParams; result: SessionResumeResult };
  "session.setEffort": { params: SessionSetEffortParams; result: SessionSetEffortResult };
  "session.setMode": { params: SessionSetModeParams; result: SessionSetModeResult };
  "session.setModel": { params: SessionSetModelParams; result: SessionSetModelResult };
  "settings.get": { params: SettingsGetParams; result: SettingsGetResult };
  "settings.set": { params: SettingsSetParams; result: SettingsSetResult };
  "setup.catalog": { params: SetupCatalogParams; result: SetupCatalogResult };
  "skill.create": { params: SkillCreateParams; result: SkillCreateResult };
  "skill.install": { params: SkillInstallParams; result: SkillInstallResult };
  "skill.list": { params: SkillListParams; result: SkillListResult };
  "skill.read": { params: SkillReadParams; result: SkillReadResult };
  "skill.reload": { params: SkillReloadParams; result: SkillReloadResult };
  "skill.remove": { params: SkillRemoveParams; result: SkillRemoveResult };
  "skill.search": { params: SkillSearchParams; result: SkillSearchResult };
  "skill.write": { params: SkillWriteParams; result: SkillWriteResult };
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
  | "audio.install"
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
  | "lsp.catalog"
  | "lsp.status"
  | "mcp.add"
  | "mcp.catalog"
  | "mcp.list"
  | "mcp.reload"
  | "mcp.remove"
  | "mcp.test"
  | "mcp.update"
  | "memory.delete"
  | "memory.list"
  | "memory.search"
  | "memory.write"
  | "permission.allowlist.add"
  | "permission.allowlist.list"
  | "permission.allowlist.remove"
  | "provider.configure"
  | "provider.list"
  | "provider.loginWeb"
  | "provider.models"
  | "provider.remove"
  | "question.list"
  | "question.respond"
  | "session.close"
  | "session.compact"
  | "session.create"
  | "session.deleteSaved"
  | "session.interrupt"
  | "session.list"
  | "session.prompt"
  | "session.resume"
  | "session.setEffort"
  | "session.setMode"
  | "session.setModel"
  | "settings.get"
  | "settings.set"
  | "setup.catalog"
  | "skill.create"
  | "skill.install"
  | "skill.list"
  | "skill.read"
  | "skill.reload"
  | "skill.remove"
  | "skill.search"
  | "skill.write"
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
  | "approval.request"
  | "question.request";

/** Every server→client notification, with its payload type. */
export interface EventMap {
  "approval.pending": ApprovalPendingPayload;
  "approval.resolved": ApprovalResolvedPayload;
  "audio.install.progress": AudioInstallProgressPayload;
  "commands.changed": CommandsChangedPayload;
  "gateway.event": GatewayEventPayload;
  "job.event": JobEventPayload;
  "mcp.changed": McpChangedPayload;
  "provider.loginProgress": ProviderLoginProgressPayload;
  "question.pending": QuestionPendingPayload;
  "question.resolved": QuestionResolvedPayload;
  "session.event": SessionEventPayload;
  "settings.changed": SettingsChangedPayload;
  "system.updateProgress": SystemUpdateProgressPayload;
}

export type EventName = keyof EventMap;
export const EVENT_NAMES: readonly EventName[] = [
  "approval.pending",
  "approval.resolved",
  "audio.install.progress",
  "commands.changed",
  "gateway.event",
  "job.event",
  "mcp.changed",
  "provider.loginProgress",
  "question.pending",
  "question.resolved",
  "session.event",
  "settings.changed",
  "system.updateProgress",
];
