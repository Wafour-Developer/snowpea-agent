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
        event(i * 3 + 1, "tool.call", { callId: `c${i}`, name: "patch", args: {} }),
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

describe("prose that stops for a tool call", () => {
  it("settles the message, so the cards behind it can reach the scrollback", () => {
    // The daemon sends no `message.done` before its tool calls — that event is
    // forwarded to the chat gateways — so the store infers it instead: a
    // message the model stopped writing will not grow again.
    const state = apply(
      ask(initialState, "build it"),
      event(1, "message.delta", { text: "I will start with the parser.\n" }),
      event(2, "tool.call", { callId: "c1", name: "write_file", args: {} }),
      event(3, "tool.result", { callId: "c1", ok: true }),
      event(4, "message.done", { role: "assistant", text: "done" }),
    );

    const prose = state.messages.find((m) => m.text.startsWith("I will start"));
    expect(prose?.streaming).toBe(false);
    // User message, prose, tool card, final answer: all settled, all released.
    expect(settledCount(state, 0, 80)).toBe(state.timeline.length);
  });

  it("no longer pins a whole turn's cards behind one open message", () => {
    // Without the inference this released only the user message: an unsettled
    // entry may not be jumped over, so every card behind it stayed live and the
    // region grew for the length of the turn.
    let state = ask(initialState, "build it");
    for (let i = 0; i < 6; i += 1) {
      state = apply(
        state,
        event(i * 4 + 1, "message.delta", { text: `step ${i}\n` }),
        event(i * 4 + 2, "tool.call", { callId: `c${i}`, name: "write_file", args: {} }),
        event(i * 4 + 3, "tool.result", { callId: `c${i}`, ok: true }),
        event(i * 4 + 4, "diff", { path: `f${i}.ts`, patch: "+x" }),
      );
    }
    expect(state.messages.filter((m) => m.streaming)).toHaveLength(0);
    // Only the trailing run is still held, not the whole turn.
    const held = state.timeline.length - settledCount(state, 0, 20);
    expect(held).toBeLessThan(4);
  });
});

describe("a prompt queued while the answer is streaming", () => {
  it("keeps the in-flight message in one piece", () => {
    // The prompt must not be spliced into the timeline mid-message: the deltas
    // after it would start a second entry, leaving the answer in two halves
    // with the user's line between them and neither half ever closing.
    const streamed = apply(
      ask(initialState, "write a plan"),
      event(1, "message.delta", { text: "1. first step\n2. second step\n" }),
    );
    const queued = reducer(streamed, { type: "user/message", text: "ok" });
    const after = apply(queued, event(2, "message.delta", { text: "3. third step\n" }));

    const assistant = after.messages.filter((m) => m.role === "assistant");
    expect(assistant).toHaveLength(1);
    expect(assistant[0].text).toBe("1. first step\n2. second step\n3. third step\n");
    // The prompt is waiting, not in the transcript and not an entry on screen.
    expect(after.deferredPrompts.map((m) => m.text)).toEqual(["ok"]);
    expect(after.timeline).toHaveLength(2);
  });

  it("lands the prompt after the message it interrupted, once", () => {
    const streamed = apply(
      ask(initialState, "write a plan"),
      event(1, "message.delta", { text: "1. first step\n" }),
    );
    const queued = reducer(streamed, { type: "user/message", text: "ok" });
    const done = apply(
      apply(queued, event(2, "message.delta", { text: "2. second step\n" })),
      event(3, "message.done", { role: "assistant", text: "1. first step\n2. second step\n" }),
    );

    const shape = done.timeline.map((item) => {
      const message = done.messages.find((m) => m.id === item.id);
      return `${message?.role}:${message?.text.split("\n")[0]}`;
    });
    expect(shape).toEqual(["user:write a plan", "assistant:1. first step", "user:ok"]);
    // The prefix exists exactly once.
    expect(done.messages.filter((m) => m.text.startsWith("1. first step"))).toHaveLength(1);
    expect(done.deferredPrompts).toHaveLength(0);
    // All settled, so all of it reaches the scrollback.
    expect(settledCount(done, 0, 80)).toBe(done.timeline.length);
  });

  it("lands it before the tool card when the model stops to call a tool", () => {
    const streamed = apply(
      ask(initialState, "build it"),
      event(1, "message.delta", { text: "starting now\n" }),
    );
    const queued = reducer(streamed, { type: "user/message", text: "also add tests" });
    const after = apply(
      queued,
      event(2, "tool.call", { callId: "c1", name: "write_file", args: {} }),
    );

    const kinds = after.timeline.map((item) =>
      item.kind === "tool" ? "tool" : after.messages.find((m) => m.id === item.id)?.role,
    );
    expect(kinds).toEqual(["user", "assistant", "user", "tool"]);
    expect(after.deferredPrompts).toHaveLength(0);
  });

  it("never strands a prompt when the turn ends without a final message", () => {
    const streamed = apply(
      ask(initialState, "write a plan"),
      event(1, "message.delta", { text: "half an answer" }),
    );
    const queued = reducer(streamed, { type: "user/message", text: "ok" });
    const interrupted = apply(
      queued,
      event(2, "turn.done", { turnId: "t-1", reason: "interrupted" }),
    );

    expect(interrupted.deferredPrompts).toHaveLength(0);
    expect(interrupted.messages[interrupted.messages.length - 1].text).toBe("ok");
    expect(interrupted.messages.filter((m) => m.streaming)).toHaveLength(0);
  });
});
