/**
 * What language the harness's own wording is drawn in.
 *
 * The transcript is mostly the model's words, which already come back in the
 * user's language. The chrome around them does not: `Ran 2 shell commands` is
 * written here, and a Korean session that reads Korean prose interrupted by
 * English verbs reads worse than either language alone.
 *
 * It is a module-level setting rather than a prop because every surface that
 * draws a summary line would otherwise have to thread it through, and the
 * value is the same for the whole process: one daemon, one user, one language.
 * `rpc/client.ts` sets it from `agent.replyLanguage`, and from what the user
 * types when that setting is `auto`.
 *
 * Pure apart from the one variable, so `test/language.test.ts` can check it.
 */

/** Languages the catalogs cover; everything else falls back to `en`. */
export const UI_LANGUAGES = ["en", "ko", "ja", "zh"] as const;

export type UiLanguage = (typeof UI_LANGUAGES)[number];

const DEFAULT: UiLanguage = "en";

let current: UiLanguage = DEFAULT;

/** `"ko-KR"`, `"ko"`, `"Korean"` → `"ko"`; anything unknown → `"en"`. */
export function asUiLanguage(tag: string | null | undefined): UiLanguage {
  const key = String(tag ?? "")
    .trim()
    .toLowerCase()
    .replace("_", "-")
    .split("-")[0];
  return (UI_LANGUAGES as readonly string[]).includes(key) ? (key as UiLanguage) : DEFAULT;
}

/** The language the chrome is currently drawn in. */
export function uiLanguage(): UiLanguage {
  return current;
}

/** Set it; an unknown tag means English rather than an error. */
export function setUiLanguage(tag: string | null | undefined): UiLanguage {
  current = asUiLanguage(tag);
  return current;
}

/** Back to English — tests only, so one case cannot leak into the next. */
export function resetUiLanguage(): void {
  current = DEFAULT;
}

/**
 * Which language a piece of the user's own text is written in.
 *
 * The same script-first rule as `snowpea_core.util.lang`, kept in step with it
 * deliberately: when `replyLanguage` is `auto` the daemon detects the
 * delegation language this way, and the chrome must not disagree with the
 * brief it just sent.
 */
export function detectLanguage(text: string): UiLanguage {
  if (!text) return DEFAULT;
  let kana = 0;
  let han = 0;
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0;
    if (
      (code >= 0xac00 && code <= 0xd7a3) ||
      (code >= 0x1100 && code <= 0x11ff) ||
      (code >= 0x3130 && code <= 0x318f)
    ) {
      return "ko";
    }
    if (
      (code >= 0x3040 && code <= 0x309f) ||
      (code >= 0x30a0 && code <= 0x30ff) ||
      (code >= 0xff66 && code <= 0xff9d)
    ) {
      kana += 1;
    } else if (
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0x3400 && code <= 0x4dbf) ||
      (code >= 0xf900 && code <= 0xfaff)
    ) {
      han += 1;
    }
  }
  if (kana > 0) return "ja";
  if (han > 0) return "zh";
  return DEFAULT;
}
