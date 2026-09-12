/**
 * The logo's colours: which ramp a terminal gets, and when it gets none.
 */

import { describe, expect, it } from "vitest";

import {
  BASIC_RAMP,
  MINT,
  SNOWPEA,
  TEAL,
  colorMode,
  gradientAt,
  gradientColors,
  toHex,
} from "../src/layout/palette.js";

const env = (values: Record<string, string>): NodeJS.ProcessEnv => values as NodeJS.ProcessEnv;

describe("colorMode", () => {
  it("takes NO_COLOR at its word, whatever else the terminal says", () => {
    expect(colorMode(env({ NO_COLOR: "1", COLORTERM: "truecolor" }), true)).toBe("none");
    // An empty NO_COLOR is not set, by the convention's own rules.
    expect(colorMode(env({ NO_COLOR: "", COLORTERM: "truecolor" }), true)).toBe("truecolor");
  });

  it("draws plain when the output is not a terminal", () => {
    expect(colorMode(env({ COLORTERM: "truecolor" }), false)).toBe("none");
  });

  it("uses the full gradient only when 24-bit colour is advertised", () => {
    expect(colorMode(env({ COLORTERM: "truecolor" }), true)).toBe("truecolor");
    expect(colorMode(env({ COLORTERM: "24bit" }), true)).toBe("truecolor");
    expect(colorMode(env({ TERM: "xterm-direct" }), true)).toBe("truecolor");
  });

  it("falls back to named colours on an ordinary terminal", () => {
    expect(colorMode(env({ TERM: "xterm-256color" }), true)).toBe("basic");
    expect(colorMode(env({}), true)).toBe("basic");
  });
});

describe("the ramp", () => {
  it("runs mint → snowpea green → deep teal", () => {
    expect(toHex(gradientAt(0))).toBe(toHex(MINT));
    expect(toHex(gradientAt(0.5))).toBe(toHex(SNOWPEA));
    expect(toHex(gradientAt(1))).toBe(toHex(TEAL));
  });

  it("darkens all the way down", () => {
    const brightness = [0, 0.25, 0.5, 0.75, 1].map((at) => {
      const { r, g, b } = gradientAt(at);
      return r + g + b;
    });
    for (let index = 1; index < brightness.length; index += 1) {
      expect(brightness[index]).toBeLessThan(brightness[index - 1]);
    }
  });

  it("clamps rather than running off either end", () => {
    expect(toHex(gradientAt(-1))).toBe(toHex(MINT));
    expect(toHex(gradientAt(2))).toBe(toHex(TEAL));
  });
});

describe("gradientColors", () => {
  it("gives one hex colour per row on a truecolor terminal", () => {
    const colors = gradientColors(7, "truecolor");
    expect(colors).toHaveLength(7);
    expect(colors[0]).toBe(toHex(MINT));
    expect(colors[6]).toBe(toHex(TEAL));
    for (const color of colors) expect(color).toMatch(/^#[0-9a-f]{6}$/);
  });

  it("falls back to named colours in the same order", () => {
    const colors = gradientColors(6, "basic");
    expect(new Set(colors)).toEqual(new Set(BASIC_RAMP));
    expect(colors[0]).toBe(BASIC_RAMP[0]);
    expect(colors[colors.length - 1]).toBe(BASIC_RAMP[BASIC_RAMP.length - 1]);
  });

  it("paints nothing when the terminal takes no colour", () => {
    expect(gradientColors(7, "none")).toEqual(new Array(7).fill(undefined));
  });

  it("copes with a single row", () => {
    expect(gradientColors(1, "truecolor")).toEqual([toHex(MINT)]);
    expect(gradientColors(0, "basic")).toHaveLength(1);
  });
});
