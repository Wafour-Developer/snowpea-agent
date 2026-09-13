/**
 * The snowpea wordmark.
 *
 * Each letter is drawn once at 10×14, at a resolution high enough to carry a
 * curve, and then rendered by pairing every two bitmap rows into one terminal
 * row of half blocks — `▀` for the top half, `▄` for the bottom, `█` for both.
 * That doubles the vertical resolution a terminal cell can express, which is
 * what lets the corners come off the square and the bowls of S, O, P and A read
 * as round rather than as stacked bricks.
 *
 * Width is chosen, not fixed: the caller asks for the biggest letter width that
 * fits its terminal and the columns are resampled to it, so a wide terminal
 * gets a wide wordmark instead of the same small one with space around it.
 *
 * Pure, so `test/wordmark.test.ts` can check both the letterforms and the fit.
 */

import { BIG_MARK, MINI_MARK } from "../components/mark.js";

/** Bitmap columns each letter is drawn on. */
export const MASTER_WIDTH = 10;
/** Bitmap rows each letter is drawn on; two per terminal row. */
export const MASTER_HEIGHT = 14;
/** Terminal rows the wordmark occupies. */
export const WORDMARK_ROWS = MASTER_HEIGHT / 2;

/**
 * The letterforms, at 10×14.
 *
 * The corners are cut rather than square — a row of `..######..` above
 * `.########.` above `###....###` is a quarter-circle once the halves are
 * paired — and every letter keeps the feature that tells it apart: three bars
 * on the E, a closed bowl and no leg on the P, a crossbar and open legs on the
 * A, a notch in the W, a diagonal in the N.
 */
const MASTERS: Record<string, readonly string[]> = {
  s: [
    "..######..",
    ".########.",
    "###....###",
    "##......##",
    "##........",
    "###.......",
    ".########.",
    "..######..",
    ".......###",
    "........##",
    "##......##",
    "###....###",
    ".########.",
    "..######..",
  ],
  n: [
    "##......##",
    "###.....##",
    "####....##",
    "####....##",
    "##.##...##",
    "##.##...##",
    "##..##..##",
    "##..##..##",
    "##...##.##",
    "##...##.##",
    "##....####",
    "##....####",
    "##.....###",
    "##......##",
  ],
  o: [
    "..######..",
    ".########.",
    "###....###",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "###....###",
    ".########.",
    "..######..",
  ],
  w: [
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##..##..##",
    "##..##..##",
    "##.####.##",
    "##.####.##",
    "####..####",
    "###....###",
  ],
  p: [
    "########..",
    "#########.",
    "##.....###",
    "##......##",
    "##......##",
    "##.....###",
    "#########.",
    "########..",
    "##........",
    "##........",
    "##........",
    "##........",
    "##........",
    "##........",
  ],
  e: [
    "..########",
    ".#########",
    "###.......",
    "##........",
    "##........",
    "########..",
    "########..",
    "##........",
    "##........",
    "##........",
    "###.......",
    ".#########",
    "..########",
    "..########",
  ],
  a: [
    "..######..",
    ".########.",
    "###....###",
    "##......##",
    "##......##",
    "##......##",
    "##########",
    "##########",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
    "##......##",
  ],
};

export const WORD = "snowpea";

/**
 * Letter widths tried, widest first.
 *
 * Dense enough that a terminal of any common width gets a wordmark that nearly
 * fills it; the sizes that divide the master evenly (20, 15, 10) are the ones
 * with perfectly even stroke weight, so they lead each range.
 */
export const LETTER_WIDTHS: readonly number[] = [20, 16, 15, 13, 12, 10, 8, 6];

/** Blocks, indexed by (top set) + (bottom set) × 2. */
const HALF_BLOCKS = [" ", "▀", "▄", "█"] as const;

/** Every glyph the wordmark can contain, for the tests that check nothing else creeps in. */
export const WORDMARK_GLYPHS: readonly string[] = [...HALF_BLOCKS];

/** The gap between letters at a given letter width. */
export function gapFor(letterWidth: number): number {
  return Math.max(1, Math.round(letterWidth / 8));
}

/** Columns the letters alone need at this letter width. */
export function wordmarkWidth(letterWidth: number, word = WORD): number {
  return word.length * letterWidth + (word.length - 1) * gapFor(letterWidth);
}

/** Columns the mark occupies; it is square, so this follows its row count. */
export const MARK_WIDTH = BIG_MARK[0].length;

/**
 * The air between the mark and the S: twice the gap between letters.
 *
 * One letter's width reads as a missing letter and costs a whole rung of the
 * size ladder; twice the letter gap separates the two halves of the lockup
 * without either.
 */
export function markGapFor(letterWidth: number): number {
  return gapFor(letterWidth) * 2;
}

/** Columns the lockup needs: the mark, its air, then the word. */
export function lockupWidth(letterWidth: number, word = WORD): number {
  return MARK_WIDTH + markGapFor(letterWidth) + wordmarkWidth(letterWidth, word);
}

/**
 * The widest letter size that fits, or null when even the smallest does not.
 *
 * What has to fit is the whole lockup — the mark stands in front of the word,
 * so it is part of the measurement, not decoration added afterwards.
 */
export function letterWidthFor(columns: number, word = WORD): number | null {
  for (const width of LETTER_WIDTHS) {
    if (lockupWidth(width, word) <= columns) return width;
  }
  return null;
}

/** One letter's bitmap, resampled to `width` columns. */
function sampledRows(character: string, width: number): boolean[][] {
  const master = MASTERS[character.toLowerCase()];
  return Array.from({ length: MASTER_HEIGHT }, (_, row) =>
    Array.from({ length: width }, (_, column) => {
      // Nearest-neighbour across the master's columns: at every size the strokes
      // stay two master columns wide, so nothing thins away to nothing.
      const source = Math.min(
        MASTER_WIDTH - 1,
        Math.floor((column * MASTER_WIDTH) / Math.max(1, width)),
      );
      return master?.[row]?.[source] === "#";
    }),
  );
}

/** Pair two half-pixel rows of a bitmap into one row of half blocks. */
function halfBlockRow(bitmap: readonly string[], row: number): string {
  const top = bitmap[row * 2] ?? "";
  const bottom = bitmap[row * 2 + 1] ?? "";
  let line = "";
  for (let column = 0; column < top.length; column += 1) {
    line += HALF_BLOCKS[(top[column] === "#" ? 1 : 0) + (bottom[column] === "#" ? 2 : 0)];
  }
  return line;
}

/**
 * The word at a given letter width, as `WORDMARK_ROWS` rows of half blocks.
 *
 * `withMark` puts the sprout in front of it, which is the launch lockup; the
 * letters alone are what the tests measure against.
 */
export function renderWordmark(letterWidth: number, word = WORD, withMark = false): string[] {
  const width = Math.max(1, Math.floor(letterWidth));
  const gap = gapFor(width);
  const markGap = " ".repeat(markGapFor(width));
  const letters = word.split("").map((character) => sampledRows(character, width));

  return Array.from({ length: WORDMARK_ROWS }, (_, row) => {
    let line = withMark ? `${halfBlockRow(BIG_MARK, row)}${markGap}` : "";
    letters.forEach((letter, index) => {
      if (index > 0) line += " ".repeat(gap);
      const top = letter[row * 2];
      const bottom = letter[row * 2 + 1];
      for (let column = 0; column < width; column += 1) {
        line += HALF_BLOCKS[(top[column] ? 1 : 0) + (bottom[column] ? 2 : 0)];
      }
    });
    return line;
  });
}

/** The mark alone, two rows, for the compact form. */
export function renderMiniMark(): string[] {
  return Array.from({ length: MINI_MARK.length / 2 }, (_, row) => halfBlockRow(MINI_MARK, row));
}

/** The biggest lockup that fits `columns`, or null when none does. */
export function fitWordmark(columns: number, word = WORD): string[] | null {
  const width = letterWidthFor(Math.max(0, Math.floor(columns)), word);
  return width === null ? null : renderWordmark(width, word, true);
}

/**
 * The soft shadow drawn under the letters.
 *
 * Light shade rather than a solid rule: it reads as the wordmark sitting on
 * something, not as a second, thinner wordmark.
 */
export function shadowRow(width: number): string {
  return "░".repeat(Math.max(0, Math.floor(width)));
}

/**
 * Where the sprout goes: over the last letter, which is what it grows out of.
 */
export function sproutColumn(letterWidth: number, word = WORD): number {
  const gap = gapFor(letterWidth);
  return (word.length - 1) * (letterWidth + gap) + Math.floor(letterWidth / 2);
}

/** Raw access to a letterform, for the tests that check it is still readable. */
export function masterOf(character: string): readonly string[] | undefined {
  return MASTERS[character.toLowerCase()];
}
