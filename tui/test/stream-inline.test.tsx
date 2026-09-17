/**
 * Streaming should grow terminal scrollback like Codex/Hermes, not hide text
 * behind a "… n lines above" marker.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

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
    call: async (method: string) =>
      method === "system.info"
        ? { pid: 1, lifecycle: { summary: "idle" } }
        : { commands: [], tools: [], agents: [] },
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq: (seq += 1), kind, payload }) as SessionEvent;

describe("inline streaming scrollback", () => {
  it("commits the head to scrollback while the tail keeps streaming", async () => {
    const lines = Array.from({ length: 40 }, (_, index) => `ROW-${index + 1}`);
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 24);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(120);
    stdin.write("explain this");
    stdin.write("\r");
    await sleep(120);
    client.emit(ev("message.delta", { text: lines.join("\n") }));
    await sleep(200);

    const seen = stdout.text();
    instance.unmount();
    stdin.end();

    expect(seen).toContain("ROW-1");
    expect(seen).toContain("ROW-40");
    expect(seen).not.toContain("lines above");
  });
});
