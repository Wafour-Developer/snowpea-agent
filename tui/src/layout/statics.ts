/**
 * Which transcript entries are finished enough to leave the live region.
 *
 * The inline layout hands settled entries to Ink's `<Static>`, which writes
 * them into the terminal's own scrollback once and never touches them again.
 * That is what makes the screen fill from the bottom up and what keeps a
 * keystroke from repainting the history.
 *
 * Two rules follow from `<Static>` being append-only:
 *
 *   - entries may only be released in timeline order, so one streaming message
 *     holds back everything after it rather than letting later entries jump the
 *     queue, and
 *   - a tool call that is still the newest entry is held back even when it has
 *     a result, because Ctrl+O expands the last call and an entry already in
 *     the scrollback can no longer change.
 *
 * Pure, so `test/statics.test.ts` can check the cursor without a terminal.
 */

import type { State, TimelineItem } from "../state/store.js";

/** True once this entry will never change again. */
export function isSettled(state: State, item: TimelineItem): boolean {
  if (item.kind === "message") {
    const message = state.messages.find((m) => m.id === item.id);
    return message ? !message.streaming : false;
  }
  if (item.kind === "tool") {
    const call = state.toolCalls.find((c) => c.callId === item.id);
    return call ? call.state !== "running" : false;
  }
  // A diff arrives whole.
  return true;
}

/**
 * How many leading timeline entries may be in `<Static>`.
 *
 * Never goes backwards: `cursor` is what was already released, and entries that
 * left the live region cannot come back.
 */
export function settledCount(state: State, cursor = 0): number {
  const total = state.timeline.length;
  let count = Math.max(0, Math.min(Math.floor(cursor), total));
  while (count < total) {
    const item = state.timeline[count];
    const isLast = count === total - 1;
    if (isLast && item.kind === "tool") break;
    if (!isSettled(state, item)) break;
    count += 1;
  }
  return count;
}
