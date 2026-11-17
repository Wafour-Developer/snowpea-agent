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
  ExternalClisChangedPayload,
  ExternalClisConfirmPathParams,
  ExternalStatePayload,
  ApprovalRouteChangedPayload,
  JarvisAccessPayload,
  JarvisEscalationDismissedPayload,
  JarvisHandbackResult,
  QuestionRouteChangedPayload,
  JarvisDecisionPayload,
  JarvisDelegationChangedPayload,
  JarvisEscalationPayload,
  JarvisLedgerResult,
  JarvisOverviewResult,
  JarvisStatePayload,
  JarvisStatusResult,
  QuestionListResult,
  ApprovalResolvedPayload,
  QuestionResolvedPayload,
  SecurityBlockedPayload,
  SecurityCheckParams,
  SecurityCheckResult,
  SecurityGetResult,
  SecurityPresetsListResult,
  WebRemoteStatusResult,
  WebRemoteListenParams,
  ProjectTrustStatusResult,
  ProjectTrustChangedPayload,
  CommandResultEventPayload,
} from "@snowpea/sdk";

export { connect, defaultResolveEndpoint } from "@snowpea/sdk";

/** `session.event` notification payload (contract §1). */
export type SessionEvent = SessionEventPayload;

/** Server→client `approval.request` (contract §1, §7). */
export type ApprovalRequestParams = SdkApprovalRequestParams;
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
/** One CLI of `external.clis`. */
export type ExternalCliInfo = NonNullable<ExternalClisResult["clis"]>[number];
/** One of a CLI's own modes, as the CLI names it (1.56.0, `externalNativeModes`). */
export type ExternalNativeMode = NonNullable<ExternalCliInfo["nativeModes"]>[number];
/** `external.clis.changed`: all four CLIs, as `external.clis` would answer now. */
export type ExternalClisChanged = ExternalClisChangedPayload;
/** `external.clis.confirmPath` params. */
export type ExternalConfirmPathParams = ExternalClisConfirmPathParams;
/** `jarvis.escalation.dismissed`. */
export type JarvisEscalationDismissed = JarvisEscalationDismissedPayload;
/** `jarvis.access`: Jarvis read or instructed a session. */
export type JarvisAccessEvent = JarvisAccessPayload;
/** `approval.route.changed`. */
export type ApprovalRouteChanged = ApprovalRouteChangedPayload;
/** `question.route.changed`. */
export type QuestionRouteChanged = QuestionRouteChangedPayload;
/** `SessionSummary.jarvis`. */
export type SessionJarvisState = JarvisHandbackResult["state"];
export type ExternalEvent = ExternalEventPayload;
export type ExternalState = ExternalStatePayload;

export type JarvisStatus = JarvisStatusResult;
/** One overview row. */
export type JarvisOverviewRow = NonNullable<JarvisOverviewResult["rows"]>[number];
export type JarvisLedgerEntry = NonNullable<JarvisLedgerResult["entries"]>[number];
export type JarvisStateEvent = JarvisStatePayload;
export type JarvisDecisionEvent = JarvisDecisionPayload;
export type JarvisEscalationEvent = JarvisEscalationPayload;
export type JarvisDelegationEvent = JarvisDelegationChangedPayload;
export type QuestionList = QuestionListResult;

/** `approval.resolved` (1.54.0 adds `scope`; `decision` is null for a withdrawn request). */
export type ApprovalResolved = ApprovalResolvedPayload;
/** `question.resolved`. */
export type QuestionResolved = QuestionResolvedPayload;

/** Security lists (1.54.0, capability `securityLists`). */
export type SecurityGet = SecurityGetResult;
export type SecurityPresets = SecurityPresetsListResult;
export type SecurityCheck = SecurityCheckResult;
export type SecurityCheckDraft = NonNullable<SecurityCheckParams["draft"]>;
export type SecurityBlocked = SecurityBlockedPayload;

/** Remote web access (1.54.0, capability `webRemoteControl`): every `web.remote.*` call but rotateToken answers this shape. */
export type WebRemoteStatus = WebRemoteStatusResult;
export type WebRemoteListen = WebRemoteListenParams;

/** Project trust (1.55.0, capability `projectTrust`): every `project.trust.*` call answers this shape. */
export type ProjectTrustStatus = ProjectTrustStatusResult;
export type ProjectTrustChanged = ProjectTrustChangedPayload;
/** `command.result` session event (1.55.0, capability `commandsBesideTurn`). */
export type CommandResultEvent = CommandResultEventPayload;
