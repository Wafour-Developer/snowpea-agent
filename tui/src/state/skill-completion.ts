/**
 * Completing `/skill` and the form behind `/skill create`.
 *
 * `/skill` is really five commands wearing one name, and the daemon only tells
 * the surface about the name. The sub-actions are listed here so the palette can
 * offer them, and the pieces `/skill create` needs — a name, a description and a
 * scope — are assembled here too, so the form itself holds no syntax.
 *
 * Pure, so `test/skill-completion.test.ts` needs neither a daemon nor a
 * terminal.
 */

import type { CommandInfo } from "../rpc/sdk.js";

/** One sub-action of `/skill`, as the palette lists it. */
export interface SkillAction {
  action: string;
  summary: string;
}

/**
 * What `/skill` can do, in the order someone reaches for it.
 *
 * `create` and `learn` are the two ways to make one — from a description, or
 * from what this session just did — so they come first.
 */
export const SKILL_ACTIONS: readonly SkillAction[] = [
  { action: "create", summary: "Write a new skill from a name and a description." },
  { action: "learn", summary: "Turn what this session just did into a skill." },
  { action: "edit", summary: "Open a skill's SKILL.md in $EDITOR." },
  { action: "publish", summary: "Upload a skill directory to the registry." },
  { action: "sources", summary: "List the hubs the registry federates." },
  { action: "reload", summary: "Re-read skills from disk." },
];

/** Where a scope lands on disk, and what the flag for it is. */
export type SkillScope = "project" | "global";

/**
 * The sub-action rows for a draft, or an empty list when the draft is not
 * asking for one.
 *
 * The rows are `CommandInfo`, because they are offered by the same palette that
 * offers the daemon's commands: `/skill create` is as much a thing you can run
 * as `/help` is.
 */
export function skillSubCommands(draft: string): CommandInfo[] {
  const match = /^\/skill(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  return SKILL_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `skill ${entry.action}`,
    summary: entry.summary,
    source: "core",
  }));
}

/** True for `/skill create` with nothing after it: the form's cue. */
export function isBareSkillCreate(text: string): boolean {
  return /^\/skill\s+create\s*$/.test(text.trim());
}

/** `/skill edit <name>` → the name, or null. */
export function parseSkillEdit(text: string): string | null {
  const match = /^\/skill\s+edit\s+(\S+)\s*$/.exec(text.trim());
  return match ? match[1] : null;
}

/** What the form collected, as the command line the daemon parses. */
export function skillCreateCommand({
  name,
  description,
  scope,
}: {
  name: string;
  description: string;
  scope: SkillScope;
}): string {
  // The description is quoted, so a sentence stays one argument; a quote inside
  // it would end the argument early, so it is dropped rather than escaped.
  const quoted = description.trim().replace(/"/g, "");
  const parts = [`/skill create ${name.trim()}`, `"${quoted}"`];
  if (scope === "global") parts.push("--global");
  return parts.join(" ");
}

/**
 * The skill named by a "Created skill …" / "Learned skill …" reply, or null.
 *
 * The daemon closes both with "Run it with /<name>", which is the part worth
 * trusting: it is the command the user will actually type.
 */
export function createdSkillName(text: string): string | null {
  const runIt = /Run it with \/([A-Za-z0-9][\w.-]*)/.exec(text);
  const named = /(?:Created|Learned) skill ['"`]?([A-Za-z0-9][\w.-]*)/.exec(text);
  const found = runIt?.[1] ?? named?.[1];
  if (!found) return null;
  // The sentence's full stop is not part of the name.
  const trimmed = found.replace(/[.\-]+$/, "");
  return trimmed.length > 0 ? trimmed : null;
}

/** A skill name the daemon will accept: a command name, in other words. */
export function isValidSkillName(name: string): boolean {
  return /^[A-Za-z0-9][\w.-]*$/.test(name.trim());
}
