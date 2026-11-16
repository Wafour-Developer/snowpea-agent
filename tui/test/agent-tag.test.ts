import { describe, expect, it } from "vitest";

import {
  BROWSER_COLOR,
  EXTERNAL_COLOR,
  agentOfExternal,
  agentOfKind,
  agentOfSession,
  filterChoices,
  filterLabel,
  kindsPresent,
  legendLine,
  nextFilter,
  tagCell,
  tagColumnWidth,
} from "../src/state/agent-tag.js";
import { buildHudSegments, layoutHud, type HudInput } from "../src/layout/hud.js";
import { textWidth } from "../src/layout/text-width.js";

describe("which agent a session belongs to", () => {
  it("leaves a plain snowpea session untagged, whatever else it carries", () => {
    for (const row of [undefined, null, {}, { hostToolsFrom: null }, { hostToolsFrom: "", browserProvider: null }, { originSurface: "tui" }]) {
      const identity = agentOfSession(row);
      expect(identity.kind).toBe("snowpea");
      expect(identity.tag).toBe("");
      expect(identity.color).toBeUndefined();
    }
  });

  it("tags a browser session by hostToolsFrom, browserProvider or a browser origin surface", () => {
    for (const row of [{ hostToolsFrom: "profile-1" }, { browserProvider: "host" }, { browserProvider: "local" }, { originSurface: "browser" }]) {
      const identity = agentOfSession(row);
      expect(identity).toMatchObject({ kind: "web", tag: "[web]", label: "Browser", color: BROWSER_COLOR });
    }
  });

  it("gives each external CLI its tag in the external colour", () => {
    expect(agentOfExternal("claude")).toMatchObject({ kind: "claude", tag: "[cc]", label: "Claude Code", color: EXTERNAL_COLOR });
    expect(agentOfExternal("codex")).toMatchObject({ kind: "codex", tag: "[cx]", label: "Codex" });
    expect(agentOfExternal("agy")).toMatchObject({ kind: "agy", tag: "[ag]", label: "agy" });
    expect(agentOfExternal("cursor")).toMatchObject({ kind: "cursor", tag: "[cu]", label: "Cursor" });
  });

  it("calls an unknown or missing CLI id [ext] and never guesses a known one", () => {
    expect(agentOfExternal("aider")).toMatchObject({ kind: "ext", tag: "[ext]", label: "aider", color: EXTERNAL_COLOR });
    expect(agentOfExternal(undefined)).toMatchObject({ kind: "ext", tag: "[ext]" });
    expect(agentOfExternal(null).kind).toBe("ext");
    expect(agentOfExternal("Claude").kind).toBe("ext");
  });

  it("keeps the two colours apart from each other and from the status colours", () => {
    expect(EXTERNAL_COLOR).not.toBe(BROWSER_COLOR);
    for (const status of ["green", "yellow", "red", "cyan", "magenta", "blue"]) {
      expect(EXTERNAL_COLOR).not.toBe(status);
      expect(BROWSER_COLOR).not.toBe(status);
    }
  });

  it("uses only ASCII, one cell per character, and a different text per kind", () => {
    const tags = (["claude", "codex", "agy", "cursor", "ext", "web"] as const).map((kind) => agentOfKind(kind).tag);
    expect(new Set(tags).size).toBe(tags.length);
    for (const tag of tags) {
      expect(tag).toMatch(/^\[[a-z]+\]$/);
      expect(textWidth(tag)).toBe(tag.length);
    }
  });
});

describe("the tag column", () => {
  const sp = agentOfSession(null);
  const cc = agentOfExternal("claude");
  const web = agentOfSession({ hostToolsFrom: "p" });

  it("is not rendered when every session is snowpea", () => {
    expect(tagColumnWidth([sp, sp])).toBe(0);
    expect(legendLine([sp, sp], 60)).toBeNull();
  });

  it("is as wide as the widest tag and pads the others, blank for snowpea", () => {
    expect(tagColumnWidth([sp, cc, web])).toBe(5);
    expect(tagCell(sp, 5)).toBe("     ");
    expect(tagCell(cc, 5)).toBe("[cc] ");
    expect(textWidth(tagCell(web, 5))).toBe(5);
  });
});

describe("the legend and the filter", () => {
  const sp = agentOfSession(null);
  const cc = agentOfExternal("claude");
  const cx = agentOfExternal("codex");
  const web = agentOfSession({ browserProvider: "host" });

  it("lists only the tagged kinds present, in a fixed order", () => {
    expect(legendLine([web, sp, cx, cc], 80)).toBe("[cc] Claude Code  [cx] Codex  [web] Browser");
    expect(legendLine([cx], 80)).toBe("[cx] Codex");
    expect(legendLine([agentOfExternal("zzz")], 80)).toBe("[ext] Other CLI");
  });

  it("cuts a long legend with an ellipsis to the width", () => {
    const line = legendLine([cc, cx, web], 20) ?? "";
    expect(textWidth(line)).toBeLessThanOrEqual(20);
    expect(line.endsWith("…")).toBe(true);
  });

  it("cycles all, snowpea, then each kind present, and wraps", () => {
    const items = [sp, cc, web, cx];
    expect(kindsPresent(items)).toEqual(["snowpea", "claude", "codex", "web"]);
    expect(filterChoices(items)).toEqual(["all", "snowpea", "claude", "codex", "web"]);
    let filter: ReturnType<typeof nextFilter> = "all";
    const seen: string[] = [];
    for (let i = 0; i < 5; i += 1) {
      filter = nextFilter(filter, items);
      seen.push(filter);
    }
    expect(seen).toEqual(["snowpea", "claude", "codex", "web", "all"]);
  });

  it("offers no filter when only one kind is present, and skips snowpea when there is none", () => {
    expect(filterChoices([sp, sp])).toEqual(["all"]);
    expect(filterChoices([cc, cc])).toEqual(["all"]);
    expect(filterChoices([cc, cx])).toEqual(["all", "claude", "codex"]);
    expect(nextFilter("web", [sp, cc])).toBe("all");
  });

  it("names a filter", () => {
    expect(filterLabel("all")).toBe("all");
    expect(filterLabel("claude")).toBe("claude code");
    expect(filterLabel("web")).toBe("browser");
    expect(filterLabel("ext")).toBe("other CLI");
  });
});

describe("the status line", () => {
  const base: HudInput = {
    status: "connected",
    version: "0.2.88",
    mode: "accept",
    usage: { inputTokens: 0, outputTokens: 0 },
    sessionMs: 5000,
    pendingApprovals: 0,
    provider: null,
    model: null,
  };

  it("shows the agent tag of an external or browser session and nothing for a snowpea one", () => {
    const plain = buildHudSegments(base).map((s) => s.key);
    expect(plain).not.toContain("agent");
    const cc = agentOfExternal("claude");
    const segments = buildHudSegments({ ...base, agentTag: cc.tag, agentColor: cc.color });
    const seg = segments.find((s) => s.key === "agent");
    expect(seg).toMatchObject({ text: "[cc]", color: EXTERNAL_COLOR, bold: true });
  });

  it("keeps the tag when a 60-column row has to drop segments", () => {
    const segments = buildHudSegments({ ...base, workdir: "/home/me/a/very/long/project/folder", sessionId: "abcdef0123", agentTag: "[web]", agentColor: BROWSER_COLOR, daemonPid: 1234 });
    const rows = layoutHud(segments, 60);
    expect(rows.flat().map((s) => s.key)).toContain("agent");
  });
});
