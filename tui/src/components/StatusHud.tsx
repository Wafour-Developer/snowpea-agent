/**
 * The bottom HUD: version, model, mode, context, session, daemon, approvals,
 * the running command and the connection dot, separated by `|`.
 *
 * All the deciding lives in `layout/hud.ts`; this component only draws the rows
 * that module hands back, which is what keeps the width rules testable.
 */

import React from "react";
import { Box, Text } from "ink";

import { SEPARATOR, type HudSegment } from "../layout/hud.js";

export interface StatusHudProps {
  /** Already packed by `layoutHud`, one array per row. */
  rows: HudSegment[][];
}

function StatusHudInner({ rows }: StatusHudProps): React.ReactElement {
  return (
    <Box flexDirection="column" flexShrink={0}>
      {rows.map((segments, rowIndex) => (
        <Box key={`hud-row-${rowIndex}`}>
          <Text wrap="truncate-end">
            {segments.map((segment, index) => (
              <Text key={segment.key}>
                {index > 0 ? <Text dimColor>{SEPARATOR}</Text> : null}
                <Text color={segment.color} dimColor={segment.dimColor} bold={segment.bold}>
                  {segment.text}
                </Text>
              </Text>
            ))}
          </Text>
        </Box>
      ))}
    </Box>
  );
}

/** Memoized: only a changed row list may repaint the HUD. */
export const StatusHud = React.memo(StatusHudInner);
