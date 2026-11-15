/**
 * A fake tty for the tests that drive real Ink.
 *
 * Ink only turns the keyboard on when `stdin.isTTY` is set, and it reads with
 * `readable` + `read()`, which a `PassThrough` already supports. `stdout` has
 * to report a size and let the test see every byte written, which is what the
 * byte-level assertions are made of.
 */

import { PassThrough } from "node:stream";

export const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

export function fakeStdin(): any {
  const stream = new PassThrough() as any;
  stream.isTTY = true;
  stream.setRawMode = () => stream;
  stream.ref = () => stream;
  stream.unref = () => stream;
  return stream;
}

export interface FakeStdout {
  stream: any;
  /** Every chunk Ink wrote, in order. */
  chunks: string[];
  /** All of it as one string. */
  text(): string;
}

export function fakeStdout(columns = 80, rows = 20): FakeStdout {
  const chunks: string[] = [];
  const stream: any = new PassThrough();
  stream.columns = columns;
  stream.rows = rows;
  const write = stream.write.bind(stream);
  stream.write = (chunk: any, ...rest: any[]) => {
    chunks.push(String(chunk));
    return write(chunk, ...rest);
  };
  return { stream, chunks, text: () => chunks.join("") };
}

/** Type one character at a time, the way a person does. */
export async function type(stdin: any, characters: string, gapMs = 60): Promise<void> {
  for (const character of characters) {
    stdin.write(character);
    await sleep(gapMs);
  }
}

/** How many times `needle` appears in `haystack`. */
export function countOf(haystack: string, needle: string): number {
  let count = 0;
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return count;
    count += 1;
    from = at + needle.length;
  }
}

/**
 * Replays what Ink writes — text, `\n`, erase-line, cursor-up, column 0, SGR,
 * clear — and returns the rows a terminal of `rows` rows shows at the end.
 */
export function screen(output: string, rows: number): string[] {
  let lines: string[] = [""];
  let row = 0;
  let col = 0;
  for (let i = 0; i < output.length; i += 1) {
    const ch = output[i];
    if (ch === "\u001B") {
      if (output[i + 1] === "]") {
        // OSC: skip to BEL or ST.
        const bel = output.indexOf("\u0007", i);
        i = bel === -1 ? output.length : bel;
        continue;
      }
      const match = /^\u001B\[([0-9;?]*)([A-Za-z])/.exec(output.slice(i, i + 16));
      if (!match) continue;
      i += match[0].length - 1;
      const n = Number(match[1] || "1");
      switch (match[2]) {
        case "K":
          lines[row] = "";
          break;
        case "A":
          row = Math.max(0, row - n);
          break;
        case "G":
          col = 0;
          break;
        case "J":
          if (match[1] === "2" || match[1] === "3") {
            lines = [""];
            row = 0;
            col = 0;
          }
          break;
        default:
          break;
      }
      continue;
    }
    if (ch === "\n") {
      row += 1;
      col = 0;
      while (lines.length <= row) lines.push("");
      continue;
    }
    if (ch === "\r") {
      col = 0;
      continue;
    }
    const current = lines[row] ?? "";
    lines[row] = current.slice(0, col).padEnd(col) + ch + current.slice(col + 1);
    col += 1;
  }
  const bottom = Math.max(lines.length, row + 1);
  return lines.slice(Math.max(0, bottom - rows), bottom);
}
