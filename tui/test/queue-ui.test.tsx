/**
 * Queued prompts, the model picker and the delegation chip, through the real
 * input.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

function fakeClient(answers: Record<string, unknown> = {}) {
  let listeners: any;
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.7", latest: "0.1.7" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      return (answers[method] as any) ?? { commands: [] };
    },
    prompt: async (sessionId: string, text: string) => {
      calls.push({ method: "session.prompt", params: { sessionId, text } });
      return { turnId: `t-${calls.filter((c) => c.method === "session.prompt").length}` };
    },
    interrupt: async () => {
      calls.push({ method: "session.interrupt", params: {} });
      return undefined;
    },
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({
  sessionId: "sess-1",
  seq,
  kind,
  payload,
});

async function open(answers: Record<string, unknown> = {}) {
  const client = fakeClient(answers);
  const stdin = fakeStdin();
  const stdout = fakeStdout(110, 26);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(200);
  stdout.chunks.length = 0;
  return { client, stdin, stdout, instance };
}

describe("queued prompts", () => {
  it("shows the count, the text, and clears when the turn starts", async () => {
    const { client, stdin, stdout, instance } = await open();
    await type(stdin, "run the tests", 15);
    stdin.write("\r");
    await sleep(150);
    client.emit(event(1, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }));
    await sleep(200);

    const queued = stdout.text();
    expect(queued).toContain("⏳ 1 queued");
    expect(queued).toContain("run the tests");

    stdout.chunks.length = 0;
    client.emit(event(2, "turn.dequeued", { turnId: "t-1", reason: "started", queued: 0 }));
    await sleep(200);
    const after = stdout.text();
    instance.unmount();
    expect(after).not.toContain("⏳ 1 queued");
  });

  it("says so when an interrupt drops the queue", async () => {
    const { client, stdin, stdout, instance } = await open();
    await type(stdin, "one", 15);
    stdin.write("\r");
    await sleep(120);
    client.emit(event(1, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }));
    client.emit(event(2, "turn.queued", { turnId: "t-2", position: 2, queued: 2 }));
    await sleep(150);
    stdout.chunks.length = 0;

    client.emit(event(3, "turn.dequeued", { turnId: "t-1", reason: "dropped", queued: 1 }));
    client.emit(event(4, "turn.dequeued", { turnId: "t-2", reason: "dropped", queued: 0 }));
    await sleep(350);
    const output = stdout.text();
    const lastFrame = stdout.chunks[stdout.chunks.length - 1];
    instance.unmount();

    expect(output).toContain("2 queued prompts dropped");
    // The count is gone by the last frame; the frame between the two events
    // legitimately still shows one waiting.
    expect(lastFrame).not.toContain("⏳");
  });
});

describe("the model picker", () => {
  const answers = {
    "settings.get": {
      settings: {
        models: {
          default: "deep",
          profiles: {
            fast: { provider: "anthropic", model: "claude-haiku-4-5" },
            deep: { provider: "anthropic", model: "claude-sonnet-4-5" },
          },
        },
      },
    },
    "provider.models": { vendor: "anthropic", models: ["claude-opus-4-1"], current: null },
  };

  it("opens on a bare /model and lists profiles and discovered models", async () => {
    const { stdin, stdout, instance } = await open(answers);
    await type(stdin, "/model", 15);
    stdin.write("\r");
    await sleep(300);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("Model");
    expect(output).toContain("fast");
    expect(output).toContain("deep");
    expect(output).toContain("claude-opus-4-1");
    expect(output).toContain("Enter pick");
  });

  it("runs /model <ref> for the row that was picked", async () => {
    const { client, stdin, instance } = await open(answers);
    await type(stdin, "/model", 15);
    stdin.write("\r");
    await sleep(300);
    stdin.write("\u001B[B"); // move to the second profile
    await sleep(80);
    stdin.write("\r");
    await sleep(200);
    instance.unmount();

    const ran = client.calls.filter((call) => call.method === "command.run");
    expect(ran.map((call) => `${call.params.name} ${call.params.args}`.trim())).toContain(
      "model deep",
    );
  });

  it("closes on Esc without changing anything", async () => {
    const { client, stdin, instance } = await open(answers);
    await type(stdin, "/model", 15);
    stdin.write("\r");
    await sleep(300);
    stdin.write("\u001B");
    await sleep(150);
    instance.unmount();
    expect(client.calls.some((call) => call.method === "command.run")).toBe(false);
  });
});

describe("the delegation chip", () => {
  it("names the agent a $prefix would hand the prompt to", async () => {
    const { stdin, stdout, instance } = await open({
      "agent.list": { agents: [{ name: "reviewer", kind: "definition" }] },
    });
    await type(stdin, "$reviewer look at this", 15);
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("delegate to reviewer");
  });

  it("warns when nothing answers to that name", async () => {
    const { stdin, stdout, instance } = await open({ "agent.list": { agents: [] } });
    await type(stdin, "$nobody do it", 15);
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("no such agent");
  });
});
