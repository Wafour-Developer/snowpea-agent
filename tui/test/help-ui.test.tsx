import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/app.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

async function setup(fullscreen = false) {
  const commands = Array.from({ length: 40 }, (_, i) => ({ name: `command${i}`, summary: `Description ${i}` }));
  const rpc = {
    getStatus: () => "connected", setListeners: vi.fn(), onApprovalRequest: vi.fn(),
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false }),
    call: vi.fn(async (method: string) => method === "command.run" ? { turnId: "help" } : { commands, tools: [], agents: [] }),
    prompt: vi.fn(async (_session: string, _text: string) => ({ turnId: "t" })),
    interrupt: vi.fn(async () => {}),
  };
  const stdin = fakeStdin();
  const stdout = fakeStdout(90, 24);
  const app = render(<App client={rpc as any} sessionId="s" mode="accept" workdir="/tmp" fullscreen={fullscreen} />, {
    stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
  });
  await sleep(150);
  return { rpc, stdin, stdout, app };
}

async function key(stdin: any, value: string) { stdin.write(value); await sleep(80); }

describe("dismissible help", () => {
  it.each([false, true])("/help closes with Esc and returns input focus (fullscreen=%s)", async (fullscreen) => {
    const { rpc, stdin, stdout, app } = await setup(fullscreen);
    try {
      await key(stdin, "/help"); await key(stdin, "\r");
      expect(stdout.text()).toContain("Commands");
      stdout.chunks.length = 0;
      await key(stdin, "\u001b");
      await key(stdin, "hello"); await key(stdin, "\r");
      expect(rpc.interrupt).not.toHaveBeenCalled();
      expect(rpc.prompt.mock.calls.at(-1)?.[1]).toBe("hello");
      expect(stdout.text()).not.toContain("Commands");
    } finally { app.unmount(); }
  });

  it.each(["\u001bOP", "\u001b[11~", "\u001b[[A"])("F1 %j toggles help without entering the draft", async (sequence) => {
    const { rpc, stdin, stdout, app } = await setup();
    try {
      await key(stdin, sequence);
      expect(stdout.text()).toContain("Commands");
      stdout.chunks.length = 0;
      await key(stdin, sequence);
      await key(stdin, "hi"); await key(stdin, "\r");
      expect(rpc.prompt.mock.calls.at(-1)?.[1]).toBe("hi");
      expect(stdout.text()).not.toContain("Commands");
    } finally { app.unmount(); }
  });

  it("keeps a long help panel bounded and scrolls without changing the draft", async () => {
    const { rpc, stdin, stdout, app } = await setup();
    try {
      stdout.chunks.length = 0;
      await key(stdin, "/help"); await key(stdin, "\r");
      expect(stdout.text()).toContain("close");
      expect(stdout.text()).not.toContain("Description 39");
      await key(stdin, "\u001b[6~");
      expect(stdout.text()).toContain("scroll");
      await key(stdin, "q");
      await key(stdin, "hi"); await key(stdin, "\r");
      expect(rpc.prompt.mock.calls.at(-1)?.[1]).toBe("hi");
    } finally { app.unmount(); }
  });
});
