/**
 * One keypress per stdin read, for chunks that carry erasures.
 *
 * Ink 5 parses each chunk it reads as a single keypress, and its parser drops
 * DEL and BS bytes from a longer sequence: held Backspace, or a few keys typed
 * while the app is busy rendering, can arrive as `\x7f\x7f\x7f` or `ab\x7f`,
 * and then nothing is erased at all (or the letters land and the erasure does
 * not). Splitting such a chunk into its keys before Ink reads it lets every
 * key reach `useInput` as its own event.
 *
 * Only chunks that contain an erasure are split. A paste (bracketed, or any
 * chunk with a line break) and plain text stay whole, since the input treats a
 * multi-character chunk as a paste and a line break inside one is not Enter.
 */

const ESC = "\u001b";
const ERASE = /[\u007f\u0008]/;

/** True when `chunk` should reach Ink as several keypresses. */
export function needsSplit(chunk: string): boolean {
  return chunk.length > 1 && ERASE.test(chunk) && !/[\r\n]/.test(chunk) && !chunk.includes(`${ESC}[200~`);
}

/** The end (exclusive) of the escape sequence that starts at `start`. */
function escapeEnd(chunk: string, start: number): number {
  let at = start + 1;
  if (chunk[at] === ESC) at += 1; // ESC ESC [A: meta + arrow
  const kind = chunk[at];
  if (kind === undefined) return at;
  if (kind === "O") return Math.min(chunk.length, at + 2);
  if (kind !== "[") return at + 1; // Alt + key
  at += 1;
  if (chunk[at] === "[") return Math.min(chunk.length, at + 2); // linux console F-keys
  while (at < chunk.length) {
    const code = chunk.charCodeAt(at);
    at += 1;
    if (code >= 0x40 && code <= 0x7e) break;
  }
  return at;
}

/**
 * `chunk` cut into keys: each escape sequence and control byte on its own,
 * runs of printable text kept together.
 */
export function splitKeys(chunk: string): string[] {
  const keys: string[] = [];
  let at = 0;
  while (at < chunk.length) {
    const code = chunk.charCodeAt(at);
    if (chunk[at] === ESC) {
      const end = escapeEnd(chunk, at);
      keys.push(chunk.slice(at, end));
      at = end;
    } else if (code < 0x20 || code === 0x7f) {
      keys.push(chunk[at]);
      at += 1;
    } else {
      let end = at + 1;
      while (end < chunk.length) {
        const next = chunk.charCodeAt(end);
        if (next < 0x20 || next === 0x7f) break;
        end += 1;
      }
      keys.push(chunk.slice(at, end));
      at = end;
    }
  }
  return keys;
}

/**
 * Make `stdin.read()` hand Ink one key at a time when a chunk needs it.
 *
 * Ink drains `read()` in a loop on `readable`, so the queued keys are all
 * delivered in that same pass, in order. `data` listeners see the original
 * chunk.
 */
export function splitKeypresses(stdin: NodeJS.ReadStream): void {
  const marked = stdin as NodeJS.ReadStream & { snowpeaSplitKeys?: boolean };
  if (marked.snowpeaSplitKeys) return;
  marked.snowpeaSplitKeys = true;
  const read = stdin.read.bind(stdin);
  const queued: string[] = [];
  stdin.read = ((size?: number) => {
    if (queued.length > 0) return queued.shift();
    const chunk = read(size);
    if (chunk === null || chunk === undefined) return chunk;
    const text = typeof chunk === "string" ? chunk : String(chunk);
    if (!needsSplit(text)) return chunk;
    const [first, ...rest] = splitKeys(text);
    queued.push(...rest);
    return first;
  }) as typeof stdin.read;
}
