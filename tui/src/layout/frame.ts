/**
 * Turns Ink's whole-frame repaints into per-row updates.
 *
 * Ink has no idea which rows changed: every frame it writes the entire screen,
 * preceded by an `eraseLines` run that walks the cursor back up over the last
 * one. On a full-screen layout that is a kilobyte or more per keystroke, all of
 * it to redraw a single input row.
 *
 * The full-screen app owns the alternate buffer and always draws the same
 * number of rows at the same origin, so the frame can be addressed absolutely:
 * compare it row by row with the frame before it and move the cursor to the
 * rows that actually moved. Nothing here knows about React or Ink internals
 * beyond the shape of the chunk Ink writes, which `splitFrame` verifies before
 * this module touches anything.
 *
 * Pure and stream-free, so `test/frame.test.ts` can check the arithmetic.
 */

/**
 * The `eraseLines(n)` run that opens every Ink frame: n × (erase line, then
 * cursor up for all but the last), closed by a carriage return to column 1.
 * `eraseLines(0)` is empty, which is why the whole prefix is optional.
 */
const ERASE_LINES = /^(?:\u001B\[2K(?:\u001B\[1A)?)*\u001B\[G/;

/** Ink's other branch — a full screen clear — which this module passes through. */
const CLEAR_TERMINAL = "\u001B[2J";

/** Move to row `n`, column 1. */
export function cursorTo(row: number): string {
  return `\u001B[${row};1H`;
}

/** Drop any style still in force, then clear the rest of the row. */
export const ERASE_TO_END = "\u001B[0m\u001B[K";

/**
 * The frame inside one chunk Ink wrote, or null when the chunk is not a frame.
 *
 * `first` marks the very first frame, which carries no erase prefix because
 * there was nothing on screen to erase yet.
 */
export function splitFrame(chunk: string, first: boolean): string[] | null {
  if (chunk.includes(CLEAR_TERMINAL)) return null;
  const match = ERASE_LINES.exec(chunk);
  if (!match && !first) return null;
  const body = chunk.slice(match ? match[0].length : 0);
  if (body.length === 0) return null;
  // Ink always ends a frame with the newline `log-update` appends.
  return (body.endsWith("\n") ? body.slice(0, -1) : body).split("\n");
}

/**
 * The escape sequence that turns the screen showing `previous` into `next`.
 *
 * Rows that did not change cost nothing. Rows the new frame dropped are erased
 * so a shorter frame cannot leave the tail of a taller one behind.
 */
export function diffFrame(previous: string[] | null, next: string[]): string {
  let out = "";
  for (let row = 0; row < next.length; row += 1) {
    if (previous && previous[row] === next[row]) continue;
    out += cursorTo(row + 1) + next[row] + ERASE_TO_END;
  }
  if (previous) {
    for (let row = next.length; row < previous.length; row += 1) {
      out += cursorTo(row + 1) + ERASE_TO_END;
    }
  }
  return out;
}

/** The subset of `NodeJS.WriteStream` the writer needs. */
export interface FrameSink {
  write(chunk: string): unknown;
}

export interface FrameWriter {
  /** Feed one chunk Ink wrote; returns what was actually sent to the sink. */
  write(chunk: string): string;
  /** Forget the screen, so the next frame is painted in full. */
  reset(): void;
}

/**
 * Wrap a sink so Ink's frames reach it as row updates.
 *
 * Anything that is not a frame — a patched `console.log`, the alternate-screen
 * escapes, Ink's own full-screen clear — is passed through untouched, and
 * invalidates the remembered screen because it moved the cursor or the content
 * in ways this module did not track.
 */
export function createFrameWriter(sink: FrameSink): FrameWriter {
  let previous: string[] | null = null;

  return {
    write(chunk: string): string {
      const frame = splitFrame(chunk, previous === null);
      if (!frame) {
        previous = null;
        sink.write(chunk);
        return chunk;
      }
      const out = diffFrame(previous, frame);
      previous = frame;
      if (out.length > 0) sink.write(out);
      return out;
    },
    reset(): void {
      previous = null;
    },
  };
}
