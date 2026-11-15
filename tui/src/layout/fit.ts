/**
 * Cell-accurate truncation and padding for the Jarvis and external screens.
 *
 * Titles and project names are whatever the person wrote, so they can hold wide
 * (CJK) characters that take two cells; `String.length` would let a row run
 * past the terminal's edge. Everything here counts cells.
 */

import { graphemes, textWidth } from "./text-width.js";

/** `text` cut to at most `width` cells, with a trailing `…` when it was cut. */
export function fit(text: string, width: number): string {
  if (width <= 0) return "";
  const flat = text.replace(/[\r\n\t]+/g, " ");
  if (textWidth(flat) <= width) return flat;
  if (width === 1) return "…";
  let used = 0;
  let out = "";
  for (const part of graphemes(flat)) {
    if (used + part.width > width - 1) break;
    out += part.text;
    used += part.width;
  }
  return `${out}…`;
}

/** `text` followed by spaces up to `width` cells (never cut). */
export function padEnd(text: string, width: number): string {
  const gap = width - textWidth(text);
  return gap > 0 ? text + " ".repeat(gap) : text;
}

/**
 * A row with `left` flush to the left edge and `right` flush to the right,
 * `left` giving way first when the two do not fit.
 */
export function spread(left: string, right: string, width: number): string {
  const rightWidth = textWidth(right);
  if (rightWidth === 0) return fit(left, width);
  if (rightWidth >= width - 4) return fit(right, width);
  const room = width - rightWidth - 1;
  return `${padEnd(fit(left, room), room)} ${right}`;
}
