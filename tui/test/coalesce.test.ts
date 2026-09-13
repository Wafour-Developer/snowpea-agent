/**
 * The child-delta batcher, driven by a hand-cranked clock.
 *
 * The properties that matter are "no text is lost", "nothing is reordered" and
 * "one delivery per window, however fast the tokens arrive".
 */

import { describe, expect, it } from "vitest";

import { createChildEventBuffer, type CoalescableEvent } from "../src/state/coalesce.js";

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
