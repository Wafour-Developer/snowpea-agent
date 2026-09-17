/** Inline and full-screen messages share one Markdown/terminal-cell projection. */
import React from "react";
import { Box, Text } from "ink";
import type { Message } from "../state/store.js";
import { messageLines, wrapLine } from "../layout/transcript.js";
import { RenderedLines } from "./RenderedLines.js";

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
  const all = messageLines(message, width).flatMap((line) => wrapLine(line, width, "  "));
  let lines = all.slice(Math.max(0, Math.floor(startLine)));
  if (message.streaming && maxRows !== undefined) {
    const cap = Math.max(1, Math.floor(maxRows));
    if (lines.length > cap) lines = lines.slice(lines.length - cap);
  }
  return (
    <Box flexDirection="column" marginBottom={1}>
      <RenderedLines lines={lines} />
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
