/**
 * The HUD, the mode chip and the working line count one cell per glyph; Ink
 * measures with `string-width`. A glyph the two disagree on (⚠ ⏸ ▶ ✳ ⚙ are two
 * cells to Ink) pushes a row past the terminal's edge at 60 columns.
 */

import stringWidth from "string-width";
import { describe, expect, it } from "vitest";

import { summaryLine } from "../src/layout/bottom.js";
import { SEPARATOR, buildHudSegments, layoutHud, rowWidth, type HudInput } from "../src/layout/hud.js";
import { textWidth } from "../src/layout/text-width.js";
import { PAUSED_GLYPH, SPINNER_FRAMES, workingLine } from "../src/state/working.js";

const base: HudInput = {
  status: "connected",
  version: "0.2.88",
  workdir: "/home/dev/src/snowpea",
  sessionId: "s-50dd0a11-2222",
  latestVersion: null,
  provider: "anthropic",
  model: "claude-sonnet-5",
  mode: "plan",
  usage: { inputTokens: 12_300, outputTokens: 1200 },
  sessionMs: 12 * 60 * 1000,
  daemonPid: 1234,
  daemonSummary: "idle",
  pendingApprovals: 2,
  externalWarning: "UNRESTRICTED — snowpea does not control what this Claude Code session runs",
  jarvis: "Jarvis deciding · full",
  effort: "high",
  runningCommand: "/ralph",
};

describe("glyphs Ink and the layout agree on", () => {
  it("measures every HUD segment the same way at any width", () => {
    for (const segment of buildHudSegments(base)) {
      expect(stringWidth(segment.text), segment.text).toBe(segment.text.length);
      expect(stringWidth(segment.text), segment.text).toBe(textWidth(segment.text));
    }
  });

  it("keeps the approvals and external-warning segments inside 60 columns, as Ink counts", () => {
    for (const width of [60, 46, 40]) {
      const rows = layoutHud(buildHudSegments(base), width);
      for (const row of rows) {
        expect(rowWidth(row)).toBeLessThanOrEqual(width);
        const drawn = row.map((segment) => segment.text).join(SEPARATOR);
        expect(stringWidth(drawn), drawn).toBeLessThanOrEqual(width);
        expect(stringWidth(drawn)).toBe(rowWidth(row));
      }
    }
    const kept = layoutHud(buildHudSegments({ ...base, externalWarning: "UNRESTRICTED", pendingApprovals: 3 }), 60)
      .flat()
      .map((segment) => segment.key);
    expect(kept).toContain("approvals");
  });

  it("draws every mode chip and the working line with glyphs one cell wide to Ink", () => {
    for (const mode of ["auto", "accept", "plan"] as const) {
      const line = summaryLine({ mode, shells: 0, agents: 0 }).text;
      expect(stringWidth(line), line).toBe(textWidth(line));
      expect(textWidth(line)).toBeLessThanOrEqual(60);
    }
    for (const glyph of [PAUSED_GLYPH, ...SPINNER_FRAMES]) expect(stringWidth(glyph), glyph).toBe(1);
    const waiting = workingLine({ phase: { kind: "approval" } } as never);
    expect(stringWidth(waiting)).toBe(textWidth(waiting));
  });
});
