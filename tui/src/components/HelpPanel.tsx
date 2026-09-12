/** Bounded, scrollable command help; never expands into terminal scrollback. */
import React, { useState } from "react";
import { Box, Text, useInput } from "ink";
import type { CommandInfo } from "../rpc/sdk.js";
import { wrapLine, type Line } from "../layout/transcript.js";
import { TranscriptView } from "./TranscriptView.js";

export const WORKFLOW_COMMANDS: readonly string[] = [
  "ralph",
  "ralplan",
  "ultrawork",
  "deepinit",
  "deep-research",
  "deep-interview",
  "plan",
  "accept",
  "auto",
];

const KEYS = [
  "Esc / F1 / q / Enter close help · ↑↓ / PgUp / PgDn scroll",
  "Ctrl+C quit · Esc outside help interrupts the current turn",
  "Ctrl+O expand the newest tool call or diff · Ctrl+A open the agent panel",
  "⇧Tab cycles accept -> auto -> plan · Ctrl+P toggles plan mode",
  "/compact folds the conversation down when context is running low",
  "↑ walks through earlier prompts · ↓ past the newest moves to the footer",
  "Enter on an agent opens its conversation; Esc comes back",
  "/resume or R on empty input resumes the last session; /resume <sessionId> selects one",
  "Paste a file path to attach it · Ctrl+V pastes an image · /attach <path>",
  "Backspace on empty input removes an attachment · Ctrl+X removes all",
  "/voice arms input · Ctrl+Space records · /tts on|off speaks replies",
  "Ctrl+R focuses approvals: a allow, d deny, ↑↓ select, ←→ scope",
  "Approval menus: ↑↓ select, Enter confirm, y/a/p/n answer, Esc refuse",
];

export function HelpPanel({ commands, runningSubagents = 0, width = 80, height = 20, isActive = true }: {
  commands: CommandInfo[];
  runningSubagents?: number;
  width?: number;
  height?: number;
  isActive?: boolean;
}): React.ReactElement {
  const [offset, setOffset] = useState(0);
  const inner = Math.max(1, width - 4);
  const rows = Math.max(1, height - 3);
  const lines: Line[] = [];
  const add = (text: string, color?: string, bold = false) => {
    lines.push(...wrapLine({ key: `help-${lines.length}`, segments: [{ text, color, bold }] }, inner));
  };
  const workflows = commands.filter(command => WORKFLOW_COMMANDS.includes(command.name));
  const others = commands.filter(command => !WORKFLOW_COMMANDS.includes(command.name));
  const commandRows = (entries: CommandInfo[]) => entries.forEach(command => {
    add(`/${command.name}`, "cyan", true);
    lines.push(...wrapLine({ key: `help-${lines.length}`, segments: [{ text: `  ${command.summary}`, dimColor: true }] }, inner, "  "));
  });
  if (workflows.length) {
    add("Workflows and modes", "cyan", true);
    commandRows(workflows);
  }
  if (runningSubagents) add(`${runningSubagents} subagent(s) running`);
  add("Commands", "cyan", true);
  if (!commands.length) add("no commands reported by the daemon");
  commandRows(others);
  add("Keys", "cyan", true);
  KEYS.forEach(text => add(text));
  const maxOffset = Math.max(0, lines.length - rows);
  const start = Math.min(offset, maxOffset);
  useInput((_input, key) => {
    const step = key.pageDown ? rows : key.pageUp ? -rows : key.downArrow ? 1 : key.upArrow ? -1 : 0;
    if (step) setOffset(current => Math.min(maxOffset, Math.max(0, current + step)));
  }, { isActive });
  if (height < 4) return <Text wrap="truncate-end">Esc / F1 close help</Text>;
  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor="cyan" paddingX={1} overflow="hidden">
      <TranscriptView lines={lines.slice(start, start + rows)} height={rows} />
      <Text color="cyan" wrap="truncate-end">{`Esc / F1 / q / Enter close · ↑↓ scroll · ${start + 1}-${Math.min(start + rows, lines.length)}/${lines.length}`}</Text>
    </Box>
  );
}
