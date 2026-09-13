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

describe("the height bound on what is held back", () => {
  /** A turn that has already finished `count` edits, each with a diff. */
  function longTurn(count: number, patchRows = 8): State {
    let state = ask(initialState, "implement it");
    const patch = Array.from({ length: patchRows }, (_, i) => `+line ${i}`).join("\n");
    for (let i = 0; i < count; i += 1) {
      state = apply(
        state,
        event(i * 3 + 1, "tool.call", { callId: `c${i}`, name: "edit_file", args: {} }),
        event(i * 3 + 2, "tool.result", { callId: `c${i}`, ok: true }),
        event(i * 3 + 3, "diff", { path: `f${i}.ts`, patch }),
      );
    }
    return state;
  }

  it("still holds a short run whole, so its summary stays one line", () => {
    const state = longTurn(2);
    // 1 user message + 2 × (tool, diff)
    expect(state.timeline).toHaveLength(5);
    expect(settledCount(state, 0, 80)).toBe(1);
  });

  it("releases the older cards of a long run rather than filling the screen", () => {
    const state = longTurn(12);
    expect(state.timeline).toHaveLength(25);

    const held = state.timeline.length - settledCount(state, 0, 20);
    expect(held).toBeGreaterThan(0);
    // What stays live has to fit; the whole run would be some 120 rows.
    expect(held).toBeLessThan(6);
  });

  it("holds everything when no bound is given, as it always did", () => {
    const state = longTurn(12);
    expect(settledCount(state, 0)).toBe(1);
  });

  it("never goes backwards once entries have been released", () => {
    const state = longTurn(12);
    const first = settledCount(state, 0, 20);
    expect(settledCount(state, first, 20)).toBeGreaterThanOrEqual(first);
  });

  it("releases the whole run at the end of the turn regardless", () => {
    const state = apply(
      longTurn(12),
      event(99, "turn.done", { turnId: "t-1", reason: "complete" }),
    );
    expect(settledCount(state, 0, 20)).toBe(state.timeline.length);
  });
});
