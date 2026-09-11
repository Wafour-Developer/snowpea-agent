/**
 * Local typed view of the `@snowpea/sdk` surface (m1-core-contract §11).
 *
 * US-007 owns the real implementation. The TUI only needs the shape, so it is
 * declared here and the module is loaded lazily. That keeps `tsc -p tui` and
 * the esbuild bundle working whether or not the generated SDK has landed, and
 * the shapes below are the contract the SDK is expected to satisfy.
 */

/** `session.event` notification payload (contract §1). */
export interface SessionEvent {
  sessionId: string;
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  ts?: string;
}

/** Server→client `approval.request` (contract §1, §7). */
export interface ApprovalRequestParams {
  requestId: string;
  sessionId: string;
  tool: string;
  args: Record<string, unknown>;
  risk: string;
  timeoutSec?: number;
  scopeHint?: ApprovalScope;
}

export type ApprovalDecision = "allow" | "deny";
export type ApprovalScope = "once" | "session" | "project" | "always";

export interface ApprovalResponse {
  decision: ApprovalDecision;
  scope: ApprovalScope;
}

export type Mode = "plan" | "accept" | "auto";

/** One entry of `command.list` (contract §1, §9). */
export interface CommandInfo {
  name: string;
  summary: string;
  argsSchema?: Record<string, unknown>;
  source: "builtin" | "skill" | "plugin" | string;
}

/**
 * Minimal client surface the TUI consumes. `call` is intentionally loose here;
 * once `@snowpea/sdk` ships its generated `MethodMap` the real client's
 * narrower signature still satisfies this structural type.
 */
export interface SdkClient {
  call(method: string, params?: Record<string, unknown>): Promise<any>;
  /** Returns a disposer in the real SDK; the TUI never unsubscribes. */
  on(event: string, cb: (params: any) => void): unknown;
  off(event: string, cb?: (params: any) => void): void;
  onRequest(
    method: "approval.request",
    handler: (params: ApprovalRequestParams) => Promise<ApprovalResponse>,
  ): unknown;
  /** Highest `seq` the SDK has seen for a session (used for resume). */
  sessionSeq?(sessionId: string): number;
  close(): Promise<void> | void;
}

export interface ConnectOptions {
  port: number;
  token: string;
  clientVersion: string;
}

export type ConnectFn = (options: ConnectOptions) => Promise<SdkClient>;

/**
 * Resolve `connect` from `@snowpea/sdk` at runtime.
 *
 * Thrown errors are surfaced by the entry point as a normal startup failure
 * (non-zero exit) rather than an unhandled import-time crash.
 */
export async function loadConnect(): Promise<ConnectFn> {
  const mod = (await import("@snowpea/sdk")) as Record<string, unknown> & {
    default?: Record<string, unknown>;
  };
  const candidate = (mod.connect ?? mod.default?.connect) as ConnectFn | undefined;
  if (typeof candidate !== "function") {
    throw new Error(
      "@snowpea/sdk does not export connect(); build the SDK first (npm -w sdk run build)",
    );
  }
  return candidate;
}
