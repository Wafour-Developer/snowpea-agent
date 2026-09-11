/**
 * The opt-in full-screen shell: logo, transcript viewport, input block, HUD.
 *
 * This is what `--fullscreen` selects. The default layout is the inline one in
 * `app.tsx`, which lets the terminal keep the scrollback; this one owns the
 * alternate buffer instead and windows the transcript itself.
 *
 * The root box is pinned to the frame size — the terminal less the row
 * `RESERVED_FRAME_ROW` holds back — with `overflow="hidden"`, which is what
 * keeps Ink from scrolling the alternate screen buffer. Nothing inside may grow
 * past its allotted rows; the transcript is already sliced to fit by
 * `sliceViewport` and the input block's height is reserved by `bottomRows`.
 *
 * There is no header bar: the workdir, the session and the mode all live in the
 * HUD under the input, so the top of the screen is the logo and then content.
 */

import React from "react";
import { Box, Text } from "ink";

import type { Line } from "../layout/transcript.js";
import { Logo } from "./Logo.js";
import { TranscriptView } from "./TranscriptView.js";

export interface FullscreenLayoutProps {
  /** Rows the frame may draw into — one less than the terminal has. */
  rows: number;
  /** Terminal height, which is what decides whether the logo collapses. */
  terminalRows: number;
  /** Shown under the wordmark; the daemon's version once it is known. */
  version: string;
  columns: number;
  transcriptRows: number;
  /** Already windowed by `sliceViewport`. */
  lines: Line[];
  /** Update banner text, shown in place of the header rule while it is set. */
  banner?: string | null;
  bannerColor?: string;
  /** `▲ 42 ▼ 7` when the transcript is scrolled away from the newest line. */
  scrollIndicator?: string | null;
  /** Overlay drawn over the transcript region, e.g. the help panel. */
  overlay?: React.ReactNode;
  /** Approval queue, approval prompt or chat line, plus the error row. */
  bottom: React.ReactNode;
  /** The HUD; always the last rows of the screen. */
  status: React.ReactNode;
}

export function FullscreenLayout({
  rows,
  terminalRows,
  version,
  columns,
  transcriptRows,
  lines,
  banner = null,
  bannerColor = "cyan",
  scrollIndicator = null,
  overlay = null,
  bottom,
  status,
}: FullscreenLayoutProps): React.ReactElement {
  const inner = Math.max(1, columns - 2);
  const rule = banner === null ? "─".repeat(Math.max(0, inner - (scrollIndicator?.length ?? 0))) : null;

  return (
    <Box flexDirection="column" width={columns} height={rows} paddingX={1} overflow="hidden">
      <Logo terminalRows={terminalRows} version={version} width={inner} />

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
