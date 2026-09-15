/**
 * The bottom HUD: what it says, and what it drops when the row runs out.
 */

import { describe, expect, it } from "vitest";

import {
  MAX_HUD_ROWS,
  SEPARATOR,
  buildHudSegments,
  WORKDIR_WIDTH,
  formatElapsed,
  formatTokens,
  layoutHud,
  rowWidth,
  shortenPath,
  type HudInput,
  type HudSegment,
} from "../src/layout/hud.js";

const base: HudInput = {
  status: "connected",
  version: "0.1.2",
  workdir: "/home/dev/src/snowpea",
  sessionId: "s-50dd0a11-2222",
  latestVersion: null,
  provider: "anthropic",
  model: "claude-sonnet-5",
  mode: "accept",
  usage: { inputTokens: 12_300, outputTokens: 1200 },
  sessionMs: 12 * 60 * 1000,
  daemonPid: 1234,
  daemonSummary: "will not exit: 1 job",
  pendingApprovals: 0,
};

const text = (rows: HudSegment[][]): string[] =>
  rows.map((row) => row.map((segment) => segment.text).join(SEPARATOR));

describe("shortenPath", () => {
  it("keeps the tail of a path too long for the HUD", () => {
    expect(shortenPath("/home/dev/src/snowpea", 40)).toBe("/home/dev/src/snowpea");
    const cut = shortenPath("/home/dev/very/deep/tree/of/directories/snowpea", WORKDIR_WIDTH);
    expect(cut).toHaveLength(WORKDIR_WIDTH);
    expect(cut.startsWith("…")).toBe(true);
    expect(cut.endsWith("snowpea")).toBe(true);
  });
});

describe("formatters", () => {
  it("abbreviates token counts", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(940)).toBe("940");
    expect(formatTokens(12_345)).toBe("12.3k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
  });

  it("shows seconds, then whole minutes, then hours", () => {
    expect(formatElapsed(42_000)).toBe("42s");
    expect(formatElapsed(12 * 60_000)).toBe("12m");
    expect(formatElapsed(95 * 60_000)).toBe("1h35m");
  });
});

describe("buildHudSegments", () => {
  it("reports the version alone when no update is waiting", () => {
    const segments = buildHudSegments(base);
    expect(segments.find((s) => s.key === "version")?.text).toBe("snowpea v0.1.2");
  });

  it("names the newer version and how to take it", () => {
    const segments = buildHudSegments({ ...base, latestVersion: "0.1.3" });
    expect(segments.find((s) => s.key === "version")?.text).toBe(
      "snowpea v0.1.2 → v0.1.3 (U to update)",
    );
  });

  it("draws the effort beside the model, and nothing when it is unknown", () => {
    expect(buildHudSegments(base).find((s) => s.key === "effort")).toBeUndefined();
    const segments = buildHudSegments({ ...base, effort: "high" });
    const keys = segments.map((s) => s.key);
    expect(segments.find((s) => s.key === "effort")?.text).toBe("⚙ high");
    expect(keys.indexOf("effort")).toBe(keys.indexOf("model") + 1);
  });

  it("carries model, mode, context, session and daemon", () => {
    const segments = buildHudSegments(base);
    const byKey = Object.fromEntries(segments.map((s) => [s.key, s.text]));
    expect(byKey.model).toBe("Model: anthropic/claude-sonnet-5");
    expect(byKey.mode).toBe("Mode: ACCEPT");
    expect(byKey.tokens).toBe("tok 12.3k↑/1.2k↓");
    expect(byKey.cwd).toBe("/home/dev/src/snowpea");
    expect(byKey.session).toBe("session: s-50dd0a · 12m");
    expect(byKey.daemon).toBe("daemon: pid 1234 · will not exit: 1 job");
    expect(byKey.status).toBe("● connected");
  });

  it("adds the Shift+Tab nudge to the mode segment while the hint is live", () => {
    const segments = buildHudSegments({ ...base, modeHint: true });
    expect(segments.find((s) => s.key === "mode")?.text).toBe("Mode: ACCEPT (⇧Tab)");
  });

  it("leaves out the daemon segment when system.info has not answered", () => {
    const segments = buildHudSegments({ ...base, daemonPid: null, daemonSummary: null });
    expect(segments.some((s) => s.key === "daemon")).toBe(false);
  });

  it("warns about unattended approvals", () => {
    expect(buildHudSegments({ ...base, pendingApprovals: 1 }).find((s) => s.key === "approvals")?.text).toBe(
      "⚠ 1 approval",
    );
    expect(buildHudSegments({ ...base, pendingApprovals: 2 }).find((s) => s.key === "approvals")?.text).toBe(
      "⚠ 2 approvals",
    );
  });

  it("names the running command, or says the turn is working", () => {
    expect(
      buildHudSegments({ ...base, runningCommand: "/ralph", turnActive: true }).find(
        (s) => s.key === "command",
      )?.text,
    ).toBe("▶ /ralph");
    // The indicator above the input says the turn is running; the HUD only
    // adds how to stop it.
    expect(
      buildHudSegments({ ...base, turnActive: true }).find((s) => s.key === "command")?.text,
    ).toBe("esc to interrupt");
  });

  it("falls back to the elapsed time alone before the session id is known", () => {
    const segments = buildHudSegments({ ...base, sessionId: null });
    expect(segments.find((s) => s.key === "session")?.text).toBe("session: 12m");
  });

  it("gives the toast the leftmost slot", () => {
    const segments = buildHudSegments({ ...base, toast: "mode: AUTO" });
    expect(segments[0].key).toBe("toast");
    expect(segments[0].text).toBe("mode: AUTO");
  });
});

describe("the context segment", () => {
  it("stays hidden until the daemon reports any context usage", () => {
    expect(buildHudSegments(base).some((s) => s.key === "ctx")).toBe(false);
  });

  it("shows how full the window is, and shouts when it is nearly full", () => {
    const at = (percent: number) =>
      buildHudSegments({
        ...base,
        context: { used: 1280 * percent, window: 128_000, percent, estimated: false },
      }).find((s) => s.key === "ctx");
    expect(at(10)?.text).toBe("ctx 10% (12.8k/128.0k)");
    expect(at(10)?.color).toBeUndefined();
    expect(at(75)?.color).toBe("yellow");
    expect(at(96)?.color).toBe("red");
  });

  it("says so when the window size is unknown", () => {
    const segment = buildHudSegments({
      ...base,
      context: { used: 12_300, window: null, percent: null, estimated: true },
    }).find((s) => s.key === "ctx");
    expect(segment?.text).toBe("ctx ~12.3k used");
  });

  it("counts the session's tools when the daemon listed them", () => {
    expect(buildHudSegments({ ...base, toolCount: 20 }).find((s) => s.key === "tools")?.text).toBe(
      "🔧 20 tools",
    );
    expect(buildHudSegments(base).some((s) => s.key === "tools")).toBe(false);
  });
});

describe("layoutHud", () => {
  it("uses one row when everything fits on one", () => {
    const rows = layoutHud(buildHudSegments(base), 200);
    expect(rows).toHaveLength(1);
    expect(rows[0].map((s) => s.key)).toEqual([
      "version",
      "cwd",
      "model",
      "mode",
      "tokens",
      "session",
      "daemon",
      "status",
    ]);
  });

  it("spills onto a second row rather than dropping segments", () => {
    const rows = layoutHud(buildHudSegments(base), 100);
    expect(rows.length).toBeGreaterThan(1);
    expect(rows).toHaveLength(2);
    for (const row of rows) expect(rowWidth(row)).toBeLessThanOrEqual(100);
  });

  it("never draws a row wider than the terminal", () => {
    for (const width of [20, 30, 45, 60, 80, 100, 120, 160]) {
      const rows = layoutHud(buildHudSegments({ ...base, pendingApprovals: 2 }), width);
      expect(rows.length).toBeLessThanOrEqual(MAX_HUD_ROWS);
      for (const row of rows) expect(rowWidth(row)).toBeLessThanOrEqual(width);
    }
  });

  it("drops the least important segments first", () => {
    const rows = layoutHud(buildHudSegments(base), 60);
    const keys = text(rows).join(SEPARATOR);
    // The daemon and the elapsed time are the first to go; the connection dot
    // and the mode are the last things standing.
    expect(keys).not.toContain("daemon: pid");
    expect(keys).toContain("Mode: ACCEPT");
    expect(keys).toContain("● connected");
  });

  it("keeps the approval warning when space is scarce", () => {
    const rows = layoutHud(buildHudSegments({ ...base, pendingApprovals: 3 }), 46);
    expect(text(rows).join(" ")).toContain("⚠ 3 approvals");
  });

  it("truncates the last survivor rather than drawing nothing", () => {
    const rows = layoutHud(
      [{ key: "only", text: "a-very-long-single-segment-that-cannot-fit", priority: 0 }],
      10,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0][0].text).toBe("a-very-lo…");
    expect(rows[0][0].text).toHaveLength(10);
  });
});
