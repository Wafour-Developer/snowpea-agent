import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/app.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

function client() {
  return {
    getStatus: () => "connected",
    setListeners: vi.fn(),
    onApprovalRequest: vi.fn(),
    listApprovals: async () => ({ requests: [] }),
    call: async () => ({ commands: [], tools: [], agents: [] }),
    checkUpdate: vi.fn(async (_force?: boolean) => ({ available: true, current: "0.1.2", latest: "0.1.3" })),
    startUpdate: vi.fn(async () => ({ started: true })),
    prompt: vi.fn(async (_sessionId: string, _text: string) => ({ turnId: "turn" })),
    setMode: vi.fn(async () => ({ mode: "auto" })),
    interrupt: vi.fn(),
  };
}

async function setup() {
  const rpc = client();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 30);
  const app = render(<App client={rpc as any} sessionId="session" mode="accept" workdir="/tmp" />, {
    stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
  });
  await sleep(150);
  return { rpc, stdin, stdout, app };
}

describe("startup update notification", () => {
  it("checks freshly once at launch, not on mode changes", async () => {
    const { rpc, stdin, stdout, app } = await setup();
    try {
      expect(rpc.checkUpdate).toHaveBeenCalledWith(true);
      expect(stdout.text()).toContain("Update available v0.1.3");
      expect(rpc.startUpdate).not.toHaveBeenCalled();
      stdin.write("\u001b[Z");
      await sleep(100);
      expect(rpc.checkUpdate).toHaveBeenCalledTimes(1);
    } finally { app.unmount(); }
  });

  it("U opens confirmation without entering chat; No preserves an empty draft", async () => {
    const { rpc, stdin, stdout, app } = await setup();
    try {
      stdin.write("U"); await sleep(80);
      expect(stdout.text()).toContain("Update and restart");
      stdin.write("n"); await sleep(80);
      stdin.write("hello"); await sleep(60);
      stdin.write("\r"); await sleep(80);
      expect(rpc.startUpdate).not.toHaveBeenCalled();
      expect(rpc.prompt.mock.calls[0]?.[1]).toBe("hello");
    } finally { app.unmount(); }
  });

  it("ordinary typing containing u does not open update confirmation", async () => {
    const { rpc, stdin, app } = await setup();
    try {
      await type(stdin, "use tools", 25);
      stdin.write("\r"); await sleep(80);
      expect(rpc.prompt.mock.calls[0]?.[1]).toBe("use tools");
      expect(rpc.startUpdate).not.toHaveBeenCalled();
    } finally { app.unmount(); }
  });

  it("/update and explicit yes start one upgrade and lock chat while it runs", async () => {
    const { rpc, stdin, app } = await setup();
    try {
      stdin.write("/update"); await sleep(60);
      stdin.write("\r"); await sleep(80);
      stdin.write("y"); await sleep(80);
      stdin.write("ignored"); await sleep(60);
      stdin.write("\r"); await sleep(80);
      expect(rpc.startUpdate).toHaveBeenCalledTimes(1);
      expect(rpc.prompt).not.toHaveBeenCalled();
    } finally { app.unmount(); }
  });
});
