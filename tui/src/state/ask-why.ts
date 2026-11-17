/**
 * Why an approval is put to the person, said plainly (0.2.90).
 *
 * Core's reason for a line it cannot read ("cannot be checked against your
 * block list", "cannot tell what this code changes") read as if a block list
 * had matched. It is the opposite: snowpea cannot tell what the line touches
 * before it runs. Core may also say which parts (`uncheckedWhy`); that is
 * read defensively, never required, and shown as plain text.
 */

import { sanitizeText } from "../util/sanitize.js";
import { say } from "./errors.js";

const OPAQUE = "cannot be checked against your block list";
const UNKNOWN_CODE = "cannot tell what this code changes";

/** The reason is about a line snowpea cannot read before it runs. */
export function isUncheckable(reason: string | null | undefined): boolean {
  const text = (reason ?? "").toLowerCase();
  return text.includes(OPAQUE) || text.includes(UNKNOWN_CODE);
}

/** Core's reason in words that say what happened; any other reason is shown as core wrote it. */
export function askReasonWords(reason: string | null): string | null {
  if (reason === null) return null;
  if (isUncheckable(reason)) {
    return say(
      "snowpea cannot tell what this will touch before it runs, so it asks you",
      "snowpea가 실행 전에 무엇을 건드릴지 알 수 없어 확인합니다",
    );
  }
  return reason;
}

/** The one quiet line on where the auto-mode setting lives. */
export const askSettingLine = (): string =>
  say("To turn this check off in auto mode: /security", "auto 모드에서 이런 확인을 끄려면: 설정 > 보안 (/security)");

export interface UncheckedItem {
  part: string;
  file: string | null;
  line: number | null;
  reason: string;
  text: string;
}

const text = (value: unknown): string => (typeof value === "string" ? sanitizeText(value).trim() : "");

/** `ApprovalRequest.uncheckedWhy`: anything that is not a list of objects reads as nothing. */
export function readUncheckedWhy(raw: unknown): UncheckedItem[] {
  if (!Array.isArray(raw)) return [];
  const out: UncheckedItem[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as Record<string, unknown>;
    const item: UncheckedItem = {
      part: text(e["part"]),
      file: text(e["file"]) || null,
      line: typeof e["line"] === "number" && Number.isFinite(e["line"]) ? (e["line"] as number) : null,
      reason: text(e["reason"]),
      text: text(e["text"]),
    };
    if (item.part || item.reason || item.text) out.push(item);
  }
  return out;
}

/** One plain line: `bin/x.sh line 15: "$OUT" is written — reason`. */
export function uncheckedLine(item: UncheckedItem): string {
  const where = item.file
    ? item.line !== null
      ? say(`${item.file}, line ${item.line}`, `${item.file} ${item.line}번째 줄`)
      : item.file
    : item.part;
  const body = item.text || (item.file ? item.part : "");
  const head = body ? `${where}: ${body}` : where;
  return item.reason ? `${head} — ${item.reason}` : head;
}
