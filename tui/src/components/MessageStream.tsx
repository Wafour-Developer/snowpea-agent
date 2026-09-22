/** Inline and full-screen messages share one Markdown/terminal-cell projection. */
import React from "react";
import { Box, Text } from "ink";
import type { Message } from "../state/store.js";
import { messageLines, wrapLine, type Line, type Segment } from "../layout/transcript.js";
import { currentAccent, currentAccentDim } from "../layout/palette.js";
import { findAllFileRefs } from "../state/fileRefs.js";
import { RenderedLines } from "./RenderedLines.js";
import { SUMMARY_GLYPH } from "./ToolSummary.js";

/** One-line fold label for a skill expansion, truncated to the message width. */
export function skillExpansionLabel(
  expansion: NonNullable<Message["expansion"]>,
  width: number,
): string {
  const suffix = ` · ${expansion.lines} lines`;
  const prefix = "skill ";
  const budget = Math.max(1, width - SUMMARY_GLYPH.length - 1 - prefix.length - suffix.length);
  let name = expansion.name;
  if (name.length > budget) {
    name = budget > 1 ? `${name.slice(0, budget - 1)}…` : name.slice(0, budget);
  }
  return `${prefix}${name}${suffix}`;
}

function SkillExpansionLine({
  expansion,
  width,
}: {
  expansion: NonNullable<Message["expansion"]>;
  width: number;
}): React.ReactElement {
  return (
    <Box>
      <Text color={currentAccentDim()}>{`${SUMMARY_GLYPH} `}</Text>
      <Text dimColor>{skillExpansionLabel(expansion, width)}</Text>
    </Box>
  );
}

/** Colorize '@' file references with the theme's accent color. */
export function colorizeFileRefs(lines: Line[]): Line[] {
  return lines.map((line) => {
    const segments: Segment[] = [];
    for (const segment of line.segments) {
      if (segment.color || !segment.text) {
        segments.push(segment);
        continue;
      }
      const refs = findAllFileRefs(segment.text);
      if (refs.length === 0) {
        segments.push(segment);
        continue;
      }
      let lastIndex = 0;
      for (const ref of refs) {
        if (ref.start > lastIndex) {
          segments.push({
            ...segment,
            text: segment.text.slice(lastIndex, ref.start),
          });
        }
        segments.push({
          ...segment,
          text: ref.raw,
          color: currentAccent(),
        });
        lastIndex = ref.end;
      }
      if (lastIndex < segment.text.length) {
        segments.push({
          ...segment,
          text: segment.text.slice(lastIndex),
        });
      }
    }
    return { ...line, segments };
  });
}

export function MessageView({
  message,
  width = 80,
  maxRows,
  startLine = 0,
}: {
  message: Message;
  width?: number;
  /**
   * Rows the live tail may occupy while the message is still streaming. Lines
   * before `startLine` are already in the terminal scrollback.
   */
  maxRows?: number;
  /** Wrapped rows already committed to scrollback for this message. */
  startLine?: number;
}): React.ReactElement {
  const baseLines = messageLines(message, width);
  const formattedLines = message.role === "user" ? colorizeFileRefs(baseLines) : baseLines;
  const all = formattedLines.flatMap((line) => wrapLine(line, width, "  "));
  let lines = all.slice(Math.max(0, Math.floor(startLine)));
  if (message.streaming && maxRows !== undefined) {
    const cap = Math.max(1, Math.floor(maxRows));
    if (lines.length > cap) lines = lines.slice(lines.length - cap);
  }
  return (
    <Box flexDirection="column" marginBottom={1}>
      <RenderedLines lines={lines} />
      {message.expansion ? (
        <SkillExpansionLine expansion={message.expansion} width={width} />
      ) : null}
      {(message.attachments ?? []).map((attachment) => (
        <Text key={`${message.id}-${attachment.name}`} dimColor wrap="truncate-end">
          {`  📎 ${attachment.name}`}
        </Text>
      ))}
    </Box>
  );
}

export function MessageStream({ messages }: { messages: Message[] }): React.ReactElement {
  return (
    <Box flexDirection="column">
      {messages.map((message) => <MessageView key={message.id} message={message} />)}
    </Box>
  );
}
