/**
 * The context warning, the summary line and the compaction divider.
 */

import { describe, expect, it } from "vitest";

import {
  CONTEXT_ALERT_PERCENT,
  CONTEXT_CRITICAL_PERCENT,
  CONTEXT_WARN_PERCENT,
  compactionDivider,
  contextColor,
  contextSegment,
  contextWarning,
  summaryLine,
} from "../src/layout/bottom.js";
import type { ContextUsage } from "../src/state/store.js";

const usage = (percent: number): ContextUsage => ({
  used: Math.round(1280 * percent),
  window: 128_000,
  percent,
  estimated: false,
});

describe("contextColor", () => {
  it("stays quiet until the window starts filling", () => {
    expect(contextColor(null)).toBeUndefined();
    expect(contextColor(10)).toBeUndefined();
    expect(contextColor(CONTEXT_WARN_PERCENT - 1)).toBeUndefined();
    expect(contextColor(CONTEXT_WARN_PERCENT)).toBe("yellow");
    expect(contextColor(CONTEXT_CRITICAL_PERCENT)).toBe("red");
  });
});

describe("contextSegment", () => {
  it("says nothing before the daemon has reported anything", () => {
    expect(contextSegment(null)).toBeNull();
  });

  it("leads with the percentage, then the raw numbers", () => {
    expect(contextSegment(usage(34))?.text).toBe("ctx 34% (43.5k/128.0k)");
  });

  it("marks an estimate as one", () => {
    expect(contextSegment({ ...usage(10), estimated: true })?.text).toBe(
      "ctx 10% (~12.8k/128.0k)",
    );
  });

  it("falls back to the raw count when the window is unknown", () => {
    expect(
      contextSegment({ used: 12_300, window: null, percent: null, estimated: false })?.text,
    ).toBe("ctx 12.3k used");
  });
});

describe("contextWarning", () => {
  it("stays away until the window is four fifths full", () => {
    expect(contextWarning(null)).toBeNull();
    expect(contextWarning(usage(50))).toBeNull();
    expect(contextWarning(usage(CONTEXT_ALERT_PERCENT - 1))).toBeNull();
    expect(contextWarning(usage(CONTEXT_ALERT_PERCENT))?.color).toBe("yellow");
  });

  it("says how full it is and what to do about it", () => {
    const critical = contextWarning(usage(85));
    expect(critical?.text).toBe("[!!] context 85% — /compact to free space");
    expect(critical?.color).toBe("red");
    expect(contextWarning(usage(81))?.color).toBe("yellow");
  });

  it("stays away when the window size is unknown", () => {
    expect(contextWarning({ used: 90_000, window: null, percent: null, estimated: false })).toBeNull();
  });
});

describe("summaryLine", () => {
  it("leads with the mode chip", () => {
    const auto = summaryLine({ mode: "auto", shells: 0, agents: 0 });
    expect(auto.text).toBe("⏵⏵ auto mode on · ⇧Tab change mode · Ctrl+P plan");
    expect(auto.color).toBe("magenta");
    expect(summaryLine({ mode: "accept", shells: 0, agents: 0 }).text).toContain(
      "▶ accept mode · ⇧Tab change mode",
    );
    expect(summaryLine({ mode: "plan", shells: 0, agents: 0 }).text).toContain(
      "⏸ plan mode · ⇧Tab change mode",
    );
  });

  it("counts the shells and the agents that are actually running", () => {
    expect(summaryLine({ mode: "auto", shells: 3, agents: 1 }).text).toBe(
      "⏵⏵ auto mode on · ⇧Tab change mode · Ctrl+P plan · 3 shells · ← 1 agent",
    );
    expect(summaryLine({ mode: "auto", shells: 1, agents: 2 }).text).toBe(
      "⏵⏵ auto mode on · ⇧Tab change mode · Ctrl+P plan · 1 shell · ← 2 agents",
    );
  });
});

describe("compactionDivider", () => {
  it("fills the width around the label", () => {
    const line = compactionDivider(12_300, 2100, 60);
    expect([...line]).toHaveLength(60);
    expect(line).toContain("compacted (12.3k → 2.1k tokens)");
    expect(line.startsWith("─")).toBe(true);
    expect(line.endsWith("─")).toBe(true);
  });

  it("never spills past a narrow terminal", () => {
    expect(compactionDivider(12_300, 2100, 10)).toContain("compacted");
  });
});
