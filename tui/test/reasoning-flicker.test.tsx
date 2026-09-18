/**
 * A long implementing turn: many finished tool cards, then a reasoning stream.
 *
 * The flicker reported against v0.1.13, in the main session rather than the
 * open agent view. Two things multiplied:
 *
 *   - every `message.reasoning` moved the "Thinking (n chars)…" line, and the
 *     provider sends one per fragment, so the live region was repainted about
 *     a hundred times a second to move a number, and
 *   - a turn's finished tool cards were held out of `<Static>` until the turn
 *     ended, so after a dozen edits the live region was taller than the
 *     terminal — and at that height Ink clears the screen and rewrites the
 *     whole scrollback before *every* frame.
 *
 * Measured on a 100×30 terminal, 120 reasoning events over ~1.2 s: 130 writes,
 * 130 of them full-screen clears, 350 KB/s. Now 11 writes, no clears, 10 KB/s.
 * The control — the same stream with no tool cards — never cleared even
 * before, which is what pins the second cause to the region's height.
 *
 * Each test prints its numbers and then asserts the bound, so a regression
 * reports what it actually cost.
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
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq: (seq += 1), kind, payload }) as SessionEvent;

const PATCH = [
  "--- a/src/thing.ts",
  "+++ b/src/thing.ts",
  "@@ -1,4 +1,6 @@",
  " export function thing() {",
  "-  return 1;",
  "+  // a comment the model added",
  "+  return 2;",
  " }",
].join("\n");

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

describe("a long implementing turn", () => {
  it("measures the live region while reasoning streams", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 30);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(150);

    // Start a turn.
    stdin.write("implement the thing");
    await sleep(60);
    stdin.write("\r");
    await sleep(150);

    // Twelve finished write_file/patch cards with diffs, as in the report.
    for (let i = 0; i < 12; i += 1) {
      const callId = `call-${i}`;
      client.emit(ev("tool.call", { callId, name: "patch", args: { path: `src/f${i}.ts` } }));
      await sleep(5);
      client.emit(ev("tool.result", { callId, ok: true, output: "edited" }));
      client.emit(ev("diff", { path: `src/f${i}.ts`, patch: PATCH }));
      await sleep(5);
    }
    await sleep(150);

    const settled = stdout.chunks.length;
    stdout.chunks.length = 0;

    // Then the model thinks: one message.reasoning per delta, 8 chars each.
    const t0 = Date.now();
    let chars = 0;
    for (let i = 0; i < 120; i += 1) {
      chars += 8;
      client.emit(ev("message.reasoning", { text: "thinking", chars }));
      await sleep(8);
    }
    await sleep(120);
    const elapsed = Date.now() - t0;

    const result = stats(stdout.chunks, elapsed);
    instance.unmount();
    stdin.end();
    console.log(
      `REASONING settledWrites=${settled} ` + JSON.stringify({ elapsed, ...result }),
    );
    // The regression: every one of these was a full-screen clear followed by a
    // rewrite of the entire scrollback, eighty times a second.
    expect(result.clears).toBe(0);
    // Four thinking updates a second plus the spinner's five frames.
    expect(result.perSec).toBeLessThan(15);
  }, 40000);

  it("measures the same reasoning stream with no tool cards (control)", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 30);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(150);
    stdin.write("think");
    await sleep(60);
    stdin.write("\r");
    await sleep(150);
    stdout.chunks.length = 0;

    const t0 = Date.now();
    let chars = 0;
    for (let i = 0; i < 120; i += 1) {
      chars += 8;
      client.emit(ev("message.reasoning", { text: "thinking", chars }));
      await sleep(8);
    }
    await sleep(120);
    const elapsed = Date.now() - t0;
    const result = stats(stdout.chunks, elapsed);
    instance.unmount();
    stdin.end();
    console.log("CONTROL " + JSON.stringify({ elapsed, ...result }));
    expect(result.clears).toBe(0);
    expect(result.perSec).toBeLessThan(15);
  }, 40000);

  it("measures a usage-counter stream on its own", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 30);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(150);
    stdin.write("go");
    await sleep(60);
    stdin.write("\r");
    await sleep(150);
    stdout.chunks.length = 0;

    const t0 = Date.now();
    for (let i = 0; i < 60; i += 1) {
      client.emit(ev("usage", { inputTokens: 1000, outputTokens: 20 }));
      await sleep(10);
    }
    await sleep(120);
    const elapsed = Date.now() - t0;
    const result = stats(stdout.chunks, elapsed);
    instance.unmount();
    stdin.end();
    console.log("USAGE " + JSON.stringify({ elapsed, ...result }));
    expect(result.clears).toBe(0);
    expect(result.perSec).toBeLessThan(15);
  }, 40000);
});
