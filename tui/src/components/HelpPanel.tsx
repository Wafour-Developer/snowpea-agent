/**
 * Command help, rendered from `command.list`. Opened by F1 or by the result of
 * `/help`. Like the palette it has no built-in table.
 */

import React from "react";
import { Box, Text } from "ink";

import type { CommandInfo } from "../rpc/sdk.js";

/**
 * The multi-agent workflows and modes AC-03 expects `/help` to show, listed
 * first because they are the ones a new user is looking for. Whether each one
 * exists is still the daemon's answer: names missing from `command.list` are
 * not drawn.
 */
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

export function HelpPanel({
  commands,
  /** Live subagents, so `/help` during a ralph run says what is in flight. */
  runningSubagents = 0,
}: {
  commands: CommandInfo[];
  runningSubagents?: number;
}): React.ReactElement {
  const width = commands.reduce((max, c) => Math.max(max, c.name.length), 0) + 2;
  const workflows = WORKFLOW_COMMANDS.map((name) =>
    commands.find((command) => command.name === name),
  ).filter((command): command is CommandInfo => command !== undefined);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      {workflows.length > 0 ? (
        <Box flexDirection="column" marginBottom={1}>
          <Text bold color="cyan">
            Workflows and modes
          </Text>
          {workflows.map((command) => (
            <Text key={`workflow-${command.name}`}>
              <Text color="magenta">{`/${command.name}`.padEnd(width)}</Text>
              <Text dimColor>{command.summary}</Text>
            </Text>
          ))}
          {runningSubagents > 0 ? (
            <Text dimColor>{`  ${runningSubagents} subagent(s) running right now`}</Text>
          ) : null}
        </Box>
      ) : null}
      <Text bold color="cyan">
        Commands
      </Text>
      {commands.length === 0 ? (
        <Text dimColor>no commands reported by the daemon</Text>
      ) : (
        commands.map((command) => (
          <Text key={command.name}>
            <Text color="blue">{`/${command.name}`.padEnd(width)}</Text>
            <Text dimColor>{command.summary}</Text>
          </Text>
        ))
      )}
      <Box marginTop={1} flexDirection="column">
        <Text bold color="cyan">
          Keys
        </Text>
        <Text dimColor>  F1 close · Ctrl+C quit · Esc interrupt the current turn</Text>
        <Text dimColor>
          {"  Ctrl+O expand the newest tool call or diff · "}
          {"Ctrl+A open the agent panel out"}
        </Text>
        <Text dimColor>
          {"  ⇧Tab cycles mode accept -> auto -> plan -> accept · "}
          {"Ctrl+P toggles plan mode"}
        </Text>
        <Text dimColor>
          {"  /compact folds the conversation down when the ctx segment turns "}
          {"yellow or red"}
        </Text>
        <Text dimColor>
          {"  Delegates and named agents are listed under the status line; "}
          {"Ctrl+A shows every row"}
        </Text>
        <Text dimColor>
          {"  ↑ walks back through your earlier prompts · ↓ past the newest one "}
          {"moves onto the rows below the input"}
        </Text>
        <Text dimColor>
          {"  Enter on the footer lists what is running; Enter on an agent opens "}
          {"its conversation, Esc comes back"}
        </Text>
        <Text dimColor>
          {"  /resume (or R on an empty input) reopens the session this "}
          {"directory was last in"}
        </Text>
        <Text dimColor>
          {"  Paste or drop a file path to attach it · Ctrl+V takes an image "}
          {"from the clipboard · /attach <path>"}
        </Text>
        <Text dimColor>
          {"  Backspace on an empty input drops the newest attachment, "}
          {"Ctrl+X drops them all"}
        </Text>
        <Text dimColor>
          {"  /voice arms voice input (Ctrl+Space records) · /tts on|off "}
          {"speaks the replies"}
        </Text>
        <Text dimColor>
          {"  Ctrl+R focus the unattended approval queue: [a] allow [d] deny, "}
          {"↑/↓ pick, ←/→ scope"}
        </Text>
        <Text dimColor>
          {"  An approval asks with a menu: ↑↓ move, Enter confirms, "}
          {"y/a/p/n answer directly, Esc refuses"}
        </Text>
      </Box>
    </Box>
  );
}
