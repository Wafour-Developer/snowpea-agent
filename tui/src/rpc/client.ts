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

import { detectLanguage, setUiLanguage } from "../layout/language.js";
import {
  connect as sdkConnect,
  type ApprovalRequestParams,
  type ApprovalResponse,
  type ConnectFn,
  type McpChangedPayload,
  type Mode,
  type QuestionRequestParams,
  type QuestionResponse,
  type SdkClient,
  type SessionEvent,
  type UpdateCheck,
  type UpdateProgress,
  type UpdateStart,
} from "./sdk.js";

export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "closed";

/** One file sent with a prompt (contract §1, `session.prompt`). */
export interface PromptAttachment {
  kind?: "file" | "image" | "text";
  name?: string;
  path?: string;
  data?: string;
  mimeType?: string;
  size?: number;
}

export type ApprovalHandler = (
  request: ApprovalRequestParams,
) => Promise<ApprovalResponse>;

/** Answers the server→client `question.request` the `ask_user` tool raises. */
export type QuestionHandler = (
  request: QuestionRequestParams,
) => Promise<QuestionResponse>;

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
  /** A question was answered elsewhere, or gave up; stop showing it. */
  onQuestionResolved?: (params: { requestId: string; by?: string }) => void;
  /** A skill reload or plugin install changed the command table (M6 §1). */
  onCommandsChanged?: () => void;
  /** The upgrade started by `system.update` moved on (CORE-update). */
  onUpdateProgress?: (params: UpdateProgress) => void;
  /** A setting changed on the daemon; capabilities may have moved with it. */
  onSettingsChanged?: (params: { keys?: string[] }) => void;
  /** An MCP server was added, removed, or changed state (M14 §3). */
  onMcpChanged?: (params: McpChangedPayload) => void;
}

export class TuiClient {
  private client: SdkClient | null = null;
  private readonly lastSeq = new Map<string, number>();
  private listeners: TuiClientListeners = {};
  private status: ConnectionStatus = "connecting";
  /** True once `agent.replyLanguage` named a language; `auto` leaves it false. */
  private languagePinned = false;

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

  /**
   * Feature flags the daemon advertised in `system.hello`.
   *
   * A surface asks this before offering something optional: a daemon without
   * "audio" has no audio methods at all, and calling them would only produce a
   * confusing error.
   */
  serverCapabilities(): string[] {
    return this.client?.capabilities ?? [];
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
    client.on("question.resolved", (params: any) => this.listeners.onQuestionResolved?.(params));
    client.on("system.updateProgress", (params: any) => this.listeners.onUpdateProgress?.(params));
    client.on("mcp.changed", (params) => this.listeners.onMcpChanged?.(params));
    client.on("settings.changed", (params: any) => {
      void this.loadUiLanguage();
      this.listeners.onSettingsChanged?.(params ?? {});
    });
    // The SDK owns reconnect and replays each tracked session with
    // `session.resume(afterSeq)` before emitting `reconnected`; the TUI only
    // renders the transition.
    client.on("disconnected", (params) =>
      this.setStatus(params.willRetry ? "reconnecting" : "closed"),
    );
    client.on("reconnected", () => this.setStatus("connected"));

    this.setStatus("connected");
    await this.loadUiLanguage();
  }

  /**
   * Draw the harness's own wording in the user's language.
   *
   * `agent.replyLanguage` is the authority when it names one; on `auto` the
   * language is not known until the user writes something, and
   * :meth:`prompt` fills it in from what they typed. A daemon that cannot
   * answer `settings.get` leaves the chrome in English, which is what it was
   * before this existed.
   */
  private async loadUiLanguage(): Promise<void> {
    try {
      const settings = await this.call("settings.get", { scope: "global" });
      const configured = String(settings?.settings?.agent?.replyLanguage ?? "auto").trim();
      if (configured && configured.toLowerCase() !== "auto") {
        setUiLanguage(configured);
        this.languagePinned = true;
      } else {
        this.languagePinned = false;
      }
    } catch {
      // Not fatal: the wording stays as it is.
    }
  }

  /** Bind the interactive server→client approval prompt. */
  onApprovalRequest(handler: ApprovalHandler): void {
    this.require().onRequest("approval.request", handler);
  }

  /** Bind the `ask_user` picker; the daemon blocks on the answer. */
  onQuestionRequest(handler: QuestionHandler): void {
    this.require().onRequest("question.request", handler);
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
    const client = this.require();
    const untyped = client.call.bind(client) as (m: string, p?: unknown) => Promise<any>;
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

  /**
   * `session.prompt`, with whatever the input had attached.
   *
   * Files travel as paths: the daemon runs on this machine and reading the
   * bytes twice, once here to base64 them and once there, would be work for
   * nothing.
   */
  prompt(
    sessionId: string,
    text: string,
    attachments: PromptAttachment[] = [],
  ): Promise<{ turnId: string }> {
    // On `auto` the user's own words are the only signal there is, and the
    // daemon derives the delegation language the same way (util/lang.py), so
    // the chrome and the briefs agree.
    if (!this.languagePinned && text.trim().length > 0) setUiLanguage(detectLanguage(text));
    return this.call("session.prompt", {
      sessionId,
      text,
      ...(attachments.length > 0 ? { attachments } : {}),
    });
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

  respondQuestion(
    requestId: string,
    answers: { selected: string[]; text: string | null }[],
  ): Promise<unknown> {
    return this.call("question.respond", { requestId, answers });
  }

  /** `system.checkUpdate`; the daemon caches the answer for 24h. */
  checkUpdate(force = false): Promise<UpdateCheck> {
    return this.call("system.checkUpdate", { force });
  }

  /** `system.update` — starts the upgrade; progress arrives as notifications. */
  startUpdate(): Promise<UpdateStart> {
    return this.call("system.update", {});
  }

  /** `system.restart` — the daemon exits so the next launch runs new code. */
  restartDaemon(): Promise<unknown> {
    return this.call("system.restart", {});
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
