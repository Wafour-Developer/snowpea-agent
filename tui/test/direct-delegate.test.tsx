import React from "react";
import { render } from "ink";
import { expect, it, vi } from "vitest";
import { App } from "../src/app.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

it("$agent task spawns that named team agent directly", async () => {
  const call = vi.fn(async (method: string) => method === "agent.list"
    ? { agents: [{ name: "core", kind: "team" }, { name: "executor", kind: "definition" }] }
    : { commands: [], tools: [], agents: [] });
  const client = {
    call, getStatus: () => "connected", setListeners: vi.fn(), onApprovalRequest: vi.fn(),
    listApprovals: async () => ({ requests: [] }), checkUpdate: async () => ({ available: false }),
    prompt: vi.fn(), interrupt: vi.fn(),
  };
  const stdin = fakeStdin();
  const stdout = fakeStdout(90, 24);
  const app = render(<App client={client as any} sessionId="s1" mode="accept" workdir="/tmp" />, {
    stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false,
  });
  try {
    await sleep(150);
    await type(stdin, "$executor fix tests", 10);
    stdin.write("\r");
    await sleep(100);
    expect(call).toHaveBeenCalledWith("agent.spawn", {
      sessionId: "s1", name: "executor", task: "fix tests",
    });
    expect(stdout.text()).toContain("core");
    expect(client.prompt).not.toHaveBeenCalled();
  } finally { app.unmount(); }
});
