/**
 * Brand accent colours per terminal colour mode.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  accentColor,
  accentDimColor,
  currentAccent,
  currentAccentDim,
  setColorModeForTests,
  toHex,
  VIOLET,
  DEEP_VIOLET,
} from "../src/layout/palette.js";

afterEach(() => {
  setColorModeForTests(null);
});

describe("accentColor", () => {
  it("uses the brand violet hex on a truecolor terminal", () => {
    expect(accentColor("truecolor")).toBe("#a78bfa");
    expect(accentColor("truecolor")).toBe(toHex(VIOLET));
  });

  it("falls back to named colours on ordinary terminals", () => {
    expect(accentColor("basic")).toBe("magentaBright");
    expect(accentColor("none")).toBe("magenta");
  });
});

describe("accentDimColor", () => {
  it("uses the deep violet hex on a truecolor terminal", () => {
    expect(accentDimColor("truecolor")).toBe("#7c3aed");
    expect(accentDimColor("truecolor")).toBe(toHex(DEEP_VIOLET));
  });

  it("falls back to magenta elsewhere", () => {
    expect(accentDimColor("basic")).toBe("magenta");
    expect(accentDimColor("none")).toBe("magenta");
  });
});

describe("currentAccent", () => {
  it("memoises per colour mode and respects the test override", () => {
    setColorModeForTests("truecolor");
    expect(currentAccent()).toBe("#a78bfa");
    expect(currentAccent()).toBe("#a78bfa");

    setColorModeForTests("basic");
    expect(currentAccent()).toBe("magentaBright");
    expect(currentAccentDim()).toBe("magenta");
  });
});
