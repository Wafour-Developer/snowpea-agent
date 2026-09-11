/**
 * Thin wrapper around `@snowpea/sdk` for the TUI (plan §3.2).
 *
 * The SDK owns transport, reconnection and `session.resume(afterSeq)` replay.
 * This wrapper only:
 *   - tracks the highest `seq` seen per session (so resume state is visible and
 *     duplicate replayed events can be dropped),
 *   - surfaces connection status transitions to the UI,
 *   - binds the server→client `approval.request` handler.
 */

import {
  connect as sdkConnect,
  type ApprovalRequestParams,
  type ApprovalResponse,
  type ConnectFn,
  type Mode,
  type SdkClient,
  type SessionEvent,
} from "./sdk.js";

export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "closed";

export type ApprovalHandler = (
  request: ApprovalRequestParams,
) => Promise<ApprovalResponse>;

export interface TuiClientOptions {
  port: number;
  token: string;
  clientVersion: string;
  /** Injected in tests; defaults to the real `@snowpea/sdk` `connect`. */
  connectFn?: ConnectFn;
}

export interface CreateSessionOptions {
  workdir: string;
  mode?: Mode;
  originSurface?: string;
}

/** Events the TUI subscribes to. */
export interface TuiClientListeners {
  onSessionEvent?: (event: SessionEvent) => void;
  onStatus?: (status: ConnectionStatus) => void;
  onApprovalResolved?: (params: { requestId: string; decision: string; by?: string }) => void;
  /** An unattended request joined the shared queue; re-read `approval.list`. */
  onApprovalPending?: (params: { request: ApprovalRequestParams }) => void;
  /** A skill reload or plugin install changed the command table (M6 §1). */
  onCommandsChanged?: () => void;
}

export class TuiClient {
  private client: SdkClient | null = null;
  private readonly lastSeq = new Map<string, number>();
  private listeners: TuiClientListeners = {};
  private status: ConnectionStatus = "connecting";

  constructor(private readonly options: TuiClientOptions) {}

  /**
   * Highest `seq` observed for a session, or 0 when nothing arrived yet.
   * Prefers the SDK's own high-water mark, which also counts resume replays.
   */
  lastSeqFor(sessionId: string): number {
    const fromSdk = this.client?.sessionSeq(sessionId) ?? 0;
    return fromSdk > 0 ? fromSdk : (this.lastSeq.get(sessionId) ?? 0);
  }

  getStatus(): ConnectionStatus {
    return this.status;
  }

  setListeners(listeners: TuiClientListeners): void {
    this.listeners = listeners;
  }

  async connect(): Promise<void> {
    const connect = this.options.connectFn ?? sdkConnect;
    const client = await connect({
      port: this.options.port,
      token: this.options.token,
      clientVersion: this.options.clientVersion,
    });
    this.client = client;

    client.on("session.event", (event: SessionEvent) => this.handleSessionEvent(event));
    client.on("approval.resolved", (params: any) => this.listeners.onApprovalResolved?.(params));
    client.on("commands.changed", () => this.listeners.onCommandsChanged?.());
    client.on("approval.pending", (params: any) => this.listeners.onApprovalPending?.(params));
    // The SDK owns reconnect and replays each tracked session with
    // `session.resume(afterSeq)` before emitting `reconnected`; the TUI only
    // renders the transition.
    client.on("disconnected", (params) =>
      this.setStatus(params.willRetry ? "reconnecting" : "closed"),
    );
    client.on("reconnected", () => this.setStatus("connected"));

    this.setStatus("connected");
  }

  /** Bind the interactive server→client approval prompt. */
  onApprovalRequest(handler: ApprovalHandler): void {
    this.require().onRequest("approval.request", handler);
  }

  /**
   * Record the seq and forward. Replayed events at or below the high-water
   * mark are dropped so `session.resume` cannot duplicate rendered output.
   */
  private handleSessionEvent(event: SessionEvent): void {
    const seen = this.lastSeq.get(event.sessionId) ?? 0;
    if (typeof event.seq === "number") {
      if (event.seq <= seen) return;
      this.lastSeq.set(event.sessionId, event.seq);
    }
    this.listeners.onSessionEvent?.(event);
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.listeners.onStatus?.(status);
  }

  private require(): SdkClient {
    if (!this.client) throw new Error("TuiClient.connect() has not completed");
    return this.client;
  }

  /**
   * Untyped escape hatch. The SDK's `call` is generic over the generated
   * `MethodMap`; the TUI reaches methods by name (slash commands forward
   * whatever the daemon reports), so it is widened here deliberately. The
   * typed helpers below are what the components actually use.
   */
  call(method: string, params: Record<string, unknown> = {}): Promise<any> {
    const untyped = this.require().call as (m: string, p?: unknown) => Promise<any>;
    return untyped(method, params);
  }

  async createSession(options: CreateSessionOptions): Promise<string> {
    const result = await this.call("session.create", {
      workdir: options.workdir,
      ...(options.mode ? { mode: options.mode } : {}),
      originSurface: options.originSurface ?? "tui",
    });
    return result.sessionId as string;
  }

  prompt(sessionId: string, text: string): Promise<{ turnId: string }> {
    return this.call("session.prompt", { sessionId, text });
  }

  interrupt(sessionId: string): Promise<unknown> {
    return this.call("session.interrupt", { sessionId });
  }

  setMode(sessionId: string, mode: Mode): Promise<{ mode: Mode }> {
    return this.call("session.setMode", { sessionId, mode });
  }

  listApprovals(sessionId?: string): Promise<{ requests: ApprovalRequestParams[] }> {
    return this.call("approval.list", sessionId ? { sessionId } : {});
  }

  respondApproval(
    requestId: string,
    decision: ApprovalResponse["decision"],
    scope: ApprovalResponse["scope"],
  ): Promise<unknown> {
    return this.call("approval.respond", { requestId, decision, scope });
  }

  async closeSession(sessionId: string): Promise<void> {
    await this.call("session.close", { sessionId });
  }

  async close(): Promise<void> {
    if (!this.client) return;
    await this.client.close();
    this.client = null;
    this.setStatus("closed");
  }
}
