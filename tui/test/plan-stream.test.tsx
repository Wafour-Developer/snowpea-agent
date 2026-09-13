/**
 * One long assistant message, streaming — plan mode.
 *
 * A plan is a single `message.delta` stream that runs for minutes with no tool
 * cards to break it up. The entry stays in the live region until
 * `message.done`, so its height grows without bound; once it passes the
 * terminal's rows Ink clears the screen and rewrites the whole scrollback on
 * every token, which inside tmux reads as the view shaking between the top and
 * the bottom of the text.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const CLEAR_TERMINAL = "[2J";

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  return {
    getStatus: () => "connected",
    setListeners: (l: any) => {
      onSessionEvent = l.onSessionEvent;
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
    setMode: async () => ({ mode: "plan" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq: (seq += 1), kind, payload }) as SessionEvent;

/** A plan: 300 numbered lines, streamed a line at a time. */
const PLAN_LINES = Array.from({ length: 300 }, (_, i) => `${i + 1}. step number ${i + 1}`);
const PLAN = PLAN_LINES.join("\n");

function stats(chunks: string[], ms: number) {
  const bytes = chunks.reduce((n, c) => n + c.length, 0);
  return {
    chunks: chunks.length,
    clears: chunks.filter((c) => c.includes(CLEAR_TERMINAL)).length,
    bytes,
    perSec: Number((chunks.length / (ms / 1000)).toFixed(1)),
    bytesPerSec: Math.round(bytes / (ms / 1000)),
  };
}

async function streamPlan(rows = 30) {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, rows);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="plan" workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(150);
  stdin.write("plan the work");
  await sleep(60);
  stdin.write("\r");
  await sleep(150);
  stdout.chunks.length = 0;

  const t0 = Date.now();
  for (const line of PLAN_LINES) {
    client.emit(ev("message.delta", { text: `${line}\n` }));
    await sleep(4);
  }
  await sleep(150);
  const elapsed = Date.now() - t0;
  return { client, stdin, stdout, instance, elapsed };
}

describe("a long plan streaming into the live region", () => {
  it.each([16, 18, 20, 24, 30, 50])("stays inside a %i-row terminal while it streams", async (rows) => {
    const { client, stdin, stdout, instance, elapsed } = await streamPlan(rows);
    const result = stats(stdout.chunks, elapsed);
    console.log(`PLAN rows=${rows} ` + JSON.stringify({ elapsed, ...result }));
    // Ink opens each incremental frame with eraseLines(previousHeight): one
    // ESC[2K per row and an ESC[1A between them, so the count of cursor-ups
    // plus one is exactly how tall the live region was on the frame before.
    const heights = stdout.chunks
      .filter((c) => !c.includes(CLEAR_TERMINAL))
      .map((c) => (c.match(/\u001B\[1A/g) ?? []).length + 1);
    const maxHeight = Math.max(0, ...heights);
    console.log(`LIVE_HEIGHT rows=${rows} max=${maxHeight} samples=${heights.length}`);
    // The invariant: Ink keeps updating in place only while the dynamic region
    // is shorter than the terminal. At or above it, every frame clears the
    // screen and re-emits the whole scrollback.
    expect(maxHeight).toBeLessThan(rows);

    // The shake: a full-screen clear plus a rewrite of the scrollback, per token.
    expect(result.clears).toBe(0);
    // Fifteen text frames a second plus the spinner's five.
    expect(result.perSec).toBeLessThan(25);

    client.emit(ev("message.done", { role: "assistant", text: PLAN }));
    await sleep(200);
    instance.unmount();
    stdin.end();
  }, 40000);

  it("stays inside the terminal during an implementing turn too", async () => {
    // What the user reported second: accept mode, edits landing with diffs
    // while prose streams between them. Both kinds of growth at once.
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(150);
    stdin.write("build it");
    await sleep(60);
    stdin.write("\r");
    await sleep(150);
    stdout.chunks.length = 0;

    const patch = Array.from({ length: 14 }, (_, i) => `+  line ${i}`).join("\n");
    const t0 = Date.now();
    for (let round = 0; round < 6; round += 1) {
      for (let i = 0; i < 8; i += 1) {
        client.emit(ev("message.delta", { text: `explaining module ${round} part ${i}\n` }));
        await sleep(5);
      }
      // No `message.done` here, deliberately: this is exactly what the daemon
      // sends, and the store has to infer from the tool call that the prose is
      // finished. Without that inference the message never settles, and an
      // unsettled entry holds every card behind it in the live region.
      const callId = `c-${round}`;
      client.emit(ev("tool.call", { callId, name: "write_file", args: { path: `m${round}.js` } }));
      await sleep(10);
      client.emit(ev("tool.result", { callId, ok: true, output: "written" }));
      client.emit(ev("diff", { path: `m${round}.js`, patch }));
      await sleep(20);
    }
    await sleep(200);
    const elapsed = Date.now() - t0;

    const result = stats(stdout.chunks, elapsed);
    const heights = stdout.chunks
      .filter((c) => !c.includes(CLEAR_TERMINAL))
      .map((c) => (c.match(/\u001B\[1A/g) ?? []).length + 1);
    const maxHeight = Math.max(0, ...heights);
    console.log("IMPLEMENTING " + JSON.stringify({ elapsed, ...result, maxHeight }));

    instance.unmount();
    stdin.end();
    expect(result.clears).toBe(0);
    expect(maxHeight).toBeLessThan(35);
    // The write rate here is not the text rate: every entry promoted into
    // `<Static>` makes Ink render immediately, by design, so six rounds of
    // done/call/result/diff are frames of their own. They are incremental
    // repaints of a short region rather than screen clears, which is why the
    // bound that matters is the bytes.
    expect(result.bytesPerSec).toBeLessThan(150_000);
  }, 40000);

  it("puts the whole plan in the scrollback once it is done", async () => {
    const { client, stdin, stdout, instance } = await streamPlan();
    client.emit(ev("message.done", { role: "assistant", text: PLAN }));
    client.emit(ev("turn.done", { turnId: "t", reason: "complete" }));
    await sleep(300);

    const seen = stdout.text();
    instance.unmount();
    stdin.end();

    // Every line reached the terminal, in order, and the text is complete.
    let at = -1;
    for (const line of PLAN_LINES) {
      const next = seen.indexOf(line, at + 1);
      expect(next, `"${line}" missing from the scrollback`).toBeGreaterThan(at);
      at = next;
    }
  }, 40000);
});
