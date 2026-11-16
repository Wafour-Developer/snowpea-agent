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
import { sanitizeDeep } from "../util/sanitize.js";
import {
  connect as sdkConnect,
  type ApprovalRequestParams,
  type ApprovalResponse,
  type ApprovalResolved,
  type ApprovalRouteChanged,
  type QuestionRouteChanged,
  type ConnectFn,
  type ExternalClisChanged,
  type ExternalEvent,
  type ExternalState,
  type JarvisAccessEvent,
  type JarvisDecisionEvent,
  type JarvisDelegationEvent,
  type JarvisEscalationDismissed,
  type JarvisEscalationEvent,
  type JarvisStateEvent,
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

export interface FileCompleteParams {
  limit?: number | null;
  query?: string;
  sessionId?: string | null;
  workdir?: string | null;
}

export interface FileCompleteEntry {
  kind: "file" | "dir";
  path: string;
  size?: number | null;
}

export interface FileCompleteResult {
  entries?: FileCompleteEntry[];
  truncated?: boolean;
}

/** Answers the server→client `question.request` the `ask_user` tool raises. */
export type QuestionHandler = (
  request: QuestionRequestParams,
) => Promise<QuestionResponse>;

export interface TuiClientOptions {
  port: number;
  token: string;
  clientVersion: string;
  /**
   * True when the daemon is on another machine, reached through a tunnel on
   * `port`.  A reconnect then retries that same port and token: the SDK's
   * default re-reads the *local* daemon.json, which would quietly move the
   * session to a different daemon.
   */
  remote?: boolean;
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
  onReconnecting?: (params: { attempt: number }) => void;
  onReconnected?: (params: { attempts: number; sessions: string[] }) => void;
  /** `decision` is null for a withdrawn request (`outcome: "withdrawn"`); `scope` is what core applied (1.54.0). */
  onApprovalResolved?: (params: ApprovalResolved) => void;
  /** An unattended request joined the shared queue; re-read `approval.list`. */
  onApprovalPending?: (params: { request: ApprovalRequestParams }) => void;
  /** A question was answered elsewhere, or gave up; stop showing it. */
  onQuestionResolved?: (params: { requestId: string; by?: string }) => void;
  /** A skill reload or plugin install changed the command table (M6 §1). */
  onCommandsChanged?: () => void;
  /** The upgrade started by `system.update` moved on (CORE-update). */
  onUpdateProgress?: (params: UpdateProgress) => void;
  /** A setting changed on the daemon; capabilities may have moved with it. */
  onSettingsChanged?: (params: { keys?: string[]; waitingKeys?: string[] }) => void;
  /** An MCP server was added, removed, or changed state (M14 §3). */
  onMcpChanged?: (params: McpChangedPayload) => void;
  /** A question joined the shared queue (1.53.0 shows it in the Jarvis panel). */
  onQuestionPending?: (params: unknown) => void;
  /** A waiting approval's route changed (person, jarvis, auto, or null when Jarvis mode went off). */
  onApprovalRouteChanged?: (params: ApprovalRouteChanged) => void;
  /** A waiting question's route changed. */
  onQuestionRouteChanged?: (params: QuestionRouteChanged) => void;
  /** Jarvis mode (1.53.0, capability `jarvis` / `jarvisDelegation`). */
  onJarvisState?: (params: JarvisStateEvent) => void;
  onJarvisDecision?: (params: JarvisDecisionEvent) => void;
  onJarvisEscalation?: (params: JarvisEscalationEvent) => void;
  onJarvisDelegation?: (params: JarvisDelegationEvent) => void;
  /** Jarvis read or instructed a session. */
  onJarvisAccess?: (params: JarvisAccessEvent) => void;
  /** Escalations dismissed on any surface. */
  onJarvisEscalationDismissed?: (params: JarvisEscalationDismissed) => void;
  /** A session's summary changed (carries `jarvis` with `levelScope`). */
  onSessionsChanged?: (params: { sessionId?: string; jarvis?: unknown }) => void;
  /** External CLI sessions (1.53.0, capability `externalSessions`). */
  onExternalEvent?: (params: ExternalEvent) => void;
  onExternalState?: (params: ExternalState) => void;
  /** The CLI list changed (models listed, a CLI installed or switched off). */
  onExternalClisChanged?: (params: ExternalClisChanged) => void;
  /** A command or path was refused by the security lists (capability `securityLists`). */
  onSecurityBlocked?: (params: unknown) => void;
  /** A project was trusted or its trust taken back (capability `projectTrust`). */
  onProjectTrustChanged?: (params: unknown) => void;
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
      ...(this.options.remote
        ? { resolveEndpoint: () => ({ port: this.options.port, token: this.options.token }) }
        : {}),
    });
    this.client = client;

    // Every notification passes the terminal-safety filter before any
    // listener sees it (security review M7): this class is the TUI's one
    // boundary for server-provided strings (notifications, call results and
    // server->client requests alike).
    const rawOn = client.on.bind(client) as (event: string, handler: (params: any) => void) => unknown;
    const untypedOn = (event: string, handler: (params: any) => void): unknown =>
      rawOn(event, (params: any) => handler(sanitizeDeep(params)));
    untypedOn("session.event", (event: SessionEvent) => this.handleSessionEvent(event));
    untypedOn("approval.resolved", (params: any) => this.listeners.onApprovalResolved?.(params));
    untypedOn("commands.changed", () => this.listeners.onCommandsChanged?.());
    untypedOn("approval.pending", (params: any) => this.listeners.onApprovalPending?.(params));
    untypedOn("question.resolved", (params: any) => this.listeners.onQuestionResolved?.(params));
    untypedOn("system.updateProgress", (params: any) => this.listeners.onUpdateProgress?.(params));
    untypedOn("mcp.changed", (params) => this.listeners.onMcpChanged?.(params));
    // The 1.53.0 notifications are subscribed through the untyped `on` so a
    // pre-1.53 SDK build or daemon never has to know them.
    untypedOn("question.pending", (params) => this.listeners.onQuestionPending?.(params));
    untypedOn("approval.route.changed", (params) => this.listeners.onApprovalRouteChanged?.(params));
    untypedOn("question.route.changed", (params) => this.listeners.onQuestionRouteChanged?.(params));
    untypedOn("jarvis.state", (params) => this.listeners.onJarvisState?.(params));
    untypedOn("jarvis.decision", (params) => this.listeners.onJarvisDecision?.(params));
    untypedOn("jarvis.escalation", (params) => this.listeners.onJarvisEscalation?.(params));
    untypedOn("jarvis.delegation.changed", (params) => this.listeners.onJarvisDelegation?.(params));
    untypedOn("external.event", (params) => this.listeners.onExternalEvent?.(params));
    untypedOn("external.state", (params) => this.listeners.onExternalState?.(params));
    untypedOn("jarvis.access", (params) => this.listeners.onJarvisAccess?.(params));
    untypedOn("jarvis.escalation.dismissed", (params) => this.listeners.onJarvisEscalationDismissed?.(params));
    untypedOn("external.clis.changed", (params) => this.listeners.onExternalClisChanged?.(params));
    untypedOn("security.blocked", (params) => this.listeners.onSecurityBlocked?.(params));
    untypedOn("project.trust.changed", (params) => this.listeners.onProjectTrustChanged?.(params));
    untypedOn("sessions.changed", (params) => this.listeners.onSessionsChanged?.(params));
    untypedOn("settings.changed", (params: any) => {
      void this.loadUiLanguage();
      this.listeners.onSettingsChanged?.(params ?? {});
    });
    // The SDK owns reconnect and replays each tracked session with
    // `session.resume(afterSeq)` before emitting `reconnected`; the TUI only
    // renders the transition.
    client.on("disconnected", (params) =>
      this.setStatus(params.willRetry ? "reconnecting" : "closed"),
    );
    client.on("reconnecting", (params: any) => {
      this.setStatus("reconnecting");
      this.listeners.onReconnecting?.(params);
    });
    client.on("reconnected", (params: any) => {
      this.setStatus("connected");
      this.listeners.onReconnected?.(params);
    });

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
    this.require().onRequest("approval.request", ((params: any) =>
      handler(sanitizeDeep(params))) as ApprovalHandler);
  }

  /** Bind the `ask_user` picker; the daemon blocks on the answer. */
  onQuestionRequest(handler: QuestionHandler): void {
    this.require().onRequest("question.request", ((params: any) =>
      handler(sanitizeDeep(params))) as QuestionHandler);
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
    return untyped(method, params).then((result) => sanitizeDeep(result));
  }

  async createSession(options: CreateSessionOptions): Promise<string> {
    return (await this.createSessionInfo(options)).sessionId;
  }

  /** `session.create`, with what the daemon said about the new session. */
  async createSessionInfo(
    options: CreateSessionOptions,
  ): Promise<{ sessionId: string; delegation: boolean }> {
    const result = await this.call("session.create", {
      workdir: options.workdir,
      ...(options.mode ? { mode: options.mode } : {}),
      originSurface: options.originSurface ?? "tui",
    });
    return { sessionId: result.sessionId as string, delegation: result.delegation === true };
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

  fileComplete(params: FileCompleteParams): Promise<FileCompleteResult> {
    return this.call("file.complete", params as Record<string, unknown>);
  }

  /** `autoAfterPlan` (1.32.0): with `"plan"`, plan with every approval answered yes. */
  setMode(
    sessionId: string,
    mode: Mode,
    extra: { autoAfterPlan?: boolean } = {},
  ): Promise<{ mode: Mode; autoAfterPlan?: boolean }> {
    return this.call("session.setMode", { ...extra, sessionId, mode });
  }

  listApprovals(sessionId?: string): Promise<{ requests: ApprovalRequestParams[] }> {
    return this.call("approval.list", sessionId ? { sessionId } : {});
  }

  respondApproval(
    requestId: string,
    decision: ApprovalResponse["decision"],
    scope: ApprovalResponse["scope"],
    reason?: string,
  ): Promise<unknown> {
    return this.call("approval.respond", { requestId, decision, scope, reason });
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
