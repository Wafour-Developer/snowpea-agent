/**
 * Core's refusals and session errors, in the words the person reads (1.54.0).
 *
 * Two kinds reach the TUI. An RPC call can fail (`error.data.code`, with
 * `details` that this module reads defensively: any field may be missing and
 * none is trusted to be a string). A session can also announce a problem as an
 * `error` event (`code`, `message`), which is how `hook_failed` and
 * `context_window_too_small` arrive. Pure, so the tests cover every case.
 */

import { uiLanguage } from "../layout/language.js";
import { sanitizeText } from "../util/sanitize.js";

/** One string in the language the chrome is drawn in (English unless Korean is set). */
export function say(en: string, ko: string): string {
  return uiLanguage() === "ko" ? ko : en;
}

interface RpcErrorShape {
  message?: unknown;
  code?: unknown;
  data?: { code?: unknown; details?: unknown } | null;
}

function detailsOf(error: unknown): Record<string, unknown> {
  const details = (error as RpcErrorShape | null)?.data?.details;
  return details && typeof details === "object" && !Array.isArray(details) ? (details as Record<string, unknown>) : {};
}

const str = (value: unknown): string | undefined => (typeof value === "string" && value !== "" ? value : undefined);

/** The protocol error code of a failed call, when it has one. */
export function rpcCode(error: unknown): string | undefined {
  const raw = error as RpcErrorShape | null;
  return str(raw?.data?.code) ?? str(raw?.code);
}

function messageOf(error: unknown): string {
  const raw = (error as RpcErrorShape | null)?.message;
  return sanitizeText(typeof raw === "string" ? raw : String(error));
}

/** The reason core gives for refusing to raise a mode (`jarvis_person_only`, `details.reason: "mode_raise"`). */
export const MODE_RAISE_LINE =
  "The mode was not raised: raising the permission mode needs a person, and core refused this request (jarvis_person_only, mode_raise).";

/** A refusal core attached to an open approval or question (1.55.0 `refused`), or null when there is none. */
export function refusalOf(raw: unknown): { code: string; message: string; reason: string } | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const message = typeof value["message"] === "string" ? sanitizeText(value["message"]) : "";
  const code = typeof value["code"] === "string" ? sanitizeText(value["code"]) : "";
  const reason = typeof value["reason"] === "string" ? sanitizeText(value["reason"]) : "";
  return code === "" && message === "" && reason === "" ? null : { code, message, reason };
}

/** What a refused reply says on the still-open prompt: the reason in words (from `reason`), then core's own message. */
export function refusalWords(refused: { code: string; message: string; reason: string }): string {
  const lead =
    refused.reason === "person_only"
      ? say("Your answer was not accepted: only a person on this machine can decide this.", "답변이 받아들여지지 않았습니다. 이 결정은 이 컴퓨터의 사람만 할 수 있습니다.")
      : refused.reason === "mode_raise"
        ? say("Your answer was not accepted: raising the mode needs a person on this machine.", "답변이 받아들여지지 않았습니다. 모드를 올리려면 이 컴퓨터의 사람이 필요합니다.")
        : say("Your answer was not accepted.", "답변이 받아들여지지 않았습니다.");
  return refused.message !== "" ? `${lead} ${refused.message}` : lead;
}

/** What a `local_only` refusal names in `details.what` (1.55.0, security review 7), in words. */
const LOCAL_ONLY_WHAT: Record<string, [string, string]> = {
  target: ["changing where a saved server connects", "저장된 서버의 접속 대상 변경"],
  variable: ["a ${VAR} in a server entry", "서버 항목의 ${VAR} 사용"],
  agent: ["a global agent", "전역 에이전트"],
  command: ["a global command", "전역 명령"],
  builtin_name: ["a name a built-in already uses", "기본 제공 기능이 쓰는 이름"],
  front_matter: ["front matter that widens what it may do", "권한을 넓히는 front matter"],
  // 1.55.0, security review 8
  credential: ["saving a key or token", "키나 토큰 저장"],
  briefing: ["changing the briefing", "브리핑 변경"],
  settings: ["a setting that sets an address, a program or a key", "주소, 프로그램, 키를 정하는 설정"],
  "provider.loginWeb": ["signing in to a provider", "공급자 로그인"],
  "mcp.authorize.complete": ["finishing an MCP sign-in", "MCP 로그인 완료"],
  "jarvis.delegation.set": ["granting Jarvis rights", "Jarvis에 권한 부여"],
};

/** `jarvis_person_only` / `local_only`: a remote login (or another non-local kind) may not do this. */
export function localOnlyLine(details: Record<string, unknown>): string {
  const rawWhat = str(details["what"]);
  const known = rawWhat ? LOCAL_ONLY_WHAT[rawWhat] : undefined;
  const what = known ? say(...known) : rawWhat ? sanitizeText(rawWhat) : undefined;
  const kind = str(details["kind"]);
  const who =
    kind === "remote_login" ? "a remote login" : kind === "browser" ? "a browser host" : kind === "connector" ? "a connector" : "this connection";
  const whoKo =
    kind === "remote_login" ? "원격 로그인" : kind === "browser" ? "브라우저 호스트" : kind === "connector" ? "커넥터" : "이 연결";
  return say(
    `Only snowpea on this machine can do that${what ? ` (${what})` : ""} — not ${who}.`,
    `이 컴퓨터의 snowpea만 할 수 있습니다${what ? ` (${what})` : ""} — ${whoKo}에서는 안 됩니다.`,
  );
}

/** `jarvis_person_only` / `mode_raise` with `details.max` for a remote sign-in (`web.remote.maxMode`). */
export function modeLimitLine(max: string): string {
  const mode = sanitizeText(max);
  return say(
    `The mode was not raised: remote sign-ins are limited to ${mode} on this computer (/web). Lower the mode, or raise the limit there.`,
    `모드를 올리지 않았습니다. 이 컴퓨터에서 원격 로그인은 ${mode}까지로 제한되어 있습니다 (/web). 모드를 낮추거나 그곳에서 제한을 올리세요.`,
  );
}

/** `invalid_params` / `snowpea_home`: the folder is snowpea's own home, or holds it; it is no project. */
export function snowpeaHomeLine(details: Record<string, unknown>): string {
  const what = str(details["what"]);
  const inside =
    what === "parent"
      ? say("It is the folder that holds snowpea's home (usually your home directory), so it is not a project.", "snowpea 홈을 담고 있는 폴더(보통 홈 디렉터리)라서 프로젝트가 아닙니다.")
      : say("It is snowpea's own home, or inside it, so it is not a project.", "snowpea 자신의 홈(또는 그 안)이라서 프로젝트가 아닙니다.");
  return say(`Nothing was written: ${inside} Open snowpea in a project folder.`, `아무것도 쓰지 않았습니다. ${inside} 프로젝트 폴더에서 snowpea를 여세요.`);
}

/** `jarvis_person_only` with `details.kind: "closed"`: the connection that asked is gone. */
export const CLOSED_LINE = (): string =>
  say("The connection that asked has closed; nothing was done.", "요청한 연결이 닫혔습니다. 아무것도 실행되지 않았습니다.");

/** `invalid_params` / `global_only`: a project scope tried to set a key only the global settings may hold. */
export function globalOnlyLine(details: Record<string, unknown>): string {
  const key = str(details["key"]);
  return say(
    `${key ? `${sanitizeText(key)} ` : "That setting "}can only be set for all projects (global), not for one project.`,
    `${key ? `${sanitizeText(key)} 설정은 ` : "이 설정은 "}프로젝트별이 아니라 전체(전역)로만 바꿀 수 있습니다.`,
  );
}

/** An internal error: core keeps the cause in its log under this id. */
export function internalErrorLine(errorId: string): string {
  return say(
    `snowpea hit an internal error (id ${sanitizeText(errorId)}). The details are in the daemon log under that id.`,
    `snowpea 내부 오류가 났습니다 (id ${sanitizeText(errorId)}). 자세한 내용은 데몬 로그에서 이 id로 찾을 수 있습니다.`,
  );
}

export const TRUST_CHANGED_LINE = "The project's files changed after you were shown them. Nothing was trusted. Review them again.";
export const TRUST_CHANGED_LINE_KO = "보여 드린 뒤 프로젝트 파일이 바뀌었습니다. 아무것도 신뢰하지 않았습니다. 다시 확인해 주세요.";

/**
 * One plain line for a failed call. A code this module does not know keeps the
 * text the TUI always showed (`String(error)`).
 */
export function rpcErrorText(error: unknown): string {
  const code = rpcCode(error);
  const details = detailsOf(error);
  if (code === "jarvis_person_only") {
    const reason = str(details["reason"]);
    if (str(details["kind"]) === "closed") return CLOSED_LINE();
    if (reason === "mode_raise") {
      const max = str(details["max"]);
      return max !== undefined ? modeLimitLine(max) : MODE_RAISE_LINE;
    }
    if (reason === "local_only") return localOnlyLine(details);
    return "Only a person can do that — Jarvis and scripts cannot.";
  }
  if (code === "invalid_params" && str(details["reason"]) === "snowpea_home") return snowpeaHomeLine(details);
  if (code === "invalid_params" && str(details["reason"]) === "global_only") return globalOnlyLine(details);
  if (code === "trust_changed") return say(TRUST_CHANGED_LINE, TRUST_CHANGED_LINE_KO);
  const errorId = str(details["errorId"]);
  if (errorId !== undefined) return internalErrorLine(errorId);
  if (code === "steer_slash_command") {
    return "A slash command cannot be sent as a steer — send it on its own as a prompt.";
  }
  if (code === "invalid_params" && str(details["reason"]) === "catch_all") {
    const why = messageOf(error).replace(/^bad allowlist pattern:\s*/i, "");
    return `A rule must name a program — ${why}`;
  }
  if (code === "security_blocked") {
    return `Blocked by your security list: ${messageOf(error)}`;
  }
  if (code === "protected") {
    return str(details["reason"]) === "local_only" ? localOnlyLine(details) : `That path is protected: ${messageOf(error)}`;
  }
  return sanitizeText(String(error));
}

// --- session `error` events -------------------------------------------------------

const number = (value: unknown): number | undefined => (typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined);

const group = (value: number): string => value.toLocaleString("en-US");

/** The window and the need, from `details` (`windowTokens`, `neededTokens`) or the figures in core's message. */
function contextNumbers(message: string, details?: unknown): { window?: number; needed?: number } {
  const given = details && typeof details === "object" ? (details as Record<string, unknown>) : {};
  let window = number(given["windowTokens"]);
  let needed = number(given["neededTokens"]);
  if (window === undefined) {
    const found = /window is (\d[\d,]*) tokens/i.exec(message);
    if (found) window = number(Number(found[1].replace(/,/g, "")));
  }
  if (needed === undefined) {
    const found = /need(?:s)? about (\d[\d,]*)/i.exec(message);
    if (found) needed = number(Number(found[1].replace(/,/g, "")));
  }
  return { window, needed };
}

/** The short form for the status line and the stop reason: both numbers when known. */
export function contextWindowShort(message: string, details?: unknown): string {
  const { window, needed } = contextNumbers(message, details);
  const figures = window !== undefined && needed !== undefined ? `${group(window)} tokens, needs about ${group(needed)}` : window !== undefined ? `${group(window)} tokens` : "";
  return `context_window_too_small${figures ? `: window ${figures}` : ""}`;
}

/**
 * The line for `context_window_too_small`: both numbers when core sent them
 * (`details.windowTokens` / `neededTokens`, else the figures in its message),
 * and no hint that trying again would help — it cannot.
 */
export function contextWindowLine(message: string, details?: unknown): string {
  const { window, needed } = contextNumbers(message, details);
  const numbers =
    window !== undefined && needed !== undefined
      ? `The model's context window is ${group(window)} tokens, but snowpea's system prompt and tool definitions alone need about ${group(needed)}.`
      : window !== undefined
        ? `The model's context window is only ${group(window)} tokens — too small for snowpea's system prompt and tool definitions.`
        : "The model's context window is too small for snowpea's system prompt and tool definitions.";
  return `${numbers} Use a model or server setting with a larger context window; asking again will not help.`;
}

/** The notice for `hook_failed`: which plugin's hook is not running and why, with where to look. */
export function hookFailedLine(message: string): string {
  const flat = sanitizeText(message).replace(/\s+/g, " ").trim();
  const text = flat === "" ? "a plugin guard hook is not running" : flat;
  return `${text}  (/skill hooks)`;
}

/**
 * The notice for `project_untrusted` (sent once per session): core's own line,
 * calm, ending "(/trust)". It is not a failed turn.
 */
export function projectUntrustedLine(message: string): string {
  const flat = sanitizeText(message).replace(/\s+/g, " ").trim();
  const text =
    flat === ""
      ? say(
          "This project's own hooks, MCP servers and commands are switched off until you trust it.",
          "이 프로젝트의 훅, MCP 서버, 명령은 신뢰하기 전까지 꺼져 있습니다.",
        )
      : flat;
  return /\(\/trust\)\s*$/.test(text) ? text : `${text}  (/trust)`;
}

/** How the reducer treats a session `error` event. */
export type SessionErrorView =
  /** A one-off notice in the transcript; the turn is not marked failed. */
  | { kind: "notice"; text: string }
  /** A refusal by the security lists: a red line. */
  | { kind: "blocked"; text: string }
  /** The red status line and the turn's stop reason; `detail` is also written in full, wrapped, in the transcript. */
  | { kind: "error"; text: string; detail?: string };

export function sessionErrorView(code: string | undefined, message: string, details?: unknown): SessionErrorView {
  switch (code) {
    case "hook_failed":
      return { kind: "notice", text: hookFailedLine(message) };
    case "context_window_too_small":
      return { kind: "error", text: contextWindowShort(message, details), detail: contextWindowLine(message, details) };
    case "security_blocked":
      return { kind: "blocked", text: `Blocked by your security list: ${message}`.trim() };
    case "project_untrusted":
      return { kind: "notice", text: projectUntrustedLine(message) };
    default: {
      // An internal failure carries an id core keeps in its log (1.55.0); show it.
      const errorId = details && typeof details === "object" ? str((details as Record<string, unknown>)["errorId"]) : undefined;
      if (errorId !== undefined) return { kind: "error", text: `${code ?? "error"}: internal error (id ${sanitizeText(errorId)})`, detail: internalErrorLine(errorId) };
      return { kind: "error", text: `${code ?? "error"}: ${message}` };
    }
  }
}
