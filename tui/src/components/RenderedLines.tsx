import React from "react";
import { Text } from "ink";
import type { Line } from "../layout/transcript.js";
import { textWidth } from "../layout/text-width.js";

/**
 * One styled transcript row, shared by messages and stream scrollback chunks.
 *
 * With `background`, every row is filled to `width` cells in that colour, so
 * a block (a prompt you sent) reads as one band rather than as coloured words.
 */
export function RenderedLines({
  lines,
  background,
  width,
}: {
  lines: Line[];
  background?: string;
  width?: number;
}): React.ReactElement {
  return (
    <>
      {lines.map((line) => {
        const used = background && width
          ? line.segments.reduce((sum, segment) => sum + textWidth(segment.text), 0)
          : 0;
        const pad = background && width ? Math.max(0, width - used) : 0;
        return (
          <Text key={line.key}>
            {line.segments.map((segment, index) => (
              <Text
                key={index}
                color={segment.color}
                backgroundColor={background}
                dimColor={segment.dimColor}
                bold={segment.bold}
                italic={segment.italic}
              >
                {segment.text}
              </Text>
            ))}
            {pad > 0 ? <Text backgroundColor={background}>{" ".repeat(pad)}</Text> : null}
          </Text>
        );
      })}
    </>
  );
}
