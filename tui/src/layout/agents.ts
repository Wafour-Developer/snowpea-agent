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
import { formatDuration } from "../state/working.js";
import type { State, SubagentEntry, TeamTaskEntry } from "../state/store.js";

/** Rows drawn before the overflow line takes over. */
export const MAX_AGENT_ROWS = 6;
/** Idle agents past this many are counted rather than listed. */
export const MAX_IDLE_ROWS = 3;

/** Marks the session the user is typing into. */
export const CURRENT_GLYPH = "●";
/** Marks anything working on the session's behalf. */
export const AGENT_GLYPH = "◯";

export type AgentStatus = "current" | "idle" | "queued" | "running" | "done" | "error";

const STATUS_GLYPH: Record<AgentStatus, string> = {
  current: CURRENT_GLYPH,
  idle: AGENT_GLYPH,
  queued: "⏳",
  running: "✳",
  done: "✓",
  error: "✗",
};

const STATUS_COLOR: Record<AgentStatus, string | undefined> = {
  current: "green",
  idle: undefined,
  queued: "yellow",
  running: "cyan",
  done: "green",
  error: "red",
};

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

function subagentRow(entry: SubagentEntry, now: number): AgentRow {
  const status = entry.status as AgentStatus;
  return {
    key: `agent-${entry.agentId}`,
    glyph: STATUS_GLYPH[status] ?? AGENT_GLYPH,
    color: STATUS_COLOR[status],
    name: entry.name || "agent",
    // The model's own one-line title beats the brief it wrote for the child:
    // the brief is written for a machine, often in English, and is long.
    task: entry.title || entry.task || entry.lastText || "",
    status: agentStatusText(entry, now),
    dim: entry.status === "done",
  };
}

function teamRow(task: TeamTaskEntry): AgentRow {
  const running = task.status === "running" || task.status === "claimed";
  return {
    key: `team-${task.taskId}`,
    glyph: running ? STATUS_GLYPH.running : AGENT_GLYPH,
    color: task.status === "conflict" || task.status === "failed" ? "red" : undefined,
    name: task.assignee || task.teamId || "team",
    task: `task ${task.taskId}`,
    status: task.status,
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

/**
 * Every row the panel draws, the current session first.
 *
 * Idle agents are the ones the daemon merely knows about; past
 * `MAX_IDLE_ROWS` they become a single counted row, because a long list of
 * things doing nothing is noise. Whatever is left over past `MAX_AGENT_ROWS`
 * becomes the overflow line.
 */
export function buildAgentRows({
  state,
  known = [],
  roster,
  now,
  expanded = false,
  currentLabel = "main",
}: AgentRowsInput): AgentRow[] {
  const rows: AgentRow[] = [
    {
      key: "current",
      glyph: CURRENT_GLYPH,
      color: STATUS_COLOR.current,
      name: currentLabel,
      task: "",
      status: "",
      dim: false,
    },
  ];

  const live = state.subagents.map((entry) => subagentRow(entry, now));
  rows.push(...live.filter((row) => !row.dim));
  rows.push(...state.teamTasks.map(teamRow).filter((row) => !row.dim));

  // Idle rows: agents the daemon defines that are not part of this turn.
  const busy = new Set(state.subagents.map((entry) => entry.name).filter(Boolean));
  const onTeam = roster ? new Set(roster) : null;
  const idle = known
    .filter((agent) => agent.kind !== "subagent" && !busy.has(agent.name))
    .filter((agent) => !onTeam || onTeam.has(agent.name) || agent.kind === "agent")
    .map<AgentRow>((agent) => ({
      key: `idle-${agent.name}`,
      glyph: AGENT_GLYPH,
      name: agent.name,
      task: agent.description ?? "",
      status: "idle",
      dim: true,
    }));

  if (expanded || idle.length <= MAX_IDLE_ROWS) {
    rows.push(...idle);
  } else {
    rows.push(...idle.slice(0, MAX_IDLE_ROWS));
    const hidden = idle.slice(MAX_IDLE_ROWS);
    rows.push({
      key: "idle-more",
      glyph: AGENT_GLYPH,
      name: `${hidden.length} more idle agent${hidden.length === 1 ? "" : "s"}`,
      task: `- ${hidden.map((agent) => agent.name).join(", ")}`,
      status: "",
      dim: true,
    });
  }

  // Finished delegates stay visible while nothing else needs the room.
  rows.push(...live.filter((row) => row.dim));

  if (expanded || rows.length <= MAX_AGENT_ROWS) return rows;
  const shown = rows.slice(0, MAX_AGENT_ROWS);
  shown.push({
    key: "overflow",
    glyph: "↓",
    name: `${rows.length - MAX_AGENT_ROWS} more`,
    task: "",
    status: "",
    dim: true,
  });
  return shown;
}

/**
 * Glyphs a terminal draws two cells wide.
 *
 * Only the panel's own glyphs need to be known, and they are all listed above,
 * so this is exact rather than a general width table.
 */
const WIDE_GLYPHS = new Set(["⏳"]);

/** Columns a string occupies on screen, counting the wide glyphs as two. */
export function cells(text: string): number {
  let width = 0;
  for (const character of text) width += WIDE_GLYPHS.has(character) ? 2 : 1;
  return width;
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
