/**
 * Typing must not repaint the screen.
 *
 * Two regressions are pinned here, both of which the user saw as a flicker on
 * every keystroke:
 *
 *   - the transcript re-rendering because the sliced window was a fresh array
 *     on each render, and
 *   - Ink writing a full screen clear before each frame, which it does as soon
 *     as the frame is as tall as the terminal.
 *
 * The harness drives real Ink over a fake tty, which is the only way to observe
 * either of them.
 */

import { PassThrough } from "node:stream";
import React, { useMemo, useState } from "react";
import { Box, Text, render, useInput } from "ink";
import { describe, expect, it } from "vitest";

import { TranscriptView, transcriptRenderCount } from "../src/components/TranscriptView.js";
import { usableRows } from "../src/layout/viewport.js";
import type { Line } from "../src/layout/transcript.js";

const ROWS = 20;
const COLUMNS = 80;

/** The full screen clear Ink writes before a frame that fills the terminal. */
const CLEAR_SCREEN = "[2J";

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function fakeStdin(): any {
  const stream = new PassThrough() as any;
  stream.isTTY = true;
  stream.setRawMode = () => stream;
  stream.ref = () => stream;
  stream.unref = () => stream;
  return stream;
}

function fakeStdout(): { stream: any; chunks: string[] } {
  const chunks: string[] = [];
  const stream: any = new PassThrough();
  stream.columns = COLUMNS;
  stream.rows = ROWS;
  const write = stream.write.bind(stream);
  stream.write = (chunk: any, ...rest: any[]) => {
    chunks.push(String(chunk));
    return write(chunk, ...rest);
  };
  return { stream, chunks };
}

function transcript(count: number): Line[] {
  return Array.from({ length: count }, (_, index) => ({
    key: `line-${index}`,
    segments: [{ text: `transcript line ${index}` }],
  }));
}

/** The shape of the real layout: a fixed transcript above a live input row. */
function Harness({ lines, frameRows }: { lines: Line[]; frameRows: number }) {
  const [draft, setDraft] = useState("");
  useInput((input) => setDraft((value) => value + input));
  // `app.tsx` does the same: the window is memoized, so it keeps its identity
  // across renders that only moved the draft.
  const window = useMemo(() => lines.slice(-(frameRows - 2)), [lines, frameRows]);
  return (
    <Box flexDirection="column" height={frameRows} width={COLUMNS} overflow="hidden">
      <TranscriptView lines={window} height={frameRows - 2} />
      <Text>{`> ${draft}`}</Text>
    </Box>
  );
}

async function type(stdin: any, characters: string): Promise<void> {
  for (const character of characters) {
    stdin.write(character);
    await sleep(60);
  }
}

describe("typing", () => {
  it("never re-renders the transcript", async () => {
    const stdin = fakeStdin();
    const { stream: stdout } = fakeStdout();
    const instance = render(<Harness lines={transcript(30)} frameRows={usableRows(ROWS)} />, {
      stdin,
      stdout,
      exitOnCtrlC: false,
      patchConsole: false,
    });

    await sleep(80);
    const before = transcriptRenderCount.value;
    await type(stdin, "hello");
    const after = transcriptRenderCount.value;
    instance.unmount();

    expect(after - before).toBe(0);
  });

  it("does not clear the screen once the frame leaves a row spare", async () => {
    const stdin = fakeStdin();
    const { stream: stdout, chunks } = fakeStdout();
    const instance = render(<Harness lines={transcript(30)} frameRows={usableRows(ROWS)} />, {
      stdin,
      stdout,
      exitOnCtrlC: false,
      patchConsole: false,
    });

    await sleep(80);
    chunks.length = 0;
    await type(stdin, "hello");
    instance.unmount();

    expect(chunks.join("")).not.toContain(CLEAR_SCREEN);
  });

  it("clears the whole screen on every frame when the frame fills the terminal", async () => {
    // The regression this guards: Ink's `outputHeight >= stdout.rows` branch
    // writes `clearTerminal` before every frame, which is the flicker.
    const stdin = fakeStdin();
    const { stream: stdout, chunks } = fakeStdout();
    const instance = render(<Harness lines={transcript(30)} frameRows={ROWS} />, {
      stdin,
      stdout,
      exitOnCtrlC: false,
      patchConsole: false,
    });

    await sleep(80);
    chunks.length = 0;
    await type(stdin, "hello");
    instance.unmount();

    expect(chunks.join("")).toContain(CLEAR_SCREEN);
  });
});
