/**
 * Full-screen layout arithmetic.
 *
 * The transcript is a flat array of already-wrapped lines; the terminal shows a
 * window onto its tail. `scrollOffset` counts lines *above* the bottom, so 0 is
 * "pinned to the newest line" and the maximum is "pinned to the oldest".
 *
 * Everything here is pure so `test/layout.test.ts` can check the window without
 * a terminal.
 */

/** Rows the header block occupies beside the logo: the banner/rule line. */
export const HEADER_ROWS = 1;
/** Rows the footer occupies when the HUD packs itself into a single row. */
export const STATUS_ROWS = 1;
/**
 * One row of the terminal the frame deliberately leaves empty.
 *
 * Ink repaints with `log-update` only while the frame is shorter than the
 * terminal; a frame as tall as the terminal takes its other branch, which
 * writes `ESC[2J ESC[3J ESC[H` — a full screen clear — before *every* frame,
 * and that clear is what the user sees as a flicker on each keystroke. Giving
 * the last row back costs one line of transcript and removes the clear.
 */
export const RESERVED_FRAME_ROW = 1;

/** Rows the frame may use on a terminal of `terminalRows` rows. */
export function usableRows(terminalRows: number): number {
  return Math.max(1, Math.floor(terminalRows) - RESERVED_FRAME_ROW);
}

/** The transcript never collapses below this, however small the terminal is. */
export const MIN_TRANSCRIPT_ROWS = 1;

export interface Layout {
  headerRows: number;
  transcriptRows: number;
  /** Input block: chat line, slash palette, approval prompt, error line. */
  bottomRows: number;
  statusRows: number;
  columns: number;
  rows: number;
}

/**
 * Split the terminal into header / transcript / input / status.
 *
 * The transcript is the elastic region: it absorbs whatever the fixed blocks
 * leave over, down to `MIN_TRANSCRIPT_ROWS`.
 */
export function computeLayout({
  rows,
  columns,
  bottomRows,
  headerRows = HEADER_ROWS,
  statusRows = STATUS_ROWS,
  minTranscriptRows = MIN_TRANSCRIPT_ROWS,
}: {
  rows: number;
  columns: number;
  bottomRows: number;
  headerRows?: number;
  statusRows?: number;
  minTranscriptRows?: number;
}): Layout {
  const safeRows = Math.max(1, Math.floor(rows));
  const safeColumns = Math.max(1, Math.floor(columns));
  const fixed = headerRows + statusRows + Math.max(0, bottomRows);
  const transcriptRows = Math.max(minTranscriptRows, safeRows - fixed);
  return {
    headerRows,
    transcriptRows,
    bottomRows: Math.max(0, bottomRows),
    statusRows,
    columns: safeColumns,
    rows: safeRows,
  };
}

/** Clamp a scroll offset to the range the transcript actually supports. */
export function clampScroll(offset: number, total: number, height: number): number {
  const max = Math.max(0, total - Math.max(1, height));
  if (!Number.isFinite(offset)) return 0;
  return Math.min(Math.max(0, Math.floor(offset)), max);
}

/** How far PgUp / PgDn / Ctrl+U / Ctrl+D move, keeping one line of overlap. */
export function pageStep(height: number): number {
  return Math.max(1, Math.max(1, Math.floor(height)) - 1);
}

/** Half a page — what Ctrl+U and Ctrl+D move, following the vi convention. */
export function halfPageStep(height: number): number {
  return Math.max(1, Math.floor(Math.max(1, Math.floor(height)) / 2));
}

export interface Viewport<T> {
  /** The lines to draw, oldest first. */
  lines: T[];
  /** Index of the first visible line in the full transcript. */
  start: number;
  /** Index one past the last visible line. */
  end: number;
  /** The offset actually used, after clamping. */
  scrollOffset: number;
  hiddenAbove: number;
  hiddenBelow: number;
  atTop: boolean;
  atBottom: boolean;
}

/**
 * The window of `lines` to draw for a given height and scroll offset.
 *
 * With `scrollOffset` 0 the window ends at the newest line, which is what the
 * app wants while a turn is streaming.
 */
export function sliceViewport<T>(lines: T[], height: number, scrollOffset = 0): Viewport<T> {
  const safeHeight = Math.max(1, Math.floor(height));
  const total = lines.length;
  const offset = clampScroll(scrollOffset, total, safeHeight);
  const end = Math.max(0, total - offset);
  const start = Math.max(0, end - safeHeight);
  return {
    lines: lines.slice(start, end),
    start,
    end,
    scrollOffset: offset,
    hiddenAbove: start,
    hiddenBelow: total - end,
    atTop: start === 0,
    atBottom: offset === 0,
  };
}

/** Rows the slash-command palette occupies, border and hint line included. */
export function paletteRows(commandCount: number, maxRows = 8): number {
  if (commandCount <= 0) return 0;
  return Math.min(commandCount, maxRows) + 1 + 2;
}

/**
 * Rows the interactive approval prompt occupies.
 *
 * `menuRows` is the answer menu: four options, one of which carries a hint
 * line, plus the key legend under them.
 */
export function approvalPromptRows(argCount: number, menuRows = 6): number {
  // border(2) + title + tool line + args + blank + the menu
  return 2 + 1 + 1 + Math.max(0, argCount) + 1 + menuRows;
}

/** Rows the unattended approval backlog occupies. */
export function approvalQueueRows(requestCount: number, focused = false): number {
  if (requestCount <= 0) return 0;
  // border(2) + title + one row per request + (focused ? blank+scope+hint : hint)
  return 2 + 1 + requestCount + (focused ? 3 : 1);
}

/**
 * Rows the bottom block needs, so `computeLayout` can hand the rest to the
 * transcript. Deliberately generous: over-reserving leaves a blank row, while
 * under-reserving would push content past the last row and make Ink scroll.
 */
export function bottomRows({
  paletteCommands = 0,
  approvalArgs = null,
  queueRequests = 0,
  queueFocused = false,
  errorVisible = false,
  workingVisible = false,
  noticeVisible = false,
  queuedRows = 0,
  delegationVisible = false,
}: {
  paletteCommands?: number;
  /** Number of argument lines on the interactive prompt, or null when absent. */
  approvalArgs?: number | null;
  queueRequests?: number;
  queueFocused?: boolean;
  errorVisible?: boolean;
  /** The working indicator, drawn just above the input while a turn runs. */
  workingVisible?: boolean;
  /** A message too long for the status line, drawn above the input. */
  noticeVisible?: boolean;
  /** Rows the queued-prompt list occupies. */
  queuedRows?: number;
  /** The `$agent` chip above the input. */
  delegationVisible?: boolean;
} = {}): number {
  const input =
    approvalArgs === null ? 1 + paletteRows(paletteCommands) : approvalPromptRows(approvalArgs);
  return (
    input +
    approvalQueueRows(queueRequests, queueFocused) +
    (errorVisible ? 1 : 0) +
    (workingVisible ? 1 : 0) +
    (noticeVisible ? 1 : 0) +
    Math.max(0, queuedRows) +
    (delegationVisible ? 1 : 0)
  );
}

/** `▲ 42 more` / `▼ 7 more` — the scroll indicator shown in the header rule. */
export function scrollIndicator(view: Pick<Viewport<unknown>, "hiddenAbove" | "hiddenBelow">): string | null {
  if (view.hiddenAbove === 0 && view.hiddenBelow === 0) return null;
  const parts: string[] = [];
  if (view.hiddenAbove > 0) parts.push(`▲ ${view.hiddenAbove}`);
  if (view.hiddenBelow > 0) parts.push(`▼ ${view.hiddenBelow}`);
  return parts.join(" ");
}
