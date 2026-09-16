/**
 * Mouse input helpers for terminal SGR reports.
 *
 * Ink does not abstract mouse events, so the app parses `useInput` strings
 * directly and maps terminal rows to panel rows with pure helpers.
 */

export interface MouseReport {
  button: number;
  col: number;
  row: number;
  /** True for `M` (press), false for `m` (release). */
  press: boolean;
}

/**
 * Parse one SGR mouse report: `ESC [ < button ; col ; row M|m`.
 *
 * Returns null for anything else, including malformed numbers.
 */
const REPORT = /(?:\u001b)?\[?<(\d+);(\d+);(\d+)([Mm])/g;

/**
 * Every SGR mouse report in one stdin chunk, in order.
 *
 * Ink hands `useInput` the sequence with its ESC stripped (and sometimes
 * without the `[`), and a fast click lands press and release in the same
 * chunk, so `<0;11;22M<0;11;22m` and `[<0;2;30M` are as common as the
 * textbook `ESC [ < 0;11;22 M`. Anything that is not wholly made of reports
 * is not mouse input and yields nothing.
 */
export function mouseReports(input: string): MouseReport[] {
  if (!/^(?:(?:\u001b)?\[?<\d+;\d+;\d+[Mm])+$/.test(input)) return [];
  const reports: MouseReport[] = [];
  for (const match of input.matchAll(REPORT)) {
    const button = Number(match[1]);
    const col = Number(match[2]);
    const row = Number(match[3]);
    if (button < 0 || col < 1 || row < 1) continue;
    reports.push({ button, col, row, press: match[4] === "M" });
  }
  return reports;
}

/** The first report in the chunk, or null when the chunk is not mouse input. */
export function parseMouse(input: string): MouseReport | null {
  return mouseReports(input)[0] ?? null;
}

export interface PanelMouseLayout {
  /** Terminal row count in the coordinate system `row` uses. */
  totalRows: number;
  /** Rows reserved by the bottom/input block. */
  bottomRows: number;
  /** Number of visible rows in the agent panel. */
  panelRows: number;
}

/**
 * Map a terminal row to an index in the panel, or null when outside the panel.
 *
 * The panel is the last block inside status, immediately above the bottom box.
 */
export function panelRowAt(row: number, layout: PanelMouseLayout): number | null {
  const y = Math.floor(row);
  if (!Number.isFinite(y) || y < 1) return null;
  const totalRows = Math.max(1, Math.floor(layout.totalRows));
  const bottomRows = Math.max(0, Math.floor(layout.bottomRows));
  const panelRows = Math.max(0, Math.floor(layout.panelRows));
  if (panelRows < 1) return null;

  const top = totalRows - bottomRows - panelRows + 1;
  const bottom = top + panelRows - 1;
  if (y < Math.max(1, top) || y > bottom) return null;
  const index = y - top;
  return index >= 0 && index < panelRows ? index : null;
}
