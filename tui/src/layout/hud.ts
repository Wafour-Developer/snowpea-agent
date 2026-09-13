/**
 * The bottom status HUD: which segments exist, and which of them fit.
 *
 * Kept pure and React-free so the truncation rules are testable without a
 * terminal. `buildHudSegments` turns session state into the ordered segment
 * list; `layoutHud` packs that list into the one or two rows the width allows,
 * dropping the least important segments first and truncating only as a last
 * resort.
 */

import { contextSegment } from "./bottom.js";
import type { ConnectionStatus } from "../rpc/client.js";
import type { ContextUsage } from "../state/store.js";
import type { Mode } from "../rpc/sdk.js";

/**
 * Rows the HUD may use.
 *
 * The second one appears only when the first overflows, so a wide terminal
 * still gets a single line and a narrow one keeps its segments instead of
 * dropping them.
 */
export const MAX_HUD_ROWS = 2;

/** Drawn between segments, Claude-Code style. */
export const SEPARATOR = " | ";

export interface HudSegment {
  key: string;
  text: string;
  color?: string;
  dimColor?: boolean;
  bold?: boolean;
  /**
   * Drop order when the row runs out of room: the highest priority number goes
   * first, so 0 is "never drop this".
   */
  priority: number;
}

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "yellow",
  connected: "green",
  reconnecting: "yellow",
  closed: "red",
};

const MODE_COLOR: Record<Mode, string> = {
  plan: "cyan",
  accept: "green",
  auto: "red",
};

/** `~/src/snowpea` — keeps the tail of a long path, which is the useful half. */
export function shortenPath(path: string, max: number): string {
  if (max <= 1 || path.length <= max) return path;
  return `…${path.slice(path.length - (max - 1))}`;
}

/** The first segment of a session id is enough to tell two sessions apart. */
export function shortSessionId(sessionId: string | null | undefined): string {
  if (!sessionId) return "—";
  return sessionId.length <= 8 ? sessionId : sessionId.slice(0, 8);
}

/** Longest workdir the HUD will draw before it keeps only the tail. */
export const WORKDIR_WIDTH = 28;

/** `12345` → `12.3k`; token counts are read at a glance, not audited. */
export function formatTokens(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return "0";
  if (count < 1000) return String(Math.round(count));
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/** `742000` → `12m`; whole units only, because the row is precious. */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

export interface HudInput {
  status: ConnectionStatus;
  /** Version of the running daemon. */
  version: string;
  /** Newer version `system.checkUpdate` found, when there is one. */
  latestVersion?: string | null;
  /** Directory the session runs in; the HUD is the only place it appears. */
  workdir?: string;
  /** Session id, shortened; likewise only here. */
  sessionId?: string | null;
  provider: string | null;
  model: string | null;
  /** Where that model came from: pin, profile, project, global. */
  modelSource?: string | null;
  mode: Mode;
  usage: { inputTokens: number; outputTokens: number };
  /** Context-window usage; the segment is hidden until the daemon reports it. */
  context?: ContextUsage | null;
  /** Tools registered for the session, from `tool.list`. */
  toolCount?: number | null;
  /** True while assistant replies are being spoken. */
  speaking?: boolean;
  /** Milliseconds since this TUI attached to its session. */
  sessionMs: number;
  daemonPid?: number | null;
  /** `system.info` lifecycle summary, e.g. "will not exit: 1 job". */
  daemonSummary?: string | null;
  /** Unattended approvals still waiting for someone. */
  pendingApprovals: number;
  /** `/ralph` while a command turn is running, else null. */
  runningCommand?: string | null;
  /** True while the session is answering; the HUD says how to interrupt. */
  turnActive?: boolean;
  /** Transient confirmation; takes the leftmost slot while it is up. */
  toast?: string | null;
  /**
   * True while the one-time Shift+Tab nudge is live. It rides on the mode
   * segment rather than taking a slot of its own, because a separate "⇧Tab:
   * mode" segment would just repeat what that segment already says.
   */
  modeHint?: boolean;
}

/**
 * The full segment list, in display order, before anything is dropped.
 *
 * Every segment that has nothing to say is left out entirely rather than drawn
 * empty, so a session with no daemon info and no approvals simply has a
 * shorter HUD.
 */
export function buildHudSegments(input: HudInput): HudSegment[] {
  const segments: HudSegment[] = [];

  if (input.toast) {
    segments.push({ key: "toast", text: input.toast, color: "cyan", priority: 1 });
  }

  segments.push({
    key: "version",
    text: input.latestVersion
      ? `snowpea v${input.version} → v${input.latestVersion} (U to update)`
      : `snowpea v${input.version}`,
    color: input.latestVersion ? "yellow" : undefined,
    dimColor: !input.latestVersion,
    priority: input.latestVersion ? 1 : 5,
  });

  if (input.workdir) {
    segments.push({
      key: "cwd",
      text: shortenPath(input.workdir, WORKDIR_WIDTH),
      dimColor: true,
      priority: 4,
    });
  }

  const model = [input.provider, input.model].filter(Boolean).join("/");
  segments.push({
    key: "model",
    // The source matters when two places could have set it: a session pin and
    // a project default look identical until one of them is named.
    text: `Model: ${model || "default"}${input.modelSource ? ` (${input.modelSource})` : ""}`,
    dimColor: true,
    priority: 3,
  });

  segments.push({
    key: "mode",
    text: `Mode: ${input.mode.toUpperCase()}${input.modeHint ? " (⇧Tab)" : ""}`,
    color: MODE_COLOR[input.mode],
    priority: 2,
  });

  // How full the model's context window is — the number that decides whether
  // the user needs to run /compact — ranks above the running token total.
  const context = contextSegment(input.context ?? null);
  if (context) {
    segments.push({ key: "ctx", ...context, priority: 1 });
  }

  segments.push({
    key: "tokens",
    text: `tok ${formatTokens(input.usage.inputTokens)}↑/${formatTokens(input.usage.outputTokens)}↓`,
    dimColor: true,
    priority: 5,
  });

  if (input.speaking) {
    segments.push({ key: "tts", text: "🔊", color: "cyan", priority: 2 });
  }

  if (input.toolCount && input.toolCount > 0) {
    segments.push({
      key: "tools",
      text: `🔧 ${input.toolCount} tools`,
      dimColor: true,
      priority: 8,
    });
  }

  segments.push({
    key: "session",
    text: input.sessionId
      ? `session: ${shortSessionId(input.sessionId)} · ${formatElapsed(input.sessionMs)}`
      : `session: ${formatElapsed(input.sessionMs)}`,
    dimColor: true,
    priority: 6,
  });

  if (input.daemonPid) {
    const summary = input.daemonSummary ? ` · ${input.daemonSummary}` : "";
    segments.push({
      key: "daemon",
      text: `daemon: pid ${input.daemonPid}${summary}`,
      dimColor: true,
      priority: 7,
    });
  }

  if (input.pendingApprovals > 0) {
    segments.push({
      key: "approvals",
      text: `⚠ ${input.pendingApprovals} approval${input.pendingApprovals === 1 ? "" : "s"}`,
      color: "yellow",
      bold: true,
      priority: 1,
    });
  }

  // The working indicator above the input already says *that* the turn is
  // running and for how long, so the HUD only adds what it does not: the name
  // of the command that owns the turn, and how to stop it.
  if (input.runningCommand) {
    segments.push({ key: "command", text: `▶ ${input.runningCommand}`, color: "yellow", priority: 2 });
  } else if (input.turnActive) {
    segments.push({ key: "command", text: "esc to interrupt", dimColor: true, priority: 3 });
  }

  segments.push({
    key: "status",
    text: `● ${input.status}`,
    color: STATUS_COLOR[input.status],
    priority: 0,
  });

  return segments;
}



/** Width of a row once its segments are joined by the separator. */
export function rowWidth(segments: HudSegment[]): number {
  if (segments.length === 0) return 0;
  let total = 0;
  for (const segment of segments) total += segment.text.length;
  return total + SEPARATOR.length * (segments.length - 1);
}

/** Pack segments into at most `rows` rows of `width`, or return null. */
function pack(segments: HudSegment[], width: number, rows: number): HudSegment[][] | null {
  const out: HudSegment[][] = [[]];
  for (const segment of segments) {
    // A segment wider than a whole row can never be packed; the caller then
    // drops something or falls back to truncating the sole survivor.
    if (segment.text.length > width) return null;
    const current = out[out.length - 1];
    if (current.length === 0) {
      current.push(segment);
      continue;
    }
    if (rowWidth(current) + SEPARATOR.length + segment.text.length <= width) {
      current.push(segment);
      continue;
    }
    if (out.length >= rows) return null;
    out.push([segment]);
  }
  return out;
}

/** Cut a segment to fit, marking the cut with an ellipsis. */
function truncate(segment: HudSegment, width: number): HudSegment {
  if (segment.text.length <= width) return segment;
  if (width <= 1) return { ...segment, text: "…".slice(0, Math.max(0, width)) };
  return { ...segment, text: `${segment.text.slice(0, width - 1)}…` };
}

/**
 * The rows to draw.
 *
 * Everything that fits on one row stays on one row; the rest spills onto a
 * second. When even that is not enough, segments are dropped worst-first
 * (highest `priority`, then rightmost) until the rest fit. The last survivor is
 * truncated rather than dropped, so the HUD is never empty.
 */
export function layoutHud(segments: HudSegment[], width: number, rows = MAX_HUD_ROWS): HudSegment[][] {
  const safeWidth = Math.max(1, Math.floor(width));
  const safeRows = Math.max(1, Math.floor(rows));
  let candidates = segments.slice();

  while (candidates.length > 0) {
    const packed = pack(candidates, safeWidth, safeRows);
    if (packed) return packed;
    // Drop the least important remaining segment; ties break to the right.
    let worst = 0;
    for (let i = 1; i < candidates.length; i += 1) {
      if (candidates[i].priority >= candidates[worst].priority) worst = i;
    }
    if (candidates.length === 1) break;
    candidates = candidates.filter((_, index) => index !== worst);
  }

  if (candidates.length === 0) return [[]];
  return [[truncate(candidates[0], safeWidth)]];
}
