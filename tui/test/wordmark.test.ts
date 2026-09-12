/**
 * The launch wordmark: it must fit, and every letter must be readable.
 */

import { describe, expect, it } from "vitest";

import {
  SCALES,
  WORDMARK_ROWS,
  fitWordmark,
  renderWordmark,
  scaleFor,
  wordmarkWidth,
} from "../src/layout/wordmark.js";

describe("fitting", () => {
  it("takes the largest size the terminal has room for", () => {
    for (const scale of SCALES) {
      const width = wordmarkWidth(scale);
      expect(scaleFor(width)).toBe(scale);
      // One column short and it drops to the next size down, or gives up.
      expect(scaleFor(width - 1) ?? 0).toBeLessThan(scale);
    }
  });

  it("gives up rather than spilling past a narrow terminal", () => {
    expect(scaleFor(wordmarkWidth(1) - 1)).toBeNull();
    expect(fitWordmark(20)).toBeNull();
  });

  it("never draws wider than it was asked for", () => {
    for (const columns of [41, 60, 83, 100, 124, 160, 200]) {
      const rows = fitWordmark(columns);
      expect(rows).not.toBeNull();
      for (const row of rows!) expect(row.length).toBeLessThanOrEqual(columns);
    }
  });

  it("is the same height at every size", () => {
    for (const scale of SCALES) expect(renderWordmark(scale)).toHaveLength(WORDMARK_ROWS);
  });
});

describe("the letters", () => {
  /** The columns one letter occupies at scale 1, without the gap. */
  const letter = (rows: string[], index: number): string[] =>
    rows.map((row) => row.slice(index * 6, index * 6 + 5));

  const rows = renderWordmark(1);

  it("gives the E three bars, so it cannot read as a C", () => {
    const e = letter(rows, 5);
    expect(e[0]).toBe("█████");
    expect(e[2]).toBe("█████");
    expect(e[5]).toBe("█████");
  });

  it("closes the P's bowl and leaves it no leg", () => {
    const p = letter(rows, 4);
    expect(p[2]).toBe("█████");
    expect(p[5]).toBe("█    ");
  });

  it("keeps A open at the bottom, unlike O", () => {
    const a = letter(rows, 6);
    const o = letter(rows, 2);
    expect(a[5]).toBe("█   █");
    expect(o[5]).toBe("█████");
  });

  it("draws the W with its middle notch", () => {
    const w = letter(rows, 3);
    expect(w[3]).toBe("█ █ █");
    expect(w[4]).toBe("██ ██");
  });

  it("draws the N with its diagonal", () => {
    const n = letter(rows, 1);
    expect(n[1]).toBe("██  █");
    expect(n[3]).toBe("█  ██");
  });

  it("scales by thickening strokes, not by changing shapes", () => {
    const one = renderWordmark(1);
    const two = renderWordmark(2);
    expect(two[0].startsWith("██")).toBe(true);
    expect(wordmarkWidth(2)).toBe(wordmarkWidth(1) * 2);
    expect(one).toHaveLength(two.length);
  });
});
