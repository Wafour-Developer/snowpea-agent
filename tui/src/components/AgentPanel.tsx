/**
 * The persistent list of who is working for this session.
 *
 * It is up whether or not a turn is running, so the user can always see the
 * shape of their team: the current session first, then whatever is delegating,
 * then the agents the daemon merely knows about. The rows and the columns are
 * decided by `layout/agents.ts`; this paints them.
 */

import React from "react";
import { Box, Text } from "ink";

import { layoutAgentRow, type AgentRow, rowColor } from "../layout/agents.js";

export interface AgentPanelProps {
  rows: AgentRow[];
  width: number;
  /** Row the keyboard is on, when the cursor has walked down into the panel. */
  focusedIndex?: number | null;
  /**
   * The key hint. It shares the `↓ 5 more` row when the panel ends in one,
   * and takes a row of its own otherwise; `hintShares` says which.
   */
  hint?: string;
}

/** True when the hint fits on the panel's last row instead of a row of its own. */
export function hintShares(rows: readonly AgentRow[]): boolean {
  const last = rows[rows.length - 1];
  return last !== undefined && (last.key === "overflow" || last.key === "idle-more");
}

function AgentPanelInner({
  rows,
  width,
  focusedIndex = null,
  hint,
}: AgentPanelProps): React.ReactElement | null {
  if (rows.length === 0) return hint ? <Text dimColor wrap="truncate-end">{hint}</Text> : null;
  const shared = hint !== undefined && hintShares(rows);
  return (
    <Box flexDirection="column" flexShrink={0} width={width}>
      {rows.map((row, index) => {
        const focused = index === focusedIndex;
        // One column short of the panel: a row that fills the last cell makes
        // the terminal wrap it onto a second line.
        const line = layoutAgentRow(row, width - 1);
        if (shared && index === rows.length - 1) {
          return (
            <Box key={row.key} width={width} flexWrap="nowrap" overflow="hidden">
              <Text color={rowColor(row)} dimColor={!focused} inverse={focused} bold={focused}>
                {line.left}
              </Text>
              <Text dimColor wrap="truncate-end">{`  ·  ${hint}`}</Text>
            </Box>
          );
        }
        return (
          <Box key={row.key} width={width} flexWrap="nowrap" overflow="hidden">
            <Text color={rowColor(row)} dimColor={row.dim && !rowColor(row)} inverse={focused} bold={focused}>
              {line.left}
            </Text>
            <Text dimColor={!focused} inverse={focused} wrap="truncate-end">
              {line.task}
            </Text>
            <Text dimColor inverse={focused}>
              {line.gap}
            </Text>
            <Text dimColor={row.dim && !focused} color={row.color} inverse={focused}>
              {line.status}
            </Text>
          </Box>
        );
      })}
      {hint !== undefined && !shared ? <Text dimColor wrap="truncate-end">{hint}</Text> : null}
    </Box>
  );
}

/** Memoized: only a changed row list may repaint the panel. */
export const AgentPanel = React.memo(AgentPanelInner);
