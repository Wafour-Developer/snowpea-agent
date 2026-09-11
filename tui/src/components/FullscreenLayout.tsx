/**
 * The full-screen shell: header, transcript viewport, input block, status row.
 *
 * The root box is pinned to the exact terminal size with `overflow="hidden"`,
 * which is what keeps Ink from scrolling the alternate screen buffer. Nothing
 * inside may grow past its allotted rows; the transcript is already sliced to
 * fit by `sliceViewport` and the input block's height is reserved by
 * `bottomRows`.
 */

import React from "react";
import { Box, Text } from "ink";

import type { Line } from "../layout/transcript.js";
import type { Mode } from "../rpc/sdk.js";
import { TranscriptView } from "./TranscriptView.js";

const MODE_COLOR: Record<Mode, string> = {
  plan: "cyan",
  accept: "green",
  auto: "red",
};

/** `~/src/snowpea` — keeps the tail of a long path, which is the useful half. */
export function shortenPath(path: string, max: number): string {
  if (max <= 1 || path.length <= max) return path;
  return `…${path.slice(path.length - (max - 1))}`;
}

export interface FullscreenLayoutProps {
  rows: number;
  columns: number;
  transcriptRows: number;
  /** Already windowed by `sliceViewport`. */
  lines: Line[];
  workdir: string;
  sessionId: string | null;
  mode: Mode;
  /** Update banner text, shown in place of the header rule while it is set. */
  banner?: string | null;
  bannerColor?: string;
  /** `▲ 42 ▼ 7` when the transcript is scrolled away from the newest line. */
  scrollIndicator?: string | null;
  /** Overlay drawn over the transcript region, e.g. the help panel. */
  overlay?: React.ReactNode;
  /** Approval queue, approval prompt or chat line, plus the error row. */
  bottom: React.ReactNode;
  /** Mode bar and status line; always the last rows of the screen. */
  status: React.ReactNode;
}

export function FullscreenLayout({
  rows,
  columns,
  transcriptRows,
  lines,
  workdir,
  sessionId,
  mode,
  banner = null,
  bannerColor = "cyan",
  scrollIndicator = null,
  overlay = null,
  bottom,
  status,
}: FullscreenLayoutProps): React.ReactElement {
  const inner = Math.max(1, columns - 2);
  const session = sessionId ? sessionId.slice(0, 8) : "—";
  const right = ` session ${session} · ${mode.toUpperCase()}`;
  const left = `snowpea · ${shortenPath(workdir, Math.max(1, inner - right.length - 1))}`;
  const rule = banner === null ? "─".repeat(Math.max(0, inner - (scrollIndicator?.length ?? 0))) : null;

  return (
    <Box flexDirection="column" width={columns} height={rows} paddingX={1} overflow="hidden">
      <Box flexShrink={0} justifyContent="space-between">
        <Text bold color="cyan" wrap="truncate-end">
          {left}
        </Text>
        <Text color={MODE_COLOR[mode]} wrap="truncate-end">
          {right}
        </Text>
      </Box>

      <Box flexShrink={0} justifyContent="space-between">
        {banner === null ? (
          <Text dimColor wrap="truncate-end">
            {rule}
          </Text>
        ) : (
          <Text color={bannerColor} wrap="truncate-end">
            {banner}
          </Text>
        )}
        {scrollIndicator ? <Text dimColor>{scrollIndicator}</Text> : null}
      </Box>

      {overlay ? (
        <Box flexDirection="column" height={transcriptRows} flexGrow={1} flexShrink={1} overflow="hidden">
          {overlay}
        </Box>
      ) : (
        <TranscriptView lines={lines} height={transcriptRows} />
      )}

      <Box flexDirection="column" flexShrink={0}>
        {bottom}
      </Box>

      <Box flexDirection="column" flexShrink={0}>
        {status}
      </Box>
    </Box>
  );
}
