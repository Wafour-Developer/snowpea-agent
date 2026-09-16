/**
 * Prompts that wait: what the store does with `turn.queued` and
 * `turn.dequeued`.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";
import { queuedLabel } from "../src/state/working.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

beforeEach(() => {
  __resetIdCounter();
});

describe("the queue", () => {
  it("starts empty", () => {
    expect(initialState.queued).toEqual([]);
  });

  it("remembers a prompt the daemon queued, in order", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }),
      event(2, "turn.queued", { turnId: "t-2", position: 2, queued: 2 }),
    );
    expect(state.queued.map((entry) => entry.turnId)).toEqual(["t-1", "t-2"]);
    expect(state.queued[1].position).toBe(2);
  });

  it("takes the text from the prompt call that answered with the same id", () => {
    let state = apply(initialState, event(1, "turn.queued", { turnId: "t-1", position: 1 }));
    expect(state.queued[0].text).toBe("");
    state = reducer(state, { type: "prompt/turn", turnId: "t-1", text: "run the tests" });
    expect(state.queued[0].text).toBe("run the tests");
  });

  it("keeps the text when the prompt call answers before the queue event", () => {
    // This is the real order: `session.prompt` returns, and only then does the
    // daemon say the prompt went into the queue.
    let state = reducer(initialState, { type: "prompt/turn", turnId: "t-1", text: "run the tests" });
    expect(state.queued).toEqual([]);
    state = apply(state, event(1, "turn.queued", { turnId: "t-1", position: 1 }));
    expect(state.queued[0].text).toBe("run the tests");
  });

  it("forgets a turn's text once the turn is over", () => {
    let state = reducer(initialState, { type: "prompt/turn", turnId: "t-1", text: "done with" });
    state = apply(state, event(1, "turn.done", { turnId: "t-1", reason: "complete" }));
    expect(state.promptTexts).toEqual({});
  });

  it("updates the position when the daemon re-reports one", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 2 }),
      event(2, "turn.queued", { turnId: "t-1", position: 1 }),
    );
    expect(state.queued).toHaveLength(1);
    expect(state.queued[0].position).toBe(1);
  });

  it("drops an entry when its turn starts", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 1 }),
      event(2, "turn.queued", { turnId: "t-2", position: 2 }),
      event(3, "turn.dequeued", { turnId: "t-1", reason: "started", queued: 1 }),
    );
    expect(state.queued.map((entry) => entry.turnId)).toEqual(["t-2"]);
  });

  it("drops an entry that was discarded", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 1 }),
      event(2, "turn.dequeued", { turnId: "t-1", reason: "dropped", queued: 0 }),
    );
    expect(state.queued).toEqual([]);
  });

  it("clears the queued pill on steered and keeps one user line", () => {
    let state = reducer(initialState, { type: "user/message", text: "run this now" });
    state = reducer(state, { type: "prompt/turn", turnId: "t-1", text: "run this now" });
    state = apply(
      state,
      event(1, "turn.queued", { turnId: "t-1", position: 1, queued: 1 }),
      event(2, "turn.dequeued", { turnId: "t-1", reason: "steered", queued: 0 }),
      event(3, "message.user", { text: "run this now", steered: true }),
    );
    expect(state.queued).toEqual([]);
    expect(state.pendingEchoes).toEqual([]);
    expect(state.messages.filter((entry) => entry.role === "user" && entry.text === "run this now")).toHaveLength(1);
  });

  it("ignores a queue event with no turn id", () => {
    const state = apply(initialState, event(1, "turn.queued", { position: 1 }));
    expect(state.queued).toEqual([]);
  });
});

describe("queuedLabel", () => {
  it("says how many are waiting", () => {
    expect(queuedLabel(1)).toBe("⏳ 1 queued");
    expect(queuedLabel(3)).toBe("⏳ 3 queued");
  });
});

describe("model.changed", () => {
  it("follows the daemon's routing, provider and all", () => {
    const state = apply(
      initialState,
      event(1, "model.changed", { model: "claude-sonnet-4-5", provider: "anthropic" }),
    );
    expect(state.model).toBe("claude-sonnet-4-5");
    expect(state.provider).toBe("anthropic");
  });

  it("keeps a source tag when the event carries one, and leaves it alone when not", () => {
    let state = apply(initialState, event(1, "model.changed", { model: "a", source: "pin" }));
    expect(state.modelSource).toBe("pin");
    state = apply(state, event(2, "model.changed", { model: "b" }));
    expect(state.modelSource).toBe("pin");
    expect(state.model).toBe("b");
  });

  it("clears the model when a pin is dropped, rather than naming a gone pin", () => {
    // What the daemon actually sends on `session.setModel {model: null}`.
    const state = apply(
      initialState,
      event(1, "model.changed", { model: "pinned-model", provider: "fake", source: "pin" }),
      event(2, "model.changed", { model: null, provider: null }),
    );
    expect(state).toMatchObject({ model: null, provider: null, modelSource: null });
  });

  it("holds what it had when the event says nothing", () => {
    const state = apply(
      initialState,
      event(1, "model.changed", { model: "a", provider: "openai" }),
      event(2, "model.changed", {}),
    );
    expect(state).toMatchObject({ model: "a", provider: "openai" });
  });
});
