/**
 * The default inline layout, driven through real Ink over a fake tty.
 *
 * What matters here is that a finished transcript entry is *written once*. Ink
 * hands `<Static>` output straight to the terminal and never redraws it, so an
 * entry that appears twice would mean it is being repainted — the flicker, and
 * a scrollback full of duplicates.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { countOf, fakeStdin, fakeStdout, sleep, type } from "./tty.js";

/** The few `TuiClient` methods `App` reaches for, and a hook to push events. */
function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  return {
    getStatus: () => "connected",
    setListeners: (listeners: any) => {
      onSessionEvent = listeners.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string) =>
      method === "system.info"
        ? { pid: 4242, lifecycle: { summary: "will not exit: 1 job" } }
        : { commands: [] },
    prompt: async () => ({ turnId: "turn-1" }),
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

describe("inline layout", () => {
  it("writes each finished entry to the scrollback exactly once", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 20);

    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    await sleep(80);
    // A turn: the assistant streams, then finishes.
    client.emit(event(1, "message.delta", { text: "partial" }));
    await sleep(60);
    client.emit(event(2, "message.done", { text: "SETTLED-ANSWER", role: "assistant" }));
    client.emit(event(3, "tool.call", { callId: "c1", name: "read", args: { path: "a.ts" } }));
    client.emit(event(4, "tool.result", { callId: "c1", ok: true, output: "ok" }));
    client.emit(event(5, "message.done", { text: "SECOND-ANSWER", role: "assistant" }));
    client.emit(event(6, "turn.done", {}));
    await sleep(120);

    // Typing must not bring any of it back.
    await type(stdin, "hello");
    const output = stdout.text();
    instance.unmount();

    expect(countOf(output, "SETTLED-ANSWER")).toBe(1);
    expect(countOf(output, "SECOND-ANSWER")).toBe(1);
    // The logo is printed once, at the top of the session.
    expect(countOf(output, "open-source multi-vendor coding agent")).toBe(1);
    // The live region is all that repaints: no screen clear, no scrollback wipe.
    expect(output).not.toContain("[2J");
    expect(output).not.toContain("[3J");
  });

  it("keeps the HUD under the input and never draws a header bar", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 20);

    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );

    await sleep(150);
    await type(stdin, "hi");
    const output = stdout.text();
    instance.unmount();

    // The workdir appears in the HUD, and only there.
    expect(output).toContain("/tmp/project");
    expect(output).toContain("Mode: ACCEPT");
    expect(output).toContain("daemon: pid 4242");
    // The old header put the product name and the path on one line above the
    // transcript; nothing like it is drawn any more.
    expect(output).not.toContain("snowpea · /tmp/project");
  });
});
