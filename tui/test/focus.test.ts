/**
 * Walking the cursor out of the input and into the rows below it.
 */

import { describe, expect, it } from "vitest";

import { INPUT_FOCUS, clampFocus, focusDown, focusUp, isInput } from "../src/state/focus.js";

describe("focusDown", () => {
  it("goes input → footer → the agent rows", () => {
    const first = focusDown(INPUT_FOCUS, 3);
    expect(first).toEqual({ zone: "footer" });
    const second = focusDown(first, 3);
    expect(second).toEqual({ zone: "agent", index: 0 });
    expect(focusDown(second, 3)).toEqual({ zone: "agent", index: 1 });
  });

  it("stops at the last row instead of falling off the panel", () => {
    expect(focusDown({ zone: "agent", index: 2 }, 3)).toEqual({ zone: "agent", index: 2 });
  });

  it("stays on the footer when there are no agent rows", () => {
    expect(focusDown({ zone: "footer" }, 0)).toEqual({ zone: "footer" });
  });
});

describe("focusUp", () => {
  it("retraces the same path back to the input", () => {
    expect(focusUp({ zone: "agent", index: 2 })).toEqual({ zone: "agent", index: 1 });
    expect(focusUp({ zone: "agent", index: 0 })).toEqual({ zone: "footer" });
    expect(focusUp({ zone: "footer" })).toEqual(INPUT_FOCUS);
    expect(focusUp(INPUT_FOCUS)).toEqual(INPUT_FOCUS);
  });
});

describe("clampFocus", () => {
  it("moves off a row that no longer exists", () => {
    expect(clampFocus({ zone: "agent", index: 5 }, 3)).toEqual({ zone: "agent", index: 2 });
    expect(clampFocus({ zone: "agent", index: 0 }, 0)).toEqual({ zone: "footer" });
  });

  it("leaves the input and the footer alone", () => {
    expect(clampFocus(INPUT_FOCUS, 0)).toEqual(INPUT_FOCUS);
    expect(clampFocus({ zone: "footer" }, 0)).toEqual({ zone: "footer" });
  });
});

describe("isInput", () => {
  it("is what decides whether the chat line takes keys", () => {
    expect(isInput(INPUT_FOCUS)).toBe(true);
    expect(isInput({ zone: "footer" })).toBe(false);
    expect(isInput({ zone: "agent", index: 0 })).toBe(false);
  });
});
