/**
 * A turn rebuilt from `step.done` when its deltas are not in the log (M18 5.3).
 *
 * `step.done` carries each model call's settled text. Live, and in a full
 * replay, the deltas already built that text and `step.done` adds nothing; a
 * log that keeps only the settled steps must still read the same.
 */

import { describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer } from "../src/state/store.js";

const S = "sess-steps";

function events(withDeltas: boolean): SessionEvent[] {
  const out: SessionEvent[] = [];
  let seq = 0;
  const push = (kind: string, payload: Record<string, unknown>): void => {
    seq += 1;
    out.push({ sessionId: S, seq, kind, payload } as SessionEvent);
  };
  push("step.started", { turnId: "t1", step: 1 });
  if (withDeltas) {
    push("message.delta", { text: "Let me read " });
    push("message.delta", { text: "the file." });
  }
  push("step.done", { turnId: "t1", step: 1, outcome: "tool_use", text: "Let me read the file.", toolCalls: 1 });
  push("tool.call", { callId: "c1", name: "read_file", args: { path: "a.ts" } });
  push("tool.result", { callId: "c1", ok: true, output: "x" });
  push("step.started", { turnId: "t1", step: 2 });
  if (withDeltas) push("message.delta", { text: "It exports add." });
  push("step.done", { turnId: "t1", step: 2, outcome: "end_turn", text: "It exports add.", toolCalls: 0 });
  push("message.done", { role: "assistant", text: "It exports add." });
  push("turn.done", { turnId: "t1", reason: "complete" });
  return out;
}

function transcript(evs: SessionEvent[]): string[] {
  const state = reducer(initialState, { type: "session/replay", events: evs });
  return state.timeline.map((entry) => {
    if (entry.kind === "message") {
      const m = state.messages.find((x) => x.id === entry.id);
      return `${m?.role}:${m?.text}`;
    }
    if (entry.kind === "tool") return `tool:${entry.id}`;
    return entry.kind;
  });
}

describe("step.done replay", () => {
  it("reads the same with and without the deltas", () => {
    const full = transcript(events(true));
    expect(full.slice(0, 3)).toEqual(["assistant:Let me read the file.", "tool:c1", "assistant:It exports add."]);
    expect(transcript(events(false))).toEqual(full);
  });

  it("does not double a step whose deltas arrived", () => {
    let state = initialState;
    for (const e of events(true)) state = reducer(state, { type: "session/event", event: e });
    const texts = state.messages.filter((m) => m.role === "assistant").map((m) => m.text);
    expect(texts).toEqual(["Let me read the file.", "It exports add."]);
  });
});
