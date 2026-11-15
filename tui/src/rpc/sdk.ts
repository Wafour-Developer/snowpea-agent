/**
 * The `@snowpea/sdk` surface the TUI uses, re-exported under local names.
 *
 * Every shape here is now *derived* from the generated protocol types rather
 * than redeclared, so a protocol regeneration cannot silently drift away from
 * the TUI. The indirection stays because it keeps the rest of the TUI's imports
 * pointed at one module.
 */

import type {
  ApprovalRequestParams as SdkApprovalRequestParams,
  CompactionStartedEventPayload,
  ToolProgressEventPayload,
  TurnStartedEventPayload,
  LspDiagnosticsEventPayload,
  LspStatusResult,
  McpChangedPayload as SdkMcpChangedPayload,
  ApprovalRequestResult,
  Client,
  QuestionRequestParams as SdkQuestionRequestParams,
  QuestionRequestResult,
  CommandListResult,
  SessionEventPayload,
  SessionSetModeParams,
  SystemCheckUpdateResult,
  SystemUpdateProgressPayload,
  SystemUpdateResult,
  ExternalEventPayload,
  ExternalListResult,
  ExternalOpenResult,
  ExternalClisResult,
  ExternalStatePayload,
  JarvisDecisionPayload,
  JarvisDelegationChangedPayload,
  JarvisEscalationPayload,
  JarvisLedgerResult,
  JarvisOverviewResult,
  JarvisStatePayload,
  JarvisStatusResult,
  QuestionListResult,
} from "@snowpea/sdk";

export { connect, defaultResolveEndpoint } from "@snowpea/sdk";

/** `session.event` notification payload (contract §1). */
export type SessionEvent = SessionEventPayload;

/** Server→client `approval.request` (contract §1, §7). */
export type ApprovalRequestParams = SdkApprovalRequestParams & {
  /**
   * True on a request only a person may answer (core 1.53): Jarvis's
   * `delegation_set` proposal and the `tmux.adopt` confirmation. Declared here
   * until the SDK carries it; shaped like the generated type.
   */
  personOnly?: boolean | null;
};
export type ApprovalResponse = ApprovalRequestResult;

export type ApprovalDecision = ApprovalRequestResult["decision"];
/** `scope` is optional on the wire; the TUI always sends one. */
export type ApprovalScope = NonNullable<ApprovalRequestResult["scope"]>;

/** Server→client `question.request` — the `ask_user` tool's picker. */
export type QuestionRequestParams = SdkQuestionRequestParams;
export type QuestionResponse = QuestionRequestResult;
/** One question of the batch; the TUI draws each as a tab. */
export type QuestionItem = NonNullable<SdkQuestionRequestParams["questions"]>[number];
/** One row of the picker. */
export type QuestionOption = NonNullable<QuestionItem["options"]>[number];
/** One question's answer, as `question.respond` carries it. */
export type QuestionAnswerItem = NonNullable<QuestionRequestResult["answers"]>[number];

export type Mode = SessionSetModeParams["mode"];

/** `system.checkUpdate` result (CORE-update). */
export type UpdateCheck = SystemCheckUpdateResult;
/** `system.update` result. */
export type UpdateStart = SystemUpdateResult;
/** `system.updateProgress` notification payload. */
export type UpdateProgress = SystemUpdateProgressPayload;

/** One entry of `command.list` (contract §1, §9). */
export type CommandInfo = NonNullable<CommandListResult["commands"]>[number];

/** The connected client. `TuiClient` wraps this. */
export type SdkClient = Client;

export type ConnectFn = (options: {
  port: number;
  token: string;
  clientVersion: string;
  /** Where to find the daemon again after a drop; the SDK's default reads the local daemon.json. */
  resolveEndpoint?: () => { port: number; token: string } | null;
}) => Promise<SdkClient>;

/** `lsp.status` result (M13 §4). */
export type LspStatus = LspStatusResult;
/** One server row of that result. */
export type LspServerStatus = NonNullable<LspStatusResult["servers"]>[number];
/** `lsp.diagnostics` notification payload. */
export type LspDiagnostics = LspDiagnosticsEventPayload;

/** `mcp.changed` notification payload (M14 §3). */
export type McpChangedPayload = SdkMcpChangedPayload;

/** `turn.started` — the moment a turn's clock starts. */
export type TurnStarted = TurnStartedEventPayload;
/** `tool.progress` — a fragment of a running tool's output. */
export type ToolProgress = ToolProgressEventPayload;
/** `compaction.started` — compaction is under way. */
export type CompactionStarted = CompactionStartedEventPayload;

/** One external CLI: `claude`, `codex`, `agy` or `cursor` (1.53.0). */
export type ExternalCli = NonNullable<ExternalClisResult["clis"]>[number]["id"];
/** The handle of an external session. */
export interface ExternalRef {
  cli: ExternalCli;
  id: string;
}
/** One row of `external.list` / the session an `external.open` returns. */
export type ExternalSessionRow = NonNullable<ExternalListResult["sessions"]>[number];
/** One entry of an external timeline page. */
export type ExternalTimelineEvent = NonNullable<ExternalOpenResult["events"]>[number];
export type ExternalModeName = NonNullable<ExternalSessionRow["mode"]>;
type SdkExternalCliInfo = NonNullable<ExternalClisResult["clis"]>[number];
/**
 * One CLI of `external.clis`. `models`, `modes` and `capabilities.steer` are
 * core 1.53 additions the SDK build here does not carry yet; declared locally,
 * shaped like the generated ones.
 */
export type ExternalCliInfo = Omit<SdkExternalCliInfo, "capabilities"> & {
  capabilities?: (NonNullable<SdkExternalCliInfo["capabilities"]> & { steer?: boolean }) | null;
  /** Models the CLI lists (empty until it answered; `external.clis.changed` follows). */
  models?: string[];
  /** The modes a new session may use (also when the CLI is not installed). */
  modes?: ExternalModeName[];
};
/** `external.clis.changed`: all four CLIs, as `external.clis` would answer now. */
export interface ExternalClisChanged {
  clis?: ExternalCliInfo[];
}
/** `jarvis.escalation.dismissed`. */
export interface JarvisEscalationDismissed {
  ids: string[];
}
/** `jarvis.access`: Jarvis read or instructed a session. */
export interface JarvisAccessEvent {
  action: "read" | "prompt" | "steer" | "interrupt" | "create" | "approval" | "question";
  external?: ExternalRef | null;
  hostToolsFrom?: string | null;
  id: string;
  project?: string | null;
  /** `ext:<cli>:<id>` for an external session. */
  sessionId: string;
  summary?: string;
  ts: string;
}
/** `SessionSummary.jarvis` (1.53 + levelScope/grantId). */
export interface SessionJarvisState {
  exposed: boolean;
  grantId?: string | null;
  level?: "staged" | "full" | null;
  levelScope?: "session" | "project" | "global" | "default" | null;
  pausedUntil?: string | null;
  takenOver?: boolean;
}
export type ExternalEvent = ExternalEventPayload;
export type ExternalState = ExternalStatePayload;

export type JarvisStatus = JarvisStatusResult;
/** One overview row; the external-session fields are core 1.53 additions. */
export type JarvisOverviewRow = NonNullable<JarvisOverviewResult["rows"]>[number] & {
  approvals?: "bridged" | "person" | "degraded" | "none" | null;
  /** False: snowpea does not gate what the session runs (UNCONTROLLED). */
  controlled?: boolean | null;
  /** False: hidden from Jarvis (project not exposed). */
  exposed?: boolean;
  mode?: ExternalModeName | null;
};
export type JarvisLedgerEntry = NonNullable<JarvisLedgerResult["entries"]>[number];
export type JarvisStateEvent = JarvisStatePayload;
export type JarvisDecisionEvent = JarvisDecisionPayload;
export type JarvisEscalationEvent = JarvisEscalationPayload;
export type JarvisDelegationEvent = JarvisDelegationChangedPayload;
export type QuestionList = QuestionListResult;
