import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

import { Chat } from "../src/components/Chat.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";
import type { FileCompleteResult } from "../src/rpc/client.js";

const DEFAULT_FILES: FileCompleteResult = {
  entries: [
    { path: "src/", kind: "dir" },
    { path: "docs/", kind: "dir" },
    { path: "package.json", kind: "file", size: 1024 },
    { path: "README.md", kind: "file", size: 2048 },
  ],
  truncated: false,
};

describe("file completion popup keys", () => {
  it("opens popup when @ is typed and Tab accepts highlighted entry", async () => {
    const onSubmit = vi.fn();
    const onFileComplete = vi.fn(async (_query: string) => DEFAULT_FILES);
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={onSubmit}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      await sleep(50);
      await type(stdin, "@", 20);
      await sleep(80);

      expect(onFileComplete).toHaveBeenCalledWith("");
      const text = stdout.text();
      expect(text).toContain("src/");
      expect(text).toContain("package.json");

      // Press down to select docs/
      stdin.write("\u001b[B");
      await sleep(20);

      // Press Tab to accept docs/
      stdin.write("\t");
      await sleep(40);

      // Directory keeps popup open; press Esc to close popup, then Enter to submit
      stdin.write("\u001b");
      await sleep(30);
      stdin.write("\r");
      await sleep(40);

      expect(onSubmit).toHaveBeenCalledWith("@docs/");
    } finally {
      instance.unmount();
    }
  });

  it("Enter accepts highlighted file and adds trailing space", async () => {
    const onSubmit = vi.fn();
    const onFileComplete = vi.fn(async (_query: string) => DEFAULT_FILES);
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={onSubmit}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      stdin.write("@");
      await sleep(50);

      // Navigate down twice to package.json
      stdin.write("\u001b[B");
      await sleep(20);
      stdin.write("\u001b[B");
      await sleep(20);

      // Press Enter to accept package.json
      stdin.write("\r");
      await sleep(40);

      // The popup should now be closed and draft has "@package.json "
      // Pressing Enter again submits the prompt normally!
      stdin.write("\r");
      await sleep(40);

      expect(onSubmit).toHaveBeenCalledWith("@package.json");
    } finally {
      instance.unmount();
    }
  });

  it("typing filters the file queries", async () => {
    const onSubmit = vi.fn();
    const onFileComplete = vi.fn(async (query: string) => {
      if (query === "p") {
        return {
          entries: [{ path: "package.json", kind: "file", size: 1024 }],
          truncated: false,
        };
      }
      return DEFAULT_FILES;
    });

    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={onSubmit}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      await type(stdin, "@p", 15);
      await sleep(50);

      expect(onFileComplete).toHaveBeenCalledWith("p");
      const text = stdout.text();
      expect(text).toContain("package.json");
    } finally {
      instance.unmount();
    }
  });

  it("submits prompt normally on Enter when popup is closed or empty", async () => {
    const onSubmit = vi.fn();
    const onFileComplete = vi.fn(async (_query: string) => ({
      entries: [],
      truncated: false,
    }));

    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={onSubmit}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      // Empty popup case
      await type(stdin, "@nonexistent", 10);
      await sleep(50);

      stdin.write("\r");
      await sleep(40);

      expect(onSubmit).toHaveBeenCalledWith("@nonexistent");
    } finally {
      instance.unmount();
    }
  });
});

describe("Esc dismissal scoped to one token", () => {
  it("Esc dismisses current token popup, but new token opens popup", async () => {
    const onFileComplete = vi.fn(async (_query: string) => DEFAULT_FILES);
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={vi.fn()}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      await type(stdin, "@foo", 10);
      await sleep(50);
      expect(stdout.text()).toContain("package.json");

      // Press Esc to dismiss popup for this token
      stdin.write("\u001b");
      await sleep(50);
      stdout.chunks.length = 0;

      // Typing more on the same token does not reopen popup
      await type(stdin, "bar", 10);
      await sleep(50);
      // Popup should stay closed for this token
      const midText = stdout.text();
      expect(midText).not.toContain("Tab complete");

      // Now type a space and start a new @ token
      stdout.chunks.length = 0;
      await type(stdin, " @baz", 10);
      await sleep(80);

      // New token opens popup!
      const afterText = stdout.text();
      expect(afterText).toContain("package.json");
    } finally {
      instance.unmount();
    }
  });
});

describe("no popup for emails or escapes", () => {
  it("does not call onFileComplete for emails or escaped @", async () => {
    const onFileComplete = vi.fn(async (_query: string) => DEFAULT_FILES);
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={vi.fn()}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      await type(stdin, "user@host.com and \\@escaped", 10);
      await sleep(50);

      expect(onFileComplete).not.toHaveBeenCalled();
      expect(stdout.text()).not.toContain("package.json");
    } finally {
      instance.unmount();
    }
  });
});

describe("slash completion unaffected", () => {
  it("slash command palette opens on / and Tab completes as usual", async () => {
    const onSubmit = vi.fn();
    const onFileComplete = vi.fn(async (_query: string) => DEFAULT_FILES);
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={onSubmit}
        completions={[
          { name: "help", summary: "Show help" },
          { name: "compact", summary: "Compact session" },
        ]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      await type(stdin, "/h", 10);
      await sleep(50);

      const text = stdout.text();
      expect(text).toContain("/help");
      expect(text).toContain("Show help");

      // Press Tab to complete /help
      stdin.write("\t");
      await sleep(40);

      // Press Enter to submit /help
      stdin.write("\r");
      await sleep(40);

      expect(onSubmit).toHaveBeenCalledWith("/help");
      expect(onFileComplete).not.toHaveBeenCalled();
    } finally {
      instance.unmount();
    }
  });
});

describe("directory display and truncation row", () => {
  it("shows trailing slash for directories, dim sizes for files, and more row when truncated", async () => {
    const onFileComplete = vi.fn(async (_query: string) => ({
      entries: [
        { path: "mydir/", kind: "dir" as const },
        { path: "small.txt", kind: "file" as const, size: 50 },
        { path: "large.bin", kind: "file" as const, size: 1024 * 1024 * 2 },
      ],
      truncated: true,
    }));

    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <Chat
        onSubmit={vi.fn()}
        completions={[]}
        onFileComplete={onFileComplete}
        fileCompleteDebounceMs={10}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    try {
      stdin.write("@");
      await sleep(50);

      const text = stdout.text();
      expect(text).toContain("mydir/");
      expect(text).toContain("small.txt");
      expect(text).toContain("50B");
      expect(text).toContain("2.0MB");
      expect(text).toContain("… more");
    } finally {
      instance.unmount();
    }
  });
});
