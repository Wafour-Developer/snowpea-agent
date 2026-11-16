import { describe, expect, it } from "vitest";

import { textWidth } from "../src/layout/text-width.js";
import { EXTERNAL_COLOR } from "../src/state/agent-tag.js";
import { initialJarvis, panelLines, sessionLabel, tagSegments, type JarvisState } from "../src/state/jarvis.js";

const NOW = Date.parse("2026-11-16T12:00:00Z");

const rows: any[] = [
  { sessionId: "s-1", project: "/work/snowpea", title: "fix auth", status: "running" },
  { sessionId: null, project: "/work/웹서비스", title: "배포 스크립트 점검과 인증 모듈 리팩터링", status: "idle", external: { cli: "claude", id: "abcdef0123" }, host: "terminal" },
  { sessionId: null, project: "/work/docs", title: "", status: "idle", external: { cli: "aider", id: "zzzzzzzz99" }, host: "none" },
];

const state: JarvisState = {
  ...initialJarvis,
  status: { enabled: true, running: true, folder: "/j", delegationDefault: "staged", sessionId: "j", model: "fast" } as any,
  rows,
  waiting: [
    { requestId: "a-1", kind: "approval", sessionId: "ext:codex:x1", external: { cli: "codex", id: "x1" }, label: "[cx] docs", summary: "shell: git push", request: {} as any },
  ],
};

describe("agent tags in the Jarvis panel", () => {
  it("names external sessions by tag and leaves snowpea sessions untagged", () => {
    expect(sessionLabel(rows[0])).toBe("snowpea · fix auth");
    expect(sessionLabel(rows[1])).toBe("[cc] 웹서비스 · 배포 스크립트 점검과 인증 모듈 리팩터링");
    expect(sessionLabel(rows[2])).toBe("[ext] docs");
  });

  it("draws the tag in the external colour as its own segment, text unchanged", () => {
    const segments = tagSegments("   2 ◯ [cc] project · title    idle", { cli: "claude", id: "x" }, "yellow");
    expect(segments.map((s) => s.text).join("")).toBe("   2 ◯ [cc] project · title    idle");
    expect(segments.find((s) => s.text === "[cc]")).toMatchObject({ color: EXTERNAL_COLOR, bold: true });
    expect(segments[0].color).toBe("yellow");
    expect(tagSegments("plain row", null, "red")).toEqual([{ text: "plain row", color: "red" }]);
  });

  it("tags the session rows and the waiting-for-you list, and keeps both inside 60 cells", () => {
    const lines = panelLines(state, 60, 30, NOW);
    const text = lines.map((line) => line.segments.map((s) => s.text).join(""));
    expect(text.find((line) => line.includes("fix auth"))).not.toMatch(/\[/);
    expect(text.find((line) => line.includes("웹서비스"))).toMatch(/ \[cc\] 웹서비스/);
    expect(text.find((line) => line.includes("docs") && line.includes("[ext]"))).toBeTruthy();
    expect(text.find((line) => line.includes("shell: git push"))).toMatch(/! \[cx\] docs/);
    const tagged = lines.flatMap((line) => line.segments).filter((s) => /^\[(cc|cx|ext)\]$/.test(s.text));
    expect(tagged.length).toBeGreaterThanOrEqual(3);
    for (const segment of tagged) expect(segment.color).toBe(EXTERNAL_COLOR);
    for (const line of text) expect(textWidth(line), line).toBeLessThanOrEqual(60);
  });
});
