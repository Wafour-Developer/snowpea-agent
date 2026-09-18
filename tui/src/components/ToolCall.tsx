/** One-line tool call summary; output expands when `expanded` is set. */

import React from "react";
import { Box, Text } from "ink";

import { visibleToolBodyLines } from "../layout/transcript.js";
import { diagnosticLineColor } from "../state/lsp.js";
import type { ToolCallEntry } from "../state/store.js";

const STATE_GLYPH: Record<ToolCallEntry["state"], { glyph: string; color: string }> = {
  running: { glyph: "◌", color: "yellow" },
  ok: { glyph: "✓", color: "green" },
  error: { glyph: "✗", color: "red" },
};

/** Compact one-line rendering of the tool arguments. */
export function summarizeArgs(args: Record<string, unknown>, max = 60): string {
  const parts = Object.entries(args).map(([k, v]) => {
    const value = typeof v === "string" ? v : JSON.stringify(v);
    return `${k}=${value}`;
  });
  const joined = parts.join(" ");
  return joined.length > max ? `${joined.slice(0, max - 1)}…` : joined;
}

export function ToolCall({
  call,
  expanded = false,
  maxOutputLines = 12,
}: {
  call: ToolCallEntry;
  expanded?: boolean;
  maxOutputLines?: number;
}): React.ReactElement {
  const meta = STATE_GLYPH[call.state];
  const { shown, total } = visibleToolBodyLines(call, expanded, maxOutputLines);
  const hidden = total - shown.length;
  // While the call is in flight the daemon streams its output; the tail says
  // the thing is alive and what it is chewing on. The final result replaces it.
  const tail = call.state === "running" ? (call.progress ?? []) : [];

  return (
    <Box flexDirection="column">
      <Text>
        <Text color={meta.color}>{meta.glyph} </Text>
        <Text bold>{call.name}</Text>
        <Text dimColor> {summarizeArgs(call.args)}</Text>
        {!expanded && total > 0 && shown.length === 0 ? (
          <Text dimColor> ({total} lines)</Text>
        ) : null}
      </Text>
      {tail.map((line, index) => (
        <Text
          key={`${call.callId}-p${index}`}
          dimColor={line.stream === "stdout"}
          color={line.stream === "stderr" ? "yellow" : undefined}
        >
          {"  "}
          {line.text}
        </Text>
      ))}
      {tail.length > 0 && call.progressTruncated ? <Text dimColor>{"  … truncated"}</Text> : null}
      {shown.map((line, index) => {
        // A Diagnostics block from a language server is the part of a tool
        // result worth reading in colour: its severities are the news.
        const severity = diagnosticLineColor(line);
        return (
          <Text
            key={`${call.callId}-o${index}`}
            dimColor={!call.error && severity === undefined}
            color={call.error ? "red" : severity}
          >
            {"  "}
            {line}
          </Text>
        );
      })}
      {hidden > 0 ? <Text dimColor>{`  … ${hidden} more lines`}</Text> : null}
    </Box>
  );
}
