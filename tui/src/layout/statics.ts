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
 * That second rule is bounded by height. What is held stays on screen and is
 * repainted on every frame, so a long turn's worth of edits must not accumulate
 * there: `liveRows` caps the hold at what the layout has room for.
 *
 * Pure, so `test/statics.test.ts` can check the cursor without a terminal.
 */

import type { State, TimelineItem } from "../state/store.js";
import { diffLines, toolCallLines } from "./transcript.js";

/**
 * Rows a held-back entry is assumed to cost when it cannot be measured.
 *
 * Only reached for a tool call or diff the state no longer knows about, which
 * is a race rather than a normal path; one row is the smallest honest guess.
 */
const UNKNOWN_ENTRY_ROWS = 1;

/**
 * Rows a card spends on chrome the flat `…Lines` helpers do not produce.
 *
 * The inline layout draws components, not lines: a card carries a margin row
 * and may fold its tail behind a "… n more" line. Counting two rather than
 * measuring the components keeps this module off the components' internals,
 * and erring high is the safe direction — it releases an entry to the
 * scrollback a little early, where the worst case is a summary line split in
 * two, while erring low brings back the full-screen clear this bound exists to
 * prevent.
 */
const ENTRY_CHROME_ROWS = 2;

/** Rows this entry occupies in the live region, unexpanded and rounded up. */
export function entryRows(state: State, item: TimelineItem): number {
  if (item.kind === "tool") {
    const call = state.toolCalls.find((c) => c.callId === item.id);
    return call ? toolCallLines(call, false).length + ENTRY_CHROME_ROWS : UNKNOWN_ENTRY_ROWS;
  }
  if (item.kind === "diff") {
    const diff = state.diffs.find((d) => d.id === item.id);
    return diff ? diffLines(diff).length + ENTRY_CHROME_ROWS : UNKNOWN_ENTRY_ROWS;
  }
  return UNKNOWN_ENTRY_ROWS;
}

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
export function settledCount(
  state: State,
  cursor = 0,
  liveRows = Number.POSITIVE_INFINITY,
): number {
  const total = state.timeline.length;
  const start = Math.max(0, Math.min(Math.floor(cursor), total));
  let count = start;
  while (count < total && isSettled(state, state.timeline[count])) count += 1;

  // Two kinds of entry are held back while the turn is live.
  //
  // A tool run that ends the released range is not finished being a run: more
  // calls can still join it, and releasing it now would split one summary line
  // into several. A diff is held for a different reason — a language server
  // publishes its diagnostics a moment after the edit, and the badge those put
  // on the header cannot be added to a line already in the scrollback.
  //
  // Either way, a non-tool, non-diff entry after them, or the end of the turn,
  // proves there is nothing more to come.
  //
  // Both reasons are about the newest part of the run, so the hold is bounded
  // by what there is room for. An implementing turn can finish thirty edits
  // before it says anything, and holding all of them keeps thirty cards —
  // diffs included — in the live region for minutes. Ink redraws that region
  // on every frame, and once it is as tall as the terminal it clears the
  // screen and rewrites the whole scrollback before each one, which is what a
  // long turn looked like to the user: continuous flicker. `liveRows` is what
  // the inline layout has left for the live region; entries past it are
  // released early, at the cost of an extra summary line and a diagnostics
  // badge on an edit that has already scrolled away.
  if (state.turnActive) {
    let rows = 0;
    while (count > start) {
      const item = state.timeline[count - 1];
      if (item.kind !== "tool" && item.kind !== "diff") break;
      rows += entryRows(state, item);
      if (rows > liveRows) break;
      count -= 1;
    }
  }
  return count;
}
