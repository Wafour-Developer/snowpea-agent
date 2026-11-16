/**
 * `/web`: this surface's own command for remote web access (capability
 * `webRemoteControl`). Pure.
 */

import type { CommandInfo } from "../rpc/sdk.js";

export type WebCommand =
  /** Open the Remote access screen. */
  | { kind: "enter" }
  /** Print the one-screen summary into the transcript. */
  | { kind: "status" }
  | { kind: "invalid"; usage: string };

export const WEB_USAGE = "/web [status]";

export const WEB_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "status", summary: "Print whether remote access is on, the addresses and who is signed in." },
];

/** `null` when the line is not a `/web` command. */
export function parseWebCommand(line: string): WebCommand | null {
  const match = /^\/web(?:\s+([\s\S]*))?$/.exec(line.trim());
  if (!match) return null;
  const rest = (match[1] ?? "").trim();
  if (rest === "") return { kind: "enter" };
  return rest === "status" ? { kind: "status" } : { kind: "invalid", usage: WEB_USAGE };
}

export function webSubCommands(draft: string): CommandInfo[] {
  const match = /^\/web(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  if (typed === "" && /\s$/.test(draft)) return [];
  return WEB_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `web ${entry.action}`,
    summary: entry.summary,
    source: "tui",
  }));
}
