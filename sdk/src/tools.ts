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
