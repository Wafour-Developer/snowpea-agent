/**
 * The one-line trace a run of tool calls leaves in the scrollback.
 *
 * `Read 3 files (128 lines)` instead of three cards. The wording comes from
 * `layout/summary.ts`; what is drawn here is written once into `<Static>` and
 * never redrawn, so it has to be self-contained.
 */

import React from "react";
import { Box, Text } from "ink";

import { hiddenLines, summarizeCalls } from "../layout/summary.js";
import type { ToolCallEntry } from "../state/store.js";

/** Marks a completed action, the way the role glyphs mark messages. */
export const SUMMARY_GLYPH = "⏺";

export function ToolSummary({ calls }: { calls: ToolCallEntry[] }): React.ReactElement | null {
  if (calls.length === 0) return null;
  const lines = hiddenLines(calls);
  return (
    <Box>
      <Text color="green">{`${SUMMARY_GLYPH} `}</Text>
      <Text>{summarizeCalls(calls)}</Text>
      {lines > 0 ? <Text dimColor>{` (${lines} lines)`}</Text> : null}
    </Box>
  );
}
