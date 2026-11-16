/**
 * Core's refusals and session errors, in the words the person reads (1.54.0).
 *
 * Two kinds reach the TUI. An RPC call can fail (`error.data.code`, with
 * `details` that this module reads defensively: any field may be missing and
 * none is trusted to be a string). A session can also announce a problem as an
 * `error` event (`code`, `message`), which is how `hook_failed` and
 * `context_window_too_small` arrive. Pure, so the tests cover every case.
 */

import { sanitizeText } from "../util/sanitize.js";

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

/**
 * One plain line for a failed call. A code this module does not know keeps the
 * text the TUI always showed (`String(error)`).
 */
export function rpcErrorText(error: unknown): string {
  const code = rpcCode(error);
  const details = detailsOf(error);
  if (code === "jarvis_person_only") {
    if (str(details["reason"]) === "mode_raise") return MODE_RAISE_LINE;
    return "Only a person can do that — Jarvis and scripts cannot.";
  }
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
    default:
      return { kind: "error", text: `${code ?? "error"}: ${message}` };
  }
}
