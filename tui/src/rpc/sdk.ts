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
  ApprovalRequestResult,
  Client,
  CommandListResult,
  SessionEventPayload,
  SessionSetModeParams,
} from "@snowpea/sdk";

export { connect } from "@snowpea/sdk";

/** `session.event` notification payload (contract §1). */
export type SessionEvent = SessionEventPayload;

/** Server→client `approval.request` (contract §1, §7). */
export type ApprovalRequestParams = SdkApprovalRequestParams;
export type ApprovalResponse = ApprovalRequestResult;

export type ApprovalDecision = ApprovalRequestResult["decision"];
/** `scope` is optional on the wire; the TUI always sends one. */
export type ApprovalScope = NonNullable<ApprovalRequestResult["scope"]>;

export type Mode = SessionSetModeParams["mode"];

/** One entry of `command.list` (contract §1, §9). */
export type CommandInfo = NonNullable<CommandListResult["commands"]>[number];

/** The connected client. `TuiClient` wraps this. */
export type SdkClient = Client;

export type ConnectFn = (options: {
  port: number;
  token: string;
  clientVersion: string;
}) => Promise<SdkClient>;
