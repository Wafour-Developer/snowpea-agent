import { describe, expect, it } from "vitest";

import { lineText, toolCallLines } from "../src/layout/transcript.js";
import type { ToolCallEntry } from "../src/state/store.js";

describe("failed tool calls", () => {
  it("shows the error in scrollback without Ctrl+O", () => {
    const call: ToolCallEntry = {
      callId: "c-err",
      name: "patch",
      args: { path: "x.css", new: ".shot {}" },
      state: "error",
      error: "stale_file: read_file the whole file before writing it",
    };
    const lines = toolCallLines(call, false).map(lineText);
    expect(lines.some((line) => line.includes("stale_file:"))).toBe(true);
    expect(lines.some((line) => line.includes("(1 lines)"))).toBe(false);
  });
});

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
