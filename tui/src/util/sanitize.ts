/**
 * Terminal-safe text for everything the daemon sends (security review M7).
 *
 * External CLI transcripts, titles, previews, errors and approval arguments
 * are written by other programs (and whatever they read). Printed raw, an
 * escape sequence in them would drive the person's terminal: OSC 52 writes
 * the clipboard, OSC 8 hides a link behind text, CSI moves the cursor or
 * rewrites what is on screen. The daemon already strips them; this is the
 * same rule at the TUI's one input boundary (`TuiClient`), so a daemon that
 * does not (an older one, a bug) still cannot reach the terminal.
 *
 * Removed: ESC-introduced sequences (OSC/DCS/SOS/PM/APC strings, CSI, and
 * two-character escapes), their 8-bit C1 forms, and every other C0/C1 control
 * character except newline and tab. Carriage returns go too (they overwrite
 * a line). Nothing else is changed.
 */

// OSC / DCS / SOS / PM / APC strings (terminated by BEL or ST, or unterminated),
// CSI sequences, then any other two-character escape.
const ESCAPES =
  /(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)?|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?|\x1b[ -/]*[0-~]?/g;
// C0 except \t (0x09) and \n (0x0a), DEL and C1.
const CONTROLS = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/g;
// Fast path: nothing to strip.
const ANY = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/;

/** `text` with escape sequences and control characters (but \n and \t) removed. */
export function sanitizeText(text: string): string {
  if (!ANY.test(text)) return text;
  return text.replace(/\r\n/g, "\n").replace(ESCAPES, "").replace(CONTROLS, "");
}

/** {@link sanitizeText} applied to every string (keys too) of a JSON value. */
export function sanitizeDeep<T>(value: T, depth = 0): T {
  if (typeof value === "string") return sanitizeText(value) as unknown as T;
  if (value === null || typeof value !== "object" || depth > 64) return value;
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeDeep(item, depth + 1)) as unknown as T;
  }
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    out[sanitizeText(key)] = sanitizeDeep(item, depth + 1);
  }
  return out as T;
}
