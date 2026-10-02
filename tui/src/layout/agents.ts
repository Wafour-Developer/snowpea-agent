/**
 * The agent panel: who is working for this session, and what they are doing.
 *
 * Rows come from three places that all describe the same thing from different
 * angles — the subagent events of this session, the daemon's `agent.list`, and
 * the team board — so this module merges them into one ordered list and decides
 * what fits. It is the panel under the status line, and it is also what the
 * working indicator shows while a turn is delegating.
 *
 * Pure, so `test/agents.test.ts` can check the collapsing and the columns.
 */

import { formatTokens } from "./hud.js";
import { currentAccent } from "./palette.js";
import { textWidth } from "./text-width.js";
import { formatDuration } from "../state/working.js";
import type { State, SubagentEntry, TeamTaskEntry } from "../state/store.js";

/** Delegate/team rows shown in the compact footer before the overflow line. */
export const MAX_COMPACT_AGENT_ROWS = 2;
/** Finished delegates stay in compact view only long enough to be noticed. */
export const FINISHED_AGENT_GRACE_MS = 10_000;

/** Marks the session the user is typing into. */
export const CURRENT_GLYPH = "●";
/** Marks anything working on the session's behalf. */
export const AGENT_GLYPH = "◯";

export type AgentStatus = "current" | "idle" | "queued" | "running" | "done" | "error";
export type AgentOrigin = "team" | "external" | "named" | "current";

/**
 * Where a row's agent comes from, said with colour rather than a tag: the
 * active team's members keep the panel's plain colour, an agent someone
 * delegated to from outside the team is magenta, a named persistent agent
 * is blue (and already carries the ◆ glyph).
 */
export const ORIGIN_COLOR: Partial<Record<AgentOrigin, string>> = {
  external: "magenta",
  named: "blue",
};

/** The colour a row is drawn in: status first, origin when the status has none. */
export function rowColor(row: Pick<AgentRow, "color" | "origin">): string | undefined {
  return row.color ?? (row.origin ? ORIGIN_COLOR[row.origin] : undefined);
}

const STATUS_GLYPH: Record<AgentStatus, string> = {
  current: CURRENT_GLYPH,
  idle: AGENT_GLYPH,
  queued: "⏳",
  running: "✳",
  done: "✓",
  error: "✗",
};

function statusColor(status: AgentStatus): string | undefined {
  switch (status) {
    case "current":
      return currentAccent();
    case "idle":
      return undefined;
    case "queued":
      return "yellow";
    case "running":
      return "cyan";
    case "done":
      return "green";
    case "error":
      return "red";
    default:
      return undefined;
  }
}

export interface AgentRow {
  key: string;
  glyph: string;
  color?: string;
  /** Agent definition name, or the role it is filling. */
  name: string;
  /** What it was asked to do; truncated to whatever room is left. */
  task: string;
  /** Right-aligned column: `idle`, `running · 8m 40s · ↓ 316.6k`, … */
  status: string;
  /** Where this row's agent comes from, when the active roster is known. */
  origin?: AgentOrigin;
  /** True for the rows that only count other rows. */
  dim: boolean;
}

/** `running · 8m 40s · ↑ 41.2k · ↓ 316.6k tokens`, as much of it as there is to say. */
export function agentStatusText(
  entry: Pick<SubagentEntry, "status" | "startedAt" | "endedAt" | "inputTokens" | "outputTokens">,
  now: number,
): string {
  const parts: string[] = [entry.status];
  if (entry.status === "running" && entry.startedAt) {
    parts.push(formatDuration(now - entry.startedAt));
  }
  if ((entry.status === "done" || entry.status === "error") && entry.startedAt && entry.endedAt) {
    parts.push(formatDuration(entry.endedAt - entry.startedAt));
  }
  // A running child reports its usage as it goes, so its row counts up with it.
  // Nothing is said until it has reported something, or every fresh row would
  // claim a hard zero.
  if (entry.outputTokens > 0 || entry.inputTokens > 0) {
    // Same shape as the desktop's rows: input first when there is any.
    if (entry.inputTokens > 0) parts.push(`↑ ${formatTokens(entry.inputTokens)}`);
    parts.push(`↓ ${formatTokens(entry.outputTokens)} tokens`);
  }
  return parts.join(" · ");
}

function subagentRow(
  entry: SubagentEntry,
  now: number,
  origin: AgentOrigin | undefined,
): AgentRow {
  const status = entry.status as AgentStatus;
  return {
    key: `agent-${entry.agentId}`,
    glyph: STATUS_GLYPH[status] ?? AGENT_GLYPH,
    color: statusColor(status),
    name: entry.name || "agent",
    // The model's own one-line title beats the brief it wrote for the child:
    // the brief is written for a machine, often in English, and is long.
    task: entry.title || entry.task || entry.lastText || "",
    status: agentStatusText(entry, now),
    origin,
    dim: entry.status === "done" || entry.status === "error",
  };
}

function activityTime(entry: SubagentEntry): number {
  return entry.endedAt ?? entry.startedAt ?? 0;
}

function recentFirst<T>(entries: T[], time: (entry: T) => number): T[] {
  return entries
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => time(b.entry) - time(a.entry) || b.index - a.index)
    .map(({ entry }) => entry);
}

function isCompactVisible(entry: SubagentEntry, now: number): boolean {
  if (entry.status === "running" || entry.status === "queued") return true;
  if (!entry.endedAt) return true;
  return now - entry.endedAt <= FINISHED_AGENT_GRACE_MS;
}

function teamRow(task: TeamTaskEntry, origin: AgentOrigin | undefined): AgentRow {
  const running = task.status === "running" || task.status === "claimed";
  return {
    key: `team-${task.taskId}`,
    glyph: running ? STATUS_GLYPH.running : AGENT_GLYPH,
    color: task.status === "conflict" || task.status === "failed" ? "red" : undefined,
    name: task.assignee || task.teamId || "team",
    task: `task ${task.taskId}`,
    status: task.status,
    origin,
    dim: task.status === "merged" || task.status === "done",
  };
}

/** Named agents the daemon knows about but that are not running anything. */
export interface KnownAgent {
  name: string;
  kind?: string;
  description?: string;
  status?: string | null;
  task?: string | null;
  /** For kind "team": its members in roster order. */
  agents?: string[];
  /** For kind "team": the project's active team. */
  active?: boolean;
}

export interface AgentRowsInput {
  state: State;
  /** `agent.list`, when the daemon answered it. */
  known?: KnownAgent[];
  /**
   * The active team's roster. When given, idle rows are that team's members
   * (plus named persistent agents), not every definition the daemon knows:
   * the panel is "who works for this project", not the catalogue.
   */
  roster?: string[];
  now: number;
  /** Ctrl+A shows every row instead of collapsing. */
  expanded?: boolean;
  /** Label for the session itself. */
  currentLabel?: string;
}

const NAMED_GLYPH = "◆";

function rowOrigin(
  name: string,
  roster: Set<string> | null,
  named: Set<string>,
): AgentOrigin | undefined {
  if (!roster) return undefined;
  if (roster.has(name)) return "team";
  if (named.has(name)) return "named";
  return "external";
}

/**
 * Every row the panel draws, the current session first.
 *
 * Compact view shows at most two delegate/idle rows and an overflow control.
 * Expanded view retains all recorded children, including expired terminal rows.
 */
export function buildAgentRows({
  state,
  known = [],
  roster,
  now,
  expanded = false,
  currentLabel = "main",
}: AgentRowsInput): AgentRow[] {
  const onTeam = roster ? new Set(roster) : null;
  const named = new Set(
    known
      .filter((agent) => agent.kind === "agent")
      .map((agent) => agent.name)
      .filter((name) => name.length > 0),
  );
  const rows: AgentRow[] = [
    {
      key: "current",
      glyph: CURRENT_GLYPH,
      color: statusColor("current"),
      name: currentLabel,
      task: "",
      status: "",
      origin: "current",
      dim: false,
    },
  ];

  const orderedSubagents = recentFirst(state.subagents, activityTime);
  const compactSubagents = orderedSubagents.filter((entry) => isCompactVisible(entry, now));
  const teamRows = recentFirst(state.teamTasks, (task) => Number(task.taskId) || 0)
    .map((task) =>
      teamRow(task, rowOrigin(task.assignee || task.teamId || "team", onTeam, named)),
    )
    .filter((row) => !row.dim);
  const allLive = orderedSubagents.map((entry) =>
    subagentRow(entry, now, rowOrigin(entry.name, onTeam, named)),
  );
  const compactLive = compactSubagents.map((entry) =>
    subagentRow(entry, now, rowOrigin(entry.name, onTeam, named)),
  );
  const allWorkingRows = [...allLive, ...teamRows];
  const compactWorkingRows = [...compactLive, ...teamRows];

  // Idle rows: agents the daemon defines that are not part of this turn.
  const busy = new Set(state.subagents.map((entry) => entry.name).filter(Boolean));
  const idle = known
    .filter((agent) => agent.kind !== "subagent" && !busy.has(agent.name))
    .filter((agent) => !onTeam || onTeam.has(agent.name) || agent.kind === "agent")
    .map<AgentRow>((agent) => ({
      key: `idle-${agent.name}`,
      glyph: agent.kind === "agent" ? NAMED_GLYPH : AGENT_GLYPH,
      name: agent.name,
      task: agent.description ?? "",
      status: "idle",
      origin: rowOrigin(agent.name, onTeam, named),
      dim: true,
    }));

  if (expanded) {
    rows.push(...allWorkingRows, ...idle);
    return rows;
  }

  const compactRows = compactWorkingRows.length > 0 ? compactWorkingRows : idle;
  const expandableCount =
    (allWorkingRows.length > 0 ? allWorkingRows.length : idle.length) +
    (allWorkingRows.length > 0 ? idle.length : 0);
  const shownCompactRows = compactRows.slice(0, MAX_COMPACT_AGENT_ROWS);
  rows.push(...shownCompactRows);
  const compactHidden = Math.max(0, expandableCount - shownCompactRows.length);
  if (compactHidden > 0) {
    rows.push({
      key: "overflow",
      glyph: "↓",
      name: `${compactHidden} more`,
      task: "",
      status: "",
      dim: true,
    });
  }


  return rows;
}

/** Columns a string occupies on screen, counting the wide glyphs as two. */
export function cells(text: string): number {
  return textWidth(text);
}

export interface AgentRowLayout {
  /** `◯ executor` plus its padding, ready to draw. */
  left: string;
  /** The task text, already cut to what is left over. */
  task: string;
  /** Spaces that push the status to the right edge. */
  gap: string;
  status: string;
}

/** Name column width, so the task text of every row starts in one place. */
export const NAME_WIDTH = 16;

/**
 * Place one row's three columns inside `width`.
 *
 * The status column is right-aligned and never truncated — it is the part that
 * says whether anything is happening — so the task text is what gives way.
 */
export function layoutAgentRow(row: AgentRow, width: number): AgentRowLayout {
  const safeWidth = Math.max(10, Math.floor(width));
  const left = `${row.glyph} ${row.name}`;
  // Padding is measured in screen cells, not characters, or a row whose glyph
  // is two cells wide would push its task one column out of the column.
  const pad = Math.max(0, NAME_WIDTH + 2 - cells(left));
  const padded = row.task.length > 0 ? left + " ".repeat(pad) : left;
  const leftCells = cells(padded);
  const statusCells = cells(row.status);
  const room = safeWidth - leftCells - (statusCells > 0 ? statusCells + 1 : 0);
  let task = row.task;
  if (room <= 0) task = "";
  else if (task.length > room) task = `${task.slice(0, Math.max(1, room - 1))}…`;
  const used = leftCells + task.length + statusCells;
  return {
    left: padded,
    task,
    gap: " ".repeat(Math.max(statusCells > 0 ? 1 : 0, safeWidth - used)),
    status: row.status,
  };
}
