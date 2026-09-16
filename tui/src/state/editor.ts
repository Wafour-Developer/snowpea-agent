import { graphemes, textWidth } from "../layout/text-width.js";

export interface EditorState {
  text: string;
  cursor: number;
}

export interface EditorLine {
  start: number;
  end: number;
  cells: number;
}

export interface EditorLayout {
  lines: EditorLine[];
  cursorRow: number;
  cursorCol: number;
}

interface Part {
  text: string;
  index: number;
  end: number;
  width: number;
}

const WHITESPACE = /^\s$/u;

function partsOf(text: string): Part[] {
  return graphemes(text).map((part) => ({
    ...part,
    end: part.index + part.text.length,
  }));
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function safeWidth(width: number): number {
  const rounded = Number.isFinite(width) ? Math.floor(width) : 1;
  return Math.max(1, rounded);
}

function floorBoundary(parts: Part[], cursor: number, textLength: number): number {
  if (cursor <= 0) return 0;
  if (cursor >= textLength) return textLength;
  let last = 0;
  for (const part of parts) {
    if (part.index === cursor || part.end === cursor) return cursor;
    if (part.end > cursor) return part.index;
    last = part.end;
  }
  return last;
}

function previousBoundary(parts: Part[], cursor: number): number {
  if (cursor <= 0) return 0;
  let last = 0;
  for (const part of parts) {
    if (part.end >= cursor) return part.index;
    last = part.end;
  }
  return last;
}

function nextBoundary(parts: Part[], cursor: number, textLength: number): number {
  if (cursor >= textLength) return textLength;
  for (const part of parts) {
    if (part.index >= cursor || (part.index < cursor && cursor < part.end)) return part.end;
  }
  return textLength;
}

function withCursor(state: EditorState, cursor: number): EditorState {
  return { text: state.text, cursor };
}

function normalizedCursor(text: string, cursor: number, parts = partsOf(text)): number {
  const clamped = clamp(cursor, 0, text.length);
  return floorBoundary(parts, clamped, text.length);
}

function isWhitespace(part: Part): boolean {
  return WHITESPACE.test(part.text);
}

function locateCursorRow(lines: EditorLine[], cursor: number): number {
  for (let row = 0; row < lines.length; row += 1) {
    const line = lines[row];
    if (cursor < line.end) return row;
    if (cursor === line.end) {
      const next = lines[row + 1];
      if (next && next.start === cursor) return row + 1;
      return row;
    }
  }
  return Math.max(0, lines.length - 1);
}

function cursorAtColumn(text: string, line: EditorLine, column: number): number {
  if (column <= 0) return line.start;
  const local = partsOf(text.slice(line.start, line.end));
  let used = 0;
  for (const part of local) {
    const partStart = line.start + part.index;
    const partEnd = line.start + part.end;
    const next = used + part.width;
    if (column < next) {
      const before = column - used;
      const after = next - column;
      return after <= before ? partEnd : partStart;
    }
    used = next;
    if (column === used) return partEnd;
  }
  return line.end;
}

function moveVertical(
  state: EditorState,
  width: number,
  direction: -1 | 1,
  stickyColumn?: number,
): EditorState {
  const view = layout(state.text, width, state.cursor);
  const row = view.cursorRow + direction;
  if (row < 0 || row >= view.lines.length) return state;
  const target = stickyColumn ?? view.cursorCol;
  return withCursor(state, cursorAtColumn(state.text, view.lines[row], target));
}

export function insert(state: EditorState, chunk: string): EditorState {
  if (chunk.length === 0) return state;
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  const text = state.text.slice(0, at) + chunk + state.text.slice(at);
  return { text, cursor: at + chunk.length };
}

export function backspace(state: EditorState): EditorState {
  if (state.text.length === 0 || state.cursor <= 0) return state;
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  if (at <= 0) return withCursor(state, 0);
  const start = previousBoundary(parts, at);
  if (start === at) return state;
  return {
    text: state.text.slice(0, start) + state.text.slice(at),
    cursor: start,
  };
}

export function del(state: EditorState): EditorState {
  if (state.text.length === 0) return state;
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  const end = nextBoundary(parts, at, state.text.length);
  if (end === at) return state;
  return {
    text: state.text.slice(0, at) + state.text.slice(end),
    cursor: at,
  };
}

export function left(state: EditorState): EditorState {
  if (state.cursor <= 0) return withCursor(state, 0);
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  return withCursor(state, previousBoundary(parts, at));
}

export function right(state: EditorState): EditorState {
  if (state.cursor >= state.text.length) return withCursor(state, state.text.length);
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  return withCursor(state, nextBoundary(parts, at, state.text.length));
}

export function home(state: EditorState): EditorState {
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  const previousBreak = state.text.lastIndexOf("\n", Math.max(0, at - 1));
  return withCursor(state, previousBreak === -1 ? 0 : previousBreak + 1);
}

export function end(state: EditorState): EditorState {
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  const nextBreak = state.text.indexOf("\n", at);
  return withCursor(state, nextBreak === -1 ? state.text.length : nextBreak);
}

export function up(state: EditorState, width: number, stickyColumn?: number): EditorState {
  return moveVertical(state, width, -1, stickyColumn);
}

export function down(state: EditorState, width: number, stickyColumn?: number): EditorState {
  return moveVertical(state, width, 1, stickyColumn);
}

export function wordLeft(state: EditorState): EditorState {
  if (state.cursor <= 0) return withCursor(state, 0);
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  let index = parts.length - 1;
  while (index >= 0 && parts[index].end > at) index -= 1;
  while (index >= 0 && isWhitespace(parts[index])) index -= 1;
  while (index >= 0 && !isWhitespace(parts[index])) index -= 1;
  return withCursor(state, index < 0 ? 0 : parts[index + 1].index);
}

export function wordRight(state: EditorState): EditorState {
  if (state.cursor >= state.text.length) return withCursor(state, state.text.length);
  const parts = partsOf(state.text);
  const at = normalizedCursor(state.text, state.cursor, parts);
  let index = 0;
  while (index < parts.length && parts[index].end <= at) index += 1;
  if (index >= parts.length) return withCursor(state, state.text.length);
  if (isWhitespace(parts[index])) {
    while (index < parts.length && isWhitespace(parts[index])) index += 1;
    return withCursor(state, index < parts.length ? parts[index].index : state.text.length);
  }
  while (index < parts.length && !isWhitespace(parts[index])) index += 1;
  while (index < parts.length && isWhitespace(parts[index])) index += 1;
  return withCursor(state, index < parts.length ? parts[index].index : state.text.length);
}

export function layout(text: string, width: number, cursor = text.length): EditorLayout {
  const limit = safeWidth(width);
  const safeCursor = clamp(cursor, 0, text.length);
  const parts = partsOf(text);
  const lines: EditorLine[] = [];
  let start = 0;
  let used = 0;

  for (const part of parts) {
    if (part.text === "\n") {
      lines.push({ start, end: part.index, cells: used });
      start = part.end;
      used = 0;
      continue;
    }
    if (used > 0 && used + part.width > limit) {
      lines.push({ start, end: part.index, cells: used });
      start = part.index;
      used = 0;
    }
    used += part.width;
  }
  lines.push({ start, end: text.length, cells: used });
  // A line that fills the width exactly has no cell left for the caret: with
  // the cursor at its end the caret belongs on a fresh line below, the way
  // every editor shows it — otherwise the terminal wraps the caret cell on
  // its own and the row the layout reports is not the row on screen.
  if (used >= limit && safeCursor === text.length && !text.endsWith("\n")) {
    lines.push({ start: text.length, end: text.length, cells: 0 });
  }

  const cursorRow = locateCursorRow(lines, safeCursor);
  const line = lines[cursorRow] ?? { start: 0, end: 0, cells: 0 };
  const stop = clamp(safeCursor, line.start, line.end);
  return {
    lines,
    cursorRow,
    cursorCol: textWidth(text.slice(line.start, stop)),
  };
}
