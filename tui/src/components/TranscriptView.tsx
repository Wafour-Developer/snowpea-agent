/**
 * Draws a pre-sliced window of transcript lines into a fixed-height box.
 *
 * Every line has already been wrapped to the viewport width by
 * `layout/transcript.ts`, so `wrap="truncate-end"` here is only a guard: if a
 * line were ever too wide it must be cut, never reflowed onto a second row,
 * because a second row would push the last row off screen and make Ink scroll
 * the alternate buffer.
 */

import React from "react";
import { Box, Text } from "ink";

import type { Line } from "../layout/transcript.js";

export function TranscriptView({
  lines,
  height,
}: {
  lines: Line[];
  height: number;
}): React.ReactElement {
  return (
    <Box flexDirection="column" height={height} flexGrow={1} flexShrink={1} overflow="hidden">
      {lines.map((line) => (
        <Text key={line.key} wrap="truncate-end">
          {line.segments.map((segment, index) => (
            <Text
              key={`${line.key}-s${index}`}
              color={segment.color}
              dimColor={segment.dimColor}
              bold={segment.bold}
              italic={segment.italic}
            >
              {segment.text}
            </Text>
          ))}
        </Text>
      ))}
    </Box>
  );
}
