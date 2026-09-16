import { describe, expect, it } from "vitest";

import {
  backspace,
  del,
  down,
  insert,
  layout,
  left,
  right,
  up,
} from "../src/state/editor.js";

describe("editor model", () => {
  it("edits ASCII text at the cursor", () => {
    let state = { text: "hello", cursor: 5 };
    state = left(state);
    expect(state).toEqual({ text: "hello", cursor: 4 });
    state = insert(state, "X");
    expect(state).toEqual({ text: "hellXo", cursor: 5 });
    state = backspace(state);
    expect(state).toEqual({ text: "hello", cursor: 4 });
    expect(del({ text: "hello", cursor: 1 })).toEqual({ text: "hllo", cursor: 1 });
    expect(right({ text: "hello", cursor: 1 })).toEqual({ text: "hello", cursor: 2 });
  });

  it("wraps Korean text by terminal cells", () => {
    const text = "안녕하세요 세계";
    const view = layout(text, 10, text.length);
    expect(view.lines.map((line) => text.slice(line.start, line.end))).toEqual([
      "안녕하세요",
      " 세계",
    ]);
    expect(view.lines.map((line) => line.cells)).toEqual([10, 5]);
  });

  it("deletes an emoji+modifier as one grapheme", () => {
    const text = "A👍🏽B";
    const afterEmoji = { text, cursor: "A👍🏽".length };
    expect(backspace(afterEmoji)).toEqual({ text: "AB", cursor: 1 });
  });

  it("keeps a sticky visual column across short wide-char lines", () => {
    const text = "0123456789\n가\nabcdefghij";
    const secondStart = text.indexOf("\n") + 1;
    const thirdStart = text.lastIndexOf("\n") + 1;

    const fromFirst = { text, cursor: 8 };
    const toSecond = down(fromFirst, 40, 8);
    expect(toSecond.cursor).toBe(secondStart + 1);

    const toThird = down(toSecond, 40, 8);
    expect(toThird.cursor).toBe(thirdStart + 8);

    const backToSecond = up(toThird, 40, 8);
    expect(backToSecond.cursor).toBe(secondStart + 1);
  });

  it("maps a soft-wrap boundary cursor to the next visual row", () => {
    const text = "12345678가나Z";
    const view = layout(text, 10, 9);
    expect(view.lines.map((line) => text.slice(line.start, line.end))).toEqual([
      "12345678가",
      "나Z",
    ]);
    expect(view.cursorRow).toBe(1);
    expect(view.cursorCol).toBe(0);
  });

  it("keeps the cursor at the end of pasted multiline text", () => {
    const pasted = "한줄\n둘째줄\n👍🏽";
    const next = insert({ text: "", cursor: 0 }, pasted);
    expect(next.text).toBe(pasted);
    expect(next.cursor).toBe(pasted.length);
    const view = layout(next.text, 10, next.cursor);
    expect(view.cursorRow).toBe(view.lines.length - 1);
  });
});
