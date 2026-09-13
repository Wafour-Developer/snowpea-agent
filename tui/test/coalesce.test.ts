/**
 * The child-delta batcher, driven by a hand-cranked clock.
 *
 * The properties that matter are "no text is lost", "nothing is reordered" and
 * "one delivery per window, however fast the tokens arrive".
 */

import { describe, expect, it } from "vitest";

import {
  createChildEventBuffer,
  createLiveEventThrottle,
  type CoalescableEvent,
} from "../src/state/coalesce.js";
import {
  AGENT_TRANSCRIPT_ROWS,
  MIN_AGENT_TRANSCRIPT_ROWS,
  agentTranscriptRows,
} from "../src/app.js";

/** A timer the test fires by hand. */
function manualTimer() {
  let pending: (() => void) | null = null;
  return {
    setTimer: (fn: () => void) => {
      pending = fn;
      return 1;
    },
    clearTimer: () => {
      pending = null;
    },
    /** Close the current window. */
    tick: () => {
      const fn = pending;
      pending = null;
      fn?.();
    },
    armed: () => pending !== null,
  };
}

const delta = (text: string, seq: number): CoalescableEvent => ({
  sessionId: "child",
  seq,
  kind: "message.delta",
  payload: { text },
});

function harness() {
  const timer = manualTimer();
  const delivered: Array<{ sessionId: string; event: CoalescableEvent }> = [];
  const buffer = createChildEventBuffer<CoalescableEvent>(
    (sessionId, event) => delivered.push({ sessionId, event }),
    { intervalMs: 66, setTimer: timer.setTimer, clearTimer: timer.clearTimer },
  );
  const texts = () => delivered.map((d) => (d.event.payload as { text?: string })?.text);
  return { timer, delivered, buffer, texts };
}

describe("the child event batcher", () => {
  it("lets the first token straight through and batches the rest of the window", () => {
    const { timer, buffer, texts } = harness();

    buffer.push("child", delta("a", 1));
    expect(texts()).toEqual(["a"]);

    for (const [i, token] of ["b", "c", "d", "e"].entries()) {
      buffer.push("child", delta(token, 2 + i));
    }
    // Still one delivery: everything after the leading edge is waiting.
    expect(texts()).toEqual(["a"]);

    timer.tick();
    expect(texts()).toEqual(["a", "bcde"]);
  });

  it("keeps the window open while text keeps arriving, then lets it lapse", () => {
    const { timer, buffer, texts } = harness();

    buffer.push("child", delta("a", 1));
    buffer.push("child", delta("b", 2));
    timer.tick();
    expect(texts()).toEqual(["a", "b"]);
    // Re-armed, because there was something to send.
    expect(timer.armed()).toBe(true);

    timer.tick();
    // Nothing arrived in that window, so it closes rather than re-arming.
    expect(timer.armed()).toBe(false);
    expect(texts()).toEqual(["a", "b"]);
  });

  it("loses no text however many tokens land in one window", () => {
    const { timer, buffer, texts } = harness();
    for (let i = 0; i < 500; i += 1) buffer.push("child", delta(`t${i} `, i));
    timer.tick();

    const seen = texts().join("");
    expect(seen).toBe(Array.from({ length: 500 }, (_, i) => `t${i} `).join(""));
    // 500 tokens, two deliveries: the leading edge and the window.
    expect(texts()).toHaveLength(2);
  });

  it("carries the newest seq, so the store's lastSeq never goes backwards", () => {
    const { timer, buffer, delivered } = harness();
    buffer.push("child", delta("a", 10)); // leading edge
    buffer.push("child", delta("b", 11));
    buffer.push("child", delta("c", 12));
    timer.tick();
    expect(delivered.map((d) => d.event.seq)).toEqual([10, 12]);
  });

  it("flushes the pending text before anything that is not a delta", () => {
    const { buffer, delivered } = harness();
    buffer.push("child", delta("hello ", 1)); // leading edge
    buffer.push("child", delta("world", 2)); // pending
    buffer.push("child", {
      sessionId: "child",
      seq: 3,
      kind: "message.done",
      payload: { role: "assistant", text: "hello world" },
    });

    expect(delivered.map((d) => d.event.kind)).toEqual([
      "message.delta",
      "message.delta",
      "message.done",
    ]);
    expect((delivered[1].event.payload as { text: string }).text).toBe("world");
  });

  it("keeps two delegates apart", () => {
    const { timer, buffer, delivered } = harness();
    buffer.push("one", delta("1a", 1)); // leading edge, arms the window
    buffer.push("one", delta("1b", 2));
    buffer.push("two", delta("2a", 1));
    buffer.push("two", delta("2b", 2));
    timer.tick();

    expect(delivered.map((d) => [d.sessionId, (d.event.payload as any).text])).toEqual([
      ["one", "1a"],
      ["one", "1b"],
      ["two", "2a2b"],
    ]);
  });

  it("delivers what is pending when it is torn down", () => {
    const { buffer, texts } = harness();
    buffer.push("child", delta("a", 1));
    buffer.push("child", delta("b", 2));
    buffer.dispose();
    expect(texts()).toEqual(["a", "b"]);
  });
});

/**
 * The main session's counter events: the thinking line and the token counters.
 * Text is deliberately not in here — an answer must appear as it is written.
 */
describe("the live counter throttle", () => {
  function liveHarness() {
    const timer = manualTimer();
    const delivered: CoalescableEvent[] = [];
    const throttle = createLiveEventThrottle<CoalescableEvent>(
      (event) => delivered.push(event),
      { intervalMs: 250, setTimer: timer.setTimer, clearTimer: timer.clearTimer },
    );
    return { timer, delivered, throttle };
  }

  const reasoning = (chars: number, text = "…"): CoalescableEvent => ({
    sessionId: "sess-1",
    kind: "message.reasoning",
    payload: { text, chars },
  });

  it("shows the first thought at once, then one update per window", () => {
    const { timer, delivered, throttle } = liveHarness();
    throttle.push(reasoning(8));
    expect(delivered).toHaveLength(1);

    for (let i = 2; i <= 40; i += 1) throttle.push(reasoning(i * 8));
    expect(delivered).toHaveLength(1);

    timer.tick();
    expect(delivered).toHaveLength(2);
    // The count is a running total, so the newest one says it all.
    expect((delivered[1].payload as any).chars).toBe(320);
  });

  it("adds up usage, which is an increment and not a total", () => {
    const { timer, delivered, throttle } = liveHarness();
    const usage = (input: number, output: number): CoalescableEvent => ({
      kind: "usage",
      payload: { inputTokens: input, outputTokens: output },
    });
    throttle.push(usage(1000, 10)); // leading edge
    for (let i = 0; i < 9; i += 1) throttle.push(usage(100, 5));
    timer.tick();

    const totals = delivered.reduce(
      (sum, e) => ({
        input: sum.input + Number((e.payload as any).inputTokens),
        output: sum.output + Number((e.payload as any).outputTokens),
      }),
      { input: 0, output: 0 },
    );
    expect(totals).toEqual({ input: 1900, output: 55 });
    expect(delivered).toHaveLength(2);
  });

  it("shows the first token at once and batches the answer at 15 Hz", () => {
    const { timer, delivered, throttle } = liveHarness();
    for (let i = 0; i < 5; i += 1) {
      throttle.push({ kind: "message.delta", payload: { text: `tok${i} ` } });
    }
    // The first token is on screen; the rest wait out the text window.
    expect(delivered).toHaveLength(1);
    timer.tick();

    // Every token, in order, nothing repeated and nothing lost.
    const text = delivered.map((e) => (e.payload as any).text).join("");
    expect(text).toBe("tok0 tok1 tok2 tok3 tok4 ");
    expect(delivered).toHaveLength(2);
  });

  it("lets text pre-empt the longer window the counters are queued on", () => {
    const { timer, delivered, throttle } = liveHarness();
    throttle.push(reasoning(8)); // leading edge, arms the 250 ms window
    throttle.push({ kind: "message.delta", payload: { text: "a" } });
    throttle.push(reasoning(16));
    // Text has the shorter window, so it pre-empts; one tick sends both, and
    // the counters ride along on a repaint the text has already paid for.
    timer.tick();
    expect(delivered.map((e) => e.kind)).toEqual([
      "message.reasoning",
      "message.delta",
      "message.reasoning",
    ]);
  });

  it("flushes the counters before the event that ends them", () => {
    const { delivered, throttle } = liveHarness();
    throttle.push(reasoning(8)); // leading edge
    throttle.push(reasoning(16)); // held
    throttle.push({ kind: "message.done", payload: { role: "assistant", text: "hi" } });

    expect(delivered.map((e) => e.kind)).toEqual([
      "message.reasoning",
      "message.reasoning",
      "message.done",
    ]);
    expect((delivered[1].payload as any).chars).toBe(16);
  });

  it("publishes the last count when it is torn down mid-think", () => {
    const { delivered, throttle } = liveHarness();
    throttle.push(reasoning(8));
    throttle.push(reasoning(4096));
    throttle.dispose();
    expect((delivered[delivered.length - 1].payload as any).chars).toBe(4096);
  });
});

/**
 * The other half of the fix: how tall the open agent's window is allowed to be.
 * Inline, it is whatever the status block and the input leave over, because a
 * live region as tall as the terminal makes Ink clear the whole screen before
 * every frame.
 */
describe("the agent transcript's height", () => {
  it("takes what the inline layout leaves over, and never more than the cap", () => {
    // A roomy terminal: the cap wins.
    expect(
      agentTranscriptRows({ fullscreen: false, usable: 60, statusRows: 10, bottomRows: 4 }),
    ).toBe(AGENT_TRANSCRIPT_ROWS);
    // An ordinary 24-row window with a panel and a HUD: the room wins.
    expect(
      agentTranscriptRows({ fullscreen: false, usable: 23, statusRows: 9, bottomRows: 3 }),
    ).toBe(7);
  });

  it("never collapses below a readable window, however cramped", () => {
    expect(
      agentTranscriptRows({ fullscreen: false, usable: 10, statusRows: 9, bottomRows: 6 }),
    ).toBe(MIN_AGENT_TRANSCRIPT_ROWS);
  });

  it("leaves the full-screen layout on its constant", () => {
    expect(
      agentTranscriptRows({ fullscreen: true, usable: 12, statusRows: 9, bottomRows: 6 }),
    ).toBe(AGENT_TRANSCRIPT_ROWS);
  });
});
