/**
 * The one-press confirmation for a delegation Jarvis proposes (1.53.0).
 *
 * Jarvis may only *propose* a change to what it may decide; the daemon raises a
 * person-only approval and nothing Jarvis reads can answer it. This shows
 * exactly what would change — scope, level, guarded classes, expiry — and takes
 * one key: `y` confirms, `n` or Esc declines. Enter does nothing on purpose, so
 * a stray Return can never grant authority.
 */

import React from "react";
import { Box, Text, useInput } from "ink";

import { shortcutKey } from "../input/hangulKeys.js";
import { mouseReports } from "../input/mouse.js";
import { fit } from "../layout/fit.js";
import type { PersonOnlyView } from "../state/jarvis.js";

/** Rows the confirmation occupies (border, title, session, fields, hint). */
export function delegationConfirmRows(view: PersonOnlyView): number {
  return 2 + 1 + 1 + view.lines.length + 1;
}

export function DelegationConfirm({
  view,
  label,
  width,
  onAnswer,
  isActive = true,
}: {
  view: PersonOnlyView;
  /** The session the request came from. */
  label: string;
  width: number;
  onAnswer: (confirmed: boolean) => void;
  isActive?: boolean;
}): React.ReactElement {
  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      const pressed = shortcutKey(input).toLowerCase();
      if (key.escape || pressed === "n") {
        onAnswer(false);
        return;
      }
      if (pressed === "y") onAnswer(true);
    },
    { isActive },
  );
  const inner = Math.max(10, width - 4);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="magenta" paddingX={1} width={width}>
      <Text bold color="magenta" wrap="truncate-end">
        {fit(view.title, inner)}
      </Text>
      <Text dimColor wrap="truncate-end">{fit(`from ${label}`, inner)}</Text>
      {view.lines.map((line, index) => (
        <Text key={`pl-${index}`} wrap="truncate-end" color={view.warnPrefix && line.startsWith(view.warnPrefix) ? "red" : undefined}>
          {fit(line, inner)}
        </Text>
      ))}
      <Text dimColor wrap="truncate-end">
        <Text bold color="green">y</Text>
        {" confirm · "}
        <Text bold color="red">n</Text>
        {" or Esc decline"}
      </Text>
    </Box>
  );
}
