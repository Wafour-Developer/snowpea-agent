import { describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer } from "../src/state/store.js";
import { turnSummaryLine } from "../src/state/working.js";

const S = "sess-stop";
let seq = 0;
function ev(kind: string, payload: Record<string, unknown>, ts?: string): SessionEvent {
  seq += 1;
  return { sessionId: S, seq, kind, payload, ts } as SessionEvent;
}

function replayNote(reason: string, error?: Record<string, unknown>): string {
  const evs = [
    ev("turn.started", { turnId: "t1", prompt: "go" }, "2026-01-01T00:00:00.000Z"),
    ...(error ? [ev("error", error)] : []),
    ev("turn.done", { turnId: "t1", reason }, "2026-01-01T00:01:05.000Z"),
  ];
  const state = reducer(initialState, { type: "session/replay", events: evs });
  return state.messages.filter((m) => m.role === "note").at(-1)?.text ?? "";
}

describe("turn stop reason", () => {
  it("live: tracks the turn's last error and resets on a new turn", () => {
    let state = reducer(initialState, { type: "session/event", event: ev("turn.started", { turnId: "t1" }) });
    state = reducer(state, { type: "session/event", event: ev("error", { code: "internal", message: "boom" }) });
    expect(state.lastTurnError).toBe("internal: boom");
    state = reducer(state, { type: "session/event", event: ev("turn.done", { turnId: "t1", reason: "error" }) });
    expect(state.lastTurnError).toBe("internal: boom");
    state = reducer(state, { type: "session/event", event: ev("turn.started", { turnId: "t2" }) });
    expect(state.lastTurnError).toBeNull();
  });

  it("summary line appends the error for reason error, truncated", () => {
    const long = "x".repeat(300);
    const line = turnSummaryLine({ ok: false, elapsedMs: 1000, reason: "error", error: `internal: ${long}` });
    const [head, why] = line.split("\n");
    expect(head).toBe("✗ Stopped after 1s · ↓ 0 tokens");
    expect(why.startsWith("  reason: internal: xxx")).toBe(true);
    expect(why.endsWith("…")).toBe(true);
    expect(why.length).toBeLessThanOrEqual(212);
  });

  it("summary line shows the reason word for other stops and nothing for complete", () => {
    expect(turnSummaryLine({ ok: false, elapsedMs: 1000, reason: "interrupted" })).toContain("\n  reason: interrupted");
    expect(turnSummaryLine({ ok: true, elapsedMs: 1000, reason: "complete" })).not.toContain("reason");
  });

  it("replay: error turn names the error", () => {
    const text = replayNote("error", { code: "internal", message: "ConnectError: nodename nor servname provided" });
    expect(text).toBe("✗ Stopped after 1m 5s\n  reason: internal: ConnectError: nodename nor servname provided");
  });

  it("replay: denied shows the word, complete shows none", () => {
    expect(replayNote("denied")).toBe("✗ Stopped after 1m 5s\n  reason: denied");
    expect(replayNote("complete")).toBe("✓ Done in 1m 5s");
  });
});
