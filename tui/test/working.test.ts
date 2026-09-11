/**
 * The working indicator: which phase the session is in, and what it reads as.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import {
  ENGLISH_VERBS,
  KOREAN_VERBS,
  VERB_PERIOD_MS,
  isKorean,
  verbAt,
  verbsFor,
} from "../src/state/verbs.js";
import {
  SPINNER_FRAMES,
  derivePhase,
  formatDuration,
  formatStats,
  lastUserPrompt,
  toolLabel,
  turnSummaryLine,
  workingLine,
} from "../src/state/working.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

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
    expect(verbAt(ENGLISH_VERBS, 0)).toBe(ENGLISH_VERBS[0]);
    expect(verbAt(ENGLISH_VERBS, VERB_PERIOD_MS - 1)).toBe(ENGLISH_VERBS[0]);
    expect(verbAt(ENGLISH_VERBS, VERB_PERIOD_MS)).toBe(ENGLISH_VERBS[1]);
    expect(verbAt(ENGLISH_VERBS, VERB_PERIOD_MS * 2)).toBe(ENGLISH_VERBS[2]);
  });

  it("wraps around the list instead of running out", () => {
    const past = VERB_PERIOD_MS * ENGLISH_VERBS.length;
    expect(verbAt(ENGLISH_VERBS, past)).toBe(ENGLISH_VERBS[0]);
  });

  it("starts two turns on different words", () => {
    expect(verbAt(ENGLISH_VERBS, 0, 0)).not.toBe(verbAt(ENGLISH_VERBS, 0, 1));
  });

  it("offers enough words not to repeat inside a turn", () => {
    expect(ENGLISH_VERBS.length).toBeGreaterThanOrEqual(40);
    expect(KOREAN_VERBS.length).toBeGreaterThanOrEqual(40);
    expect(new Set(ENGLISH_VERBS).size).toBe(ENGLISH_VERBS.length);
    expect(new Set(KOREAN_VERBS).size).toBe(KOREAN_VERBS.length);
  });

  it("answers in Korean when the user wrote in Korean", () => {
    expect(isKorean("이 파일 고쳐줘")).toBe(true);
    expect(isKorean("fix this file")).toBe(false);
    expect(verbsFor("이 파일 고쳐줘")).toBe(KOREAN_VERBS);
    expect(verbsFor("fix this file")).toBe(ENGLISH_VERBS);
    expect(verbsFor(null)).toBe(ENGLISH_VERBS);
  });

  it("rotates on the clock, with fake timers standing in for the wait", () => {
    vi.useFakeTimers();
    try {
      const startedAt = Date.now();
      vi.advanceTimersByTime(VERB_PERIOD_MS * 3 + 10);
      const elapsed = Date.now() - startedAt;
      expect(verbAt(ENGLISH_VERBS, elapsed)).toBe(ENGLISH_VERBS[3]);
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
    expect(derivePhase(state)).toEqual({ kind: "subagents", running: 3 });
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
      prompt: "hello",
      verbOffset: 0,
    });
    expect(line).toBe(`${SPINNER_FRAMES[0]} ${ENGLISH_VERBS[8]}… (1m 11s · ↓ 3.7k tokens)`);
  });

  it("cycles the spinner glyph with the frame", () => {
    const at = (frame: number) =>
      workingLine({ phase: { kind: "thinking" }, elapsedMs: 0, frame, prompt: "hi" })!.slice(0, 1);
    expect(at(0)).toBe(SPINNER_FRAMES[0]);
    expect(at(3)).toBe(SPINNER_FRAMES[3]);
    expect(at(SPINNER_FRAMES.length)).toBe(SPINNER_FRAMES[0]);
  });

  it("writes each phase in its own words", () => {
    const stats = { elapsedMs: 12_000, outputTokens: 1200, frame: 1 };
    expect(workingLine({ ...stats, phase: { kind: "tool", label: "Reading app.py" } })).toBe(
      `${SPINNER_FRAMES[1]} Reading app.py… (12s · ↓ 1.2k tokens)`,
    );
    expect(workingLine({ ...stats, phase: { kind: "subagents", running: 3 } })).toBe(
      `${SPINNER_FRAMES[1]} 3 agents working… (12s · ↓ 1.2k tokens)`,
    );
    expect(workingLine({ ...stats, phase: { kind: "subagents", running: 1 } })).toContain(
      "1 agent working…",
    );
    expect(workingLine({ ...stats, phase: { kind: "command", name: "/ralph" } })).toBe(
      `${SPINNER_FRAMES[1]} /ralph… (12s · ↓ 1.2k tokens)`,
    );
    expect(workingLine({ ...stats, phase: { kind: "approval" } })).toBe("⏸ Waiting for approval");
    expect(workingLine({ ...stats, phase: { kind: "idle" } })).toBeNull();
  });

  it("uses the Korean list for a Korean prompt", () => {
    const line = workingLine({
      phase: { kind: "thinking" },
      elapsedMs: 0,
      frame: 0,
      prompt: "이 파일 고쳐줘",
      verbOffset: 0,
    });
    expect(line).toBe(`${SPINNER_FRAMES[0]} ${KOREAN_VERBS[0]}… (0s · ↓ 0 tokens)`);
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

describe("lastUserPrompt", () => {
  it("finds the most recent thing the user said", () => {
    const state = apply(
      ask(ask(initialState, "first"), "두 번째"),
      event(1, "message.done", { text: "answer", role: "assistant" }),
    );
    expect(lastUserPrompt(state)).toBe("두 번째");
    expect(lastUserPrompt(initialState)).toBeNull();
  });
});
