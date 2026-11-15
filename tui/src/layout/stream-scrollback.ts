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
import { messageLines, messageSource, wrapLine } from "./transcript.js";
import type { Message, State } from "../state/store.js";

export interface StreamChunkEntry {
  key: string;
  kind: "stream-chunk";
  lines: Line[];
}

/**
 * What of a message is already in the scrollback, counted in SOURCE lines.
 *
 * Not in wrapped rows: the same text wraps into a different number of rows
 * after a resize (a tmux split or zoom), so a row index taken at one width
 * skipped or repeated lines at the next. The text is kept so the prefix can be
 * found again when `message.done` hands over text that is not byte-for-byte
 * what streamed.
 */
export interface CommittedPrefix {
  lines: number;
  text: string;
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

/**
 * Leading source lines whose rendering can no longer change.
 *
 * The last line is still growing (an open `**`, a `#` without its title yet,
 * a word that moves to the next row). A run of `|` lines at the end may be a
 * table that a separator or a wider cell will redraw from its first row.
 */
function finalSourceLines(message: Message, source: string[]): number {
  if (!message.streaming) return source.length;
  let count = source.length - 1;
  while (count > 0 && source[count - 1].includes("|")) count -= 1;
  return count;
}

/** How many source lines of `source` the scrollback already holds. */
function committedSourceLines(source: string[], entry: CommittedPrefix | undefined): number {
  if (!entry || entry.lines <= 0) return 0;
  if (source.slice(0, entry.lines).join("\n") === entry.text) return entry.lines;
  // `message.done` rewrote the text. Resume after the place the last frozen
  // lines reappear (nearest to where they were), so nothing prints twice.
  const frozen = entry.text.split("\n");
  const window = Math.min(3, frozen.length);
  const tail = frozen.slice(-window).join("\n");
  let best = -1;
  for (let end = window; end <= source.length; end += 1) {
    if (source.slice(end - window, end).join("\n") !== tail) continue;
    if (best === -1 || Math.abs(end - entry.lines) < Math.abs(best - entry.lines)) best = end;
  }
  return best !== -1 ? best : Math.min(entry.lines, source.length);
}

/** Index of the first wrapped row drawn from source line `line` or later. */
function rowOf(lines: Line[], line: number): number {
  const at = lines.findIndex((row) => (row.source ?? 0) >= line);
  return at === -1 ? lines.length : at;
}

export function commitStreamingPrefixes(
  state: State,
  fromTimelineIndex: number,
  width: number,
  tailRows: number,
  committed: ReadonlyMap<string, CommittedPrefix>,
): { chunks: StreamChunkEntry[]; committed: Map<string, CommittedPrefix> } {
  const chunks: StreamChunkEntry[] = [];
  const next = new Map(committed);
  for (let index = fromTimelineIndex; index < state.timeline.length; index += 1) {
    const item = state.timeline[index];
    if (item.kind !== "message") continue;
    const message = state.messages.find((entry) => entry.id === item.id);
    if (!message) continue;
    // A skill prompt waits for its `message.user` (the fold line) first.
    if (message.awaitingFold) continue;
    const source = messageSource(message);
    const lines = wrappedMessageLines(message, width);
    const start = committedSourceLines(source, next.get(message.id));
    let target = finalSourceLines(message, source);
    // While streaming, the newest rows stay live: freeze only what leaves at
    // least `tailRows` rows behind it.
    const keep = Math.max(1, Math.floor(tailRows));
    while (message.streaming && target > start && lines.length - rowOf(lines, target) < keep) target -= 1;
    if (target <= start) {
      if (start > 0) next.set(message.id, { lines: start, text: source.slice(0, start).join("\n") });
      continue;
    }
    chunks.push({
      key: `stream-${message.id}-${start}-${target}`,
      kind: "stream-chunk",
      lines: lines.slice(rowOf(lines, start), rowOf(lines, target)),
    });
    next.set(message.id, { lines: target, text: source.slice(0, target).join("\n") });
  }
  return { chunks, committed: next };
}

/** Wrapped rows, at `width`, of the part of the message already in scrollback. */
export function messageStartRow(
  message: Message,
  width: number,
  committed: ReadonlyMap<string, CommittedPrefix>,
): number {
  const start = committedSourceLines(messageSource(message), committed.get(message.id));
  return start > 0 ? rowOf(wrappedMessageLines(message, width), start) : 0;
}

/** True when scrollback chunks already contain the whole message. */
export function messageFullyCommitted(
  message: Message,
  _width: number,
  committed: ReadonlyMap<string, CommittedPrefix>,
): boolean {
  if (message.awaitingFold) return false;
  const source = messageSource(message);
  return committedSourceLines(source, committed.get(message.id)) >= source.length;
}
