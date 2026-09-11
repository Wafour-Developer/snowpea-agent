/**
 * The line that says the agent is busy, drawn just above the input.
 *
 * All the wording is decided by `state/working.ts`; this only paints it. It is
 * memoized on the finished string so a render that did not change the text —
 * anything the user types, for instance — cannot repaint it.
 */

import React from "react";
import { Box, Text } from "ink";

export interface WorkingIndicatorProps {
  /** The finished line, or null while nothing is running. */
  line: string | null;
}

function WorkingIndicatorInner({ line }: WorkingIndicatorProps): React.ReactElement | null {
  if (!line) return null;
  return (
    <Box flexShrink={0}>
      <Text dimColor wrap="truncate-end">
        {line}
      </Text>
    </Box>
  );
}

export const WorkingIndicator = React.memo(WorkingIndicatorInner);
