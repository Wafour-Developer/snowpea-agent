/** Inline and full-screen messages share one Markdown/terminal-cell projection. */
import React from "react";
import { Box, Text } from "ink";
import type { Message } from "../state/store.js";
import { messageLines, wrapLine } from "../layout/transcript.js";

/** `… 274 lines above` — what is off the top of a capped message. */
export function aboveMarker(hidden: number): string {
  return `… ${hidden} ${hidden === 1 ? "line" : "lines"} above`;
}

export function MessageView({
  message,
  width = 80,
  maxRows,
}: {
  message: Message;
  width?: number;
  /**
   * Rows this message may occupy, marker row included. Only the inline live
   * region passes one: a message being streamed grows without bound, and a
   * live region taller than the terminal makes Ink clear the screen and
   * rewrite the whole scrollback on every update — which is the view shaking.
   * The text is not lost, it is just below the window until `message.done`
   * hands the whole message to `<Static>`.
   */
  maxRows?: number;
}): React.ReactElement {
  const all = messageLines(message, width).flatMap(line => wrapLine(line, width, "  "));
  const cap = maxRows === undefined ? all.length : Math.max(1, Math.floor(maxRows));
  // One row of the cap goes to the marker, so the window is one shorter.
  const hidden = all.length > cap ? all.length - (cap - 1) : 0;
  const lines = hidden > 0 ? all.slice(hidden) : all;
  return (
    <Box flexDirection="column" marginBottom={1}>
      {hidden > 0 ? <Text dimColor>{`  ${aboveMarker(hidden)}`}</Text> : null}
      {lines.map(line => (
        <Text key={line.key}>
          {line.segments.map((segment, index) => (
            <Text key={index} color={segment.color} dimColor={segment.dimColor}
              bold={segment.bold} italic={segment.italic}>{segment.text}</Text>
          ))}
        </Text>
      ))}
      {(message.attachments ?? []).map(attachment => (
        <Text key={`${message.id}-${attachment.name}`} dimColor wrap="truncate-end">
          {`  📎 ${attachment.name}`}
        </Text>
      ))}
    </Box>
  );
}

export function MessageStream({ messages }: { messages: Message[] }): React.ReactElement {
  return <Box flexDirection="column">{messages.map(message => <MessageView key={message.id} message={message} />)}</Box>;
}
