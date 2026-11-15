/**
 * The streaming scrollback, as a property: whatever the deltas, the widths and
 * the tail, every line of an answer reaches `<Static>` exactly once, in order.
 *
 * `commitStreamingPrefixes` freezes the top of a message that is still being
 * written. What it freezes must never be drawn again by the live tail, and what
 * the live tail skips must already be frozen — otherwise the user sees a line
 * twice, or never. Three things used to break that:
 *
 *   A. a resize mid-stream (the frozen count was a row index at the old width),
 *   B. markdown whose earlier rows change when later text arrives (a table's
 *      separator or a wider cell, `**bold` closing, a line still growing), and
 *   C. `message.done` text that is not byte-for-byte the streamed text.
 *
 * Every source line carries a marker (`L007`) so the check reads the rendered
 * rows the way a person reads the terminal: which lines are there, how often.
 */

import { describe, expect, it } from "vitest";

import {
  commitStreamingPrefixes,
  messageStartRow,
  wrappedMessageLines,
  type CommittedPrefix,
} from "../src/layout/stream-scrollback.js";
import { lineText, type Line } from "../src/layout/transcript.js";
import type { Message, State } from "../src/state/store.js";

function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const KO = [
  "스크롤 위치가 어긋나면 방금 보낸 메시지가 화면에서 사라질 수 있습니다",
  "터미널 너비가 바뀌면 같은 문장도 다른 줄 수로 감깁니다",
  "표와 코드 블록은 나중에 도착한 글자가 앞줄의 모양을 바꿉니다",
];
const EN = [
  "the committed index must survive a resize in the middle of a long answer",
  "a **bold phrase that spans** several words and `inline code` near the wrap point",
  "nothing may be lost and nothing may be printed twice",
];

/** A long markdown answer: Korean and English, a table, a fence, nested lists. */
function answer(): string {
  let n = 0;
  const m = () => `L${String(++n).padStart(3, "0")}`;
  const out: string[] = [];
  out.push(`# ${m()} 개요 Overview`);
  out.push("");
  for (let i = 0; i < 4; i += 1) out.push(`${m()} ${KO[i % 3]} — ${EN[i % 3]} ${KO[(i + 1) % 3]}.`);
  out.push("");
  out.push(`## ${m()} 표 Table`);
  out.push(`| ${m()} 이름 | 설명 |`);
  out.push("|---|---|");
  for (let i = 0; i < 6; i += 1) {
    // Later rows are wider, so every earlier row re-renders when they arrive.
    out.push(`| ${m()} row${i} | ${"넓은 셀 wide cell ".repeat(i + 1).trim()} |`);
  }
  out.push("");
  out.push("```ts");
  for (let i = 0; i < 5; i += 1) out.push(`const ${m()} = a | b; // ${EN[i % 3]}`);
  out.push("```");
  out.push("");
  for (let i = 0; i < 4; i += 1) {
    out.push(`- ${m()} ${KO[i % 3]} **강조 ${i}** with \`code\``);
    out.push(`  - ${m()} nested ${EN[(i + 2) % 3]} ${KO[(i + 2) % 3]}`);
  }
  out.push("");
  out.push(`### ${m()} 끝 End`);
  for (let i = 0; i < 6; i += 1) out.push(`${m()} ${EN[i % 3]} ${KO[i % 3]} ${EN[(i + 1) % 3]}`);
  return out.join("\n");
}

const markers = (lines: Line[]): string[] =>
  lines.flatMap((line) => lineText(line).match(/L\d{3}/g) ?? []);

const stateWith = (message: Message): State =>
  ({ timeline: [{ kind: "message", id: message.id }], messages: [message] }) as State;

interface Run {
  /** Rows written to `<Static>` for the message, in order. */
  written: Line[];
  committed: Map<string, CommittedPrefix>;
}

/** One frame: commit what may leave, then check frozen + live == whole. */
function frame(run: Run, message: Message, width: number, tail: number): void {
  const result = commitStreamingPrefixes(stateWith(message), 0, width, tail, run.committed);
  for (const chunk of result.chunks) run.written.push(...chunk.lines);
  run.committed = result.committed;
  const all = wrappedMessageLines(message, width);
  const live = all.slice(messageStartRow(message, width, run.committed));
  // A line `message.done` dropped may already be in the scrollback; that one
  // is past saving, but it must not shift what is drawn after it.
  const whole = markers(all);
  const frozen = markers(run.written).filter((marker) => whole.includes(marker));
  expect([...frozen, ...markers(live)]).toEqual(whole);
}

function stream(
  seed: number,
  final: (text: string) => string,
  widths = [24, 31, 47, 60, 80, 100, 120],
  lead = "",
): { run: Run; text: string } {
  const random = rng(seed);
  // What streams; `final` turns it into what `message.done` carries.
  const text = lead + answer();
  let width = widths[Math.floor(random() * widths.length)];
  const run: Run = { written: [], committed: new Map() };
  let at = 0;
  while (at < text.length) {
    at = Math.min(text.length, at + 1 + Math.floor(random() * 40));
    if (random() < 0.15) width = widths[Math.floor(random() * widths.length)];
    const tail = [1, 2, 4, 8][Math.floor(random() * 4)];
    frame(run, { id: "m", role: "assistant", text: text.slice(0, at), streaming: true }, width, tail);
  }
  // `message.done`: the reducer swaps the text and closes the message.
  if (random() < 0.5) width = widths[Math.floor(random() * widths.length)];
  const done = final(text);
  frame(run, { id: "m", role: "assistant", text: done, streaming: false }, width, 4);
  return { run, text: done };
}

describe("streaming scrollback writes every line exactly once", () => {
  for (let seed = 1; seed <= 40; seed += 1) {
    it(`seed ${seed}: random deltas, widths and tails`, () => {
      const { run, text } = stream(seed, (t) => t);
      const expected = text.match(/L\d{3}/g) ?? [];
      expect(markers(run.written)).toEqual(expected);
    });
  }

  it("one width throughout: markdown that re-renders earlier rows (B)", () => {
    for (const width of [24, 47, 80]) {
      for (let seed = 50; seed < 60; seed += 1) {
        const { run, text } = stream(seed, (t) => t, [width]);
        expect(markers(run.written)).toEqual(text.match(/L\d{3}/g));
      }
    }
  });

  it("message.done text trimmed of the whitespace around it", () => {
    for (let seed = 100; seed < 110; seed += 1) {
      const { run, text } = stream(seed, (t) => t.trim(), undefined, "\n  ");
      expect(markers(run.written)).toEqual(text.match(/L\d{3}/g));
    }
  });

  it("message.done text with an early line dropped keeps the rest exactly once", () => {
    for (let seed = 200; seed < 210; seed += 1) {
      const { run, text } = stream(seed, (t) => t.split("\n").filter((l) => !l.includes("L003")).join("\n"));
      const seen = markers(run.written);
      expect(new Set(seen).size).toBe(seen.length);
      for (const marker of text.match(/L\d{3}/g) ?? []) expect(seen).toContain(marker);
    }
  });
});
