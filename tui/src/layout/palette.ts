/**
 * The colours the wordmark is painted in, and what to do when the terminal
 * cannot show them.
 *
 * Three stops — lavender, violet, deep violet — interpolated down the rows of
 * the logo. A terminal that speaks 24-bit colour gets the real gradient; one
 * that does not gets three named greens chosen to keep the same light-to-dark
 * direction; `NO_COLOR` or a pipe gets no colour at all, which is what makes the
 * captured output in a log readable.
 *
 * Pure, so `test/palette.test.ts` can check the ramp and the fallbacks.
 */

export type ColorMode = "truecolor" | "basic" | "none";

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

/** Lavender at the top. */
export const LAVENDER: Rgb = { r: 0xdd, g: 0xd6, b: 0xfe };
/** Violet in the middle; the brand colour. */
export const VIOLET: Rgb = { r: 0xa7, g: 0x8b, b: 0xfa };
/** Deep violet at the bottom. */
export const DEEP_VIOLET: Rgb = { r: 0x7c, g: 0x3a, b: 0xed };

/** Legacy names kept for callers that still import them. */
export const MINT = LAVENDER;
export const SNOWPEA = VIOLET;
export const TEAL = DEEP_VIOLET;

/** The named colours that stand in for the ramp on a 256-colour terminal. */
export const BASIC_RAMP: readonly string[] = ["magentaBright", "magenta", "blue"];

/**
 * What this terminal can do.
 *
 * `NO_COLOR` wins over everything, by its own convention. Otherwise 24-bit
 * colour has to be advertised: `COLORTERM` is the only portable signal, and
 * guessing wrong makes a mess of every frame, not just this one.
 */
export function colorMode(env: NodeJS.ProcessEnv, isTTY: boolean): ColorMode {
  if (env.NO_COLOR !== undefined && env.NO_COLOR !== "") return "none";
  if (!isTTY) return "none";
  const colorTerm = (env.COLORTERM ?? "").toLowerCase();
  if (colorTerm.includes("truecolor") || colorTerm.includes("24bit")) return "truecolor";
  if ((env.TERM ?? "").includes("direct")) return "truecolor";
  return "basic";
}

function mix(from: Rgb, to: Rgb, amount: number): Rgb {
  const at = Math.min(1, Math.max(0, amount));
  return {
    r: Math.round(from.r + (to.r - from.r) * at),
    g: Math.round(from.g + (to.g - from.g) * at),
    b: Math.round(from.b + (to.b - from.b) * at),
  };
}

export function toHex({ r, g, b }: Rgb): string {
  return `#${[r, g, b].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

/** The colour at `position` (0 at the top, 1 at the bottom) of the ramp. */
export function gradientAt(position: number): Rgb {
  const at = Math.min(1, Math.max(0, position));
  return at <= 0.5 ? mix(LAVENDER, VIOLET, at * 2) : mix(VIOLET, DEEP_VIOLET, (at - 0.5) * 2);
}

/**
 * One colour per row, top to bottom.
 *
 * `undefined` entries mean "draw it plain", which is what Ink wants for a
 * terminal with no colour at all.
 */
export function gradientColors(rows: number, mode: ColorMode): (string | undefined)[] {
  const count = Math.max(1, Math.floor(rows));
  if (mode === "none") return new Array(count).fill(undefined);
  if (mode === "basic") {
    return Array.from({ length: count }, (_, row) => {
      const index = Math.min(
        BASIC_RAMP.length - 1,
        Math.floor((row / Math.max(1, count - 1)) * BASIC_RAMP.length),
      );
      return BASIC_RAMP[index];
    });
  }
  return Array.from({ length: count }, (_, row) =>
    toHex(gradientAt(count === 1 ? 0 : row / (count - 1))),
  );
}
