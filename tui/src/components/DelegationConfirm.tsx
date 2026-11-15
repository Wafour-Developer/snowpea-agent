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

import { mouseReports } from "../input/mouse.js";
import { fit } from "../layout/fit.js";
import { proposalLines, type DelegationProposal } from "../state/jarvis.js";

/** Rows the confirmation occupies (border, title, session, fields, hint). */
export function delegationConfirmRows(proposal: DelegationProposal): number {
  return 2 + 1 + 1 + proposalLines(proposal, 0).length + 1;
}

export function DelegationConfirm({
  proposal,
  label,
  width,
  now,
  onAnswer,
  isActive = true,
}: {
  proposal: DelegationProposal;
  /** The session the proposal came from. */
  label: string;
  width: number;
  now: number;
  onAnswer: (confirmed: boolean) => void;
  isActive?: boolean;
}): React.ReactElement {
  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (key.escape || input.toLowerCase() === "n") {
        onAnswer(false);
        return;
      }
      if (input.toLowerCase() === "y") onAnswer(true);
    },
    { isActive },
  );
  const inner = Math.max(10, width - 4);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="magenta" paddingX={1} width={width}>
      <Text bold color="magenta" wrap="truncate-end">
        Jarvis asks to change what it may decide
      </Text>
      <Text dimColor wrap="truncate-end">{fit(`from ${label}`, inner)}</Text>
      {proposalLines(proposal, now).map((line, index) => (
        <Text key={`pl-${index}`} wrap="truncate-end" color={line.startsWith("Guarded") && proposal.allow.length > 0 ? "red" : undefined}>
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
