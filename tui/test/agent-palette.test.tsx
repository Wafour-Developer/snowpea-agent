/**
 * Both completion flows, driven through the real input.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const AGENTS = [
  { name: "executor", kind: "definition", description: "Implements one change" },
  { name: "explorer", kind: "definition", description: "Finds things in the codebase" },
  { name: "reviewer", kind: "definition", description: "Reviews a diff" },
];

function fakeClient() {
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: () => undefined,
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.10", latest: "0.1.10" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "agent.list") return { agents: AGENTS };
      if (method === "settings.get") return { settings: { agents: { models: { executor: "fast" } } } };
      return { commands: [] };
    },
    prompt: async () => ({ turnId: "t-1" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
  };
}

/**
 * Just the completion box out of a frame.
 *
 * The agent panel at the bottom names the same agents, so a whole-frame
 * assertion cannot tell "offered" from "listed down there".
 */
function paletteOf(frame: string): string {
  const start = frame.indexOf("╭");
  const end = frame.indexOf("╯");
  return start === -1 || end === -1 ? "" : frame.slice(start, end);
}

async function open() {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(110, 26);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(250);
  stdout.chunks.length = 0;
  return { client, stdin, stdout, instance };
}

describe("the $ form", () => {
  it("opens the whole list on the bare sigil", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("$");
    await sleep(200);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("executor");
    expect(output).toContain("explorer");
    expect(output).toContain("reviewer");
    expect(output).toContain("Tab or Enter accept");
  });

  it("shows the model an agent is assigned", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("$");
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("[fast]");
  });

  it("narrows to what has been typed", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "$exp", 15);
    await sleep(200);
    // The frames before "exp" was finished legitimately showed the whole list,
    // so what matters is the one on screen now.
    const palette = paletteOf(stdout.chunks[stdout.chunks.length - 1]);
    instance.unmount();
    expect(palette).toContain("explorer");
    expect(palette).not.toContain("reviewer");
  });

  it("Tab accepts the highlighted name and closes the list", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "$exp", 15);
    await sleep(150);
    stdout.chunks.length = 0;
    stdin.write("\t");
    await sleep(250);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("$explorer");
    // The hint chip takes over once the name is complete.
    expect(output).toContain("delegate to explorer");
    expect(output).not.toContain("Tab or Enter accept");
  });

  it("↓ then Tab takes the second candidate", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "$ex", 15);
    await sleep(150);
    stdin.write("\u001B[B");
    await sleep(80);
    stdin.write("\t");
    await sleep(250);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("$explorer");
  });

  it("Esc closes the list without touching the draft or the turn", async () => {
    const { client, stdin, stdout, instance } = await open();
    await type(stdin, "$exp", 15);
    await sleep(150);
    stdout.chunks.length = 0;
    stdin.write("\u001B");
    await sleep(250);
    const output = stdout.text();
    instance.unmount();

    expect(output).not.toContain("Tab or Enter accept");
    expect(output).toContain("$exp");
    expect(client.calls.some((call) => call.method === "session.interrupt")).toBe(false);
  });

  it("says so when nothing answers to the name being typed", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "$zz", 15);
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain('no agent starts with "zz"');
  });
});

describe("/delegate", () => {
  it("completes the first argument, not the command", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "/delegate rev", 15);
    await sleep(200);
    expect(stdout.text()).toContain("reviewer");

    stdout.chunks.length = 0;
    stdin.write("\t");
    await sleep(250);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("/delegate reviewer");
  });

  it("closes once the task text begins", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "/delegate reviewer look", 10);
    await sleep(200);
    const frame = stdout.chunks[stdout.chunks.length - 1];
    instance.unmount();
    expect(frame).not.toContain("Tab or Enter accept");
  });
});
