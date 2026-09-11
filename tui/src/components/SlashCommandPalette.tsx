/**
 * Autocomplete popup shown while the input starts with `/`.
 * Entries come from `command.list` only — the TUI has no command table.
 */

import React from "react";
import { Box, Text } from "ink";

import type { CommandInfo } from "../rpc/sdk.js";

export function SlashCommandPalette({
  commands,
  selectedIndex = 0,
  maxRows = 8,
}: {
  commands: CommandInfo[];
  selectedIndex?: number;
  maxRows?: number;
}): React.ReactElement | null {
  if (commands.length === 0) return null;
  const start = Math.max(0, Math.min(selectedIndex - maxRows + 1, commands.length - maxRows));
  const shown = commands.slice(Math.max(0, start), Math.max(0, start) + maxRows);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="blue" paddingX={1}>
      {shown.map((command) => {
        const active = commands.indexOf(command) === selectedIndex;
        return (
          <Text key={command.name} inverse={active}>
            <Text color="blue">/{command.name}</Text>
            <Text dimColor> {command.summary}</Text>
            <Text dimColor> [{command.source}]</Text>
          </Text>
        );
      })}
      <Text dimColor>↑/↓ select · Tab complete · Enter run</Text>
    </Box>
  );
}
