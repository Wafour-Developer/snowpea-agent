/**
 * Subagent, persistent-agent, and team helpers.
 *
 * The schema is frozen at M1 but the daemon answers most of these with
 * `error{code:"not_implemented"}` until M7; catch `RpcError` and check
 * `err.is("not_implemented")` if you need to degrade gracefully.
 */

import type { Client } from "./client.js";
import type { MethodMap } from "./protocol.js";

type P<M extends keyof MethodMap> = MethodMap[M]["params"];
type R<M extends keyof MethodMap> = MethodMap[M]["result"];

/** Persistent named agents. */
export function listAgents(client: Client): Promise<R<"agent.list">> {
  return client.call("agent.list", {} as P<"agent.list">);
}

/** Create a persistent agent from a natural-language description. */
export function createAgent(client: Client, description: string): Promise<R<"agent.create">> {
  return client.call("agent.create", { description } as P<"agent.create">);
}

/**
 * Spawn a one-shot subagent; progress arrives as `subagent.*` session events.
 *
 * Pass `sessionId` to say which session the child belongs to — its
 * `subagent.spawn`, `subagent.update` and `subagent.done` events are published
 * there. Without it the daemon uses the session this connection opened.
 * The call answers with the `agentId` as soon as the child is queued; the run
 * itself continues in the background.
 */
export function spawnAgent(
  client: Client,
  name: string,
  task: string,
  sessionId?: string,
): Promise<R<"agent.spawn">> {
  return client.call("agent.spawn", { name, task, sessionId } as P<"agent.spawn">);
}

/** Bind a persistent agent to a messenger channel. */
export function bindAgentChannel(client: Client, name: string, channel: string): Promise<R<"agent.bindChannel">> {
  return client.call("agent.bindChannel", { name, channel } as P<"agent.bindChannel">);
}

/** Delete a persistent agent. */
export function deleteAgent(client: Client, name: string): Promise<R<"agent.delete">> {
  return client.call("agent.delete", { name } as P<"agent.delete">);
}

/** Start a team run of `n` workers on a shared task list. */
export function startTeam(client: Client, sessionId: string, n: number, task: string): Promise<R<"team.start">> {
  return client.call("team.start", { sessionId, n, task } as P<"team.start">);
}

/** Current state of a team run. */
export function teamStatus(client: Client, teamId: string): Promise<R<"team.status">> {
  return client.call("team.status", { teamId } as P<"team.status">);
}
