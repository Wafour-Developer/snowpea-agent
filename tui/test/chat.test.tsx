import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

import { Chat } from "../src/components/Chat.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

describe("Chat line editing", () => {
  it("moves left and inserts text in the middle of the draft", async () => {
    const onSubmit = vi.fn();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 10);
    const instance = render(<Chat onSubmit={onSubmit} completions={[]} />, {
      stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
    });
    try {
      await type(stdin, "넌 어떤걸 할", 30);
      stdin.write("\u001b[D");
      await sleep(20);
      await type(stdin, "수 ", 30);
      await sleep(40);
      stdin.write("\r");
      await sleep(40);
      expect(onSubmit).toHaveBeenCalledWith("넌 어떤걸 수 할");
    } finally { instance.unmount(); }
  });

  it("backspace removes the character before the moved cursor", async () => {
    const onSubmit = vi.fn();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 10);
    const instance = render(<Chat onSubmit={onSubmit} completions={[]} />, {
      stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
    });
    try {
      await type(stdin, "abcd", 5);
      stdin.write("\u001b[D");
      await sleep(40);
      stdin.write("\u001b[D");
      await sleep(40);
      stdin.write("\u007f");
      await sleep(40);
      stdin.write("\r");
      await sleep(40);
      expect(onSubmit).toHaveBeenCalledWith("acd");
    } finally { instance.unmount(); }
  });

  it("restores the in-progress draft after walking back down through history", async () => {
    const onSubmit = vi.fn();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 10);
    const instance = render(
      <Chat onSubmit={onSubmit} completions={[]} initialHistory={["older", "newer"]} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    try {
      await type(stdin, "working draft", 5);
      for (const sequence of ["\u001b[A", "\u001b[A", "\u001b[B", "\u001b[B"]) {
        stdin.write(sequence);
        await sleep(20);
      }
      stdin.write("\r");
      await sleep(60);
      expect(onSubmit).toHaveBeenCalledWith("working draft");
    } finally { instance.unmount(); }
  });

  it("edits a multiline draft with arrow keys and submits expected text", async () => {
    const onSubmit = vi.fn();
    const stdin = fakeStdin();
    const stdout = fakeStdout(40, 10);
    const instance = render(<Chat onSubmit={onSubmit} completions={[]} draftWidth={20} />, {
      stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
    });
    try {
      stdin.write("first line\nsecond line");
      await sleep(40);
      stdin.write("\u001b[A");
      await sleep(30);
      await type(stdin, "!", 20);
      await sleep(30);
      stdin.write("\u001b[B");
      await sleep(30);
      stdin.write("\r");
      await sleep(60);
      expect(onSubmit).toHaveBeenCalledWith("first line!\nsecond line");
    } finally { instance.unmount(); }
  });

  it("pressing Esc invokes onInterrupt and keeps the in-progress draft", async () => {
    const onSubmit = vi.fn();
    const onInterrupt = vi.fn();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 10);
    const instance = render(
      <Chat onSubmit={onSubmit} onInterrupt={onInterrupt} completions={[]} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    try {
      await type(stdin, "keep this draft", 10);
      stdin.write("\u001b");
      await sleep(40);
      expect(onInterrupt).toHaveBeenCalled();
      stdin.write("\r");
      await sleep(40);
      expect(onSubmit).toHaveBeenCalledWith("keep this draft");
    } finally {
      instance.unmount();
    }
  });
});
