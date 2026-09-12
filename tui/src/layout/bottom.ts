/**
 * The two rows between the status line and the agent panel.
 *
 * One warns when the context window is running out, the other says in one
 * glance what the session is doing: which mode it is in, how many shells are in
 * flight, how many agents are working. Both are pure so `test/bottom.test.ts`
 * can check the wording and the thresholds.
 */

import { formatTokens } from "./hud.js";
import type { ContextUsage } from "../state/store.js";
import type { Mode } from "../rpc/sdk.js";

/** Amber from here up: the window is filling. */
export const CONTEXT_WARN_PERCENT = 70;
/** The warning row appears from here up. */
export const CONTEXT_ALERT_PERCENT = 80;
/** Red from here up. */
export const CONTEXT_CRITICAL_PERCENT = 85;

/** Mode chips, in the reference's glyphs. */
const MODE_CHIP: Record<Mode, string> = {
  auto: "⏵⏵ auto mode on",
  accept: "▶ accept mode",
  plan: "⏸ plan mode",
};

const MODE_COLOR: Record<Mode, string> = {
  auto: "red",
  accept: "green",
  plan: "cyan",
};

export interface Painted {
  text: string;
  color?: string;
  dimColor?: boolean;
  bold?: boolean;
}

/** How full the window is, as a colour. */
export function contextColor(percent: number | null): string | undefined {
  if (percent === null) return undefined;
  if (percent >= CONTEXT_CRITICAL_PERCENT) return "red";
  if (percent >= CONTEXT_WARN_PERCENT) return "yellow";
  return undefined;
}

/**
 * `ctx 34% (68k/200k)`, or `ctx 12.3k used` when the daemon did not say how big
 * the window is. Null until a `context` event has arrived at all.
 */
export function contextSegment(context: ContextUsage | null): Painted | null {
  if (!context) return null;
  const estimated = context.estimated ? "~" : "";
  const used = `${estimated}${formatTokens(context.used)}`;
  if (!context.window || context.window <= 0) {
    return { text: `ctx ${used} used`, dimColor: true };
  }
  const percent = context.percent ?? (context.used / context.window) * 100;
  const color = contextColor(percent);
  return {
    text: `ctx ${Math.round(percent)}% (${used}/${formatTokens(context.window)})`,
    color,
    dimColor: color === undefined,
    bold: percent >= CONTEXT_CRITICAL_PERCENT,
  };
}

/**
 * The row that only appears when the window is nearly full.
 *
 * It names the threshold it crossed and what to do about it, because "96%" on
 * its own tells the user nothing they can act on.
 */
export function contextWarning(context: ContextUsage | null): Painted | null {
  if (!context || !context.window || context.window <= 0) return null;
  const percent = context.percent ?? (context.used / context.window) * 100;
  if (percent < CONTEXT_ALERT_PERCENT) return null;
  return {
    text: `[!!] context ${Math.round(percent)}% — /compact to free space`,
    color: percent >= CONTEXT_CRITICAL_PERCENT ? "red" : "yellow",
    bold: percent >= CONTEXT_CRITICAL_PERCENT,
  };
}

export interface SummaryLineInput {
  mode: Mode;
  /** Shell tool calls in flight. */
  shells: number;
  /** Delegates working right now. */
  agents: number;
}

/** `⏵⏵ auto mode on · 3 shells · ← 1 agent`. */
export function summaryLine({ mode, shells, agents }: SummaryLineInput): Painted {
  const parts = [MODE_CHIP[mode]];
  if (shells > 0) parts.push(`${shells} ${shells === 1 ? "shell" : "shells"}`);
  if (agents > 0) parts.push(`← ${agents} ${agents === 1 ? "agent" : "agents"}`);
  return { text: parts.join(" · "), color: MODE_COLOR[mode], dimColor: mode === "accept" };
}

/** `— compacted (12.3k → 2.1k tokens) —`, sized to the terminal. */
export function compactionDivider(before: number, after: number, width: number): string {
  const label = ` compacted (${formatTokens(before)} → ${formatTokens(after)} tokens) `;
  const room = Math.max(0, Math.floor(width) - label.length);
  const left = Math.floor(room / 2);
  return `${"─".repeat(left)}${label}${"─".repeat(room - left)}`;
}
