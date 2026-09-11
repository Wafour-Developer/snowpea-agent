import { describe, expect, it } from "vitest";

import { clip, summarise } from "../src/components/SubagentTree.js";
import type { SubagentEntry } from "../src/state/store.js";

function entry(agentId: string, status: SubagentEntry["status"]): SubagentEntry {
  return {
    agentId,
    name: "",
    task: `task for ${agentId}`,
    status,
    lastText: "",
    summary: "",
    sessionId: null,
    inputTokens: 0,
    outputTokens: 0,
  };
}

describe("SubagentTree helpers", () => {
  it("summarises the statuses in a fixed order", () => {
    const summary = summarise([
      entry("a-1", "done"),
      entry("a-2", "running"),
      entry("a-3", "running"),
      entry("a-4", "queued"),
    ]);
    expect(summary).toBe("2 running · 1 queued · 1 done");
  });

  it("clips long task lines and flattens newlines", () => {
    expect(clip("a\n  b   c", 40)).toBe("a b c");
    expect(clip("x".repeat(20), 10)).toBe(`${"x".repeat(9)}…`);
  });
});
