import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";
import { messageLines, lineText, wrapLine, transcriptLines } from "../src/layout/transcript.js";
import { initialState } from "../src/state/store.js";
import { MessageView } from "../src/components/MessageStream.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const text = [
  "확인한 위치:",
  "| 위치 | 결과 |",
  "|---|---|",
  "| ~/.snowpea/agents/ | 디렉토리 자체가 없음 |",
  "| <workdir>/.snowpea/agents/ | 없음 (설정만 존재) |",
  "| **한글** | `a\\|b` |",
].join("\n");
const message = { id: "table", role: "assistant" as const, text, streaming: false };
// Independent oracle for these Korean/ASCII fixtures.
const cells = (value: string) => [...value].reduce((n, ch) => n + (/[가-힣]/u.test(ch) ? 2 : 1), 0);

describe("terminal markdown", () => {
  it.each([40, 80])("aligns Korean table borders and wraps long paths at %i columns", (width) => {
    const lines = messageLines(message, width).map(lineText);
    const rows = lines.filter(line => line.includes("│"));
    expect(rows.length).toBeGreaterThan(3);
    const borders = rows.map(row => [...row.matchAll(/│/g)].map(m => cells(row.slice(0, m.index))));
    for (const positions of borders) expect(positions).toEqual(borders[0]);
    for (const line of lines) expect(cells(line)).toBeLessThanOrEqual(width);
    expect(lines.join("\n")).toContain("a|b");
    expect(lines.join("\n")).not.toContain("**한글**");
  });

  it("supports unbordered Markdown tables and right/center alignment", () => {
    const lines = messageLines({ ...message, text: "Name | Count | State\n--- | ---: | :---:\nA | 1 | ok\nLong | 100 | yes" }, 60).map(lineText);
    expect(lines.join("\n")).toContain("│ A    │     1 │  ok   │");
    expect(lines.join("\n")).toContain("│ Long │   100 │  yes  │");
  });

  it("stacks columns instead of dropping values on a very narrow terminal", () => {
    const lines = messageLines({ ...message, text: "| Key | Value | Third |\n|---|---|---|\n| one | two | three |" }, 16).map(lineText);
    expect(lines.join("\n")).toContain("Key: one");
    expect(lines.join("\n")).toContain("Third: three");
    for (const line of lines) expect(cells(line)).toBeLessThanOrEqual(16);
  });

  it("leaves pipes inside a code fence alone", () => {
    const lines = messageLines({ ...message, text: "```\n| a | b |\n|---|---|\n```" }, 80);
    expect(lines.map(lineText).join("\n")).not.toContain("┌");
    expect(lines.map(lineText).join("\n")).toContain("|---|---|");
  });

  it("wraps Korean by terminal cells without cutting emoji or combining sequences", () => {
    const lines = wrapLine({ key: "wide", segments: [{ text: "한글한글", bold: true }] }, 4);
    expect(lines.map(lineText)).toEqual(["한글", "한글"]);
    expect(wrapLine({ key: "emoji", segments: [{ text: "👩‍💻é👩‍💻" }] }, 3).map(lineText))
      .toEqual(["👩‍💻é", "👩‍💻"]);
  });

  it("preserves table rows in the full-screen transcript projection", () => {
    const state = { ...initialState, messages: [message], timeline: [{ kind: "message" as const, id: message.id }] };
    const expected = messageLines(message, 40).map(lineText);
    expect(transcriptLines(state, 40).map(lineText)).toEqual([...expected, ""]);
  });

  it("uses the same table projection in the inline Ink view", async () => {
    const stdout = fakeStdout(80, 30);
    const instance = render(<MessageView message={message} width={78} />, {
      stdin: fakeStdin(), stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
    });
    try {
      await sleep(60);
      expect(stdout.text()).toContain("┌");
      expect(stdout.text()).toContain("디렉토리 자체가 없음");
      expect(stdout.text()).not.toContain("|---|---|");
    } finally { instance.unmount(); }
  });
});
