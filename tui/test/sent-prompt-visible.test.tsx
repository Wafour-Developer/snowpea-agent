/**
 * A prompt the user just sent is on screen in the very next frame — idle,
 * mid-answer, and after the daemon steers it into the running turn.
 *
 * Mid-answer the prompt waits in `deferredPrompts` (splicing it into the
 * timeline would split the open answer in two). It used to be drawn only by
 * the queued list, which is fed by `turn.queued`: for the round trip before
 * that event, and from `turn.dequeued {reason: "steered"}` until the answer
 * closed, the prompt was nowhere — "입력했는데 입력된 message가 안보이는" case.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer, waitingPrompts, type State } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({
  sessionId: "sess-1",
  seq,
  kind,
  payload,
});

const apply = (state: State, ...events: SessionEvent[]): State =>
  events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);

/** Where a person can see the prompt: the transcript, or the list under the input. */
function visible(state: State, text: string): boolean {
  const inTimeline = state.timeline.some(
    (item) => item.kind === "message" && state.messages.find((m) => m.id === item.id)?.text === text,
  );
  return inTimeline || waitingPrompts(state).some((entry) => entry.text === text);
}

describe("a sent prompt is always somewhere visible (reducer)", () => {
  const streaming = apply(
    initialState,
    event(1, "turn.started", { turnId: "t-0" }),
    event(2, "message.delta", { text: "an answer in progress" }),
  );

  it("idle: it enters the timeline", () => {
    const state = reducer(initialState, { type: "user/message", text: "hello" });
    expect(visible(state, "hello")).toBe(true);
  });

  it("mid-answer, before turn.queued arrives", () => {
    const state = reducer(streaming, { type: "user/message", text: "두 번째 질문" });
    expect(state.deferredPrompts).toHaveLength(1);
    expect(visible(state, "두 번째 질문")).toBe(true);
  });

  it("mid-answer, queued, then steered, then the answer closes", () => {
    let state = reducer(streaming, { type: "user/message", text: "steer me" });
    state = reducer(state, { type: "prompt/turn", turnId: "t-1", text: "steer me" });
    state = apply(state, event(3, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }));
    expect(visible(state, "steer me")).toBe(true);
    // One row, not the queued row and the deferred prompt both.
    expect(waitingPrompts(state).filter((entry) => entry.text === "steer me")).toHaveLength(1);

    state = apply(
      state,
      event(4, "turn.dequeued", { turnId: "t-1", reason: "steered", queued: 0 }),
      event(5, "message.user", { text: "steer me", steered: true }),
    );
    expect(visible(state, "steer me")).toBe(true);

    state = apply(state, event(6, "message.done", { text: "an answer in progress, finished" }));
    expect(waitingPrompts(state)).toEqual([]);
    expect(visible(state, "steer me")).toBe(true);
    expect(state.messages.filter((m) => m.text === "steer me")).toHaveLength(1);
  });
});

function fakeClient() {
  let listeners: any;
  let turns = 0;
  return {
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.7", latest: "0.1.7" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: `t-${++turns}` }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(e: SessionEvent) {
      listeners?.onSessionEvent?.(e);
    },
  };
}

describe("a sent prompt is in the next frame (real Ink)", () => {
  it("mid-answer, before the queue event and after it was steered", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 24);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    client.emit(event(1, "turn.started", { turnId: "t-0" }));
    client.emit(event(2, "message.delta", { text: "긴 답변이 아직 오는 중입니다 " }));
    await sleep(150);

    await type(stdin, "follow up please", 10);
    stdout.chunks.length = 0;
    stdin.write("\r");
    await sleep(120);
    const afterEnter = stdout.chunks.at(-1) ?? "";

    client.emit(event(3, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }));
    client.emit(event(4, "turn.dequeued", { turnId: "t-1", reason: "steered", queued: 0 }));
    client.emit(event(5, "message.user", { text: "follow up please", steered: true }));
    client.emit(event(6, "message.delta", { text: "더 많은 글자 " }));
    await sleep(200);
    const afterSteer = stdout.chunks.at(-1) ?? "";

    client.emit(event(7, "message.done", { text: "긴 답변이 아직 오는 중입니다 더 많은 글자" }));
    await sleep(200);
    const all = stdout.text();
    instance.unmount();
    stdin.end();

    expect(afterEnter).toContain("follow up please");
    expect(afterSteer).toContain("follow up please");
    expect(all).toContain("follow up please");
  }, 20000);
});
