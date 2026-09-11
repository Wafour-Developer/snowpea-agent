/** Approval-queue helpers. The queue lives in the daemon, not in the client. */

import type { Client } from "./client.js";
import type { ApprovalRequestParams, ApprovalRequestResult, MethodMap } from "./protocol.js";

type P<M extends keyof MethodMap> = MethodMap[M]["params"];
type R<M extends keyof MethodMap> = MethodMap[M]["result"];

/** The decision half of an `approval.request` reply. */
export type ApprovalDecision = ApprovalRequestResult["decision"];
/** How long a decision sticks: once, session, project, or always. */
export type ApprovalScope = ApprovalRequestResult["scope"];

/** Pending approval requests, optionally narrowed to one session. */
export function listApprovals(client: Client, sessionId?: string): Promise<R<"approval.list">> {
  return client.call("approval.list", (sessionId ? { sessionId } : {}) as P<"approval.list">);
}

/** Answer a pending request that was surfaced elsewhere (TUI, messenger, CLI). */
export function respondToApproval(
  client: Client,
  requestId: string,
  decision: ApprovalDecision,
  scope: ApprovalScope,
): Promise<R<"approval.respond">> {
  return client.call("approval.respond", { requestId, decision, scope } as P<"approval.respond">);
}

/**
 * Register the handler for server→client `approval.request`. Returns a disposer.
 * The daemon only sends these to the session's originating surface.
 */
export function onApprovalRequest(
  client: Client,
  handler: (params: ApprovalRequestParams) => Promise<ApprovalRequestResult> | ApprovalRequestResult,
): () => void {
  return client.onRequest("approval.request", handler);
}

/** Allowlist a command pattern so `ask` becomes `allow` for it. */
export function addAllowlistPattern(
  client: Client,
  pattern: string,
  scope: P<"permission.allowlist.add">["scope"],
): Promise<R<"permission.allowlist.add">> {
  return client.call("permission.allowlist.add", { pattern, scope } as P<"permission.allowlist.add">);
}

/** List allowlist patterns, optionally for one scope. */
export function listAllowlistPatterns(
  client: Client,
  scope?: P<"permission.allowlist.list">["scope"],
): Promise<R<"permission.allowlist.list">> {
  return client.call("permission.allowlist.list", (scope ? { scope } : {}) as P<"permission.allowlist.list">);
}

/** Remove one allowlist pattern by id. */
export function removeAllowlistPattern(
  client: Client,
  patternId: string,
): Promise<R<"permission.allowlist.remove">> {
  return client.call("permission.allowlist.remove", { patternId } as P<"permission.allowlist.remove">);
}
