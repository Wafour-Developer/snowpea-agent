/**
 * Settings edited on disk (protocol 1.55.0, security review 7).
 *
 * While the daemon runs, an edit of `settings.json` does not change the
 * `security`, `jarvis` or `web.remote` sections: core keeps what it holds and
 * says which sections wait (`settings.changed {waitingKeys}`, the result of
 * `system.reloadSettings`). A local person applies the file or keeps the
 * current settings (`settings.acceptDiskChange`). Everything here is pure; the
 * absence of `waitingKeys` (an older core) means nothing is ever shown.
 */

import { say } from "./errors.js";
import { sanitizeText } from "../util/sanitize.js";

/** The sections core can hold back, in the order they are listed. */
export const WAITING_SECTIONS: readonly string[] = ["security", "jarvis", "web.remote"];

const SECTION_WORDS: Record<string, [string, string]> = {
  security: ["security", "보안"],
  jarvis: ["Jarvis", "Jarvis"],
  "web.remote": ["remote access", "원격 접속"],
};

/** The sections named in a `waitingKeys` field, known ones first in their order; undefined when the field is absent. */
export function normalizeWaiting(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const given = raw.filter((item): item is string => typeof item === "string" && item !== "").map((item) => sanitizeText(item));
  const known = WAITING_SECTIONS.filter((section) => given.includes(section));
  return [...known, ...given.filter((item) => !WAITING_SECTIONS.includes(item))];
}

export const sectionWord = (section: string): string => {
  const known = SECTION_WORDS[section];
  return known ? say(...known) : section;
};

/** The persistent notice: which kinds of setting wait, and the command that deals with them. */
export const DISK_NOTICE_LINE = (): string =>
  say(
    "settings.json was edited outside snowpea: security / Jarvis / remote access settings in it wait for you (/settings disk)",
    "settings.json이 snowpea 밖에서 수정되었습니다. 보안 / Jarvis / 원격 접속 설정은 확인할 때까지 적용되지 않습니다 (/settings disk)",
  );

/** The one row the top banner has: short enough for 60 columns. */
export const DISK_BANNER = (): string => say("! settings.json edited outside snowpea (/settings disk)", "! settings.json이 밖에서 수정됨 (/settings disk)");

/** The same notice on a screen of one section, while that section waits; null otherwise. */
export function sectionNotice(waiting: readonly string[], section: string): string | null {
  if (!waiting.includes(section)) return null;
  return say(
    `settings.json was edited outside snowpea: the ${sectionWord(section)} settings in it wait for you (/settings disk)`,
    `settings.json이 snowpea 밖에서 수정되었습니다. 그 안의 ${sectionWord(section)} 설정은 확인할 때까지 적용되지 않습니다 (/settings disk)`,
  );
}

export const DISK_NOTHING_WAITS = (): string => say("Nothing waits: settings.json and the running settings agree.", "대기 중인 설정이 없습니다. settings.json과 실행 중인 설정이 같습니다.");

export const DISK_REQUIRES_LINE = (): string =>
  say("Settings edited on disk: requires a newer core — this daemon is older. Run /update.", "디스크에서 수정된 설정: 더 새로운 core가 필요합니다. 이 데몬은 오래된 버전입니다. /update를 실행하세요.");

/** The lines of the choice: what waits, and what each key does. */
export function choiceLines(waiting: readonly string[]): string[] {
  return [
    say("settings.json was edited outside snowpea.", "settings.json이 snowpea 밖에서 수정되었습니다."),
    say(`These sections wait for you: ${waiting.map(sectionWord).join(", ")}.`, `확인을 기다리는 부분: ${waiting.map(sectionWord).join(", ")}.`),
    say("They are not applied until you decide. Everything else already took effect.", "결정하기 전에는 적용되지 않습니다. 나머지 설정은 이미 적용되었습니다."),
    say("a applies what the file says · k keeps the current settings (the file is written back)", "a 파일 내용을 적용 · k 현재 설정 유지 (파일을 되돌려 씁니다)"),
  ];
}

/** `settings.acceptDiskChange {accept}` answer in words. */
export function resultLine(result: { applied?: unknown; discarded?: unknown } | null | undefined): string {
  const list = (value: unknown): string[] => (Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").map(sectionWord) : []);
  const applied = list(result?.applied);
  const discarded = list(result?.discarded);
  const parts = [
    ...(applied.length > 0 ? [say(`applied from the file: ${applied.join(", ")}`, `파일에서 적용: ${applied.join(", ")}`)] : []),
    ...(discarded.length > 0 ? [say(`kept the current settings, file written back: ${discarded.join(", ")}`, `현재 설정 유지, 파일을 되돌려 씀: ${discarded.join(", ")}`)] : []),
  ];
  return parts.length > 0 ? parts.join(" · ") : say("Nothing was waiting any more.", "더 이상 대기 중인 설정이 없었습니다.");
}
