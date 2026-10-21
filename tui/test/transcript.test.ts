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

describe("a finished call that streamed output", () => {
  it("keeps as many lines as it showed while it ran, so nothing below moves", () => {
    const running: ToolCallEntry = {
      callId: "c-sh",
      name: "shell",
      args: { command: "cat big.txt" },
      state: "running",
      progress: Array.from({ length: 8 }, (_, i) => ({ text: `line ${i + 13}`, stream: "stdout" as const })),
    };
    const done: ToolCallEntry = {
      ...running,
      state: "ok",
      output: Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join("\n") + "\n",
    };
    const before = toolCallLines(running, false).map(lineText);
    const after = toolCallLines(done, false).map(lineText);
    expect(after).toHaveLength(before.length);
    expect(after[0]).toContain("(last 8 of 20 lines)");
    expect(after[after.length - 1]).toContain("line 20");
  });

  it("folds a call that never streamed, as before", () => {
    const call: ToolCallEntry = {
      callId: "c-rf",
      name: "read_file",
      args: { path: "a.ts" },
      state: "ok",
      output: "a\nb\nc",
    };
    const lines = toolCallLines(call, false).map(lineText);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("(3 lines)");
  });
});
