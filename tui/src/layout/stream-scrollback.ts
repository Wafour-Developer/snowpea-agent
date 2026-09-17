/**
 * Push the prefix of a streaming message into Ink `<Static>` scrollback.
 *
 * Hermes/Codex-style CLIs grow the terminal scrollback as text arrives; only a
 * short tail stays in the live region. Snowpea's inline layout cannot let the
 * live region outgrow the terminal without Ink clearing the screen, so this
 * module commits complete wrapped lines to scrollback while the message is
 * still open.
 */

import type { Line } from "./transcript.js";
import { messageLines, wrapLine } from "./transcript.js";
import type { Message, State } from "../state/store.js";

export interface StreamChunkEntry {
  key: string;
  kind: "stream-chunk";
  lines: Line[];
}

/** Wrapped terminal rows for one message, including the role glyph. */
export function wrappedMessageLines(message: Message, width: number): Line[] {
  return messageLines(message, width).flatMap((line) => wrapLine(line, width, "  "));
}

/**
 * Lines of a streaming message that may leave the live tail for scrollback.
 *
 * `tailRows` is how many wrapped rows the live region may keep; everything
 * before that is frozen into `<Static>` once and never repainted.
 */
export function streamingCommitTarget(lineCount: number, tailRows: number, streaming: boolean): number {
  const tail = Math.max(1, Math.floor(tailRows));
  if (!streaming) return lineCount;
  return Math.max(0, lineCount - tail);
}

export function commitStreamingPrefixes(
  state: State,
  fromTimelineIndex: number,
  width: number,
  tailRows: number,
  committed: ReadonlyMap<string, number>,
): { chunks: StreamChunkEntry[]; committed: Map<string, number> } {
  const chunks: StreamChunkEntry[] = [];
  const next = new Map(committed);
  for (let index = fromTimelineIndex; index < state.timeline.length; index += 1) {
    const item = state.timeline[index];
    if (item.kind !== "message") continue;
    const message = state.messages.find((entry) => entry.id === item.id);
    if (!message) continue;
    const lines = wrappedMessageLines(message, width);
    const previous = next.get(message.id) ?? 0;
    const target = streamingCommitTarget(lines.length, tailRows, message.streaming);
    if (target <= previous) continue;
    chunks.push({
      key: `stream-${message.id}-${previous}`,
      kind: "stream-chunk",
      lines: lines.slice(previous, target),
    });
    next.set(message.id, target);
  }
  return { chunks, committed: next };
}

/** True when scrollback chunks already contain the whole message. */
export function messageFullyCommitted(
  message: Message,
  width: number,
  committed: ReadonlyMap<string, number>,
): boolean {
  const total = wrappedMessageLines(message, width).length;
  return total > 0 && (committed.get(message.id) ?? 0) >= total;
}
