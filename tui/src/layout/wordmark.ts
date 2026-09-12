/**
 * The snowpea wordmark, drawn as big as the terminal allows.
 *
 * Each letter is a 5×6 bitmap scaled horizontally, rather than seven
 * hand-drawn pictures per size. That is what keeps the letters legible at every
 * width: the E always has its three bars, the P always closes its bowl, and a
 * wider terminal gets the same shapes in thicker strokes instead of a different
 * font with different mistakes. A terminal cell is about twice as tall as it is
 * wide, so the horizontal scale is what varies; the six rows stay six rows.
 *
 * Pure, so `test/wordmark.test.ts` can check both the shapes and the fitting.
 */

/** Letter shapes, one string per row, `#` where the ink goes. */
const GLYPHS: Record<string, readonly string[]> = {
  s: ["#####", "#....", "#####", "....#", "....#", "#####"],
  n: ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
  o: ["#####", "#...#", "#...#", "#...#", "#...#", "#####"],
  w: ["#...#", "#...#", "#...#", "#.#.#", "##.##", "#...#"],
  p: ["#####", "#...#", "#####", "#....", "#....", "#...."],
  e: ["#####", "#....", "#####", "#....", "#....", "#####"],
  a: ["#####", "#...#", "#####", "#...#", "#...#", "#...#"],
};

export const WORD = "snowpea";

/** Rows every size occupies. */
export const WORDMARK_ROWS = 6;

/** Cells one bitmap column is wide, per size. */
export const SCALES: readonly number[] = [4, 3, 2, 1];

/** Ink and gap, so a wide terminal gets thicker strokes rather than a new font. */
const INK = "█";

/** Columns the wordmark needs at this scale, gaps between letters included. */
export function wordmarkWidth(scale: number, word = WORD): number {
  const letter = 5 * scale;
  return word.length * letter + (word.length - 1) * scale;
}

/** The largest scale that fits `columns`, or null when even the smallest does not. */
export function scaleFor(columns: number, word = WORD): number | null {
  for (const scale of SCALES) {
    if (wordmarkWidth(scale, word) <= columns) return scale;
  }
  return null;
}

/**
 * The word at one scale, as six rows of block characters.
 *
 * Unknown characters are drawn as a blank letter rather than throwing: the
 * wordmark is decoration, and a typo in a caller must not take the screen down.
 */
export function renderWordmark(scale: number, word = WORD): string[] {
  const safeScale = Math.max(1, Math.floor(scale));
  const rows: string[] = [];
  for (let row = 0; row < WORDMARK_ROWS; row += 1) {
    let line = "";
    word.split("").forEach((character, index) => {
      if (index > 0) line += " ".repeat(safeScale);
      const glyph = GLYPHS[character.toLowerCase()];
      const pattern = glyph?.[row] ?? ".....";
      for (const pixel of pattern) line += (pixel === "#" ? INK : " ").repeat(safeScale);
    });
    rows.push(line);
  }
  return rows;
}

/**
 * The biggest wordmark that fits, or null when nothing does.
 *
 * The caller falls back to its one-line form below that, which is the only
 * thing that still reads on a very narrow terminal.
 */
export function fitWordmark(columns: number, word = WORD): string[] | null {
  const scale = scaleFor(Math.max(0, Math.floor(columns)), word);
  return scale === null ? null : renderWordmark(scale, word);
}
