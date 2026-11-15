/**
 * An external CLI session's conversation, opened in place of the live area.
 *
 * The same bounded, scrollable window `AgentTranscript` is, with what is
 * particular to a session that lives in another program: which CLI and where
 * its process runs, the mode it runs in, a persistent warning when snowpea does
 * not gate what it does, and — when sending is refused — why.
 */

import React from "react";
import { Box, Text } from "ink";

import { TranscriptView } from "./TranscriptView.js";
import { fit, spread } from "../layout/fit.js";
import type { Line } from "../layout/transcript.js";

export interface ExternalTranscriptProps {
  /** `Codex · fix the build`. */
  title: string;
  /** `running · other terminal · default`. */
  status: string;
  /** Persistent red line when the session is not gated; else null. */
  warning: string | null;
  /** Why the input is off; else null. */
  viewOnly: string | null;
  /** A quiet line (a tmux pane's prompts are answered in the terminal); else null. */
  note?: string | null;
  lines: Line[];
  height: number;
  width: number;
  scrollIndicator?: string | null;
  empty?: boolean;
}

/** Rows around the transcript: border (2), header and the key hint. */
export const EXTERNAL_CHROME_ROWS = 4;

/** Extra rows the warning and the view-only line take. */
export function externalExtraRows(
  warning: string | null,
  viewOnly: string | null,
  note: string | null = null,
): number {
  return (warning ? 1 : 0) + (viewOnly ? 1 : 0) + (note ? 1 : 0);
}

function ExternalTranscriptInner({
  title,
  status,
  warning,
  viewOnly,
  note = null,
  lines,
  height,
  width,
  scrollIndicator = null,
  empty = false,
}: ExternalTranscriptProps): React.ReactElement {
  const inner = Math.max(10, width - 4);
  return (
    <Box
      flexDirection="column"
      width={width}
      borderStyle="round"
      borderColor={warning ? "red" : "blue"}
      paddingX={1}
      flexShrink={0}
    >
      <Text wrap="truncate-end">
        <Text bold color="blue">{"◈ "}</Text>
        <Text bold>{spread(title, scrollIndicator ? `${scrollIndicator} · ${status}` : status, inner - 2)}</Text>
      </Text>
      {warning ? (
        <Text bold color="red" wrap="truncate-end">{`! ${fit(warning, inner - 2)}`}</Text>
      ) : null}
      {viewOnly ? (
        <Text color="yellow" wrap="truncate-end">{fit(viewOnly, inner)}</Text>
      ) : null}

      {note ? (
        <Text dimColor wrap="truncate-end">{fit(note, inner)}</Text>
      ) : null}

      {empty ? (
        <Box height={height}>
          <Text dimColor>nothing in this session yet</Text>
        </Box>
      ) : (
        <TranscriptView lines={lines} height={height} />
      )}

      <Text dimColor wrap="truncate-end">
        {fit("↑↓ PgUp/PgDn scroll · Esc stops the turn, or goes back when idle", inner)}
      </Text>
    </Box>
  );
}

export const ExternalTranscript = React.memo(ExternalTranscriptInner);
