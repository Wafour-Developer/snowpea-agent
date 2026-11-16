/**
 * `/security`: this surface's own command for the blocked-command and
 * protected-folder lists (capability `securityLists`). Pure.
 */

import type { CommandInfo } from "../rpc/sdk.js";

export type SecurityCommand =
  /** Open the two-tab screen. */
  | { kind: "enter" }
  | { kind: "status" }
  /** Judge a command (`check`) or a path (`check-path`); the text is never run. */
  | { kind: "check"; what: "command" | "path"; text: string }
  | { kind: "invalid"; usage: string };

export const SECURITY_USAGE = "/security [status | check <command> | check-path <path>]";

export const SECURITY_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "status", summary: "Counts and which presets are on." },
  { action: "check", summary: "/security check <command> — is it blocked? (nothing is run)" },
  { action: "check-path", summary: "/security check-path <path> — is it protected? (nothing is run)" },
];

/** `null` when the line is not a `/security` command. */
export function parseSecurityCommand(line: string): SecurityCommand | null {
  const match = /^\/security(?:\s+([\s\S]*))?$/.exec(line.trim());
  if (!match) return null;
  const rest = (match[1] ?? "").trim();
  if (rest === "") return { kind: "enter" };
  const [action, ...words] = rest.split(/\s+/);
  const argument = rest.slice(action.length).trim();
  switch (action) {
    case "status":
      return words.length === 0 ? { kind: "status" } : { kind: "invalid", usage: SECURITY_USAGE };
    case "check":
      return argument !== "" ? { kind: "check", what: "command", text: argument } : { kind: "invalid", usage: SECURITY_USAGE };
    case "check-path":
      return argument !== "" ? { kind: "check", what: "path", text: argument } : { kind: "invalid", usage: SECURITY_USAGE };
    default:
      return { kind: "invalid", usage: SECURITY_USAGE };
  }
}

export function securitySubCommands(draft: string): CommandInfo[] {
  const match = /^\/security(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  if (typed === "" && /\s$/.test(draft)) return [];
  return SECURITY_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `security ${entry.action}`,
    summary: entry.summary,
    source: "tui",
  }));
}
