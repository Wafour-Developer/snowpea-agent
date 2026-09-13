/**
 * The snowpea mark, as half-block bitmaps.
 *
 * Generated from the brand SVG by `tui/scripts/generate-mark.mjs` — do not
 * edit by hand; re-run the script when the mark changes. Each row is one
 * half-pixel line, two of them to a terminal row, exactly like the letters in
 * `layout/wordmark.ts`.
 *
 * Source: /mnt/data/work/mediagen/snowpea-site/shared/brand/mark.svg
 * Shape: a monoline sprout — a stem on the diagonal, one leaf, an open ring
 * for the pea and WAFOUR's counter dot as a second pea.
 */

/** 16 columns × 14 half-pixels, 7 terminal rows. */
export const BIG_MARK: readonly string[] = [
  "................",
  ".........####...",
  "........######..",
  "........##...##.",
  "........##..##..",
  "..####..######..",
  "..##########....",
  "..#######.......",
  "...#####...##...",
  ".....###...###..",
  "......##....#...",
  ".....###........",
  "......#.........",
  "................",
];

/** 6 columns × 4 half-pixels, 2 terminal rows. */
export const MINI_MARK: readonly string[] = [
  ".####.",
  "##..##",
  "##..##",
  ".####.",
];
