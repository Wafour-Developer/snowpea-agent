/** Session helpers — thin, typed wrappers over `client.call`. */

import type { Client } from "./client.js";
import type { MethodMap } from "./protocol.js";

type P<M extends keyof MethodMap> = MethodMap[M]["params"];
type R<M extends keyof MethodMap> = MethodMap[M]["result"];

/** Open a session rooted at `workdir`. The client starts tracking its `seq`. */
export function createSession(client: Client, params: P<"session.create">): Promise<R<"session.create">> {
  return client.call("session.create", params);
}

/** List the daemon's open sessions. */
export function listSessions(client: Client): Promise<R<"session.list">> {
  return client.call("session.list", {} as P<"session.list">);
}

/** Close a session and stop resuming it after reconnects. */
export function closeSession(client: Client, sessionId: string): Promise<R<"session.close">> {
  return client.call("session.close", { sessionId } as P<"session.close">);
}

/** Start a turn from user text. Returns the turn id; output arrives as events. */
export function prompt(
  client: Client,
  sessionId: string,
  text: string,
  extra: Omit<P<"session.prompt">, "sessionId" | "text"> = {} as Omit<P<"session.prompt">, "sessionId" | "text">,
): Promise<R<"session.prompt">> {
  return client.call("session.prompt", { ...extra, sessionId, text } as P<"session.prompt">);
}

/** Interrupt the running turn; the session ends it with `turn.done{interrupted}`. */
export function interrupt(client: Client, sessionId: string): Promise<R<"session.interrupt">> {
  return client.call("session.interrupt", { sessionId } as P<"session.interrupt">);
}

/** Switch the permission mode of a session. */
export function setMode(
  client: Client,
  sessionId: string,
  mode: P<"session.setMode">["mode"],
): Promise<R<"session.setMode">> {
  return client.call("session.setMode", { sessionId, mode } as P<"session.setMode">);
}

/**
 * Replay events after `afterSeq`. Defaults to the highest `seq` this client has
 * seen for the session, which is what a manual gap-fill usually wants.
 */
export function resume(client: Client, sessionId: string, afterSeq?: number): Promise<R<"session.resume">> {
  const after = afterSeq ?? client.sessionSeq(sessionId);
  return client.call("session.resume", { sessionId, afterSeq: after } as P<"session.resume">);
}
