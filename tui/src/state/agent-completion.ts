/**
 * Completing an agent's name while it is being typed.
 *
 * Two places want the same list: `/delegate <agent> <task>`, where the name is
 * the command's first argument, and the `$name ` prefix, which is the short way
 * to say the same thing. Both are answered from what the daemon actually has —
 * the agent definitions, the named agents, and whoever is working on a team
 * right now — so a name that completes is a name that exists.
 *
 * Pure, so `test/agent-completion.test.ts` can check the parsing and the
 * filtering without a daemon or a terminal.
 */

import type { KnownAgent } from "../layout/agents.js";
import type { TeamTaskEntry } from "./store.js";

/** One row of the completion list. */
export interface AgentCandidate {
  name: string;
  /** The definition's one-line summary, when it has one. */
  description: string;
  /** Model profile the settings assign to this agent, when one is assigned. */
  model: string | null;
  /** Where it came from, which is what the row's tag says. */
  kind: "definition" | "named" | "subagent" | "team";
}

/** Commands whose first argument is an agent name. */
const COMMAND_PREFIXES: readonly string[] = ["/delegate ", "/agent spawn "];

/** The `$name` form, anchored at the start of the draft. */
const DOLLAR = /^\$([A-Za-z0-9][\w.-]*)?$/;

/**
 * Every agent worth offering, in the order a person would look for one.
 *
 * Definitions first — those are the roles someone wrote down — then anything
 * running, then the team. A name is offered once however many places know it.
 */
export function agentCandidates({
  known = [],
  teamTasks = [],
  models = {},
}: {
  known?: readonly KnownAgent[];
  teamTasks?: readonly TeamTaskEntry[];
  /** `agents.models` from the settings: agent name → model profile. */
  models?: Record<string, string>;
}): AgentCandidate[] {
  const byName = new Map<string, AgentCandidate>();

  for (const agent of known) {
    if (!agent.name || byName.has(agent.name)) continue;
    const kind = agent.kind === "named" || agent.kind === "subagent" ? agent.kind : "definition";
    byName.set(agent.name, {
      name: agent.name,
      description: agent.description ?? "",
      model: models[agent.name] ?? null,
      kind,
    });
  }

  for (const task of teamTasks) {
    const name = task.assignee;
    if (!name || byName.has(name)) continue;
    byName.set(name, {
      name,
      description: `working on ${task.teamId || "the team"}`,
      model: models[name] ?? null,
      kind: "team",
    });
  }

  return [...byName.values()];
}

/** Where in the draft an agent name is being typed. */
export interface AgentQuery {
  /** What has been typed of the name so far. */
  prefix: string;
  /** Index the name starts at. */
  start: number;
  /** Index one past what has been typed. */
  end: number;
  /** `$name ` inserts the sigil back; a command argument does not. */
  form: "dollar" | "command";
}

/**
 * Read the agent name the cursor is in, or null.
 *
 * Only the first word after the trigger counts: once there is a space after the
 * name the user has moved on to the task, and a completion list over their
 * prose would be in the way.
 */
export function agentQuery(draft: string, cursor = draft.length): AgentQuery | null {
  const head = draft.slice(0, cursor);

  const dollar = DOLLAR.exec(head);
  if (dollar) {
    return { prefix: dollar[1] ?? "", start: 1, end: head.length, form: "dollar" };
  }

  for (const trigger of COMMAND_PREFIXES) {
    if (!head.startsWith(trigger)) continue;
    const rest = head.slice(trigger.length);
    if (/\s/.test(rest)) return null;
    return { prefix: rest, start: trigger.length, end: head.length, form: "command" };
  }
  return null;
}

/** Candidates whose name starts with the prefix, case-insensitively. */
export function filterAgents(
  candidates: readonly AgentCandidate[],
  prefix: string,
): AgentCandidate[] {
  if (prefix.length === 0) return [...candidates];
  const needle = prefix.toLowerCase();
  return candidates.filter((candidate) => candidate.name.toLowerCase().startsWith(needle));
}

/** The draft and cursor after accepting a name. */
export function applyAgentCompletion(
  draft: string,
  query: AgentQuery,
  name: string,
): { text: string; cursor: number } {
  const inserted = `${name} `;
  const text = draft.slice(0, query.start) + inserted + draft.slice(query.end);
  return { text, cursor: query.start + inserted.length };
}

/** `claude-haiku` / `team`, the tag drawn after a row's description. */
export function candidateTag(candidate: AgentCandidate): string {
  if (candidate.model) return candidate.model;
  return candidate.kind === "definition" ? "" : candidate.kind;
}
