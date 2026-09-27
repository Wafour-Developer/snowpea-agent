import React from "react";
import chalk from "chalk";
import { render } from "ink";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { RenderedLines } from "../src/components/RenderedLines.js";
import { inputBackgroundColor } from "../src/layout/palette.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

/** The rows Ink wrote for `node`, without its escape sequences. */
async function frame(node: React.ReactElement): Promise<string[]> {
  const stdin = fakeStdin();
  const stdout = fakeStdout(40, 10);
  const instance = render(node, { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false });
  await sleep(30);
  instance.unmount();
  stdin.end();
  // eslint-disable-next-line no-control-regex
  const plain = stdout.text().replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "");
  return plain.split("\n").filter((row) => row.length > 0);
}

describe("inputBackgroundColor", () => {
  it("is a dark band by default and a light one when COLORFGBG says the terminal is light", () => {
    expect(inputBackgroundColor("truecolor", {})).toBe("#26232e");
    expect(inputBackgroundColor("truecolor", { COLORFGBG: "0;15" })).toBe("#ece9f3");
    expect(inputBackgroundColor("basic", {})).toBe("blackBright");
    expect(inputBackgroundColor("basic", { COLORFGBG: "0;7" })).toBe("white");
  });

  it("is off with no colour, and can be overridden or turned off", () => {
    expect(inputBackgroundColor("none", {})).toBeUndefined();
    expect(inputBackgroundColor("truecolor", { SNOWPEA_TUI_INPUT_BG: "#101010" })).toBe("#101010");
    expect(inputBackgroundColor("truecolor", { SNOWPEA_TUI_INPUT_BG: "none" })).toBeUndefined();
  });
});

describe("RenderedLines with a band", () => {
  // A band is spaces in a colour; with colour off (a test's stdout is not a
  // terminal) Ink trims them as trailing whitespace, which is right for a
  // colourless terminal and says nothing about this code. Colour on.
  const level = chalk.level;
  beforeAll(() => {
    chalk.level = 3;
  });
  afterAll(() => {
    chalk.level = level;
  });

  it("fills every row to the full width", async () => {
    const lines = [
      { key: "a", segments: [{ text: "› " }, { text: "hello" }] },
      { key: "b", segments: [{ text: "  " }, { text: "한글" }] },
    ];
    const rows = await frame(<RenderedLines lines={lines} background="#26232e" width={20} />);
    expect(rows[0]).toBe("› hello" + " ".repeat(13));
    // Two wide characters take four cells, so the pad is 14, not 16.
    expect(rows[1]).toBe("  한글" + " ".repeat(14));
  });

  it("adds nothing without a band", async () => {
    const rows = await frame(<RenderedLines lines={[{ key: "a", segments: [{ text: "hi" }] }]} width={20} />);
    expect(rows[0]).toBe("hi");
  });
});
