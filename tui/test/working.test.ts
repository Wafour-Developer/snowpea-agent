/**
 * The working indicator: which phase the session is in, and what it reads as.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { VERBS, VERB_PERIOD_MS, verbAt } from "../src/state/verbs.js";
import {
  SPINNER_FRAMES,
  derivePhase,
  formatDuration,
  formatStats,
  toolLabel,
  turnSummaryLine,
  workingLine,
} from "../src/state/working.js";
import {
  __resetIdCounter,
  initialState,
  outputLimitNote,
  reducer,
  type State,
} from "../src/state/store.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const ask = (state: State, text: string): State => reducer(state, { type: "user/message", text });

const call = (name: string, args: Record<string, unknown> = {}) => ({
  callId: "c1",
  name,
  args,
  state: "running" as const,
});

beforeEach(() => {
  __resetIdCounter();
});

describe("verb rotation", () => {
  it("holds a verb for the whole period and then moves on", () => {
    expect(verbAt(0)).toBe(VERBS[0]);
    expect(verbAt(VERB_PERIOD_MS - 1)).toBe(VERBS[0]);
    expect(verbAt(VERB_PERIOD_MS)).toBe(VERBS[1]);
    expect(verbAt(VERB_PERIOD_MS * 2)).toBe(VERBS[2]);
  });

  it("wraps around the list instead of running out", () => {
    const past = VERB_PERIOD_MS * VERBS.length;
    expect(verbAt(past)).toBe(VERBS[0]);
  });

  it("starts two turns on different words", () => {
    expect(verbAt(0, 0)).not.toBe(verbAt(0, 1));
  });

  it("offers enough words not to repeat inside a turn", () => {
    expect(VERBS.length).toBeGreaterThanOrEqual(40);
    expect(new Set(VERBS).size).toBe(VERBS.length);
  });

  it("rotates on the clock, with fake timers standing in for the wait", () => {
    vi.useFakeTimers();
    try {
      const startedAt = Date.now();
      vi.advanceTimersByTime(VERB_PERIOD_MS * 3 + 10);
      const elapsed = Date.now() - startedAt;
      expect(verbAt(elapsed)).toBe(VERBS[3]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("derivePhase", () => {
  it("is idle with nothing running", () => {
    expect(derivePhase(initialState).kind).toBe("idle");
  });

  it("thinks as soon as the turn starts", () => {
    expect(derivePhase(ask(initialState, "hello")).kind).toBe("thinking");
  });

  it("names the tool while one is in flight, and stops when it returns", () => {
    const running = apply(
      ask(initialState, "hello"),
      event(1, "tool.call", { callId: "c1", name: "bash", args: { command: "ls -la" } }),
    );
    expect(derivePhase(running)).toEqual({ kind: "tool", label: "Running shell: ls -la" });

    const done = apply(running, event(2, "tool.result", { callId: "c1", ok: true, output: "x" }));
    expect(done.turnActive).toBe(true);
    expect(derivePhase(done).kind).toBe("thinking");
  });

  it("counts the delegates when a fan-out is live", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "subagent.spawn", { agentId: "a1", task: "one", status: "running" }),
      event(2, "subagent.spawn", { agentId: "a2", task: "two", status: "running" }),
      event(3, "subagent.spawn", { agentId: "a3", task: "three", status: "running" }),
    );
    expect(derivePhase(state)).toEqual({
      kind: "subagents",
      running: 3,
      inputTokens: 0,
      outputTokens: 0,
    });
  });

  it("sums the tokens of the running delegates only", () => {
    const state = apply(
      ask(initialState, "hello"),
      event(1, "subagent.spawn", { agentId: "a1", task: "one", status: "running" }),
      event(2, "subagent.spawn", { agentId: "a2", task: "two", status: "running" }),
      event(3, "subagent.update", {
        agentId: "a1",
        status: "running",
        usage: { inputTokens: 40_000, outputTokens: 2000 },
      }),
      event(4, "subagent.update", {
        agentId: "a2",
        status: "running",
        usage: { inputTokens: 1200, outputTokens: 1100 },
      }),
      // A finished child's tokens belong to the turn already; counting them
      // here as well would show them twice.
      event(5, "subagent.done", {
        agentId: "a2",
        usage: { inputTokens: 1200, outputTokens: 1100 },
      }),
    );
    expect(derivePhase(state)).toEqual({
      kind: "subagents",
      running: 1,
      inputTokens: 40_000,
      outputTokens: 2000,
    });
  });

  it("names the command when one owns the turn", () => {
    const state = ask(initialState, "/ralph");
    expect(derivePhase(state, { runningCommand: "/ralph" })).toEqual({
      kind: "command",
      name: "/ralph",
    });
  });

  it("puts a person ahead of the machine", () => {
    const waiting = reducer(ask(initialState, "hello"), {
      type: "approval/request",
      request: { requestId: "r1", toolName: "bash", args: {} } as any,
    });
    expect(derivePhase(waiting).kind).toBe("approval");
  });
});

describe("toolLabel", () => {
  it("says what each family of tool is doing", () => {
    expect(toolLabel(call("bash", { command: "npm test" }))).toBe("Running shell: npm test");
    expect(toolLabel(call("read_file", { path: "/src/app.py" }))).toBe("Reading app.py");
    expect(toolLabel(call("grep", { pattern: "TODO" }))).toBe('Searching "TODO"');
    expect(toolLabel(call("edit", { file_path: "/repo/README.md" }))).toBe("Editing README.md");
    expect(toolLabel(call("write", { path: "notes.txt" }))).toBe("Writing notes.txt");
    expect(toolLabel(call("fetch", { url: "https://example.com" }))).toBe(
      "Fetching https://example.com",
    );
    expect(toolLabel(call("summon_pixies"))).toBe("Running summon_pixies");
  });

  it("clips a long shell command", () => {
    const label = toolLabel(call("bash", { command: "x".repeat(120) }));
    expect(label.length).toBeLessThanOrEqual("Running shell: ".length + 40);
    expect(label.endsWith("…")).toBe(true);
  });
});

describe("formatting", () => {
  it("reads a duration the way a person would say it", () => {
    expect(formatDuration(12_000)).toBe("12s");
    expect(formatDuration(71_000)).toBe("1m 11s");
    expect(formatDuration(3_720_000)).toBe("1h 2m");
  });

  it("shows output tokens always and input tokens once they are known", () => {
    expect(formatStats({ elapsedMs: 71_000, outputTokens: 3700 })).toBe("(1m 11s · ↓ 3.7k tokens)");
    expect(formatStats({ elapsedMs: 71_000, inputTokens: 1200, outputTokens: 3700 })).toBe(
      "(1m 11s · ↑ 1.2k · ↓ 3.7k tokens)",
    );
  });

  it("draws the thinking line exactly as specified", () => {
    const line = workingLine({
      phase: { kind: "thinking" },
      elapsedMs: 71_000,
      outputTokens: 3700,
      frame: 0,
      verbOffset: 0,
    });
    expect(line).toBe(`${SPINNER_FRAMES[0]} ${VERBS[8]}… (1m 11s · ↓ 3.7k tokens)`);
  });

  it("cycles the spinner glyph with the frame", () => {
    const at = (frame: number) =>
      workingLine({ phase: { kind: "thinking" }, elapsedMs: 0, frame })!.slice(0, 1);
    expect(at(0)).toBe(SPINNER_FRAMES[0]);
    expect(at(3)).toBe(SPINNER_FRAMES[3]);
    expect(at(SPINNER_FRAMES.length)).toBe(SPINNER_FRAMES[0]);
  });

  it("writes each phase in its own words", () => {
    const stats = { elapsedMs: 12_000, outputTokens: 1200, frame: 1 };
    expect(workingLine({ ...stats, phase: { kind: "tool", label: "Reading app.py" } })).toBe(
      `${SPINNER_FRAMES[1]} Reading app.py… (12s · ↓ 1.2k tokens)`,
    );
    expect(
      workingLine({
        ...stats,
        phase: { kind: "subagents", running: 3, inputTokens: 0, outputTokens: 0 },
      }),
    ).toBe(`${SPINNER_FRAMES[1]} 3 agents working… (12s · ↓ 1.2k tokens)`);
    expect(
      workingLine({
        ...stats,
        phase: { kind: "subagents", running: 1, inputTokens: 0, outputTokens: 0 },
      }),
    ).toContain("1 agent working…");
    // The children's live usage is added to the turn's own, so the line moves
    // while the parent itself is spending nothing.
    expect(
      workingLine({
        ...stats,
        phase: { kind: "subagents", running: 1, inputTokens: 41_200, outputTokens: 1900 },
      }),
    ).toBe(`${SPINNER_FRAMES[1]} 1 agent working… (12s · ↑ 41.2k · ↓ 3.1k tokens)`);
    expect(workingLine({ ...stats, phase: { kind: "command", name: "/ralph" } })).toBe(
      `${SPINNER_FRAMES[1]} /ralph… (12s · ↓ 1.2k tokens)`,
    );
    expect(workingLine({ ...stats, phase: { kind: "approval" } })).toBe("⏸ Waiting for approval");
    expect(workingLine({ ...stats, phase: { kind: "idle" } })).toBeNull();
  });

  it("closes the turn with a result line", () => {
    expect(turnSummaryLine({ ok: true, elapsedMs: 12_000, outputTokens: 3700 })).toBe(
      "✓ Done in 12s · ↓ 3.7k tokens",
    );
    expect(turnSummaryLine({ ok: false, elapsedMs: 4000, outputTokens: 10 })).toBe(
      "✗ Stopped after 4s · ↓ 10 tokens",
    );
  });
});

describe("hidden reasoning", () => {
  it("counts the thinking without putting it in the transcript", () => {
    const state = apply(
      ask(initialState, "review this"),
      event(1, "message.reasoning", { text: "weighing", chars: 8 }),
      event(2, "message.reasoning", { text: " it up", chars: 14 }),
    );
    expect(state.reasoningChars).toBe(14);
    expect(state.messages.filter((m) => m.role === "assistant")).toHaveLength(0);
  });

  it("says how much of it there has been", () => {
    const state = apply(
      ask(initialState, "review this"),
      event(1, "message.reasoning", { text: "…", chars: 1788 }),
    );
    const phase = derivePhase(state);
    expect(phase).toEqual({ kind: "reasoning", chars: 1788 });
    expect(workingLine({ phase, elapsedMs: 1000 })).toContain("Thinking (1.8k chars)");
  });

  it("starts each turn from nothing", () => {
    const state = apply(
      ask(initialState, "one"),
      event(1, "message.reasoning", { text: "…", chars: 40 }),
    );
    expect(ask(state, "two").reasoningChars).toBe(0);
  });
});

describe("the output limit", () => {
  it("keeps quiet about a turn that never hit it", () => {
    expect(outputLimitNote(false, 0)).toBeNull();
  });

  it("footnotes a turn that was resumed, and warns about one that was not", () => {
    expect(outputLimitNote(false, 1)).toMatch(/continued/);
    expect(outputLimitNote(true, 2)).toMatch(/incomplete/);
  });

  it("puts the note in the transcript under the answer", () => {
    const state = apply(
      ask(initialState, "review this"),
      event(1, "message.delta", { text: "first half " }),
      event(2, "message.done", { text: "first half second half", continuations: 1 }),
    );
    const last = state.messages[state.messages.length - 1];
    expect(last.role).toBe("system");
    expect(last.text).toMatch(/continued/);
  });
});
