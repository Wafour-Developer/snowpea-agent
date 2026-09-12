/**
 * A file change, drawn where it happened in the conversation.
 *
 * Diffs are transcript entries like messages and tool calls: they appear inline
 * at the point the edit was made and settle into the scrollback with everything
 * else. There is no diff pane — a change you have to go and look for somewhere
 * else is a change you will not read.
 *
 * Long patches are cut to `COLLAPSED_LINES`; Ctrl+O opens the newest one while
 * it is still in the live region.
 */

import React from "react";
import { Box, Text } from "ink";

import type { DiffEntry } from "../state/store.js";

/** Lines of patch shown before the rest is folded away. */
export const COLLAPSED_LINES = 12;
/** Even expanded, one diff never gets to fill the whole screen. */
export const EXPANDED_LINES = 200;

export function diffLineColor(line: string): string | undefined {
  if (line.startsWith("+++") || line.startsWith("---")) return "cyan";
  if (line.startsWith("@@")) return "magenta";
  if (line.startsWith("+")) return "green";
  if (line.startsWith("-")) return "red";
  return undefined;
}

/** Body lines of a patch: the headers are chrome, not content. */
function bodyLines(patch: string): string[] {
  return patch.split("\n").filter((line) => !/^(\+\+\+|---|diff |index )/.test(line));
}

export interface DiffStats {
  added: number;
  removed: number;
}

/** How many lines the patch adds and removes. */
export function diffStats(patch: string): DiffStats {
  let added = 0;
  let removed = 0;
  for (const line of bodyLines(patch)) {
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

/** `✎ Edited README.md  (+3 −1)`, or `✚ Created notes.md (7 lines)`. */
export function diffHeader(diff: DiffEntry): string {
  const { added, removed } = diffStats(diff.patch);
  if (diff.created) {
    return `✚ Created ${diff.path} (${added} ${added === 1 ? "line" : "lines"})`;
  }
  return `✎ Edited ${diff.path}  (+${added} −${removed})`;
}

export function DiffView({
  diff,
  expanded = false,
}: {
  diff: DiffEntry;
  /** Ctrl+O opened this one. */
  expanded?: boolean;
}): React.ReactElement {
  const lines = diff.patch.split("\n");
  const limit = expanded ? EXPANDED_LINES : COLLAPSED_LINES;
  const shown = lines.slice(0, limit);
  const hidden = lines.length - shown.length;
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text bold color={diff.created ? "green" : "yellow"}>
        {diffHeader(diff)}
      </Text>
      {shown.map((line, index) => (
        <Text
          key={`${diff.id}-d${index}`}
          color={diffLineColor(line)}
          dimColor={diffLineColor(line) === undefined}
          wrap="truncate-end"
        >
          {line}
        </Text>
      ))}
      {hidden > 0 ? (
        <Text dimColor>{`… ${hidden} more ${hidden === 1 ? "line" : "lines"} (Ctrl+O)`}</Text>
      ) : null}
    </Box>
  );
}
