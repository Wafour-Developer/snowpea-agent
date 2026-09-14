/**
 * The three live-progress events: `turn.started`, `tool.progress` and
 * `compaction.started` — what they do to the store and what the indicator says.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import {
  __resetIdCounter,
  initialState,
  PROGRESS_TAIL,
  reducer,
  type State,
} from "../src/state/store.js";
import { derivePhase, workingLine } from "../src/state/working.js";

function event(
  seq: number,
  kind: string,
  payload: Record<string, unknown>,
  ts?: string,
): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload, ...(ts ? { ts } : {}) } as SessionEvent;
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const started = (seq: number, payload: Record<string, unknown> = {}, ts?: string) =>
  event(seq, "turn.started", { turnId: "t-1", queued: false, ...payload }, ts);

beforeEach(() => {
  __resetIdCounter();
});

describe("turn.started", () => {
  it("opens the turn and stamps the clock", () => {
    const state = apply(initialState, started(1, {}, "2026-09-14T10:00:00.000Z"));
    expect(state.turnActive).toBe(true);
    expect(state.turnStartedAt).toBe(Date.parse("2026-09-14T10:00:00.000Z"));
    expect(state.turnWaited).toBe(false);
  });

  it("falls back to the local clock when the event carries no timestamp", () => {
    const before = Date.now();
    const state = apply(initialState, started(1));
    expect(state.turnStartedAt).not.toBeNull();
    expect(state.turnStartedAt as number).toBeGreaterThanOrEqual(before);
  });

  it("says a queued turn waited, and takes it out of the queue", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 1 }),
      started(2, { queued: true }),
    );
    expect(state.turnWaited).toBe(true);
    expect(state.queued).toEqual([]);
  });

  it("still spots a wait when the daemon only dequeued it", () => {
    const state = apply(
      initialState,
      event(1, "turn.queued", { turnId: "t-1", position: 1 }),
      started(2, { queued: undefined }),
    );
    expect(state.turnWaited).toBe(true);
  });

  it("keeps the prompt text the event carries", () => {
    const state = apply(initialState, started(1, { prompt: "run the tests" }));
    expect(state.promptTexts["t-1"]).toBe("run the tests");
  });

  it("clears the stamp when the turn ends", () => {
    const state = apply(
      initialState,
      started(1, { queued: true }),
      event(2, "turn.done", { turnId: "t-1", reason: "complete" }),
    );
    expect(state.turnStartedAt).toBeNull();
    expect(state.turnWaited).toBe(false);
  });

  it("is reported as a wait by the working line", () => {
    const line = workingLine({
      phase: { kind: "thinking" },
      elapsedMs: 3000,
      outputTokens: 0,
      waited: true,
    });
    expect(line).toContain("started after waiting");
    expect(line).toContain("3s");
  });
});

describe("tool.progress", () => {
  const running = (seq: number) =>
    event(seq, "tool.call", { callId: "c-1", name: "shell", args: { command: "make" } });

  it("keeps the tail of what a running tool printed", () => {
    const state = apply(
      initialState,
      started(1),
      running(2),
      event(3, "tool.progress", { callId: "c-1", stream: "stdout", chunk: "one\ntwo\n", seq: 1 }),
      event(4, "tool.progress", { callId: "c-1", stream: "stdout", chunk: "three\n", seq: 2 }),
    );
    const call = state.toolCalls.find((c) => c.callId === "c-1");
    expect(call?.progress?.map((line) => line.text)).toEqual(["one", "two", "three"]);
    expect(call?.progress?.every((line) => line.stream === "stdout")).toBe(true);
  });

  it("keeps stderr apart", () => {
    const state = apply(
      initialState,
      started(1),
      running(2),
      event(3, "tool.progress", { callId: "c-1", stream: "stderr", chunk: "boom\n", seq: 1 }),
    );
    expect(state.toolCalls[0].progress).toEqual([{ text: "boom", stream: "stderr" }]);
  });

  it("holds only the last few lines", () => {
    const chunk = Array.from({ length: PROGRESS_TAIL + 5 }, (_, i) => `line ${i}`).join("\n");
    const state = apply(
      initialState,
      started(1),
      running(2),
      event(3, "tool.progress", { callId: "c-1", stream: "stdout", chunk, seq: 1 }),
    );
    const tail = state.toolCalls[0].progress ?? [];
    expect(tail).toHaveLength(PROGRESS_TAIL);
    expect(tail[tail.length - 1].text).toBe(`line ${PROGRESS_TAIL + 4}`);
  });

  it("remembers that the daemon stopped sending", () => {
    const state = apply(
      initialState,
      started(1),
      running(2),
      event(3, "tool.progress", { callId: "c-1", stream: "stdout", chunk: "a\n", truncated: true }),
    );
    expect(state.toolCalls[0].progressTruncated).toBe(true);
  });

  it("ignores output for a call it never saw start", () => {
    const state = apply(
      initialState,
      started(1),
      event(2, "tool.progress", { callId: "ghost", stream: "stdout", chunk: "a\n" }),
    );
    expect(state.toolCalls).toEqual([]);
  });
});

describe("compaction.started", () => {
  it("puts the indicator into a compacting phase", () => {
    const state = apply(initialState, event(1, "compaction.started", { reason: "manual", before: 90_000 }));
    expect(state.compacting).toEqual({ reason: "manual", before: 90_000 });
    const phase = derivePhase(state);
    expect(phase).toEqual({ kind: "compacting", reason: "manual" });
    expect(workingLine({ phase, elapsedMs: 0 })).toContain("Compacting");
  });

  it("marks an automatic one", () => {
    const state = apply(initialState, event(1, "compaction.started", { reason: "auto", before: 12 }));
    expect(workingLine({ phase: derivePhase(state), elapsedMs: 0 })).toContain("Compacting (auto)");
  });

  it("ends when the compaction event arrives, leaving the divider", () => {
    const state = apply(
      initialState,
      event(1, "compaction.started", { reason: "auto", before: 90_000 }),
      event(2, "compaction", { before: 90_000, after: 20_000 }),
    );
    expect(state.compacting).toBeNull();
    expect(state.compactions).toHaveLength(1);
    expect(state.timeline.some((item) => item.kind === "compaction")).toBe(true);
    expect(derivePhase(state)).toEqual({ kind: "idle" });
  });

  it("outranks a running turn but not a pending approval", () => {
    const state = apply(initialState, started(1), event(2, "compaction.started", { reason: "auto" }));
    expect(derivePhase(state).kind).toBe("compacting");
  });
});
