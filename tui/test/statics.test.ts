/**
 * Which transcript entries reach the terminal's scrollback, and when.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { isSettled, settledCount } from "../src/layout/statics.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const ask = (state: State, text: string): State => reducer(state, { type: "user/message", text });

beforeEach(() => {
  __resetIdCounter();
});

describe("settledCount", () => {
  it("releases a user message straight away", () => {
    const state = ask(initialState, "hello");
    expect(settledCount(state)).toBe(1);
  });

  it("holds a streaming assistant message back", () => {
    const state = apply(ask(initialState, "hello"), event(1, "message.delta", { text: "thin" }));
    expect(state.timeline).toHaveLength(2);
    expect(settledCount(state)).toBe(1);
  });

  it("releases the message once it is done", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "message.delta", { text: "thin" }),
      event(2, "message.done", { text: "thinking done", role: "assistant" }),
    );
    expect(settledCount(state)).toBe(2);
  });

  it("keeps the newest tool call live so Ctrl+O can still expand it", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "read", args: {} }),
      event(2, "tool.result", { callId: "c1", ok: true, output: "done" }),
    );
    expect(isSettled(state, state.timeline[1])).toBe(true);
    expect(settledCount(state)).toBe(1);
  });

  it("waits for the whole run: a second call joins the first", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "shell", args: {} }),
      event(2, "tool.result", { callId: "c1", ok: true, output: "a" }),
      event(3, "tool.call", { callId: "c2", name: "shell", args: {} }),
    );
    // c1 is settled, but c2 may still be joined by more calls in the same run.
    expect(settledCount(state)).toBe(1);
  });

  it("releases the whole run once the turn is over", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "shell", args: {} }),
      event(2, "tool.result", { callId: "c1", ok: true, output: "a" }),
      event(3, "tool.call", { callId: "c2", name: "shell", args: {} }),
      event(4, "tool.result", { callId: "c2", ok: true, output: "b" }),
      event(5, "turn.done", {}),
    );
    expect(state.turnActive).toBe(false);
    expect(settledCount(state)).toBe(3);
  });

  it("releases a finished tool call once something newer follows it", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "read", args: {} }),
      event(2, "tool.result", { callId: "c1", ok: true, output: "done" }),
      event(3, "message.done", { text: "all set", role: "assistant" }),
    );
    expect(settledCount(state)).toBe(3);
  });

  it("never lets a later entry jump ahead of an unfinished one", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "read", args: {} }),
      event(2, "diff", { path: "a.ts", patch: "+x" }),
    );
    // The diff is settled, but the running tool call is in front of it.
    expect(isSettled(state, state.timeline[2])).toBe(true);
    expect(settledCount(state)).toBe(1);
  });

  it("never goes backwards", () => {
    const state = apply(ask(initialState, "hello"), event(1, "message.delta", { text: "thin" }));
    expect(settledCount(state, 2)).toBe(2);
  });

  it("counts nothing on an empty session", () => {
    expect(settledCount(initialState)).toBe(0);
  });
});

describe("holding a diff back", () => {
  it("keeps a trailing diff live while the turn runs, so a badge can land", () => {
    const state = apply(
      ask(initialState, "edit it"),
      event(1, "diff", { path: "a.py", patch: "+x" }),
    );
    expect(state.turnActive).toBe(true);
    expect(settledCount(state)).toBe(1);
  });

  it("releases it once the turn is over", () => {
    const state = apply(
      ask(initialState, "edit it"),
      event(1, "diff", { path: "a.py", patch: "+x" }),
      event(2, "turn.done", { turnId: "t-1", reason: "complete" }),
    );
    expect(settledCount(state)).toBe(2);
  });

  it("releases it when something that is not a diff follows", () => {
    const state = apply(
      ask(initialState, "edit it"),
      event(1, "diff", { path: "a.py", patch: "+x" }),
      event(2, "message.done", { role: "assistant", text: "done" }),
    );
    expect(settledCount(state)).toBe(3);
  });
});
