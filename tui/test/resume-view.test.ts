import { describe, expect, it } from "vitest";

import { resumeRows, resumeView, type SessionRecord } from "../src/state/history.js";

const row = (sessionId: string, workdir: string, at: number): SessionRecord => ({
  sessionId,
  workdir,
  firstPrompt: `prompt ${sessionId}`,
  at,
  kind: "chat",
});

const HERE = "/home/me/work/test";
const ALL = resumeRows(
  [
    row("s-tmp", "/tmp", 300),
    row("s-here", HERE, 200),
    row("s-bench1", "/bench/a", 100),
    row("s-bench2", "/bench/b", 50),
  ],
  HERE,
);

describe("what /resume shows", () => {
  it("opens on this directory's sessions only and counts the rest", () => {
    const view = resumeView(ALL, HERE, false);
    expect(view.rows.map((r) => r.sessionId)).toEqual(["s-here"]);
    expect(view.hiddenElsewhere).toBe(3);
  });

  it("reveals everything on request, this directory still first", () => {
    const view = resumeView(ALL, HERE, true);
    expect(view.rows.map((r) => r.sessionId)).toEqual(["s-here", "s-tmp", "s-bench1", "s-bench2"]);
    expect(view.hiddenElsewhere).toBe(0);
  });

  it("shows everything at once when nothing was saved for this directory", () => {
    const view = resumeView(ALL, "/somewhere/new", false);
    expect(view.rows).toHaveLength(4);
    expect(view.hiddenElsewhere).toBe(0);
  });
});
