/**
 * A settings.json core could not read (protocol 1.55.0, security review 8).
 *
 * `settingsDegraded` on `system.hello`, `system.info` and `web.remote.status`
 * names the sections that did not validate (`"*"` is the whole file). Core then
 * runs those with safe defaults; the person is told in one line. Pure.
 */

import { say } from "./errors.js";
import { sanitizeText } from "../util/sanitize.js";

/** The sections named in a `settingsDegraded` field; empty when the field is absent or not a list. */
export function normalizeDegraded(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is string => typeof item === "string" && item !== "").map((item) => sanitizeText(item));
}

const SHOWN = 3;

/** "security, web and 2 more"; the whole file for "*". */
function sectionsWords(sections: readonly string[]): string {
  if (sections.includes("*")) return say("the whole file", "파일 전체");
  const first = sections.slice(0, SHOWN).join(", ");
  const more = sections.length - SHOWN;
  return more > 0 ? say(`${first} and ${more} more`, `${first} 외 ${more}개`) : first;
}

/** The one-line warning; null when nothing is degraded. */
export function degradedLine(sections: readonly string[]): string | null {
  if (sections.length === 0) return null;
  const what = sectionsWords(sections);
  return say(
    `settings.json: snowpea could not read ${what}; safe defaults are in use`,
    `settings.json: snowpea가 ${what}을(를) 읽지 못했습니다. 안전한 기본값을 사용 중입니다`,
  );
}

/** The one banner row: short enough for 60 columns. */
export const DEGRADED_BANNER = (): string =>
  say("! settings.json unreadable: safe defaults in use", "! settings.json을 읽지 못함: 안전한 기본값 사용 중");
