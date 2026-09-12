/**
 * One delegate's conversation, opened in place of the live area.
 *
 * A subagent runs in a session of its own, so its transcript cannot go into
 * `<Static>` beside the main one: the scrollback is append-only and this view
 * has to be switched away from. It is therefore a bounded window over the
 * child's lines, scrolled with the arrow keys, drawn with the same flattening
 * the full-screen layout uses.
 */

import React from "react";
import { Box, Text } from "ink";

import { TranscriptView } from "./TranscriptView.js";
import type { Line } from "../layout/transcript.js";

export interface AgentTranscriptProps {
  /** Agent definition name, or the role it fills. */
  name: string;
  task: string;
  status: string;
  /** Already windowed by `sliceViewport`. */
  lines: Line[];
  height: number;
  width: number;
  /** `▲ 42 ▼ 7` when the window is away from the newest line. */
  scrollIndicator?: string | null;
  /** Shown instead of the transcript while nothing has arrived yet. */
  empty?: boolean;
}

export function AgentTranscript({
  name,
  task,
  status,
  lines,
  height,
  width,
  scrollIndicator = null,
  empty = false,
}: AgentTranscriptProps): React.ReactElement {
  return (
    <Box
      flexDirection="column"
      width={width}
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
      flexShrink={0}
    >
      <Box justifyContent="space-between">
        <Text wrap="truncate-end">
          <Text bold color="cyan">{`◯ ${name}`}</Text>
          <Text dimColor>{task ? ` · ${task}` : ""}</Text>
        </Text>
        <Text dimColor>{scrollIndicator ? `${scrollIndicator} · ${status}` : status}</Text>
      </Box>

      {empty ? (
        <Box height={height}>
          <Text dimColor>waiting for the agent's first output…</Text>
        </Box>
      ) : (
        <TranscriptView lines={lines} height={height} />
      )}

      <Text dimColor>↑↓ PgUp/PgDn scroll · Esc back to the main transcript</Text>
    </Box>
  );
}
