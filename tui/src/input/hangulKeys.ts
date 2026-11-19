/**
 * Letter shortcuts under a Korean (두벌식) keyboard layout.
 *
 * With the Korean layout on, the key labelled `u` types the jamo `ㅕ`, so a
 * shortcut handler that compares against "u" never fires. `shortcutKey` turns
 * a lone compatibility jamo back into the QWERTY letter on the same physical
 * key. It is for shortcut handlers only: text fields keep the Hangul a person
 * typed.
 *
 * Anything else passes through unchanged, including a composed syllable (an
 * IME that commits `여` is typing text, not pressing one key) and a paste.
 */

const DUBEOLSIK: Record<string, string> = {
  ㅂ: "q", ㅈ: "w", ㄷ: "e", ㄱ: "r", ㅅ: "t", ㅛ: "y", ㅕ: "u", ㅑ: "i", ㅐ: "o", ㅔ: "p",
  ㅁ: "a", ㄴ: "s", ㅇ: "d", ㄹ: "f", ㅎ: "g", ㅗ: "h", ㅓ: "j", ㅏ: "k", ㅣ: "l",
  ㅋ: "z", ㅌ: "x", ㅊ: "c", ㅍ: "v", ㅠ: "b", ㅜ: "n", ㅡ: "m",
  // Shift gives a different jamo only on these keys.
  ㅃ: "Q", ㅉ: "W", ㄸ: "E", ㄲ: "R", ㅆ: "T", ㅒ: "O", ㅖ: "P",
};

/** The QWERTY letter for a single 두벌식 jamo keypress; `input` itself otherwise. */
export function shortcutKey(input: string): string {
  return DUBEOLSIK[input] ?? input;
}
