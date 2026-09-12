/** Terminal-cell measurement, with grapheme boundaries kept intact when wrapping. */
const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function graphemes(text: string): Array<{ text: string; index: number; width: number }> {
  return Array.from(segmenter.segment(text), ({ segment, index }) => ({
    text: segment,
    index,
    width: graphemeWidth(segment),
  }));
}

function graphemeWidth(text: string): number {
  if (/^[\p{Mark}\p{Control}\p{Format}]+$/u.test(text)) return 0;
  if (/\p{Emoji_Presentation}|\p{Regional_Indicator}|\uFE0F|\u20E3/u.test(text)) return 2;
  const code = text.codePointAt(0) ?? 0;
  // East Asian wide/fullwidth characters; ambiguous characters stay one cell.
  if (
    code >= 0x1100 && (
      code <= 0x115f || code === 0x2329 || code === 0x232a ||
      (code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f) ||
      (code >= 0xac00 && code <= 0xd7a3) ||
      (code >= 0xf900 && code <= 0xfaff) ||
      (code >= 0xfe10 && code <= 0xfe19) ||
      (code >= 0xfe30 && code <= 0xfe6f) ||
      (code >= 0xff01 && code <= 0xff60) ||
      (code >= 0xffe0 && code <= 0xffe6) ||
      (code >= 0x20000 && code <= 0x3fffd)
    )
  ) return 2;
  return 1;
}

export function textWidth(text: string): number {
  return graphemes(text).reduce((sum, part) => sum + part.width, 0);
}
