/**
 * Prompts the daemon took but has not started yet, listed under the input.
 *
 * Typing while a turn runs does not fail and does not interrupt: the prompt
 * waits. That is only reassuring if you can see it waiting, which is what this
 * is — dim, numbered, and gone the moment the daemon starts or drops it.
 */

import React from "react";
import { Box, Text } from "ink";

import { clip } from "../state/working.js";
import type { QueuedPrompt } from "../state/store.js";

/** Rows before the rest are counted instead of listed. */
export const MAX_QUEUED_ROWS = 3;

export function QueuedPrompts({
  queued,
  width,
}: {
  queued: QueuedPrompt[];
  width: number;
}): React.ReactElement | null {
  if (queued.length === 0) return null;
  const shown = queued.slice(0, MAX_QUEUED_ROWS);
  const hidden = queued.length - shown.length;
  return (
    <Box flexDirection="column" width={width} flexShrink={0}>
      {shown.map((entry, index) => (
        <Text key={entry.turnId} dimColor wrap="truncate-end">
          {`   ${index + 1}. ${clip(entry.text || "(queued prompt)", Math.max(10, width - 8))}`}
        </Text>
      ))}
      {hidden > 0 ? <Text dimColor>{`   … ${hidden} more queued`}</Text> : null}
    </Box>
  );
}
