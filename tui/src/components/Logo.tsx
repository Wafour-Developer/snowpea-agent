/**
 * The snowpea wordmark drawn at the top of the screen.
 *
 * Two block rows plus a tagline in the usual case; a single line once the
 * terminal is too short to spend three rows on decoration. The header is a
 * fixed block, so `logoRows` is what the layout arithmetic reserves and what
 * this component actually draws — they must not disagree, or the frame grows
 * past the terminal and Ink starts scrolling the alternate buffer.
 */

import React from "react";
import { Box, Text } from "ink";

/** Below this many terminal rows the logo collapses to one line. */
export const LOGO_COLLAPSE_ROWS = 24;

/** Rows the expanded block occupies: two wordmark rows and the tagline. */
export const LOGO_EXPANDED_ROWS = 3;
export const LOGO_COLLAPSED_ROWS = 1;

/** Hand-drawn half-block wordmark, 30 columns wide. */
export const WORDMARK: readonly [string, string] = [
  "█▀▀ █▄ █ █▀█ █ █ █ █▀█ █▀▀ ▄▀█",
  "▄██ █ ▀█ █▄█ ▀▄▀▄▀ █▀▀ ██▄ █▀█",
];

export const SPROUT = "🌱";
export const TAGLINE = "open-source multi-vendor coding agent · your own AI assistant";

/** snowpea green; the wordmark and the sprout share it. */
export const LOGO_COLOR = "green";

/** Rows the logo needs at this terminal height. */
export function logoRows(terminalRows: number): number {
  return terminalRows < LOGO_COLLAPSE_ROWS ? LOGO_COLLAPSED_ROWS : LOGO_EXPANDED_ROWS;
}

/** True when the terminal is too short for the block wordmark. */
export function isCollapsed(terminalRows: number): boolean {
  return logoRows(terminalRows) === LOGO_COLLAPSED_ROWS;
}

/** The one-line form, also used by `--no-fullscreen` on a short terminal. */
export function collapsedLine(version: string): string {
  return `${SPROUT} snowpea v${version} · ${TAGLINE}`;
}

export interface LogoProps {
  /** Terminal height; decides between the block and the single line. */
  terminalRows: number;
  version: string;
  /** Width available for the tagline row, which truncates rather than wraps. */
  width: number;
}

function LogoInner({ terminalRows, version, width }: LogoProps): React.ReactElement {
  if (isCollapsed(terminalRows)) {
    return (
      <Box flexShrink={0}>
        <Text color={LOGO_COLOR} bold wrap="truncate-end">
          {collapsedLine(version)}
        </Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" flexShrink={0} width={width}>
      <Text color={LOGO_COLOR} bold wrap="truncate-end">
        {WORDMARK[0]}
      </Text>
      <Text color={LOGO_COLOR} bold wrap="truncate-end">
        {`${WORDMARK[1]}  ${SPROUT}`}
      </Text>
      <Text dimColor wrap="truncate-end">
        {`${TAGLINE}  v${version}`}
      </Text>
    </Box>
  );
}

/**
 * Memoized: the logo depends on nothing that changes while typing, so it must
 * never take part in a keystroke re-render.
 */
export const Logo = React.memo(LogoInner);
