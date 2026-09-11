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

/*
 * Each segment is its own `<Text>` beside its neighbours rather than a nested
 * one inside a single row `<Text>`. Nesting looked tidier but measured wrong:
 * Ink gives the outer `<Text>` one Yoga node whose measurement is not
 * invalidated when a segment element appears, so the row kept the width it had
 * before the daemon segment arrived and `truncate-end` cut it short.
 */

export interface StatusHudProps {
  /** Already packed by `layoutHud`, one array per row. */
  rows: HudSegment[][];
  /**
   * Width the rows are packed for. Set explicitly: a row of nested `<Text>`
   * left to size itself can be squeezed by its siblings, and `truncate-end`
   * would then cut a row that `layoutHud` had already made fit.
   */
  width?: number;
}

function StatusHudInner({ rows, width }: StatusHudProps): React.ReactElement {
  return (
    <Box flexDirection="column" flexShrink={0} width={width}>
      {rows.map((segments, rowIndex) => (
        <Box key={`hud-row-${rowIndex}`} width={width} flexWrap="nowrap" overflow="hidden">
          {segments.map((segment, index) => (
            <React.Fragment key={segment.key}>
              {index > 0 ? <Text dimColor>{SEPARATOR}</Text> : null}
              <Text
                color={segment.color}
                dimColor={segment.dimColor}
                bold={segment.bold}
                wrap="truncate-end"
              >
                {segment.text}
              </Text>
            </React.Fragment>
          ))}
        </Box>
      ))}
    </Box>
  );
}

/** Memoized: only a changed row list may repaint the HUD. */
export const StatusHud = React.memo(StatusHudInner);
