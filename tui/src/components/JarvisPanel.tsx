/**
 * The Jarvis screen's panel: what Jarvis supervises, what waits for the person,
 * and what it decided lately. Drawn above the input while the open session is
 * the Jarvis session; the lines come from `panelLines`, which already cuts
 * every row to the width in terminal cells.
 */

import React from "react";
import { Box, Text } from "ink";

import type { PanelLine } from "../state/jarvis.js";

function JarvisPanelInner({ lines }: { lines: PanelLine[] }): React.ReactElement | null {
  if (lines.length === 0) return null;
  return (
    <Box flexDirection="column" flexShrink={0}>
      {lines.map((line) => (
        <Text key={line.key} wrap="truncate-end">
          {line.segments.map((segment, index) => (
            <Text
              key={`${line.key}-${index}`}
              color={segment.color}
              dimColor={segment.dimColor}
              bold={segment.bold}
            >
              {segment.text}
            </Text>
          ))}
        </Text>
      ))}
    </Box>
  );
}

export const JarvisPanel = React.memo(JarvisPanelInner);
