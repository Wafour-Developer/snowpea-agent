/**
 * `/settings disk`: this surface's own command for settings that were edited in
 * `settings.json` while the daemon ran and wait for the person (1.55.0). Pure.
 */

import type { CommandInfo } from "../rpc/sdk.js";

export type SettingsCommand =
  /** Open the choice (or say that nothing waits). */
  | { kind: "disk" }
  | { kind: "invalid"; usage: string };

export const SETTINGS_USAGE = "/settings disk";

export const SETTINGS_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "disk", summary: "Settings.json was edited outside snowpea: apply what the file says, or keep the current settings." },
];

/** `null` when the line is not a `/settings` command. */
export function parseSettingsCommand(line: string): SettingsCommand | null {
  const match = /^\/settings(?:\s+([\s\S]*))?$/.exec(line.trim());
  if (!match) return null;
  const rest = (match[1] ?? "").trim();
  return rest === "" || rest === "disk" ? { kind: "disk" } : { kind: "invalid", usage: SETTINGS_USAGE };
}

export function settingsSubCommands(draft: string): CommandInfo[] {
  const match = /^\/settings(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  if (typed === "" && /\s$/.test(draft)) return [];
  return SETTINGS_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `settings ${entry.action}`,
    summary: entry.summary,
    source: "tui",
  }));
}
