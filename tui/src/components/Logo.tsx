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

import {
  LETTER_WIDTHS,
  WORDMARK_ROWS,
  fitWordmark,
  letterWidthFor,
  lockupWidth,
  renderMiniMark,
  shadowRow,
} from "../layout/wordmark.js";
import { colorMode, gradientColors, type ColorMode } from "../layout/palette.js";

/** Below this many terminal rows the logo collapses to one line. */
export const LOGO_COLLAPSE_ROWS = 24;

/** Rows the compact block occupies: two wordmark rows and the tagline. */
export const LOGO_EXPANDED_ROWS = 3;
export const LOGO_COLLAPSED_ROWS = 1;
/**
 * Narrower than this and the lockup will not fit.
 *
 * The mark is square and the letters are seven rows tall, so the two together
 * have a floor: below it the compact two-row form takes over, with the mark
 * reduced to its ring.
 */
export const BIG_WORDMARK_MIN_COLUMNS = lockupWidth(LETTER_WIDTHS[LETTER_WIDTHS.length - 1]);

/** Hand-drawn half-block wordmark, 30 columns wide. */
export const WORDMARK: readonly [string, string] = [
  "█▀▀ █▄ █ █▀█ █ █ █ █▀█ █▀▀ ▄▀█",
  "▄██ █ ▀█ █▄█ ▀▄▀▄▀ █▀▀ ██▄ █▀█",
];

export const SPROUT = "🌱";
export const TAGLINE = "open-source multi-vendor coding agent · your own AI assistant";

/** Brand violet; the wordmark and the sprout share it on basic-colour terminals. */
export const LOGO_COLOR = "magenta";

/** Rows the logo needs at this terminal height. */
export function logoRows(terminalRows: number): number {
  return terminalRows < LOGO_COLLAPSE_ROWS ? LOGO_COLLAPSED_ROWS : LOGO_EXPANDED_ROWS;
}

/** Rows the launch wordmark needs: sprout, letters, shadow. */
export function bigLogoRows(terminalColumns: number): number {
  return letterWidthFor(terminalColumns) === null ? LOGO_EXPANDED_ROWS : WORDMARK_ROWS + 2;
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
  /** How much colour the terminal can take; detected when not given. */
  mode?: ColorMode;
}

function LogoInner({
  terminalRows,
  version,
  width,
  big = false,
  mode,
}: LogoProps): React.ReactElement {
  const bigRows = big ? fitWordmark(width) : null;
  if (bigRows) {
    const drawn = [...bigRows[0]].length;
    const paint = mode ?? colorMode(process.env, Boolean(process.stdout?.isTTY));
    const colors = gradientColors(bigRows.length, paint);
    const accent = paint === "none" ? undefined : LOGO_COLOR;
    return (
      <Box flexDirection="column" flexShrink={0} width={width}>
        {bigRows.map((row, index) => (
          <Text key={`wordmark-${index}`} color={colors[index]} bold wrap="truncate-end">
            {row}
          </Text>
        ))}
        <Text dimColor wrap="truncate-end">
          {shadowRow(drawn)}
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

  const mini = renderMiniMark();
  return (
    <Box flexDirection="column" flexShrink={0} width={width}>
      <Text color={LOGO_COLOR} bold wrap="truncate-end">
        {`${mini[0]} ${WORDMARK[0]}`}
      </Text>
      <Text color={LOGO_COLOR} bold wrap="truncate-end">
        {`${mini[1]} ${WORDMARK[1]}`}
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
