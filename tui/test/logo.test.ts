/**
 * The wordmark: how tall it is, and when it collapses to one line.
 */

import { describe, expect, it } from "vitest";

import {
  LOGO_COLLAPSED_ROWS,
  LOGO_COLLAPSE_ROWS,
  LOGO_EXPANDED_ROWS,
  TAGLINE,
  WORDMARK,
  collapsedLine,
  isCollapsed,
  logoRows,
} from "../src/components/Logo.js";

describe("Logo", () => {
  it("draws the block wordmark on a normal terminal", () => {
    expect(isCollapsed(40)).toBe(false);
    expect(logoRows(40)).toBe(LOGO_EXPANDED_ROWS);
    expect(logoRows(LOGO_COLLAPSE_ROWS)).toBe(LOGO_EXPANDED_ROWS);
  });

  it("collapses to a single line below 24 rows", () => {
    expect(isCollapsed(LOGO_COLLAPSE_ROWS - 1)).toBe(true);
    expect(logoRows(23)).toBe(LOGO_COLLAPSED_ROWS);
    expect(logoRows(10)).toBe(LOGO_COLLAPSED_ROWS);
  });

  it("keeps both wordmark rows the same width and under 60 columns", () => {
    expect([...WORDMARK[0]]).toHaveLength([...WORDMARK[1]].length);
    for (const row of WORDMARK) expect([...row].length).toBeLessThanOrEqual(60);
  });

  it("says what snowpea is on the collapsed line", () => {
    const line = collapsedLine("0.1.2");
    expect(line).toContain("snowpea v0.1.2");
    expect(line).toContain(TAGLINE);
  });
});
