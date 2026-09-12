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

import { WORDMARK_ROWS, fitWordmark } from "../layout/wordmark.js";

/** Below this many terminal rows the logo collapses to one line. */
export const LOGO_COLLAPSE_ROWS = 24;

/** Rows the compact block occupies: two wordmark rows and the tagline. */
export const LOGO_EXPANDED_ROWS = 3;
export const LOGO_COLLAPSED_ROWS = 1;
/** Narrower than this and even the smallest big wordmark will not fit. */
export const BIG_WORDMARK_MIN_COLUMNS = 41;

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

/** Rows the launch wordmark needs: the block plus the tagline under it. */
export function bigLogoRows(terminalColumns: number): number {
  return terminalColumns >= BIG_WORDMARK_MIN_COLUMNS ? WORDMARK_ROWS + 1 : LOGO_EXPANDED_ROWS;
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
  /**
   * Draw the launch wordmark: the biggest block letters the terminal fits,
   * flush to the left edge. The compact two-row mark is what the rest of the
   * session uses.
   */
  big?: boolean;
}

function LogoInner({ terminalRows, version, width, big = false }: LogoProps): React.ReactElement {
  const bigRows = big ? fitWordmark(width) : null;
  if (bigRows) {
    return (
      <Box flexDirection="column" flexShrink={0} width={width}>
        {bigRows.map((row, index) => (
          <Text key={`wordmark-${index}`} color={LOGO_COLOR} bold wrap="truncate-end">
            {row}
          </Text>
        ))}
        <Text color={LOGO_COLOR} wrap="truncate-end">
          {`${SPROUT} snowpea v${version}`}
        </Text>
      </Box>
    );
  }

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
