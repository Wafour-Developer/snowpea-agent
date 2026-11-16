/**
 * A slash command that ran beside a running turn (capability
 * `commandsBesideTurn`, protocol 1.55.0).
 *
 * `command.run` / `session.prompt` answer such an instant command with
 * `besideTurn: true` and a command id (`c-…`): no turn starts, the running
 * turn keeps its spinner, its interrupt and its stream. The command's answer is
 * one `command.result` session event, drawn as a compact aside. Pure.
 */

import { say } from "./errors.js";
import { sanitizeText } from "../util/sanitize.js";

export function commandsBesideTurnSupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("commandsBesideTurn");
}

/** True when the answer to `command.run` / `session.prompt` says the command ran beside the turn. */
export function ranBesideTurn(result: unknown): boolean {
  return (result as { besideTurn?: unknown } | null)?.besideTurn === true;
}

/** When the running turn sees the change a command made, in words. */
export function appliesFromWords(appliesFrom: unknown): string | null {
  switch (appliesFrom) {
    case "next_tool_call":
      return say("applies from the next tool call", "다음 도구 호출부터 적용");
    case "next_request":
      return say("applies from the next request", "다음 요청부터 적용");
    case "next_turn":
      return say("applies from the next turn", "다음 턴부터 적용");
    case "now":
      return say("applies now", "바로 적용");
    default:
      return null;
  }
}

/** Longest reply an aside shows before it folds. */
export const ASIDE_LINES = 6;

/** The aside for one `command.result` payload: the command, its answer, and when it applies. */
export function commandResultText(payload: Record<string, unknown>): string {
  const command = sanitizeText(typeof payload["command"] === "string" ? payload["command"] : "").trim();
  const ok = payload["ok"] !== false;
  const body = sanitizeText(typeof payload["text"] === "string" ? payload["text"] : "").replace(/\s+$/, "");
  const lines = body === "" ? [] : body.split("\n");
  const shown = lines.slice(0, ASIDE_LINES);
  const hidden = lines.length - shown.length;
  const applies = ok ? appliesFromWords(payload["appliesFrom"]) : null;
  const head = `${ok ? "" : "✗ "}${command || say("command", "명령")}`;
  const first = shown[0] ?? "";
  const out = [`${head}${first ? ` — ${first}` : ""}${applies ? ` (${applies})` : ""}`, ...shown.slice(1).map((line) => `  ${line}`)];
  if (hidden > 0) out.push(say(`  … ${hidden} more line${hidden === 1 ? "" : "s"}`, `  … ${hidden}줄 더`));
  return out.join("\n");
}
