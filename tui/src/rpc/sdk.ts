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
