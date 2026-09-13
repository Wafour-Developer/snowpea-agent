/**
 * What the working indicator says while a turn is running.
 *
 * The phase is derived from the same state the transcript is drawn from, so
 * there is no second source of truth about what the agent is doing: a running
 * tool call, a pending approval and a live subagent are all already in `State`.
 *
 * Pure, so `test/working.test.ts` can check every phase and every string
 * without a terminal or a clock.
 */

import { formatTokens } from "../layout/hud.js";
import type { State, ToolCallEntry } from "./store.js";
import { verbAt } from "./verbs.js";

/** The glyphs the spinner cycles through. */
export const SPINNER_FRAMES: readonly string[] = ["✢", "✳", "✶", "✻", "✽"];

/** One spinner step. Five frames a second is smooth and cheap. */
export const SPINNER_INTERVAL_MS = 200;

/** Shown instead of a spinner while the turn is blocked on a person. */
export const PAUSED_GLYPH = "⏸";

export type WorkingPhase =
  /** Nothing is running. */
  | { kind: "idle" }
  /** Blocked on a human decision. */
  | { kind: "approval" }
  /** One tool call is in flight. */
  | { kind: "tool"; label: string }
  /** Delegates are doing the work. */
  | { kind: "subagents"; running: number }
  /** A slash command owns the turn. */
  | { kind: "command"; name: string }
  /** The model is thinking. */
  | { kind: "thinking" };

/** First argument that looks like what the tool is working on. */
function firstArg(args: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

/** `/long/path/file.py` → `file.py`; anything else is left alone. */
export function basename(path: string): string {
  const cut = path.replace(/[/\\]+$/, "");
  const at = Math.max(cut.lastIndexOf("/"), cut.lastIndexOf("\\"));
  return at === -1 ? cut : cut.slice(at + 1);
}

/** Cut to `max`, marking the cut. */
export function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, Math.max(1, max - 1))}…`;
}

/** What a running tool call is doing, in words. */
export function toolLabel(call: ToolCallEntry): string {
  const name = call.name.toLowerCase();
  const args = call.args ?? {};
  if (/(^|_)(bash|shell|exec|run|terminal)/.test(name)) {
    const command = firstArg(args, ["command", "cmd", "script"]);
    return command ? `Running shell: ${clip(command, 40)}` : "Running shell";
  }
  if (/(^|_)(read|cat|open|view)/.test(name)) {
    const path = firstArg(args, ["path", "file", "file_path", "filename"]);
    return path ? `Reading ${basename(path)}` : "Reading a file";
  }
  if (/(^|_)(search|grep|glob|find|rg)/.test(name)) {
    const pattern = firstArg(args, ["pattern", "query", "q", "regex"]);
    return pattern ? `Searching "${clip(pattern, 30)}"` : "Searching";
  }
  if (/(^|_)(edit|patch|apply|replace)/.test(name)) {
    const path = firstArg(args, ["path", "file", "file_path", "filename"]);
    return path ? `Editing ${basename(path)}` : "Editing";
  }
  if (/(^|_)(write|create|save)/.test(name)) {
    const path = firstArg(args, ["path", "file", "file_path", "filename"]);
    return path ? `Writing ${basename(path)}` : "Writing";
  }
  if (/(^|_)(fetch|http|curl|web|browse)/.test(name)) {
    const url = firstArg(args, ["url", "uri", "href"]);
    return url ? `Fetching ${clip(url, 40)}` : "Fetching";
  }
  return `Running ${clip(call.name, 30)}`;
}

/**
 * What the indicator should be showing.
 *
 * The order is deliberate: anything waiting on a person outranks anything the
 * machine is doing, a tool in flight is more specific than the command that
 * started it, and "thinking" is what is left when nothing else is known.
 */
export function derivePhase(
  state: State,
  { runningCommand = null }: { runningCommand?: string | null } = {},
): WorkingPhase {
  if (state.pendingApproval || state.approvalQueue.length > 0) return { kind: "approval" };
  if (!state.turnActive) return { kind: "idle" };

  const running = state.toolCalls.filter((call) => call.state === "running");
  const last = running[running.length - 1];
  if (last) return { kind: "tool", label: toolLabel(last) };

  const agents = state.subagents.filter((agent) => agent.status === "running").length;
  if (agents > 0) return { kind: "subagents", running: agents };

  if (runningCommand) return { kind: "command", name: runningCommand };
  return { kind: "thinking" };
}

/** `71000` → `1m 11s`. Coarser than the HUD's clock, and easier to read. */
export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** `(1m 11s · ↑ 1.2k · ↓ 3.7k tokens)` — input only once the daemon reports it. */
export function formatStats({
  elapsedMs,
  inputTokens = 0,
  outputTokens = 0,
}: {
  elapsedMs: number;
  inputTokens?: number;
  outputTokens?: number;
}): string {
  const parts = [formatDuration(elapsedMs)];
  if (inputTokens > 0) parts.push(`↑ ${formatTokens(inputTokens)}`);
  parts.push(`↓ ${formatTokens(outputTokens)} tokens`);
  return `(${parts.join(" · ")})`;
}

export interface WorkingLineInput {
  phase: WorkingPhase;
  /** Milliseconds since the turn started. */
  elapsedMs: number;
  inputTokens?: number;
  outputTokens?: number;
  /** Spinner frame index; the caller owns the timer. */
  frame?: number;
  /** Moves the verb list's starting point so turns do not all open alike. */
  verbOffset?: number;
}

/** The whole line, or null when there is nothing to say. */
export function workingLine(input: WorkingLineInput): string | null {
  const { phase } = input;
  if (phase.kind === "idle") return null;
  if (phase.kind === "approval") return `${PAUSED_GLYPH} Waiting for approval`;

  const spinner = SPINNER_FRAMES[Math.abs(input.frame ?? 0) % SPINNER_FRAMES.length];
  const stats = formatStats(input);

  if (phase.kind === "tool") return `${spinner} ${phase.label}… ${stats}`;
  if (phase.kind === "subagents") {
    const plural = phase.running === 1 ? "agent" : "agents";
    return `${spinner} ${phase.running} ${plural} working… ${stats}`;
  }
  if (phase.kind === "command") return `${spinner} ${phase.name}… ${stats}`;

  const verb = verbAt(input.elapsedMs, input.verbOffset ?? 0);
  return `${spinner} ${verb}… ${stats}`;
}

/**
 * `⏳ 2 queued`, appended to the working line.
 *
 * Typing while a turn runs queues the prompt rather than interrupting; this is
 * the part that says so at a glance, with the prompts themselves listed under
 * the input.
 */
export function queuedLabel(count: number): string {
  return `⏳ ${count} queued`;
}

/** The line pushed to the scrollback once the turn is over. */
export function turnSummaryLine({
  ok,
  elapsedMs,
  inputTokens = 0,
  outputTokens = 0,
}: {
  ok: boolean;
  elapsedMs: number;
  inputTokens?: number;
  outputTokens?: number;
}): string {
  const head = ok ? `✓ Done in ${formatDuration(elapsedMs)}` : `✗ Stopped after ${formatDuration(elapsedMs)}`;
  const parts = [head];
  if (inputTokens > 0) parts.push(`↑ ${formatTokens(inputTokens)}`);
  parts.push(`↓ ${formatTokens(outputTokens)} tokens`);
  return parts.join(" · ");
}
