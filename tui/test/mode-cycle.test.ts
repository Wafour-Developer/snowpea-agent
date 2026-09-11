import { describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer } from "../src/state/store.js";
import { cycleMode, MODE_CYCLE_ORDER } from "../src/state/mode.js";

describe("cycleMode", () => {
  it("cycles accept -> auto -> plan -> accept, like Claude Code", () => {
    expect(cycleMode("accept")).toBe("auto");
    expect(cycleMode("auto")).toBe("plan");
    expect(cycleMode("plan")).toBe("accept");
  });

  it("walking the full order returns to the start", () => {
    let mode = MODE_CYCLE_ORDER[0];
    for (let i = 0; i < MODE_CYCLE_ORDER.length; i += 1) {
      mode = cycleMode(mode);
    }
    expect(mode).toBe(MODE_CYCLE_ORDER[0]);
  });

  it("falls back to accept for an unrecognized mode", () => {
    expect(cycleMode("bogus" as never)).toBe("accept");
  });
});

describe("store: mode.changed", () => {
  it("updates state.mode from a mode.changed session event (server confirmation)", () => {
    const event: SessionEvent = {
      sessionId: "sess-1",
      seq: 1,
      kind: "mode.changed",
      payload: { mode: "auto" },
    };
    const state = reducer(initialState, { type: "session/event", event });
    expect(state.mode).toBe("auto");
  });

  it("the local 'mode' action applies an optimistic change immediately", () => {
    const state = reducer(initialState, { type: "mode", mode: "plan" });
    expect(state.mode).toBe("plan");
  });

  it("a later mode.changed event can reconcile after an optimistic local change", () => {
    let state = reducer(initialState, { type: "mode", mode: "auto" });
    expect(state.mode).toBe("auto");
    const event: SessionEvent = {
      sessionId: "sess-1",
      seq: 1,
      kind: "mode.changed",
      payload: { mode: "auto" },
    };
    state = reducer(state, { type: "session/event", event });
    expect(state.mode).toBe("auto");
  });
});
