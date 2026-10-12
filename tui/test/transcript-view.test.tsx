/**
 * Every transcript line keeps its row, blank ones included.
 *
 * Ink gives an empty `Text` no height, so the blank line between two blocks
 * used to take no row: the lines under it rode up and the window ended short
 * of the input, leaving empty rows above it while scrolling.
 */

import React from "react";
import { Box, Text, render } from "ink";
import { describe, expect, it } from "vitest";

import { TranscriptView } from "../src/components/TranscriptView.js";
import type { Line } from "../src/layout/transcript.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const strip = (text: string): string => text.replace(/\u001B\[[0-9;?]*[A-Za-z]/g, "");

describe("TranscriptView", () => {
  it("gives a blank line its own row", async () => {
    const lines: Line[] = [
      { key: "a", segments: [{ text: "first" }] },
      { key: "gap", segments: [{ text: "" }] },
      { key: "b", segments: [{ text: "second" }] },
      { key: "gap2", segments: [{ text: "" }] },
      { key: "c", segments: [{ text: "last" }] },
    ];
    const { stream: stdout, text } = fakeStdout(40, 12);
    const instance = render(
      <Box flexDirection="column" height={6} width={40}>
        <TranscriptView lines={lines} height={5} />
        <Text>INPUT</Text>
      </Box>,
      { stdin: fakeStdin(), stdout, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(50);
    instance.unmount();
    const rows = strip(text()).split("\n").map((row) => row.trimEnd());
    const at = rows.indexOf("first");
    expect(rows.slice(at, at + 6)).toEqual(["first", "", "second", "", "last", "INPUT"]);
  });
});

describe("TranscriptView in a box shorter than its window", () => {
  it("clips the bottom lines instead of drawing one over another", async () => {
    const lines: Line[] = Array.from({ length: 6 }, (_, index) => ({
      key: `l${index}`,
      segments: [{ text: index % 2 === 0 ? `long line number ${index} with a tail` : `short ${index}` }],
    }));
    const { stream: stdout, text } = fakeStdout(40, 12);
    // Six lines into a frame with room for four: the extra two must go.
    const instance = render(
      <Box flexDirection="column" height={5} width={40} overflow="hidden">
        <TranscriptView lines={lines} height={6} />
        <Text>INPUT</Text>
      </Box>,
      { stdin: fakeStdin(), stdout, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(50);
    instance.unmount();
    const rows = strip(text()).split("\n").map((row) => row.trimEnd());
    const at = rows.indexOf("long line number 0 with a tail");
    expect(rows.slice(at, at + 5)).toEqual([
      "long line number 0 with a tail",
      "short 1",
      "long line number 2 with a tail",
      "short 3",
      "INPUT",
    ]);
  });
});
