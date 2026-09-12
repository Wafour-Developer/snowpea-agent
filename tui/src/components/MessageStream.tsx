/** Inline and full-screen messages share one Markdown/terminal-cell projection. */
import React from "react";
import { Box, Text } from "ink";
import type { Message } from "../state/store.js";
import { messageLines, wrapLine } from "../layout/transcript.js";

export function MessageView({ message, width = 80 }: { message: Message; width?: number }): React.ReactElement {
  const lines = messageLines(message, width).flatMap(line => wrapLine(line, width, "  "));
  return (
    <Box flexDirection="column" marginBottom={1}>
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
