/**
 * The diffing frame writer: which rows it repaints, and what it refuses to
 * touch.
 */

import { describe, expect, it } from "vitest";

import {
  ERASE_TO_END,
  createFrameWriter,
  cursorTo,
  diffFrame,
  splitFrame,
} from "../src/layout/frame.js";

/** `eraseLines(n)`, exactly as `ansi-escapes` builds it for Ink. */
function eraseLines(count: number): string {
  let out = "";
  for (let i = 0; i < count; i += 1) out += "\u001B[2K" + (i < count - 1 ? "\u001B[1A" : "");
  return count > 0 ? `${out}\u001B[G` : "";
}

/** One chunk as Ink's `log-update` writes it. */
function chunk(rows: string[], previousRows: number): string {
  return `${eraseLines(previousRows)}${rows.join("\n")}\n`;
}

describe("splitFrame", () => {
  it("reads the first frame, which has nothing to erase", () => {
    expect(splitFrame("a\nb\n", true)).toEqual(["a", "b"]);
  });

  it("strips the erase prefix from later frames", () => {
    expect(splitFrame(chunk(["a", "b"], 3), false)).toEqual(["a", "b"]);
  });

  it("refuses a chunk that is not a frame", () => {
    expect(splitFrame("some console output\n", false)).toBeNull();
    expect(splitFrame("\u001B[2J\u001B[3J\u001B[Hframe\n", false)).toBeNull();
    expect(splitFrame("", true)).toBeNull();
  });
});

describe("diffFrame", () => {
  it("paints every row of the first frame", () => {
    expect(diffFrame(null, ["a", "b"])).toBe(
      cursorTo(1) + "a" + ERASE_TO_END + cursorTo(2) + "b" + ERASE_TO_END,
    );
  });

  it("touches only the rows that changed", () => {
    const previous = ["one", "two", "three"];
    expect(diffFrame(previous, ["one", "TWO", "three"])).toBe(
      cursorTo(2) + "TWO" + ERASE_TO_END,
    );
  });

  it("writes nothing when the frame did not move", () => {
    expect(diffFrame(["a", "b"], ["a", "b"])).toBe("");
  });

  it("erases the rows a shorter frame gave up", () => {
    expect(diffFrame(["a", "b", "c"], ["a"])).toBe(cursorTo(2) + ERASE_TO_END + cursorTo(3) + ERASE_TO_END);
  });
});

describe("createFrameWriter", () => {
  it("costs one row update per keystroke", () => {
    const written: string[] = [];
    const writer = createFrameWriter({ write: (text) => written.push(text) });

    const screen = (draft: string): string[] => [
      "snowpea",
      "transcript line",
      "another transcript line",
      `> ${draft}`,
    ];

    const first = writer.write(chunk(screen(""), 0));
    const second = writer.write(chunk(screen("h"), 4));
    const third = writer.write(chunk(screen("he"), 4));

    expect(first).toContain("snowpea");
    expect(second).toBe(cursorTo(4) + "> h" + ERASE_TO_END);
    expect(third).toBe(cursorTo(4) + "> he" + ERASE_TO_END);
    // The whole first frame, then two short row updates.
    expect(second.length).toBeLessThan(first.length / 4);
    expect(written).toHaveLength(3);
  });

  it("passes anything that is not a frame straight through", () => {
    const written: string[] = [];
    const writer = createFrameWriter({ write: (text) => written.push(text) });
    writer.write(chunk(["a", "b"], 0));
    writer.write("a log line that is not a frame\n");
    // The screen is unknown again, so the next frame is painted in full.
    const next = writer.write(chunk(["a", "b"], 2));
    expect(next).toContain(cursorTo(1) + "a");
    expect(next).toContain(cursorTo(2) + "b");
  });

  it("repaints in full after a reset", () => {
    const writer = createFrameWriter({ write: () => undefined });
    writer.write(chunk(["a", "b"], 0));
    expect(writer.write(chunk(["a", "b"], 2))).toBe("");
    writer.reset();
    expect(writer.write(chunk(["a", "b"], 2))).toContain(cursorTo(1) + "a");
  });
});
