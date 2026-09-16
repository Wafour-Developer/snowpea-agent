import { describe, expect, it } from "vitest";

import { panelRowAt, parseMouse } from "../src/input/mouse.js";

describe("parseMouse", () => {
  it("parses a left-button press report", () => {
    expect(parseMouse("\u001b[<0;12;8M")).toEqual({
      button: 0,
      col: 12,
      row: 8,
      press: true,
    });
  });

  it("parses releases and wheel buttons", () => {
    expect(parseMouse("\u001b[<0;12;8m")).toEqual({
      button: 0,
      col: 12,
      row: 8,
      press: false,
    });
    expect(parseMouse("\u001b[<64;9;7M")?.button).toBe(64);
    expect(parseMouse("\u001b[<65;9;7M")?.button).toBe(65);
  });

  it("returns null for malformed input", () => {
    expect(parseMouse("x")).toBeNull();
    expect(parseMouse("\u001b[<0;10;M")).toBeNull();
    expect(parseMouse("\u001b[<0;0;3M")).toBeNull();
  });
});

describe("panelRowAt", () => {
  it("maps terminal rows inside the panel to row indexes", () => {
    const layout = { totalRows: 24, bottomRows: 5, panelRows: 4 };
    expect(panelRowAt(16, layout)).toBe(0);
    expect(panelRowAt(17, layout)).toBe(1);
    expect(panelRowAt(19, layout)).toBe(3);
    expect(panelRowAt(15, layout)).toBeNull();
    expect(panelRowAt(20, layout)).toBeNull();
  });

  it("handles a clipped top edge by keeping the right index", () => {
    const layout = { totalRows: 5, bottomRows: 4, panelRows: 4 };
    expect(panelRowAt(1, layout)).toBe(3);
  });
});

describe("mouse reports as Ink delivers them", () => {
  it("accepts a stripped ESC, a missing bracket, and several reports in one chunk", async () => {
    const { mouseReports, parseMouse } = await import("../src/input/mouse.js");
    expect(parseMouse("[<0;11;22M")).toEqual({ button: 0, col: 11, row: 22, press: true });
    expect(parseMouse("<0;2;30m")).toEqual({ button: 0, col: 2, row: 30, press: false });
    const chunk = mouseReports("[<0;11;22M[<0;11;22m[<0;2;30M[<0;2;30m");
    expect(chunk.map((r) => r.press)).toEqual([true, false, true, false]);
    expect(mouseReports("hello")).toEqual([]);
    expect(mouseReports("<not a report")).toEqual([]);
  });
});
