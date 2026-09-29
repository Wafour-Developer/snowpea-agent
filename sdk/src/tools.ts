/** Tool and slash-command helpers. */

import type { Client } from "./client.js";
import type { MethodMap } from "./protocol.js";

type P<M extends keyof MethodMap> = MethodMap[M]["params"];
type R<M extends keyof MethodMap> = MethodMap[M]["result"];

/** Tools visible to a session, with permission tag and active/inactive state. */
export function listTools(client: Client, sessionId?: string): Promise<R<"tool.list">> {
  return client.call("tool.list", (sessionId ? { sessionId } : {}) as P<"tool.list">);
}

/** Slash commands the daemon knows about (builtin, skill, or plugin). */
export function listCommands(client: Client, sessionId?: string): Promise<R<"command.list">> {
  return client.call("command.list", (sessionId ? { sessionId } : {}) as P<"command.list">);
}

/**
 * Run a slash command. This is the only execution path for them: clients must
 * not parse `/foo` themselves, they forward the text to the daemon registry.
 */
export function runCommand(
  client: Client,
  sessionId: string,
  name: string,
  args = "",
): Promise<R<"command.run">> {
  return client.call("command.run", { sessionId, name, args } as P<"command.run">);
}

/** Choose the execution backend (local, docker, ssh) for a session. */
export function setBackend(
  client: Client,
  sessionId: string,
  kind: P<"backend.set">["kind"],
  config: P<"backend.set">["config"],
): Promise<R<"backend.set">> {
  return client.call("backend.set", { sessionId, kind, config } as P<"backend.set">);
}

/** One host tool: what `tool.register` takes (protocol 1.6.0). */
export type HostToolSpec = P<"tool.register">["tools"][number];

/** What a host tool handler receives: the daemon's `tool.invoke` request. */
export type HostToolRequest = P<"tool.invoke">;

/** What a host tool handler returns. */
export type HostToolResult = R<"tool.invoke">;

/** Report progress for a running `tool.invoke` (re-emitted as `tool.progress`). */
export type ProgressReporter = (message: string) => void;

/**
 * Handler for one `tool.invoke`. `signal` aborts when the daemon sends
 * `tool.cancel` for this call (the turn was interrupted); the daemon has
 * already moved on, so the handler only needs to stop its work.
 */
export type HostToolHandler = (
  request: HostToolRequest,
  progress: ProgressReporter,
  signal: AbortSignal,
) => HostToolResult | Promise<HostToolResult>;

/**
 * Register tools this client runs itself and answer the daemon's `tool.invoke`
 * calls with `handler`. Registration is repeated after every reconnect (a
 * connection's host tools are dropped when it closes). Returns a disposer that
 * unregisters the tools and stops answering.
 *
 * A handler that throws is reported to the model as a tool error.
 */
export async function registerTools(
  client: Client,
  tools: HostToolSpec[],
  handler: HostToolHandler,
): Promise<{ registered: string[]; dispose: () => Promise<void> }> {
  const names = new Set(tools.map((tool) => tool.name));
  const running = new Map<string, AbortController>();
  const offCancel = client.on("tool.cancel", (payload) => {
    running.get(payload.callId)?.abort();
  });
  const offRequest = client.onRequest("tool.invoke", async (request) => {
    if (!names.has(request.name)) {
      return { ok: false, output: "", error: `${request.name} is not registered here` } as HostToolResult;
    }
    const progress: ProgressReporter = (message) =>
      client.notify("tool.progress", { callId: request.callId, message });
    const controller = new AbortController();
    running.set(request.callId, controller);
    try {
      return await handler(request, progress, controller.signal);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return { ok: false, output: "", error: message } as HostToolResult;
    } finally {
      running.delete(request.callId);
    }
  });
  const register = () => client.call("tool.register", { tools } as P<"tool.register">);
  const offReconnect = client.on("reconnected", () => {
    void register().catch(() => undefined);
  });
  const result = await register();
  return {
    registered: result.registered ?? [],
    dispose: async () => {
      offReconnect();
      offCancel();
      offRequest();
      await client
        .call("tool.unregister", { names: [...names] } as P<"tool.unregister">)
        .catch(() => undefined);
    },
  };
}
