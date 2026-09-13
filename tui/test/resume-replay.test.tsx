/**
 * Resuming a long session: the replay must land at once, not re-type itself.
 *
 * `session.resume` answers with every event the session ever emitted, and a
 * fresh TUI asks from seq 0. Applied one event at a time that is thousands of
 * reducer passes and thousands of Ink frames, each promoting another finished
 * entry into `<Static>` — so the old transcript appears to be typed back out,
 * slowly, which is what the user reported.
 *
 * The replay is folded in a single reducer pass instead, so the whole
 * reconstructed transcript reaches the scrollback in one render.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { countOf, fakeStdin, fakeStdout, sleep } from "./tty.js";

const OLD = "sess-old";

/** A saved session: 50 tool calls with diffs, and 300 lines of prose. */
function savedEvents(): SessionEvent[] {
  const events: SessionEvent[] = [];
  let seq = 0;
  const push = (kind: string, payload: Record<string, unknown>): void => {
    seq += 1;
    events.push({ sessionId: OLD, seq, kind, payload } as SessionEvent);
  };
  const patch = Array.from({ length: 10 }, (_, i) => `+  line ${i}`).join("\n");

  for (let round = 0; round < 50; round += 1) {
    // Prose, streamed a token at a time the way it originally arrived.
    for (let i = 0; i < 6; i += 1) {
      push("message.reasoning", { text: "thinking", chars: (round * 6 + i + 1) * 8 });
    }
    for (let i = 0; i < 6; i += 1) {
      push("message.delta", { text: `round ${round} line ${i} of the explanation\n` });
    }
    push("message.done", {
      role: "assistant",
      text: Array.from({ length: 6 }, (_, i) => `round ${round} line ${i} of the explanation`)
        .join("\n"),
    });
    const callId = `c-${round}`;
    push("tool.call", { callId, name: "write_file", args: { path: `m${round}.js` } });
    push("tool.result", { callId, ok: true, output: "written" });
    push("diff", { path: `m${round}.js`, patch });
    push("usage", { inputTokens: 500, outputTokens: 40 });
  }
  push("turn.done", { turnId: "t-old", reason: "complete" });
  return events;
}

const SAVED = savedEvents();

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  const calls: string[] = [];
  return {
    calls,
    getStatus: () => "connected",
    setListeners: (l: any) => {
      onSessionEvent = l.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string, params: any) => {
      calls.push(method);
      if (method === "session.resume") {
        return { sessionId: params.sessionId, events: SAVED };
      }
      if (method === "system.info") return { pid: 1, lifecycle: { summary: "idle" } };
      if (method === "session.list") {
        return { sessions: [{ sessionId: OLD, workdir: "/tmp/project", at: Date.now() }] };
      }
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

const live = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: OLD, seq, kind, payload }) as SessionEvent;

describe("resuming a long saved session", () => {
  it("lands the whole transcript at once instead of re-typing it", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdout.chunks.length = 0;

    // `/resume <id>` takes this same path.
    const started = Date.now();
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    // How long until the newest line of the old transcript is on the terminal —
    // that is "the session is back", as opposed to the fixed settle below.
    let visible = 0;
    for (let waited = 0; waited < 2000 && visible === 0; waited += 20) {
      if (stdout.text().includes("round 49 line 5 of the explanation")) visible = Date.now();
      else await sleep(20);
    }
    await sleep(400);
    const elapsed = Date.now() - started;

    const bytes = stdout.chunks.reduce((n, c) => n + c.length, 0);
    const text = stdout.text();
    console.log(
      "RESUME " + JSON.stringify({
        settleMs: elapsed,
        visibleMs: visible - started,
        writes: stdout.chunks.length,
        bytes,
        events: SAVED.length,
      }),
    );

    instance.unmount();
    stdin.end();

    // The transcript is there, and each line exactly once.
    expect(text).toContain("round 0 line 0 of the explanation");
    expect(text).toContain("round 49 line 5 of the explanation");
    expect(countOf(text, "round 25 line 3 of the explanation")).toBe(1);
    // One write per replayed event is what the user saw as re-typing.
    expect(stdout.chunks.length).toBeLessThan(40);
  }, 40000);

  it("keeps streaming live after the replay tail", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    await sleep(700);
    stdout.chunks.length = 0;

    // The session was still running: the tail arrives live, after the replay.
    const base = SAVED.length;
    client.emit(live(base + 1, "message.delta", { text: "STILL-ALIVE-ONE " }));
    await sleep(120);
    client.emit(live(base + 2, "message.delta", { text: "STILL-ALIVE-TWO" }));
    client.emit(live(base + 3, "message.done", {
      role: "assistant",
      text: "STILL-ALIVE-ONE STILL-ALIVE-TWO",
    }));
    await sleep(250);

    const text = stdout.text();
    instance.unmount();
    stdin.end();
    expect(text).toContain("STILL-ALIVE-ONE");
    expect(text).toContain("STILL-ALIVE-TWO");
  }, 40000);

  it("drops events the replay already applied", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    await sleep(700);
    stdout.chunks.length = 0;

    // The daemon re-sends the tail of what it already replayed.
    for (const event of SAVED.slice(-6)) client.emit(event);
    await sleep(250);

    const text = stdout.text();
    instance.unmount();
    stdin.end();
    // Nothing new was drawn for an event already accounted for.
    expect(countOf(text, "round 49 line 5 of the explanation")).toBe(0);
  }, 40000);
});
