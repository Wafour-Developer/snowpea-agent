/**
 * The open agent view, with a delegate streaming behind it.
 *
 * This is the regression the user reported as "펼치니까 계속 깜빡여": expanding a
 * subagent mid-turn made the whole terminal flicker and swallowed the keyboard.
 * Two things caused it, and both are asserted here through real Ink over a fake
 * tty, because both are only visible in the bytes that reach the terminal.
 *
 *  1. Ink clears the screen and rewrites every `<Static>` block it has ever
 *     written as soon as the live region is as tall as the terminal. A fixed
 *     12-row transcript window plus the panel, HUD and input passed that on any
 *     normal-sized terminal, so the clear landed on every frame — see
 *     `agentTranscriptRows`.
 *  2. A child streams one `message.delta` per token, and the TUI is subscribed
 *     to the child's session from the moment the view is opened. One dispatch,
 *     one re-wrap of the child's transcript and one repaint per token is a
 *     hundred frames a second — see `state/coalesce.ts`.
 *
 * The render-count probe below counts `AgentTranscript` renders directly, so a
 * clock tick that rebuilds the rows behind it also fails the test.
 */

import React from "react";
import { render } from "ink";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

/** Renders of the open agent's transcript, counted by the mock below. */
const renders = { count: 0 };

vi.mock("../src/components/AgentTranscript.js", async () => {
  const actual = await vi.importActual<typeof import("../src/components/AgentTranscript.js")>(
    "../src/components/AgentTranscript.js",
  );
  const Probe = (props: any) => {
    renders.count += 1;
    return React.createElement(actual.AgentTranscript, props);
  };
  // Memoized like the real export, so the count measures prop changes rather
  // than every render of the app around it.
  return { ...actual, AgentTranscript: React.memo(Probe) };
});

// Imported after the mock is declared so `App` picks up the probe.
const { App } = await import("../src/app.js");

/** Ink's full-screen clear — the flicker, in one escape sequence. */
const CLEAR_TERMINAL = "\u001B[2J";
const DOWN = "\u001B[B";
const ESC = "\u001B";

const CHILD = "child-1";

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
    call: async (method: string, params: any) => {
      if (method === "session.resume") return { sessionId: params.sessionId, events: [] };
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

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq, kind, payload }) as SessionEvent;

const childDelta = (seq: number, text: string): SessionEvent =>
  ({ sessionId: CHILD, seq, kind: "message.delta", payload: { text } }) as SessionEvent;

/** A running turn with one delegate, its transcript opened mid-stream. */
async function openAgentView(rows = 24) {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, rows);
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
  // A turn has to be running for the flicker to show: that is what puts the
  // spinner on a timer and the delegate on the wire.
  stdin.write("hi");
  await sleep(60);
  stdin.write("\r");
  await sleep(150);
  // input → footer → ● main → ◯ executor, then Enter to open it.
  for (let i = 0; i < 3; i += 1) {
    stdin.write(DOWN);
    await sleep(60);
  }
  stdin.write("\r");
  await sleep(200);
  return { client, stdin, stdout, instance };
}

/** Push `count` tokens `gapMs` apart, the way a fast endpoint would. */
async function stream(client: ReturnType<typeof fakeClient>, count: number, gapMs = 10) {
  for (let i = 0; i < count; i += 1) {
    client.emit(childDelta(10 + i, `tok${i} `));
    await sleep(gapMs);
  }
}

describe("the open agent view while its delegate streams", () => {
  beforeEach(() => {
    renders.count = 0;
  });

  it("never clears the terminal, whatever the delegate is saying", async () => {
    const { client, stdin, stdout, instance } = await openAgentView(24);
    expect(stdout.text()).toContain("◯ executor");
    stdout.chunks.length = 0;

    await stream(client, 60);
    await sleep(120);

    const clears = stdout.chunks.filter((chunk) => chunk.includes(CLEAR_TERMINAL));
    instance.unmount();
    stdin.end();
    // Ink's other branch rewrites the whole scrollback before every frame; on a
    // 24-row terminal the old fixed-height window took it on every single one.
    expect(clears).toHaveLength(0);
  }, 30000);

  it("repaints the transcript a few times per 100ms of tokens, not once per token", async () => {
    const { client, stdin, instance } = await openAgentView();
    renders.count = 0;

    // 40 tokens inside ~100 ms: about as fast as a local endpoint streams.
    for (let i = 0; i < 40; i += 1) client.emit(childDelta(100 + i, `t${i} `));
    await sleep(100);
    const inWindow = renders.count;

    instance.unmount();
    stdin.end();
    // The leading token, then one repaint per ~66 ms window.
    expect(inWindow).toBeGreaterThan(0);
    expect(inWindow).toBeLessThanOrEqual(4);
  }, 30000);

  it("keeps every token, in order, despite the batching", async () => {
    const { client, stdin, stdout, instance } = await openAgentView(40);
    stdout.chunks.length = 0;

    await stream(client, 12, 8);
    client.emit({
      sessionId: CHILD,
      seq: 200,
      kind: "message.done",
      payload: { role: "assistant", text: Array.from({ length: 12 }, (_, i) => `tok${i} `).join("") },
    } as SessionEvent);
    await sleep(200);

    const output = stdout.text();
    instance.unmount();
    stdin.end();
    for (const i of [0, 1, 5, 10, 11]) expect(output).toContain(`tok${i}`);
  }, 30000);

  it("does not repaint the transcript on a clock tick", async () => {
    const { stdin, instance } = await openAgentView();
    // Settle, then watch a full second of spinner frames (five of them) and a
    // tick of the once-a-second clock go by with nothing arriving.
    await sleep(200);
    renders.count = 0;
    await sleep(1100);
    const onTicks = renders.count;

    instance.unmount();
    stdin.end();
    // The elapsed column in the header moves once a second; the spinner's five
    // frames a second must not reach this subtree at all.
    expect(onTicks).toBeLessThanOrEqual(2);
  }, 30000);

  it("still answers the keyboard while the delegate streams", async () => {
    const { client, stdin, stdout, instance } = await openAgentView();
    // Enough output that the window is a window: otherwise there is nothing to
    // scroll and the indicator never appears.
    client.emit(childDelta(50, Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n")));
    await sleep(150);
    stdout.chunks.length = 0;

    // Scroll up, with tokens still arriving behind the keypress.
    const scrolling = stream(client, 30, 10);
    stdin.write("\u001B[A");
    await sleep(120);
    expect(stdout.text()).toContain("▼");

    stdout.chunks.length = 0;
    stdin.write(ESC); // back to the main transcript
    await sleep(200);
    const afterEscape = stdout.text();
    await scrolling;
    instance.unmount();
    stdin.end();
    // Esc was seen: the agent view is gone, tokens or no tokens.
    expect(afterEscape).not.toContain("Esc back to the main transcript");
    expect(afterEscape.length).toBeGreaterThan(0);
  }, 30000);
});
