/** One-line tool call summary; output expands when `expanded` is set. */

import React from "react";
import { Box, Text } from "ink";

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
  const body = call.error ?? call.output ?? "";
  const lines = body.length > 0 ? body.split("\n") : [];
  const shown = expanded ? lines.slice(0, maxOutputLines) : [];
  const hidden = lines.length - shown.length;

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text>
        <Text color={meta.color}>{meta.glyph} </Text>
        <Text bold>{call.name}</Text>
        <Text dimColor> {summarizeArgs(call.args)}</Text>
        {!expanded && lines.length > 0 ? <Text dimColor> ({lines.length} lines)</Text> : null}
      </Text>
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
      {expanded && hidden > 0 ? <Text dimColor>{`  … ${hidden} more lines`}</Text> : null}
    </Box>
  );
}
