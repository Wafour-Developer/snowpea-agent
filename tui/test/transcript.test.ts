import { describe, expect, it } from "vitest";

describe("leading blank lines", () => {
  it("puts the role glyph on the first line that says something", async () => {
    const { messageLines } = await import("../src/layout/transcript.js");
    const lines = messageLines(
      { id: "m1", role: "assistant", text: "\n\nMerged into AGENTS.md", streaming: false } as any,
      80,
    );
    expect(lines[0].segments.map((s) => s.text).join("")).toBe("◆ Merged into AGENTS.md");
    expect(lines).toHaveLength(1);
  });
});
