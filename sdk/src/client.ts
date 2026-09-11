/**
 * JSON-RPC 2.0 client for the snowpea daemon over a single WebSocket.
 *
 * The daemon owns all state; this client is a thin, typed transport with
 * automatic reconnect. After a drop it re-runs `system.hello` and replays each
 * tracked session with `session.resume(sessionId, afterSeq)`, so a consumer
 * that only listens to `session.event` never sees a gap.
 */

import WebSocket from "ws";

import {
  PROTOCOL_VERSION,
  WS_PATH,
  type ErrorCode,
  type EventMap,
  type MethodMap,
  type MethodName,
  type ServerMethod,
} from "./protocol.js";

/** Lifecycle notifications the client raises on top of the protocol events. */
export interface ClientLifecycleEvents {
  /** The socket reconnected and every tracked session was resumed. */
  reconnected: { attempt: number; resumedSessions: string[] };
  /** The socket dropped; a reconnect is scheduled unless `reconnect` is off. */
  disconnected: { code: number; reason: string; willRetry: boolean };
  /** A transport or handler error that did not reject a specific call. */
  error: { error: Error };
}

export interface ClientEvents extends EventMap, ClientLifecycleEvents {}

export type ClientEventName = keyof ClientEvents;
export type EventListener<E extends ClientEventName> = (payload: ClientEvents[E]) => void;

export interface ConnectOptions {
  /** Daemon port, from `$SNOWPEA_HOME/daemon.json`. */
  port: number;
  /** Daemon token, from `$SNOWPEA_HOME/token` or `daemon.json`. */
  token: string;
  /** Version string this client reports in `system.hello`. */
  clientVersion: string;
  /** Loopback by default; the daemon does not listen elsewhere. */
  host?: string;
  /** WebSocket path; defaults to the generated `WS_PATH`. */
  path?: string;
  /** Protocol version to negotiate; defaults to the generated constant. */
  protocolVersion?: string;
  /** Reconnect automatically after an unexpected close. Default `true`. */
  reconnect?: boolean;
  /** First reconnect delay in ms. Default `200`. */
  reconnectInitialDelayMs?: number;
  /** Reconnect delay ceiling in ms. Default `10_000`. */
  reconnectMaxDelayMs?: number;
  /** Give up after this many consecutive failures. Default `Infinity`. */
  reconnectMaxAttempts?: number;
  /** Per-call timeout in ms. Default `30_000`; `0` disables it. */
  callTimeoutMs?: number;
}

/** A JSON-RPC error response. `code` is the protocol's string code. */
export class RpcError extends Error {
  readonly rpcCode: number;
  readonly code: ErrorCode | string | undefined;
  readonly data: unknown;

  constructor(rpcCode: number, message: string, data?: unknown) {
    super(message);
    this.name = "RpcError";
    this.rpcCode = rpcCode;
    this.data = data;
    const bag = data as { code?: string } | undefined;
    this.code = bag && typeof bag.code === "string" ? bag.code : undefined;
  }

  /** True when the daemon reported the given protocol error code. */
  is(code: ErrorCode | string): boolean {
    return this.code === code;
  }
}

/** The socket closed while calls were in flight, or before one could be sent. */
export class ConnectionClosedError extends Error {
  constructor(message = "connection closed") {
    super(message);
    this.name = "ConnectionClosedError";
  }
}

/** The daemon speaks an incompatible protocol major version. */
export class ProtocolIncompatibleError extends Error {
  readonly serverProtocolVersion: string;
  readonly clientProtocolVersion: string;

  constructor(serverProtocolVersion: string, clientProtocolVersion: string) {
    super(
      `daemon protocol ${serverProtocolVersion} is incompatible with client protocol ${clientProtocolVersion}`,
    );
    this.name = "ProtocolIncompatibleError";
    this.serverProtocolVersion = serverProtocolVersion;
    this.clientProtocolVersion = clientProtocolVersion;
  }
}

type RequestHandler = (params: unknown) => unknown | Promise<unknown>;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timer?: ReturnType<typeof setTimeout>;
}

interface JsonRpcMessage {
  jsonrpc?: string;
  id?: number | string | null;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code?: number; message?: string; data?: unknown };
}

const HELLO_TIMEOUT_MS = 15_000;

function majorOf(version: string): string {
  return String(version).split(".")[0] ?? "";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class Client {
  readonly options: Required<Omit<ConnectOptions, "host" | "path" | "protocolVersion">> &
    Pick<ConnectOptions, "host" | "path" | "protocolVersion">;

  /** Filled in by the `system.hello` result. */
  serverVersion = "";
  serverProtocolVersion = "";
  capabilities: string[] = [];

  private ws: WebSocket | undefined;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly listeners = new Map<string, Set<(payload: never) => void>>();
  private readonly requestHandlers = new Map<string, RequestHandler>();
  private readonly lastSeq = new Map<string, number>();
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private attempt = 0;

  constructor(options: ConnectOptions) {
    this.options = {
      port: options.port,
      token: options.token,
      clientVersion: options.clientVersion,
      host: options.host,
      path: options.path,
      protocolVersion: options.protocolVersion,
      reconnect: options.reconnect ?? true,
      reconnectInitialDelayMs: options.reconnectInitialDelayMs ?? 200,
      reconnectMaxDelayMs: options.reconnectMaxDelayMs ?? 10_000,
      reconnectMaxAttempts: options.reconnectMaxAttempts ?? Number.POSITIVE_INFINITY,
      callTimeoutMs: options.callTimeoutMs ?? 30_000,
    };
  }

  get url(): string {
    const host = this.options.host ?? "127.0.0.1";
    const path = this.options.path ?? WS_PATH;
    return `ws://${host}:${this.options.port}${path}`;
  }

  /** True while the socket is open and the handshake has completed. */
  get connected(): boolean {
    return this.ws !== undefined && this.ws.readyState === WebSocket.OPEN;
  }

  /** Session ids this client resumes after a reconnect. */
  get trackedSessions(): string[] {
    return [...this.lastSeq.keys()];
  }

  /** Follow a session's `seq` so a reconnect can replay what was missed. */
  trackSession(sessionId: string, afterSeq = 0): void {
    const known = this.lastSeq.get(sessionId);
    if (known === undefined || afterSeq > known) this.lastSeq.set(sessionId, afterSeq);
  }

  /** Stop resuming a session (called automatically by `session.close`). */
  untrackSession(sessionId: string): void {
    this.lastSeq.delete(sessionId);
  }

  /** Highest `seq` seen for a session. */
  sessionSeq(sessionId: string): number {
    return this.lastSeq.get(sessionId) ?? 0;
  }

  // -- events ---------------------------------------------------------------

  on<E extends ClientEventName>(event: E, cb: EventListener<E>): () => void {
    let set = this.listeners.get(event as string);
    if (!set) {
      set = new Set();
      this.listeners.set(event as string, set);
    }
    set.add(cb as (payload: never) => void);
    return () => this.off(event, cb);
  }

  off<E extends ClientEventName>(event: E, cb?: EventListener<E>): void {
    const set = this.listeners.get(event as string);
    if (!set) return;
    if (cb) set.delete(cb as (payload: never) => void);
    else set.clear();
  }

  once<E extends ClientEventName>(event: E, cb: EventListener<E>): () => void {
    const dispose = this.on(event, ((payload: ClientEvents[E]) => {
      dispose();
      cb(payload);
    }) as EventListener<E>);
    return dispose;
  }

  /** Handle a server→client request such as `approval.request`. */
  onRequest<M extends Extract<ServerMethod, MethodName>>(
    method: M,
    handler: (params: MethodMap[M]["params"]) => MethodMap[M]["result"] | Promise<MethodMap[M]["result"]>,
  ): () => void {
    this.requestHandlers.set(method as string, handler as RequestHandler);
    return () => {
      if (this.requestHandlers.get(method as string) === (handler as RequestHandler)) {
        this.requestHandlers.delete(method as string);
      }
    };
  }

  offRequest(method: string): void {
    this.requestHandlers.delete(method);
  }

  private emit<E extends ClientEventName>(event: E, payload: ClientEvents[E]): void {
    const set = this.listeners.get(event as string);
    if (!set) return;
    for (const cb of [...set]) {
      try {
        (cb as EventListener<E>)(payload);
      } catch (err) {
        if (event !== "error") {
          this.emit("error", { error: err instanceof Error ? err : new Error(String(err)) });
        }
      }
    }
  }

  // -- calls ----------------------------------------------------------------

  /** Invoke a JSON-RPC method and resolve with its typed result. */
  async call<M extends MethodName>(
    method: M,
    params: MethodMap[M]["params"],
    opts: { timeoutMs?: number } = {},
  ): Promise<MethodMap[M]["result"]> {
    const result = (await this.rawCall(
      method as string,
      params as unknown,
      opts.timeoutMs ?? this.options.callTimeoutMs,
    )) as MethodMap[M]["result"];
    this.noteCall(method as string, params as unknown, result as unknown);
    return result;
  }

  /** Escape hatch for methods that are not in the generated map yet. */
  rawCall(method: string, params: unknown, timeoutMs = this.options.callTimeoutMs): Promise<unknown> {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new ConnectionClosedError(`cannot call ${method}: socket is not open`));
    }
    const id = this.nextId++;
    return new Promise<unknown>((resolve, reject) => {
      const entry: Pending = { resolve, reject };
      if (timeoutMs > 0) {
        entry.timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error(`${method} timed out after ${timeoutMs}ms`));
        }, timeoutMs);
      }
      this.pending.set(id, entry);
      ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: params ?? {} }), (err) => {
        if (!err) return;
        this.settle(id, undefined, err instanceof Error ? err : new Error(String(err)));
      });
    });
  }

  /** Send a JSON-RPC notification (no response expected). */
  notify(method: string, params: unknown = {}): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      throw new ConnectionClosedError(`cannot notify ${method}: socket is not open`);
    }
    ws.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  private noteCall(method: string, params: unknown, result: unknown): void {
    const p = params as { sessionId?: string; afterSeq?: number } | undefined;
    const r = result as { sessionId?: string } | undefined;
    if (method === "session.create" && r?.sessionId) this.trackSession(r.sessionId, 0);
    else if (method === "session.resume" && r?.sessionId) this.trackSession(r.sessionId, p?.afterSeq ?? 0);
    else if (method === "session.close" && p?.sessionId) this.untrackSession(p.sessionId);
  }

  private settle(id: number, result: unknown, error?: Error): void {
    const entry = this.pending.get(id);
    if (!entry) return;
    this.pending.delete(id);
    if (entry.timer) clearTimeout(entry.timer);
    if (error) entry.reject(error);
    else entry.resolve(result);
  }

  // -- connection -----------------------------------------------------------

  /** Open the socket and complete `system.hello`. */
  async open(): Promise<void> {
    this.closed = false;
    await this.openSocket();
    await this.hello();
  }

  private openSocket(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(this.url);
      this.ws = ws;
      let settled = false;
      let everOpen = false;

      ws.on("open", () => {
        everOpen = true;
        settled = true;
        resolve();
      });
      ws.on("message", (raw: WebSocket.RawData) => this.onMessage(String(raw)));
      ws.on("error", (err: Error) => {
        if (!settled) {
          settled = true;
          reject(err);
          return;
        }
        this.emit("error", { error: err });
      });
      ws.on("close", (code: number, reason: Buffer) => {
        const wasOpen = this.ws === ws;
        if (wasOpen) this.ws = undefined;
        this.failPending(new ConnectionClosedError(`socket closed (${code})`));
        if (!settled) {
          settled = true;
          reject(new ConnectionClosedError(`socket closed before open (${code})`));
          return;
        }
        // A socket that never opened (connection refused, wrong port) must not
        // start the reconnect loop: `connect()` rejected, so the caller holds no
        // Client to close and the loop would keep the event loop alive forever.
        const willRetry = everOpen && !this.closed && this.options.reconnect;
        this.emit("disconnected", { code, reason: reason?.toString() ?? "", willRetry });
        if (willRetry) void this.scheduleReconnect();
      });
    });
  }

  private async hello(): Promise<void> {
    const result = (await this.rawCall(
      "system.hello",
      {
        token: this.options.token,
        clientVersion: this.options.clientVersion,
        protocolVersion: this.options.protocolVersion ?? PROTOCOL_VERSION,
      },
      HELLO_TIMEOUT_MS,
    )) as { protocolVersion?: string; serverVersion?: string; capabilities?: string[] };

    this.serverProtocolVersion = result.protocolVersion ?? "";
    this.serverVersion = result.serverVersion ?? "";
    this.capabilities = result.capabilities ?? [];

    const mine = this.options.protocolVersion ?? PROTOCOL_VERSION;
    if (this.serverProtocolVersion && majorOf(this.serverProtocolVersion) !== majorOf(mine)) {
      throw new ProtocolIncompatibleError(this.serverProtocolVersion, mine);
    }
  }

  private failPending(error: Error): void {
    for (const id of [...this.pending.keys()]) this.settle(id, undefined, error);
  }

  private async scheduleReconnect(): Promise<void> {
    if (this.reconnectTimer) return;
    while (!this.closed && this.options.reconnect) {
      this.attempt += 1;
      if (this.attempt > this.options.reconnectMaxAttempts) {
        this.emit("error", { error: new ConnectionClosedError("reconnect attempts exhausted") });
        return;
      }
      const base = Math.min(
        this.options.reconnectMaxDelayMs,
        this.options.reconnectInitialDelayMs * 2 ** (this.attempt - 1),
      );
      const delay = Math.round(base * (0.5 + Math.random() / 2));
      await sleep(delay);
      if (this.closed) return;
      try {
        await this.openSocket();
        await this.hello();
        const resumed = await this.resumeTracked();
        const attempt = this.attempt;
        this.attempt = 0;
        this.emit("reconnected", { attempt, resumedSessions: resumed });
        return;
      } catch (err) {
        this.emit("error", { error: err instanceof Error ? err : new Error(String(err)) });
      }
    }
  }

  private async resumeTracked(): Promise<string[]> {
    const resumed: string[] = [];
    for (const sessionId of [...this.lastSeq.keys()]) {
      try {
        const result = (await this.rawCall("session.resume", {
          sessionId,
          afterSeq: this.lastSeq.get(sessionId) ?? 0,
        })) as { events?: EventMap["session.event"][] };
        for (const event of result.events ?? []) this.dispatchSessionEvent(event);
        resumed.push(sessionId);
      } catch (err) {
        this.emit("error", { error: err instanceof Error ? err : new Error(String(err)) });
      }
    }
    return resumed;
  }

  private dispatchSessionEvent(event: EventMap["session.event"]): void {
    const sessionId = (event as { sessionId?: string }).sessionId;
    const seq = (event as { seq?: number }).seq;
    if (sessionId && typeof seq === "number" && seq > (this.lastSeq.get(sessionId) ?? 0)) {
      this.lastSeq.set(sessionId, seq);
    }
    this.emit("session.event", event);
  }

  // -- inbound --------------------------------------------------------------

  private onMessage(raw: string): void {
    let msg: JsonRpcMessage;
    try {
      msg = JSON.parse(raw) as JsonRpcMessage;
    } catch (err) {
      this.emit("error", { error: new Error(`malformed frame: ${String(err)}`) });
      return;
    }

    if (msg.method !== undefined && (msg.id === undefined || msg.id === null)) {
      if (msg.method === "session.event") {
        this.dispatchSessionEvent(msg.params as EventMap["session.event"]);
      } else {
        this.emit(msg.method as ClientEventName, msg.params as never);
      }
      return;
    }

    if (msg.method !== undefined) {
      void this.handleServerRequest(msg);
      return;
    }

    if (typeof msg.id === "number") {
      if (msg.error) {
        this.settle(
          msg.id,
          undefined,
          new RpcError(msg.error.code ?? -32000, msg.error.message ?? "rpc error", msg.error.data),
        );
      } else {
        this.settle(msg.id, msg.result);
      }
    }
  }

  private async handleServerRequest(msg: JsonRpcMessage): Promise<void> {
    const ws = this.ws;
    const id = msg.id as number | string;
    const handler = this.requestHandlers.get(msg.method as string);
    const reply = (body: Record<string, unknown>): void => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ jsonrpc: "2.0", id, ...body }));
      }
    };
    if (!handler) {
      reply({
        error: {
          code: -32601,
          message: `no handler registered for ${msg.method}`,
          data: { code: "not_implemented" satisfies ErrorCode },
        },
      });
      return;
    }
    try {
      const result = await handler(msg.params);
      reply({ result: result ?? {} });
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      this.emit("error", { error });
      reply({ error: { code: -32000, message: error.message, data: { code: "internal" satisfies ErrorCode } } });
    }
  }

  // -- teardown -------------------------------------------------------------

  /** Close the socket and stop reconnecting. */
  async close(code = 1000, reason = "client close"): Promise<void> {
    this.closed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    const ws = this.ws;
    this.ws = undefined;
    this.failPending(new ConnectionClosedError("client closed"));
    if (!ws) return;
    await new Promise<void>((resolve) => {
      if (ws.readyState === WebSocket.CLOSED) {
        resolve();
        return;
      }
      ws.once("close", () => resolve());
      try {
        ws.close(code, reason);
      } catch {
        resolve();
      }
      setTimeout(resolve, 2000);
    });
  }
}

/** Connect to a running daemon and complete the handshake. */
export async function connect(options: ConnectOptions): Promise<Client> {
  const client = new Client(options);
  try {
    await client.open();
  } catch (err) {
    // Tear down before rethrowing: the caller never receives this Client, so it
    // could not close a half-open socket or a pending reconnect itself.
    await client.close().catch(() => undefined);
    throw err;
  }
  return client;
}

export default connect;
