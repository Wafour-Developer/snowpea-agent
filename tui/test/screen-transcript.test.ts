/**
 * The full-screen transcript draws a turn the way the inline scrollback does.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { screenTranscriptLines } from "../src/layout/screen-transcript.js";
import { lineText } from "../src/layout/transcript.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const ask = (state: State, text: string): State => reducer(state, { type: "user/message", text });
const text = (state: State, options = {}): string[] =>
  screenTranscriptLines(state, 80, options).map(lineText);

function readTwoFiles(): State {
  return apply(
    ask(initialState, "read them"),
    event(1, "turn.started", { turnId: "t-1" }),
    event(2, "tool.call", { callId: "c1", name: "read_file", args: { path: "a.py" } }),
    event(3, "tool.result", { callId: "c1", ok: true, output: "one\ntwo" }),
    event(4, "tool.call", { callId: "c2", name: "read_file", args: { path: "b.py" } }),
    event(5, "tool.result", { callId: "c2", ok: true, output: "three" }),
  );
}

beforeEach(() => {
  __resetIdCounter();
});

describe("screenTranscriptLines", () => {
  it("keeps the calls of the turn still going as cards", () => {
    const lines = text(readTwoFiles());
    expect(lines.filter((line) => line.startsWith("✓ read_file"))).toHaveLength(2);
    expect(lines.some((line) => line.startsWith("⏺"))).toBe(false);
  });

  it("folds a finished run of calls into one summary line", () => {
    const state = apply(
      readTwoFiles(),
      event(6, "message.delta", { text: "done" }),
      event(7, "message.done", {}),
      event(8, "turn.done", { turnId: "t-1", reason: "complete" }),
    );
    const lines = text(state);
    const summary = lines.filter((line) => line.startsWith("⏺"));
    expect(summary).toHaveLength(1);
    expect(summary[0]).toContain("(3 lines)");
    expect(lines.some((line) => line.includes("read_file"))).toBe(false);
    // The answer after the run sits one blank line below it.
    const at = lines.findIndex((line) => line.startsWith("⏺"));
    expect(lines[at + 1]).toBe("");
    expect(lines[at + 2]).toBe("◆ done");
  });

  it("never says a collapsed call's size twice", () => {
    const lines = text(readTwoFiles());
    expect(lines.some((line) => line.includes("more lines"))).toBe(false);
    expect(lines.some((line) => line.includes("(2 lines)"))).toBe(true);
  });

  it("puts a turn's Done line where the turn ended", () => {
    const state = apply(
      readTwoFiles(),
      event(6, "message.delta", { text: "done" }),
      event(7, "message.done", {}),
      event(8, "turn.done", { turnId: "t-1", reason: "complete" }),
    );
    const ended = state.timeline.length;
    const next = ask(state, "again");
    const lines = text(next, { notes: [{ at: ended, text: "✓ Done in 3s", ok: true }] });
    const done = lines.indexOf("✓ Done in 3s");
    expect(done).toBeGreaterThan(lines.indexOf("◆ done"));
    expect(lines.findIndex((line) => line.includes("again"))).toBeGreaterThan(done);
  });

  it("drops notes from a timeline that was replaced", () => {
    const lines = text(ask(initialState, "fresh"), { notes: [{ at: 9, text: "✓ Done in 1s", ok: true }] });
    expect(lines.some((line) => line.includes("Done"))).toBe(false);
  });

  it("draws a prompt as a full-width band", () => {
    const lines = screenTranscriptLines(ask(initialState, "hello"), 40, { inputBackground: "#223344" });
    const width = lines[0].segments.reduce((sum, segment) => sum + segment.text.length, 0);
    expect(width).toBe(40);
    expect(lines[0].segments.every((segment) => segment.backgroundColor)).toBe(true);
  });

  it("leaves the subagent list to the panel", () => {
    const state = apply(
      ask(initialState, "delegate"),
      event(1, "subagent.spawn", { agentId: "a1", name: "explore", status: "running" }),
    );
    expect(text(state).some((line) => line.includes("explore"))).toBe(false);
  });
});
