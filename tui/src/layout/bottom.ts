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
/** Red from here up, and the warning row appears. */
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
 * `ctx 12.3k / 128k (10%)`, or `ctx 12.3k / ?` when the daemon did not say how
 * big the window is. Null until a `context` event has arrived at all.
 */
export function contextSegment(context: ContextUsage | null): Painted | null {
  if (!context) return null;
  const used = formatTokens(context.used);
  if (!context.window || context.window <= 0) {
    return { text: `ctx ${used} / ?`, dimColor: true };
  }
  const percent = context.percent ?? (context.used / context.window) * 100;
  const critical = percent >= CONTEXT_CRITICAL_PERCENT;
  const rounded = Math.round(percent);
  const tail = critical ? " CRITICAL" : "";
  const estimated = context.estimated ? "~" : "";
  return {
    text: `ctx ${estimated}${used} / ${formatTokens(context.window)} (${rounded}%)${tail}`,
    color: contextColor(percent),
    dimColor: contextColor(percent) === undefined,
    bold: critical,
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
  if (percent < CONTEXT_WARN_PERCENT) return null;
  const critical = percent >= CONTEXT_CRITICAL_PERCENT;
  const threshold = critical ? CONTEXT_CRITICAL_PERCENT : CONTEXT_WARN_PERCENT;
  return {
    text: `[!!] ctx ${Math.round(percent)}% >= ${threshold}% threshold — run /compact`,
    color: critical ? "red" : "yellow",
    bold: critical,
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
