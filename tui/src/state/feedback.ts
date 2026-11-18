/**
 * 개선하기 — `/feedback` (alias `/개선하기`): send this conversation to the
 * Snowpea team (capability `improvementFeedback`, protocol 1.59.0).
 *
 * The daemon builds the report (`feedback.preview` shows it, masked and fitted,
 * with the endpoint) and uploads it (`feedback.send`) only after the person
 * confirms. Everything here is pure: the command parser, the preview lines, the
 * consent line and the error lines, so the tests read the same functions the
 * screen draws.
 */

import type { CommandInfo } from "../rpc/sdk.js";
import { say } from "./errors.js";
import { sanitizeText } from "../util/sanitize.js";

export function feedbackSupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("improvementFeedback");
}

/** The one line a daemon without the capability gets when the command is typed anyway. */
export const FEEDBACK_REQUIRES_LINE = (): string =>
  say(
    "Feedback (/feedback): requires a newer core — this daemon is older. Run /update.",
    "개선하기(/feedback): 더 새로운 core가 필요해요 — 이 데몬은 이전 버전이에요. /update 를 실행하세요.",
  );

/** Shown above the send button, word for word. */
export const FEEDBACK_CONSENT_KO = "보내기를 누르면 이 대화가 Snowpea 팀에 전달되고, 에이전트를 더 낫게 만드는 데 쓰여요.";
export const FEEDBACK_CONSENT = (): string =>
  say(
    "Pressing Send passes this conversation to the Snowpea team, who use it to make the agent better.",
    FEEDBACK_CONSENT_KO,
  );

export const FEEDBACK_COMMAND: CommandInfo = {
  name: "feedback",
  summary: "Send this conversation to the Snowpea team to improve the agent (/개선하기); you see it first.",
  source: "tui",
};

export type FeedbackCommand = { kind: "enter"; comment: string };

/** `/feedback [comment]` or `/개선하기 [comment]`; `null` when the line is neither. */
export function parseFeedbackCommand(line: string): FeedbackCommand | null {
  const match = /^\/(?:feedback|개선하기)(?:\s+([\s\S]*))?$/u.exec(line.trim());
  if (!match) return null;
  return { kind: "enter", comment: (match[1] ?? "").trim() };
}

// --- the preview -------------------------------------------------------------------

export interface FeedbackMessageView {
  id: string;
  role: string;
  text: string;
  tool?: string;
}

export interface FeedbackPreviewView {
  messages: FeedbackMessageView[];
  stats: { messages: number; toolMessages: number; rawBytes: number; redactions: number; droppedResults: number };
  endpoint: string;
}

const num = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);
const text = (value: unknown): string => (typeof value === "string" ? value : "");

/** The daemon's `feedback.preview` result, read defensively. */
export function normalizePreview(raw: unknown): FeedbackPreviewView {
  const value = (raw && typeof raw === "object" ? raw : {}) as Record<string, any>;
  const report = (value["report"] && typeof value["report"] === "object" ? value["report"] : {}) as Record<string, any>;
  const list: unknown[] = Array.isArray(report["session"]?.["messages"]) ? report["session"]["messages"] : [];
  const stats = (value["stats"] && typeof value["stats"] === "object" ? value["stats"] : {}) as Record<string, unknown>;
  return {
    messages: list
      .filter((m): m is Record<string, any> => !!m && typeof m === "object")
      .map((m) => ({
        id: text(m["id"]),
        role: text(m["role"]) || "event",
        text: text(m["text"]) || text(m["reasoning"]),
        ...(m["tool"] && typeof m["tool"] === "object" ? { tool: text(m["tool"]["name"]) || "tool" } : {}),
      })),
    stats: {
      messages: num(stats["messages"]),
      toolMessages: num(stats["toolMessages"]),
      rawBytes: num(stats["rawBytes"]),
      redactions: num(stats["redactions"]),
      droppedResults: num(stats["droppedResults"]),
    },
    endpoint: sanitizeText(text(value["endpoint"])),
  };
}

const ROLE: Record<string, [string, string]> = {
  user: ["you", "나"],
  assistant: ["agent", "에이전트"],
  system: ["system", "시스템"],
  event: ["event", "이벤트"],
};

function fit(line: string, width: number): string {
  const clean = sanitizeText(line).replace(/\s+/g, " ").trim();
  if (width <= 1) return clean.slice(0, Math.max(0, width));
  return clean.length > width ? `${clean.slice(0, width - 1)}…` : clean;
}

function kb(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** The counts, what was masked and where it goes: the head of the screen. */
export function previewSummary(preview: FeedbackPreviewView): string[] {
  const { stats } = preview;
  const lines = [
    say(
      `${stats.messages} messages (${stats.toolMessages} tool calls, not listed below) · ${kb(stats.rawBytes)}`,
      `메시지 ${stats.messages}개 (도구 호출 ${stats.toolMessages}개는 아래 목록에서 숨김) · ${kb(stats.rawBytes)}`,
    ),
    say(
      `Masked before it leaves: keys, tokens, passwords, cookies, card numbers (${stats.redactions} places).`,
      `보내기 전에 가려요: 키, 토큰, 비밀번호, 쿠키, 카드 번호 (${stats.redactions}곳).`,
    ),
  ];
  if (stats.droppedResults > 0) {
    lines.push(
      say(
        `${stats.droppedResults} large tool results or texts are cut to fit the size limit.`,
        `크기 제한 때문에 큰 도구 결과·텍스트 ${stats.droppedResults}개를 줄였어요.`,
      ),
    );
  }
  lines.push(`${say("Goes to", "보낼 곳")}: ${preview.endpoint}`);
  return lines;
}

/** The first line of each message (tool calls hidden), newest last, at most `max`. */
export function previewMessageLines(preview: FeedbackPreviewView, width: number, max = 8): string[] {
  const shown = preview.messages.filter((m) => m.role !== "tool" && m.text.trim() !== "");
  const head = shown.slice(0, max);
  const lines = head.map((m) => {
    const role = ROLE[m.role] ? say(...ROLE[m.role]) : m.role;
    const first = m.text.split(/\r?\n/).find((part) => part.trim() !== "") ?? "";
    return fit(`${role}: ${first}`, width);
  });
  if (shown.length > head.length) {
    lines.push(say(`… and ${shown.length - head.length} more`, `… 그리고 ${shown.length - head.length}개 더`));
  }
  return lines;
}

// --- the answer --------------------------------------------------------------------

export type FeedbackSendOutcome = { ok: true; id: string } | { ok: false; message: string };

export function sentLine(id: string): string {
  return say(`Sent to the Snowpea team. Thank you! Report id: ${sanitizeText(id)}`, `Snowpea 팀에 보냈어요. 고마워요! 보고서 id: ${sanitizeText(id)}`);
}

interface ErrorShape {
  message?: unknown;
  data?: { code?: unknown; details?: unknown } | null;
}

/** One line for a failed preview or send, by core's error code. */
export function feedbackErrorLine(error: unknown): string {
  const raw = (error ?? {}) as ErrorShape;
  const code = typeof raw.data?.code === "string" ? raw.data.code : "";
  const details = (raw.data?.details && typeof raw.data.details === "object" ? raw.data.details : {}) as Record<string, unknown>;
  const message = sanitizeText(typeof raw.message === "string" ? raw.message : String(error));
  const server = typeof details["serverMessage"] === "string" ? sanitizeText(details["serverMessage"]) : "";
  const status = typeof details["status"] === "number" ? details["status"] : 0;
  switch (code) {
    case "feedback_busy": {
      const wait = typeof details["retryAfterSec"] === "number" ? details["retryAfterSec"] : null;
      return wait
        ? say(`The feedback server is busy (HTTP ${status}). Try again in ${wait} s.`, `피드백 서버가 바빠요 (HTTP ${status}). ${wait}초 뒤에 다시 보내 주세요.`)
        : say(`The feedback server is busy (HTTP ${status}). Try again later.`, `피드백 서버가 바빠요 (HTTP ${status}). 잠시 뒤에 다시 보내 주세요.`);
    }
    case "feedback_rejected":
      return say(
        `The feedback server refused the report (HTTP ${status})${server ? `: ${server}` : ""}.`,
        `피드백 서버가 보고서를 받지 않았어요 (HTTP ${status})${server ? `: ${server}` : ""}.`,
      );
    case "feedback_unreachable":
      return say(
        `Could not reach the feedback server${status ? ` (HTTP ${status})` : ""}. Nothing was sent.`,
        `피드백 서버에 연결하지 못했어요${status ? ` (HTTP ${status})` : ""}. 아무것도 보내지 않았어요.`,
      );
    case "jarvis_person_only":
      return say("Only a person's own snowpea can send feedback.", "피드백은 사람이 직접 쓰는 snowpea에서만 보낼 수 있어요.");
    case "too_large":
      return say("This conversation is too large to send.", "이 대화는 너무 커서 보낼 수 없어요.");
    case "not_found":
      return say("This conversation is not saved yet; nothing to send.", "이 대화는 아직 저장되지 않아 보낼 내용이 없어요.");
    default:
      return say(`Feedback: ${message}`, `개선하기: ${message}`);
  }
}
