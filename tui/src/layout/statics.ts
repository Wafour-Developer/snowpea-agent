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
 *   - a run of tool calls at the end of the timeline is held back while the turn
 *     is still going, both because Ctrl+O expands the last call and because the
 *     run reaches the scrollback as one summary line, which can only be written
 *     once the run is known to be over.
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
  const start = Math.max(0, Math.min(Math.floor(cursor), total));
  let count = start;
  while (count < total && isSettled(state, state.timeline[count])) count += 1;

  // A tool run that ends the released range is not finished being a run: while
  // the turn is live, more calls can still join it, and releasing it now would
  // split one summary line into several. Only a non-tool entry after the run —
  // or the end of the turn — proves it is over.
  if (state.turnActive) {
    while (count > start && state.timeline[count - 1].kind === "tool") count -= 1;
  }
  return count;
}
