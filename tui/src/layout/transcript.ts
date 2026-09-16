/**
 * Flattens the session transcript into wrapped terminal lines.
 *
 * The inline layout lets Ink lay the transcript out and lets the terminal
 * scroll it. The full-screen layout cannot: it owns a fixed number of rows, so
 * it needs to know how many lines the transcript *is* before deciding which
 * ones to draw. This module is that projection — the same content the
 * `MessageStream` / `ToolCall` / `DiffView` components draw, expressed as an
 * array of styled lines that `sliceViewport` can window.
 *
 * Pure and React-free, so the line count is testable.
 */

import { graphemes, textWidth } from "./text-width.js";

import { diagnosticLineColor } from "../state/lsp.js";
import type { DiffEntry, Message, State, ToolCallEntry } from "../state/store.js";

export interface Segment {
  text: string;
  color?: string;
  dimColor?: boolean;
  bold?: boolean;
  italic?: boolean;
}

export interface Line {
  key: string;
  segments: Segment[];
}

/** Tool output stays collapsed unless Ctrl+O expanded that call. */
const TOOL_OUTPUT_LINES = 12;
/** A single diff never gets to push the whole transcript out of the window. */
const DIFF_LINES = 40;

const ROLE_MARK: Record<Message["role"], Segment> = {
  user: { text: "› ", color: "green", bold: true },
  assistant: { text: "◆ ", color: "blue", bold: true },
  system: { text: "! ", color: "yellow", bold: true },
  // An aside from the surface itself, e.g. a replayed turn's `✓ Done` line: no
  // speaker, so no marker and no attention-seeking colour.
  note: { text: "", dimColor: true },
};

const TOOL_MARK: Record<ToolCallEntry["state"], Segment> = {
  running: { text: "◌ ", color: "yellow" },
  ok: { text: "✓ ", color: "green" },
  error: { text: "✗ ", color: "red" },
};

export function plainLength(segments: Segment[]): number {
  let total = 0;
  for (const segment of segments) total += textWidth(segment.text);
  return total;
}

/** The plain text of a line, for tests and for width arithmetic. */
export function lineText(line: Line): string {
  return line.segments.map((segment) => segment.text).join("");
}

/** Character slice across a segment list, keeping each piece's styling. */
export function sliceSegments(segments: Segment[], start: number, end: number): Segment[] {
  const out: Segment[] = [];
  let cursor = 0;
  for (const segment of segments) {
    const segStart = cursor;
    const segEnd = cursor + segment.text.length;
    cursor = segEnd;
    if (segEnd <= start) continue;
    if (segStart >= end) break;
    const text = segment.text.slice(Math.max(0, start - segStart), Math.min(segment.text.length, end - segStart));
    if (text.length > 0) out.push({ ...segment, text });
  }
  return out;
}

/**
 * Soft-wrap one line to `width`, preferring to break on a space.
 *
 * Continuation lines carry `indent` so a wrapped message stays aligned under
 * its role glyph.
 */
export function wrapLine(line: Line, width: number, indent = ""): Line[] {
  if (width <= 0 || plainLength(line.segments) <= width) return [line];
  const text = lineText(line);
  const parts = graphemes(text);
  const out: Line[] = [];
  let start = 0;
  while (start < parts.length) {
    const prefix = out.length === 0 ? "" : indent;
    const room = Math.max(1, width - textWidth(prefix));
    let end = start;
    let used = 0;
    while (end < parts.length && used + parts[end].width <= room) {
      used += parts[end].width;
      end += 1;
    }
    // A single wide grapheme cannot fit a one-cell viewport: keep it intact.
    if (end === start) end += 1;
    if (end < parts.length) {
      let space = end;
      while (space > start && parts[space]?.text !== " ") space -= 1;
      if (space > start) end = space;
    }
    const segments = sliceSegments(line.segments, parts[start].index, parts[end]?.index ?? text.length);
    out.push({
      key: `${line.key}-w${out.length}`,
      segments: prefix ? [{ text: prefix, dimColor: true }, ...segments] : segments,
    });
    start = end;
    while (parts[start]?.text === " ") start += 1;
  }
  return out;
}

/** Inline markdown: `code`, **bold**, *italic*. */
export function inlineSegments(text: string): Segment[] {
  const parts: Segment[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push({ text: text.slice(last, match.index) });
    const token = match[0];
    if (token.startsWith("`")) parts.push({ text: token.slice(1, -1), color: "cyan" });
    else if (token.startsWith("**")) parts.push({ text: token.slice(2, -2), bold: true });
    else parts.push({ text: token.slice(1, -1), italic: true });
    last = match.index + token.length;
  }
  if (last < text.length) parts.push({ text: text.slice(last) });
  return parts.length > 0 ? parts : [{ text }];
}

/** Split real cell separators, not escaped pipes or pipes inside inline code. */
function tableCells(raw: string): string[] | null {
  const text = raw.trim();
  if (!text.includes("|")) return null;
  const cells: string[] = [];
  let cell = "";
  let ticks = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\\" && text[i + 1] === "|") {
      cell += "|";
      i += 1;
    } else if (text[i] === "`") {
      let end = i + 1;
      while (text[end] === "`") end += 1;
      const count = end - i;
      if (ticks === 0) ticks = count;
      else if (ticks === count) ticks = 0;
      cell += text.slice(i, end);
      i = end - 1;
    } else if (text[i] === "|" && ticks === 0) {
      cells.push(cell.trim());
      cell = "";
    } else cell += text[i];
  }
  cells.push(cell.trim());
  if (text.startsWith("|")) cells.shift();
  if (text.endsWith("|") && cells.at(-1) === "") cells.pop();
  return cells.length > 0 ? cells : null;
}

function tableAt(source: string[], start: number, width: number, id: string): { lines: Line[]; end: number } | null {
  const header = tableCells(source[start]);
  const divider = tableCells(source[start + 1] ?? "");
  if (!header || !divider || header.length !== divider.length ||
      !divider.every(cell => /^:?-{3,}:?$/.test(cell))) return null;
  const rows = [header];
  let end = start + 2;
  while (end < source.length) {
    const row = tableCells(source[end]);
    if (!row || row.length !== header.length) break;
    rows.push(row);
    end += 1;
  }
  const styled = rows.map(row => row.map(inlineSegments));
  const widths = header.map((_, col) => Math.max(2, ...styled.map(row => plainLength(row[col]))));
  const lines: Line[] = [];
  const add = (segments: Segment[]) => lines.push({ key: `${id}-table${start}-${lines.length}`, segments });
  // Too many columns for this terminal: a stacked representation preserves all values.
  if (header.length * 5 + 1 > width) {
    styled.forEach((row, rowIndex) => {
      if (rowIndex === 0) return;
      row.forEach((cell, col) => {
        const entry = { key: `${id}-table${start}-${rowIndex}-${col}`, segments: [
          ...styled[0][col].map(segment => ({ ...segment, bold: true })), { text: ": " }, ...cell,
        ] };
        lines.push(...wrapLine(entry, width));
      });
    });
    if (lines.length === 0) add(styled[0].flatMap((cell, col) => col ? [{ text: " / " }, ...cell] : cell));
    return { lines, end };
  }
  while (widths.reduce((sum, value) => sum + value, 0) + header.length * 3 + 1 > width) {
    const largest = widths.indexOf(Math.max(...widths));
    widths[largest] -= 1;
  }
  const border = (left: string, middle: string, right: string) => add([{
    text: left + widths.map(size => "─".repeat(size + 2)).join(middle) + right, dimColor: true,
  }]);
  border("┌", "┬", "┐");
  styled.forEach((row, rowIndex) => {
    const cells = row.map((segments, col) => wrapLine({ key: "cell", segments }, widths[col]));
    const height = Math.max(...cells.map(cell => cell.length));
    for (let line = 0; line < height; line += 1) {
      const segments: Segment[] = [{ text: "│ ", dimColor: true }];
      cells.forEach((cell, col) => {
        const content = cell[line]?.segments ?? [];
        const padding = Math.max(0, widths[col] - plainLength(content));
        const right = divider[col].endsWith(":");
        const left = right ? (divider[col].startsWith(":") ? Math.floor(padding / 2) : padding) : 0;
        segments.push({ text: " ".repeat(left) },
          ...content.map(segment => rowIndex === 0 ? { ...segment, bold: true } : segment),
          { text: " ".repeat(padding - left) },
          { text: col === cells.length - 1 ? " │" : " │ ", dimColor: true });
      });
      add(segments);
    }
    if (rowIndex === 0) border("├", "┼", "┤");
  });
  border("└", "┴", "┘");
  return { lines, end };
}

/** One message, markdown-lite applied, with the role glyph on the first row. */
export function messageLines(message: Message, width = 80): Line[] {
  const out: Line[] = [];
  // A model often opens with a blank line; the glyph belongs on the first
  // line that says something, not on an empty one above it.
  const text = message.text.replace(/^(?:[ \t]*\n)+/, "").replace(/(?:\n[ \t]*)+$/, "");
  const body = message.streaming ? `${text}…` : text;
  const source = body.split("\n");
  let inFence = false;

  for (let index = 0; index < source.length; index += 1) {
    const raw = source[index];
    if (!inFence) {
      const table = tableAt(source, index, Math.max(1, width - 2), message.id);
      if (table) {
        table.lines.forEach((line, row) => out.push({
          ...line,
          segments: [index === 0 && row === 0 ? ROLE_MARK[message.role] : { text: "  " }, ...line.segments],
        }));
        index = table.end - 1;
        continue;
      }
    }
    const key = `${message.id}-l${index}`;
    let segments: Segment[];
    if (raw.trimStart().startsWith("```")) {
      inFence = !inFence;
      segments = [{ text: raw, dimColor: true }];
    } else if (inFence) {
      segments = [{ text: raw, color: "cyan" }];
    } else {
      const heading = /^(#{1,6})\s+(.*)$/.exec(raw);
      const bullet = /^(\s*)[-*]\s+(.*)$/.exec(raw);
      if (heading) segments = [{ text: heading[2], bold: true, color: "yellow" }];
      else if (bullet)
        segments = [{ text: bullet[1] }, { text: "• ", color: "magenta" }, ...inlineSegments(bullet[2])];
      else segments = inlineSegments(raw);
    }
    const mark = index === 0 ? ROLE_MARK[message.role] : { text: "  " };
    out.push({ key, segments: [mark, ...segments] });
  }

  return out;
}

/** Compact one-line rendering of the tool arguments. */
export function summarizeArgs(args: Record<string, unknown>, max = 60): string {
  const joined = Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  return joined.length > max ? `${joined.slice(0, max - 1)}…` : joined;
}

export function toolCallLines(call: ToolCallEntry, expanded: boolean): Line[] {
  const body = call.error ?? call.output ?? "";
  const all = body.length > 0 ? body.split("\n") : [];
  const shown = expanded ? all.slice(0, TOOL_OUTPUT_LINES) : [];
  const hidden = all.length - shown.length;

  const head: Segment[] = [
    TOOL_MARK[call.state] ?? TOOL_MARK.running,
    { text: call.name, bold: true },
    { text: ` ${summarizeArgs(call.args)}`, dimColor: true },
  ];
  if (!expanded && all.length > 0) head.push({ text: ` (${all.length} lines)`, dimColor: true });

  const out: Line[] = [{ key: `${call.callId}-h`, segments: head }];
  shown.forEach((line, index) => {
    const severity = diagnosticLineColor(line);
    out.push({
      key: `${call.callId}-o${index}`,
      segments: [
        {
          text: `  ${line}`,
          dimColor: !call.error && severity === undefined,
          color: call.error ? "red" : severity,
        },
      ],
    });
  });
  if (expanded && hidden > 0) {
    out.push({ key: `${call.callId}-more`, segments: [{ text: `  … ${hidden} more lines`, dimColor: true }] });
  }
  return out;
}

function diffLineColor(line: string): string | undefined {
  if (line.startsWith("+++") || line.startsWith("---")) return "cyan";
  if (line.startsWith("@@")) return "magenta";
  if (line.startsWith("+")) return "green";
  if (line.startsWith("-")) return "red";
  return undefined;
}

export function diffLines(diff: DiffEntry): Line[] {
  const all = diff.patch.split("\n");
  const shown = all.slice(0, DIFF_LINES);
  const hidden = all.length - shown.length;
  const out: Line[] = [
    { key: `${diff.id}-h`, segments: [{ text: `± ${diff.path}`, bold: true, color: "yellow" }] },
  ];
  shown.forEach((line, index) =>
    out.push({ key: `${diff.id}-d${index}`, segments: [{ text: line, color: diffLineColor(line) }] }),
  );
  if (hidden > 0) {
    out.push({ key: `${diff.id}-more`, segments: [{ text: `… ${hidden} more diff lines`, dimColor: true }] });
  }
  return out;
}

/** The subagent tree, drawn under the transcript while delegates are alive. */
export function subagentLines(state: State): Line[] {
  if (state.subagents.length === 0) return [];
  const marks: Record<string, { glyph: string; color: string }> = {
    queued: { glyph: "◦", color: "gray" },
    running: { glyph: "●", color: "cyan" },
    done: { glyph: "✓", color: "green" },
    error: { glyph: "✗", color: "red" },
  };
  const counts = new Map<string, number>();
  for (const entry of state.subagents) counts.set(entry.status, (counts.get(entry.status) ?? 0) + 1);
  const summary = ["running", "queued", "done", "error"]
    .filter((status) => counts.has(status))
    .map((status) => `${counts.get(status)} ${status}`)
    .join(" · ");

  const out: Line[] = [
    { key: "subagents-h", segments: [{ text: `subagents (${summary})`, dimColor: true }] },
  ];
  state.subagents.forEach((entry, index) => {
    const last = index === state.subagents.length - 1;
    const mark = marks[entry.status] ?? marks.queued;
    const label = entry.name ? `${entry.name}: ` : "";
    out.push({
      key: `${entry.agentId}-h`,
      segments: [
        { text: last ? "  └─ " : "  ├─ ", dimColor: true },
        { text: `${mark.glyph} `, color: mark.color },
        { text: `${label}${entry.task}` },
      ],
    });
    const detail = entry.status === "done" || entry.status === "error" ? entry.summary : entry.lastText;
    if (detail) {
      out.push({
        key: `${entry.agentId}-d`,
        segments: [{ text: `  ${last ? " " : "│"}     ${detail.replace(/\s+/g, " ").trim()}`, dimColor: true }],
      });
    }
  });
  return out;
}

/**
 * The whole transcript as wrapped lines, oldest first.
 *
 * `width` is the terminal width already reduced by the layout's padding.
 */
export function transcriptLines(
  state: State,
  width: number,
  { expandedCall = null }: { expandedCall?: string | null } = {},
): Line[] {
  const raw: Line[] = [];

  for (const item of state.timeline) {
    if (item.kind === "message") {
      const message = state.messages.find((m) => m.id === item.id);
      if (!message) continue;
      raw.push(...messageLines(message, width));
      raw.push({ key: `${item.id}-gap`, segments: [{ text: "" }] });
      continue;
    }
    if (item.kind === "tool") {
      const call = state.toolCalls.find((c) => c.callId === item.id);
      if (!call) continue;
      raw.push(...toolCallLines(call, expandedCall === item.id));
      raw.push({ key: `${item.id}-gap`, segments: [{ text: "" }] });
      continue;
    }
    const diff = state.diffs.find((d) => d.id === item.id);
    if (!diff) continue;
    raw.push(...diffLines(diff));
    raw.push({ key: `${item.id}-gap`, segments: [{ text: "" }] });
  }

  const tree = subagentLines(state);
  if (tree.length > 0) {
    raw.push({ key: "subagents-gap", segments: [{ text: "" }] });
    raw.push(...tree);
  }

  const wrapped: Line[] = [];
  for (const line of raw) wrapped.push(...wrapLine(line, width, "  "));
  return wrapped;
}
