import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

import { Chat } from "../src/components/Chat.js";
import { needsSplit, splitKeypresses, splitKeys } from "../src/input/keys.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const URL = "https://github.com/deepseek-ai/deepseek-harness";

describe("splitting a chunk into keys", () => {
  it("splits only chunks that carry an erasure", () => {
    expect(needsSplit("\u007f")).toBe(false);
    expect(needsSplit("abc")).toBe(false);
    expect(needsSplit("\u007f\u007f")).toBe(true);
    expect(needsSplit("ab\u0008")).toBe(true);
    expect(needsSplit("line\u007f\nnext")).toBe(false);
    expect(needsSplit("\u001b[200~a\u007f\u001b[201~")).toBe(false);
  });

  it("keeps escape sequences and text runs whole", () => {
    expect(splitKeys("ab\u007f\u007fcd")).toEqual(["ab", "\u007f", "\u007f", "cd"]);
    expect(splitKeys("\u001b[D\u007f\u001b[1;5C\u001bOH\u001bx")).toEqual([
      "\u001b[D", "\u007f", "\u001b[1;5C", "\u001bOH", "\u001bx",
    ]);
    expect(splitKeys("한글\u007f")).toEqual(["한글", "\u007f"]);
  });
});

async function chat(width = 80) {
  const onSubmit = vi.fn();
  const stdin = fakeStdin();
  splitKeypresses(stdin);
  const stdout = fakeStdout(width + 2, 10);
  const instance = render(<Chat onSubmit={onSubmit} completions={[]} draftWidth={width} />, {
    stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
  });
  await sleep(20);
  const submit = async () => {
    stdin.write("\r");
    await sleep(60);
    return onSubmit.mock.calls.at(-1)?.[0];
  };
  return { stdin, instance, submit };
}

describe("keys that arrive together", () => {
  it("a held Backspace read as one chunk erases every character", async () => {
    const { stdin, instance, submit } = await chat();
    try {
      await type(stdin, URL, 2);
      stdin.write("\u007f".repeat(10));
      await sleep(40);
      expect(await submit()).toBe(URL.slice(0, -10));
    } finally { instance.unmount(); }
  });

  it("erasures and letters in one chunk apply in order", async () => {
    const { stdin, instance, submit } = await chat();
    try {
      stdin.write("abc");
      await sleep(20);
      stdin.write("\u007f\u007fxy");
      await sleep(40);
      expect(await submit()).toBe("axy");
    } finally { instance.unmount(); }
  });

  it("typing, erasing and retyping a long URL faster than renders", async () => {
    for (const width of [80, 30]) {
      const { stdin, instance, submit } = await chat(width);
      try {
        for (let round = 0; round < 3; round++) {
          for (const ch of URL) stdin.write(ch);
          for (let i = 0; i < URL.length + 3; i++) stdin.write("\u007f");
          await sleep(30);
        }
        for (const ch of URL) stdin.write(ch);
        await sleep(40);
        expect(await submit()).toBe(URL);
      } finally { instance.unmount(); }
    }
  });
});
