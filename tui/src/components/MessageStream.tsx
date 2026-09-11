/** Renders the assistant/user transcript with a markdown-lite pass. */

import React from "react";
import { Box, Text } from "ink";

import type { Message } from "../state/store.js";

/** Inline markdown we support: `code`, **bold**, *italic*. */
function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyPrefix}-i${i++}`;
    if (token.startsWith("`")) {
      parts.push(
        <Text key={key} color="cyan">
          {token.slice(1, -1)}
        </Text>,
      );
    } else if (token.startsWith("**")) {
      parts.push(
        <Text key={key} bold>
          {token.slice(2, -2)}
        </Text>,
      );
    } else {
      parts.push(
        <Text key={key} italic>
          {token.slice(1, -1)}
        </Text>,
      );
    }
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function MarkdownLite({ text, idPrefix }: { text: string; idPrefix: string }): React.ReactElement {
  const lines = text.split("\n");
  let inFence = false;
  return (
    <Box flexDirection="column">
      {lines.map((line, index) => {
        const key = `${idPrefix}-l${index}`;
        if (line.trimStart().startsWith("```")) {
          inFence = !inFence;
          return (
            <Text key={key} dimColor>
              {line}
            </Text>
          );
        }
        if (inFence) {
          return (
            <Text key={key} color="cyan">
              {line}
            </Text>
          );
        }
        const heading = /^(#{1,6})\s+(.*)$/.exec(line);
        if (heading) {
          return (
            <Text key={key} bold color="yellow">
              {heading[2]}
            </Text>
          );
        }
        const bullet = /^(\s*)[-*]\s+(.*)$/.exec(line);
        if (bullet) {
          return (
            <Text key={key}>
              {bullet[1]}
              <Text color="magenta">• </Text>
              {renderInline(bullet[2], key)}
            </Text>
          );
        }
        return <Text key={key}>{renderInline(line, key)}</Text>;
      })}
    </Box>
  );
}

const ROLE_LABEL: Record<Message["role"], { label: string; color: string }> = {
  user: { label: "›", color: "green" },
  assistant: { label: "◆", color: "blue" },
  system: { label: "!", color: "yellow" },
};

export function MessageView({ message }: { message: Message }): React.ReactElement {
  const meta = ROLE_LABEL[message.role];
  return (
    <Box flexDirection="row" marginBottom={1}>
      <Box marginRight={1}>
        <Text color={meta.color} bold>
          {meta.label}
        </Text>
      </Box>
      <Box flexDirection="column" flexGrow={1}>
        <MarkdownLite text={message.text} idPrefix={message.id} />
        {message.streaming ? <Text dimColor>…</Text> : null}
      </Box>
    </Box>
  );
}

export function MessageStream({ messages }: { messages: Message[] }): React.ReactElement {
  return (
    <Box flexDirection="column">
      {messages.map((m) => (
        <MessageView key={m.id} message={m} />
      ))}
    </Box>
  );
}
