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
    onQuestionRequest: () => undefined,
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

it("/delegate goes to the daemon as a prompt too, and shows in the transcript", async () => {
  const { call, client, stdin, stdout, app } = open();
  try {
    await sleep(150);
    await type(stdin, "/delegate executor fix tests", 15);
    stdin.write("\r");
    await sleep(200);

    expect(client.prompt).toHaveBeenCalledWith("s1", "/delegate executor fix tests");
    expect(call).not.toHaveBeenCalledWith("agent.spawn", expect.anything());
    expect(call.mock.calls.filter(([method]) => method === "command.run")).toHaveLength(0);
    // The line is in the conversation, not swallowed by a command path.
    expect(stdout.text()).toContain("/delegate executor fix tests");
  } finally {
    app.unmount();
  }
});
