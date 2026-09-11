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
