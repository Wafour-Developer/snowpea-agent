/**
 * When the slash palette is open, and what Enter does with it.
 *
 * Surface handlers (`skillSubCommands`, `mcpSubCommands`, …) own multi-word
 * command names; the registry only completes the first token. This module
 * decides whether the palette should still be drawn for the current draft.
 */

import type { CommandInfo } from "../rpc/sdk.js";

/** A slash row that may carry a monospace preview pane. */
export type SlashCompletion = CommandInfo & { preview?: string };

export function slashCommandText(name: string): string {
  return `/${name}`;
}

/** True once Tab or Enter has accepted a row (`/name `). */
export function isSlashCompletionAccepted(draft: string, commandName: string): boolean {
  const full = slashCommandText(commandName);
  const trimmed = draft.trimEnd();
  return trimmed === full && draft.length > trimmed.length;
}

/**
 * The palette stays open only while the draft is still picking a row.
 * It closes after a row is accepted, or once argument text no longer matches
 * any offered completion.
 */
export function shouldShowSlashPalette(
  draft: string,
  completions: readonly CommandInfo[],
): boolean {
  if (!draft.startsWith("/") || completions.length === 0) return false;

  const trimmed = draft.trimEnd();

  if (
    draft.endsWith(" ") &&
    completions.some((row) => slashCommandText(row.name) === trimmed)
  ) {
    return false;
  }

  return completions.some((row) => {
    const full = slashCommandText(row.name);
    if (trimmed.length < full.length && full.startsWith(trimmed)) return true;
    if (trimmed === full && !draft.endsWith(" ")) return true;
    if (full.startsWith(trimmed) && trimmed !== full) return true;
    if (trimmed.startsWith(`${full} `)) {
      return full.startsWith(trimmed) || trimmed.startsWith(full);
    }
    return false;
  });
}
