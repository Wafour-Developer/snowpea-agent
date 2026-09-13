/**
 * `$agent task` is a prompt, not a spawn.
 *
 * Spawning the agent from the TUI runs the delegate and stops there: nothing
 * carries its report back into the conversation, so the main agent never
 * answers. The daemon rewrites the prefix and runs the whole turn, which is why
 * the raw text goes through `session.prompt` like anything else.
 */

import React from "react";
import { render } from "ink";
import { expect, it, vi } from "vitest";
import { App } from "../src/app.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

function open() {
  const call = vi.fn(async (method: string) =>
    method === "agent.list"
      ? { agents: [{ name: "core", kind: "team" }, { name: "executor", kind: "definition" }] }
      : { commands: [], tools: [], agents: [] },
  );
  const client = {
    call,
    getStatus: () => "connected",
    setListeners: vi.fn(),
    onApprovalRequest: vi.fn(),
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false }),
    prompt: vi.fn(async () => ({ turnId: "t-1" })),
    interrupt: vi.fn(),
  };
  const stdin = fakeStdin();
  const stdout = fakeStdout(90, 24);
  const app = render(
    <App client={client as any} sessionId="s1" mode="accept" workdir="/tmp" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  return { call, client, stdin, stdout, app };
}

it("$agent task goes to the daemon as a prompt, so the main agent answers", async () => {
  const { call, client, stdin, stdout, app } = open();
  try {
    await sleep(150);
    await type(stdin, "$executor fix tests", 10);
    stdin.write("\r");
    await sleep(150);

    // The raw text, prefix and all: the daemon's own command table reads it.
    expect(client.prompt).toHaveBeenCalledWith("s1", "$executor fix tests", []);
    expect(call).not.toHaveBeenCalledWith("agent.spawn", expect.anything());
    expect(stdout.text()).toContain("Team: core");
    expect(stdout.text()).toContain("● main");
  } finally {
    app.unmount();
  }
});

it("/delegate reaches the daemon's command table", async () => {
  const { call, client, stdin, app } = open();
  try {
    await sleep(150);
    await type(stdin, "/delegate executor fix tests", 8);
    stdin.write("\r");
    await sleep(150);

    const ran = call.mock.calls.filter(([method]) => method === "command.run");
    expect(ran).toHaveLength(1);
    expect(ran[0][1]).toMatchObject({ name: "delegate", args: "executor fix tests" });
    expect(client.prompt).not.toHaveBeenCalled();
    expect(call).not.toHaveBeenCalledWith("agent.spawn", expect.anything());
  } finally {
    app.unmount();
  }
});
