/**
 * `/trust`: this surface's own command for project trust (capability
 * `projectTrust`). Pure.
 */

import type { CommandInfo } from "../rpc/sdk.js";

export type TrustCommand =
  /** Open the screen for the current project. */
  | { kind: "enter" }
  /** One line: whether the project is trusted and what waits. */
  | { kind: "status" }
  | { kind: "invalid"; usage: string };

export const TRUST_USAGE = "/trust [status]";

export const TRUST_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "status", summary: "One line: is this project trusted, and how many files wait for you." },
];

/** `null` when the line is not a `/trust` command. */
export function parseTrustCommand(line: string): TrustCommand | null {
  const match = /^\/trust(?:\s+([\s\S]*))?$/.exec(line.trim());
  if (!match) return null;
  const rest = (match[1] ?? "").trim();
  if (rest === "") return { kind: "enter" };
  return rest === "status" ? { kind: "status" } : { kind: "invalid", usage: TRUST_USAGE };
}

export function trustSubCommands(draft: string): CommandInfo[] {
  const match = /^\/trust(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  if (typed === "" && /\s$/.test(draft)) return [];
  return TRUST_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `trust ${entry.action}`,
    summary: entry.summary,
    source: "tui",
  }));
}
