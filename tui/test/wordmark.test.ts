/**
 * The wordmark: it must fit the terminal, and every letter must still be the
 * letter it is meant to be.
 *
 * The legibility checks read the masters rather than the rendered blocks,
 * because the master is where a letter's identity lives — the three bars of the
 * E, the closed bowl of the P — and a change there is what would break it.
 */

import { describe, expect, it } from "vitest";

import {
  LETTER_WIDTHS,
  MASTER_HEIGHT,
  MASTER_WIDTH,
  WORD,
  WORDMARK_GLYPHS,
  WORDMARK_ROWS,
  fitWordmark,
  gapFor,
  letterWidthFor,
  masterOf,
  renderWordmark,
  shadowRow,
  sproutColumn,
  wordmarkWidth,
} from "../src/layout/wordmark.js";

/** Ink set in the master, as a grid of booleans. */
function grid(character: string): boolean[][] {
  const master = masterOf(character);
  expect(master, `no master for ${character}`).toBeDefined();
  return master!.map((row) => [...row].map((cell) => cell === "#"));
}

const rowFilled = (row: boolean[], from: number, to: number): boolean =>
  row.slice(from, to).every(Boolean);

const anyInk = (row: boolean[]): boolean => row.some(Boolean);

describe("the masters", () => {
  it("are all the same size", () => {
    for (const character of WORD) {
      const master = masterOf(character)!;
      expect(master).toHaveLength(MASTER_HEIGHT);
      for (const row of master) expect([...row]).toHaveLength(MASTER_WIDTH);
    }
  });

  it("give the E three bars, so it cannot read as a C", () => {
    const e = grid("e");
    expect(anyInk(e[0])).toBe(true); // top
    expect(rowFilled(e[5], 0, 8)).toBe(true); // middle
    expect(anyInk(e[13])).toBe(true); // bottom
    // The middle bar is a bar, not the whole width, or it would be a solid block.
    expect(e[5][9]).toBe(false);
  });

  it("closes the P's bowl and leaves it no leg", () => {
    const p = grid("p");
    expect(rowFilled(p[6], 0, 9)).toBe(true); // the bowl closes
    expect(p[13].slice(2).some(Boolean)).toBe(false); // nothing on the right below
    expect(p[13][0]).toBe(true); // the stem carries on down
  });

  it("keeps A open at the bottom and O closed", () => {
    const a = grid("a");
    const o = grid("o");
    expect(rowFilled(a[6], 0, 10)).toBe(true); // the crossbar
    expect(a[13][4]).toBe(false); // open between the legs
    expect(o[13][4]).toBe(true); // closed underneath
    expect(o[6][4]).toBe(false); // and hollow in the middle
  });

  it("draws the W with its notch and the N with its diagonal", () => {
    const w = grid("w");
    expect(w[0][4]).toBe(false); // open at the top
    expect(w[9][4]).toBe(true); // the middle rises
    const n = grid("n");
    // The diagonal walks left to right as it goes down.
    const firstInkAfterStem = (row: boolean[]): number => row.slice(2).indexOf(true) + 2;
    expect(firstInkAfterStem(n[4])).toBeLessThan(firstInkAfterStem(n[8]));
  });

  it("gives the S two bowls with a waist between them", () => {
    const s = grid("s");
    expect(anyInk(s[0])).toBe(true);
    expect(rowFilled(s[6], 1, 9)).toBe(true); // the waist
    expect(anyInk(s[13])).toBe(true);
    expect(s[4][9]).toBe(false); // open on the right above the waist
    expect(s[9][0]).toBe(false); // and on the left below it
  });
});

describe("fitting", () => {
  it("takes the widest size the terminal has room for", () => {
    for (const width of LETTER_WIDTHS) {
      expect(letterWidthFor(wordmarkWidth(width))).toBe(width);
      expect(letterWidthFor(wordmarkWidth(width) - 1) ?? 0).toBeLessThan(width);
    }
  });

  it("gives up rather than spilling past a narrow terminal", () => {
    const smallest = LETTER_WIDTHS[LETTER_WIDTHS.length - 1];
    expect(letterWidthFor(wordmarkWidth(smallest) - 1)).toBeNull();
    expect(fitWordmark(20)).toBeNull();
  });

  it("never draws wider than it was asked for", () => {
    for (const columns of [48, 60, 80, 100, 120, 160, 200]) {
      const rows = fitWordmark(columns);
      expect(rows, `nothing fitted at ${columns}`).not.toBeNull();
      for (const row of rows!) expect([...row].length).toBeLessThanOrEqual(columns);
    }
  });

  it("grows with the terminal instead of staying small", () => {
    const narrow = fitWordmark(80)!;
    const wide = fitWordmark(160)!;
    expect([...wide[0]].length).toBeGreaterThan([...narrow[0]].length);
  });

  it("is the same height at every size", () => {
    for (const width of LETTER_WIDTHS) {
      expect(renderWordmark(width)).toHaveLength(WORDMARK_ROWS);
    }
  });
});

describe("rendering", () => {
  it("uses half blocks and nothing else", () => {
    for (const width of LETTER_WIDTHS) {
      for (const row of renderWordmark(width)) {
        for (const glyph of row) expect(WORDMARK_GLYPHS).toContain(glyph);
      }
    }
  });

  it("uses the half blocks, not only whole ones", () => {
    // This is what makes the corners round rather than square.
    const drawn = renderWordmark(20).join("");
    expect(drawn).toContain("▀");
    expect(drawn).toContain("▄");
  });

  it("keeps every row the same length", () => {
    for (const width of LETTER_WIDTHS) {
      const rows = renderWordmark(width);
      const lengths = new Set(rows.map((row) => [...row].length));
      expect(lengths.size).toBe(1);
      expect([...lengths][0]).toBe(wordmarkWidth(width));
    }
  });

  it("separates the letters", () => {
    expect(gapFor(20)).toBeGreaterThanOrEqual(1);
    // Nothing of the last letter reaches the first column of the next one.
    const rows = renderWordmark(10);
    for (const row of rows) expect([...row][10]).toBe(" ");
  });
});

describe("the trimmings", () => {
  it("draws a shadow as wide as the letters", () => {
    expect([...shadowRow(12)]).toHaveLength(12);
    expect(shadowRow(3)).toBe("░░░");
    expect(shadowRow(-1)).toBe("");
  });

  it("puts the sprout over the last letter", () => {
    for (const width of LETTER_WIDTHS) {
      const column = sproutColumn(width);
      const lastLetterStart = (WORD.length - 1) * (width + gapFor(width));
      expect(column).toBeGreaterThanOrEqual(lastLetterStart);
      expect(column).toBeLessThan(lastLetterStart + width);
    }
  });
});
