/**
 * What is running right now, opened from the footer row.
 *
 * The daemon does not track background shells separately, so this is the
 * in-flight tool calls, shells first: the same question — "what is this thing
 * doing to my machine?" — answered from the events that exist.
 */

import React from "react";
import { Box, Text } from "ink";

import { summarizeCalls, toolKind } from "../layout/summary.js";
import { formatDuration } from "../state/working.js";
import type { ToolCallEntry } from "../state/store.js";

/** In-flight calls, shells first, newest last. */
export function runningCalls(calls: ToolCallEntry[]): ToolCallEntry[] {
  const running = calls.filter((call) => call.state === "running");
  return [
    ...running.filter((call) => toolKind(call.name) === "shell"),
    ...running.filter((call) => toolKind(call.name) !== "shell"),
  ];
}

export function ShellList({
  calls,
  now,
  width,
}: {
  calls: ToolCallEntry[];
  now: number;
  width: number;
}): React.ReactElement {
  const running = runningCalls(calls);
  return (
    <Box flexDirection="column" width={width}>
      {running.length === 0 ? (
        <Text dimColor>{"    nothing running"}</Text>
      ) : (
        running.map((call) => (
          <Text key={call.callId} dimColor wrap="truncate-end">
            {`    ◦ ${summarizeCalls([call])}`}
            {call.startedAt ? ` · ${formatDuration(now - call.startedAt)}` : ""}
          </Text>
        ))
      )}
    </Box>
  );
}
