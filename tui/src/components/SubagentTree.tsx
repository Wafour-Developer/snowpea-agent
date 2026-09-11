/**
 * The subagents this session has delegated to, rendered as a tree under it.
 *
 * Fed entirely by the `subagent.spawn` / `subagent.update` / `subagent.done`
 * events on the parent session (M7 contract §3): the TUI decides nothing, it
 * only draws what the daemon reports. `/ralph` and `/ultrawork` are what make
 * several of these light up at once.
 */

import React from "react";
import { Box, Text } from "ink";

import type { SubagentEntry, SubagentStatus } from "../state/store.js";

/** Glyph and colour per status, so a glance says how the run is going. */
const MARKS: Record<SubagentStatus, { glyph: string; color: string }> = {
  queued: { glyph: "◦", color: "gray" },
  running: { glyph: "●", color: "cyan" },
  done: { glyph: "✓", color: "green" },
  error: { glyph: "✗", color: "red" },
};

/** Task and progress lines are clipped to keep one subagent on one row. */
const TASK_WIDTH = 58;
const TEXT_WIDTH = 64;

export function clip(text: string, width: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= width ? flat : `${flat.slice(0, Math.max(0, width - 1))}…`;
}

/** `3 running · 1 queued · 2 done` — the header line. */
export function summarise(subagents: SubagentEntry[]): string {
  const counts = new Map<SubagentStatus, number>();
  for (const entry of subagents) {
    counts.set(entry.status, (counts.get(entry.status) ?? 0) + 1);
  }
  const order: SubagentStatus[] = ["running", "queued", "done", "error"];
  return order
    .filter((status) => counts.has(status))
    .map((status) => `${counts.get(status)} ${status}`)
    .join(" · ");
}

export function SubagentTree({
  subagents,
  /** Hide children that have already finished; on by default while a turn runs. */
  showFinished = true,
}: {
  subagents: SubagentEntry[];
  showFinished?: boolean;
}): React.ReactElement | null {
  const visible = showFinished
    ? subagents
    : subagents.filter((entry) => entry.status === "queued" || entry.status === "running");
  if (visible.length === 0) return null;

  return (
    <Box flexDirection="column" marginY={1}>
      <Text dimColor>
        {`subagents (${summarise(subagents)})`}
      </Text>
      {visible.map((entry, index) => {
        const mark = MARKS[entry.status] ?? MARKS.queued;
        const last = index === visible.length - 1;
        const label = entry.name ? `${entry.name}: ` : "";
        const detail =
          entry.status === "done" || entry.status === "error" ? entry.summary : entry.lastText;
        return (
          <Box key={entry.agentId} flexDirection="column">
            <Text>
              <Text dimColor>{last ? "  └─ " : "  ├─ "}</Text>
              <Text color={mark.color}>{mark.glyph} </Text>
              <Text>{clip(`${label}${entry.task}`, TASK_WIDTH)}</Text>
            </Text>
            {detail ? (
              <Text dimColor>
                {`  ${last ? " " : "│"}     ${clip(detail, TEXT_WIDTH)}`}
              </Text>
            ) : null}
          </Box>
        );
      })}
    </Box>
  );
}
