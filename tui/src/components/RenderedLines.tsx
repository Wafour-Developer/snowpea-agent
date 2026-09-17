import React from "react";
import { Text } from "ink";
import type { Line } from "../layout/transcript.js";

/** One styled transcript row, shared by messages and stream scrollback chunks. */
export function RenderedLines({ lines }: { lines: Line[] }): React.ReactElement {
  return (
    <>
      {lines.map((line) => (
        <Text key={line.key}>
          {line.segments.map((segment, index) => (
            <Text
              key={index}
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
    </>
  );
}
