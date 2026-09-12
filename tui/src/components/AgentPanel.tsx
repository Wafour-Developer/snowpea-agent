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

import { layoutAgentRow, type AgentRow } from "../layout/agents.js";

export interface AgentPanelProps {
  rows: AgentRow[];
  width: number;
}

function AgentPanelInner({ rows, width }: AgentPanelProps): React.ReactElement | null {
  if (rows.length === 0) return null;
  return (
    <Box flexDirection="column" flexShrink={0} width={width}>
      {rows.map((row) => {
        // One column short of the panel: a row that fills the last cell makes
        // the terminal wrap it onto a second line.
        const line = layoutAgentRow(row, width - 1);
        return (
          <Box key={row.key} width={width} flexWrap="nowrap" overflow="hidden">
            <Text color={row.color} dimColor={row.dim && !row.color}>
              {line.left}
            </Text>
            <Text dimColor wrap="truncate-end">
              {line.task}
            </Text>
            <Text dimColor>{line.gap}</Text>
            <Text dimColor={row.dim} color={row.color}>
              {line.status}
            </Text>
          </Box>
        );
      })}
    </Box>
  );
}

/** Memoized: only a changed row list may repaint the panel. */
export const AgentPanel = React.memo(AgentPanelInner);
