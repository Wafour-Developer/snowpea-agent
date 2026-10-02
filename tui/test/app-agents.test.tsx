import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { countOf, fakeStdin, fakeStdout, sleep, type } from "./tty.js";

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  return {
    getStatus: () => "connected",
    setListeners: (listeners: any) => {
      onSessionEvent = listeners.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string) => {
      if (method === "system.info") return { pid: 4242, lifecycle: { summary: "idle" } };
      if (method === "command.list") return { commands: [{ name: "ralph", summary: "loop", source: "builtin" }] };
      if (method === "agent.list") return { agents: [] };
      if (method === "command.run") return { turnId: "t-1" };
      return {};
    },
    prompt: async () => ({ turnId: "turn-1" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(ev: SessionEvent) {
      onSessionEvent?.(ev);
    },
  };
}

describe("inline App agent footer", () => {
  it("opens More and collapses back to two rows when focus returns to the command input", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 36);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(120);
    for (let index = 0; index < 4; index += 1) {
      client.emit({
        sessionId: "sess-1",
        seq: index + 1,
        kind: "subagent.spawn",
        payload: {
          agentId: `a${index}`,
          name: `agent-${index}`,
          task: `work ${index}`,
          status: "running",
          at: Date.now() + index,
        },
      } as SessionEvent);
    }
    await sleep(120);
    expect(stdout.text()).toContain("2 more");

    stdin.write("\x1b[B");
    stdin.write("\x1b[B");
    stdin.write("\x1b[B");
    stdin.write("\x1b[B");
    stdin.write("\x1b[B");
    stdin.write("\r");
    await sleep(160);
    expect(stdout.text()).toContain("agent-0");

    stdin.write("\x1b[A");
    stdin.write("\x1b[A");
    stdin.write("\x1b[A");
    stdin.write("\x1b[A");
    stdin.write("\x1b[A");
    await sleep(200);
    const afterCollapse = stdout.chunks.at(-1) ?? "";
    expect(afterCollapse).toContain("2 more");
    expect(afterCollapse).not.toContain("agent-0");

    instance.unmount();
    stdin.end();
  });
});

describe("inline App slash echo", () => {
  it("keeps a slash skill prompt to one visible row when message.user is replayed", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 36);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(120);
    await type(stdin, "/ralph fix it", 5);
    stdin.write("\r");
    await sleep(120);
    client.emit({
      sessionId: "sess-1",
      seq: 10,
      kind: "message.user",
      payload: { text: "/ralph fix it" },
    } as SessionEvent);
    await sleep(250);
    const frames = stdout.chunks.filter((chunk) => chunk.includes("› /ralph fix it"));
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.every((chunk) => countOf(chunk, "› /ralph fix it") === 1)).toBe(true);

    instance.unmount();
    stdin.end();
  });
});
