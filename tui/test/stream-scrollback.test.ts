import { describe, expect, it } from "vitest";

import {
  commitStreamingPrefixes,
  messageFullyCommitted,
  streamingCommitTarget,
  wrappedMessageLines,
} from "../src/layout/stream-scrollback.js";
import type { Message, State } from "../src/state/store.js";

function message(id: string, text: string, streaming: boolean): Message {
  return { id, role: "assistant", text, streaming };
}

function stateWith(message: Message): State {
  return {
    timeline: [{ kind: "message", id: message.id }],
    messages: [message],
  } as State;
}

describe("streamingCommitTarget", () => {
  it("keeps only the tail while streaming", () => {
    expect(streamingCommitTarget(50, 8, true)).toBe(42);
  });

  it("commits everything once streaming stops", () => {
    expect(streamingCommitTarget(50, 8, false)).toBe(50);
  });
});

describe("commitStreamingPrefixes", () => {
  const long = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`).join("\n");

  it("writes overflow lines to scrollback chunks", () => {
    const msg = message("m1", long, true);
    const first = commitStreamingPrefixes(stateWith(msg), 0, 40, 6, new Map());
    expect(first.chunks).toHaveLength(1);
    expect(first.chunks[0].lines.length).toBe(24);
    expect(first.committed.get("m1")).toBe(24);

    const grown = message("m1", `${long}\nline 31\nline 32`, true);
    const second = commitStreamingPrefixes(
      stateWith(grown),
      0,
      40,
      6,
      first.committed,
    );
    expect(second.chunks).toHaveLength(1);
    expect(second.committed.get("m1")).toBe(26);
  });

  it("flushes the remainder when streaming ends", () => {
    const msg = message("m1", long, false);
    const result = commitStreamingPrefixes(stateWith(msg), 0, 40, 6, new Map());
    expect(result.committed.get("m1")).toBe(wrappedMessageLines(msg, 40).length);
    expect(messageFullyCommitted(msg, 40, result.committed)).toBe(true);
  });
});
