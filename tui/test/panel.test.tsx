/**
 * Walking down into the panel, and opening a delegate's conversation.
 *
 * The drill-down is the part worth pinning: a subagent runs in its own child
 * session, so its messages must reach that agent's transcript and never the
 * main one.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { countOf, fakeStdin, fakeStdout, sleep } from "./tty.js";

const CHILD = "child-session-1";

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    setListeners: (listeners: any) => {
      onSessionEvent = listeners.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "session.resume") {
        return {
          sessionId: params.sessionId,
          events: [
            {
              sessionId: params.sessionId,
              seq: 1,
              kind: "message.done",
              payload: { role: "assistant", text: "CHILD-ONLY-ANSWER" },
            },
          ],
        };
      }
      if (method === "system.info") return { pid: 1, lifecycle: { summary: "idle" } };
      return { commands: [], tools: [], agents: [] };
    },
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({
  sessionId: "sess-1",
  seq,
  kind,
  payload,
});

async function withDelegate() {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(150);
  client.emit(
    event(1, "subagent.spawn", {
      agentId: "a1",
      name: "executor",
      task: "do the delegated work",
      status: "running",
      sessionId: CHILD,
    }),
  );
  await sleep(120);
  return { client, stdin, stdout, instance };
}

describe("the bottom panel", () => {
  it("walks the cursor from the input down to the agent rows", async () => {
    const { stdin, stdout, instance } = await withDelegate();
    stdout.chunks.length = 0;

    stdin.write("\u001B[B"); // into the footer
    await sleep(80);
    expect(stdout.text()).toContain("Enter to list them");

    stdin.write("\r"); // open what is running
    await sleep(80);
    expect(stdout.text()).toContain("nothing running");

    stdin.write("\u001B"); // back to the input
    await sleep(80);
    instance.unmount();
  });

  it("opens a delegate's own conversation and keeps it out of the main one", async () => {
    const { client, stdin, stdout, instance } = await withDelegate();

    // input → footer → ● main → ◯ executor
    for (let i = 0; i < 3; i += 1) {
      stdin.write("\u001B[B");
      await sleep(60);
    }
    stdin.write("\r");
    await sleep(200);

    const resumed = client.calls.filter((call) => call.method === "session.resume");
    const output = stdout.text();
    expect(resumed).toHaveLength(1);
    expect(resumed[0].params).toEqual({ sessionId: CHILD, afterSeq: 0 });
    // The child's answer is on screen, inside the agent's own view.
    expect(output).toContain("CHILD-ONLY-ANSWER");
    expect(output).toContain("◯ executor");

    // Leaving puts the main transcript back, and the child's text is not in it.
    stdout.chunks.length = 0;
    stdin.write("\u001B");
    await sleep(150);
    const afterEscape = stdout.text();
    instance.unmount();
    expect(countOf(afterEscape, "CHILD-ONLY-ANSWER")).toBe(0);
  });

  it("never mixes a child's events into the main transcript", async () => {
    const { client, stdin, stdout, instance } = await withDelegate();
    stdout.chunks.length = 0;
    client.emit({
      sessionId: CHILD,
      seq: 2,
      kind: "message.done",
      payload: { role: "assistant", text: "STRAY-CHILD-TEXT" },
    } as SessionEvent);
    await sleep(150);
    const output = stdout.text();
    instance.unmount();
    stdin.end();
    expect(output).not.toContain("STRAY-CHILD-TEXT");
  });
});
