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
};

const TOOL_MARK: Record<ToolCallEntry["state"], Segment> = {
  running: { text: "◌ ", color: "yellow" },
  ok: { text: "✓ ", color: "green" },
  error: { text: "✗ ", color: "red" },
};

export function plainLength(segments: Segment[]): number {
  let total = 0;
  for (const segment of segments) total += segment.text.length;
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
  const total = plainLength(line.segments);
  if (width <= 0 || total <= width) return [line];

  const text = lineText(line);
  const out: Line[] = [];
  let pos = 0;
  let n = 0;
  while (pos < total) {
    const room = out.length === 0 ? width : Math.max(1, width - indent.length);
    let end = Math.min(total, pos + room);
    if (end < total) {
      const lastSpace = text.lastIndexOf(" ", end);
      if (lastSpace > pos) end = lastSpace;
    }
    if (end <= pos) end = Math.min(total, pos + room);
    const segments = sliceSegments(line.segments, pos, end);
    out.push({
      key: `${line.key}-w${n}`,
      segments: out.length === 0 || indent.length === 0 ? segments : [{ text: indent, dimColor: true }, ...segments],
    });
    n += 1;
    pos = end;
    while (pos < total && text[pos] === " ") pos += 1;
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

/** One message, markdown-lite applied, with the role glyph on the first row. */
export function messageLines(message: Message): Line[] {
  const out: Line[] = [];
  const body = message.streaming ? `${message.text}…` : message.text;
  const source = body.split("\n");
  let inFence = false;

  source.forEach((raw, index) => {
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
  });

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
  shown.forEach((line, index) =>
    out.push({
      key: `${call.callId}-o${index}`,
      segments: [{ text: `  ${line}`, dimColor: !call.error, color: call.error ? "red" : undefined }],
    }),
  );
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
      raw.push(...messageLines(message));
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
