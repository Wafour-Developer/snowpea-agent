/**
 * Letter shortcuts under a Korean (두벌식) layout: a lone jamo stands for the
 * QWERTY letter on the same key; anything else is left as it is.
 */

import { describe, expect, it } from "vitest";

import { shortcutKey } from "../src/input/hangulKeys.js";

describe("shortcutKey", () => {
  it("maps every unshifted jamo to its key", () => {
    const jamo = "ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ";
    const keys = "qwertyuiopasdfghjklzxcvbnm";
    expect(Array.from(jamo).map(shortcutKey).join("")).toBe(keys);
  });

  it("maps the shifted jamo to capitals", () => {
    expect(Array.from("ㅃㅉㄸㄲㅆㅒㅖ").map(shortcutKey).join("")).toBe("QWERTOP");
  });

  it("leaves letters, digits, syllables, pastes and control keys alone", () => {
    for (const input of ["u", "U", "1", "?", " ", "", "여", "ㅕㅕ", "한글", "\r", "\u001b", "[A"]) {
      expect(shortcutKey(input)).toBe(input);
    }
  });
});
