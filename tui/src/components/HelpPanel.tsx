/**
 * Command help, rendered from `command.list`. Opened by F1 or by the result of
 * `/help`. Like the palette it has no built-in table.
 */

import React from "react";
import { Box, Text } from "ink";

import type { CommandInfo } from "../rpc/sdk.js";

export function HelpPanel({
  commands,
}: {
  commands: CommandInfo[];
}): React.ReactElement {
  const width = commands.reduce((max, c) => Math.max(max, c.name.length), 0) + 2;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
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
        <Text dimColor>  F1 close · Ctrl+C quit · Ctrl+O expand the last tool call</Text>
        <Text dimColor>  Esc interrupt the current turn</Text>
        <Text dimColor>
          {"  Ctrl+A focus the unattended approval queue: [a] allow [d] deny, "}
          {"↑/↓ pick, ←/→ scope"}
        </Text>
        <Text dimColor>
          {"  Approval scopes: once · session · project · always "}
          {"(project/always also store an allowlist pattern)"}
        </Text>
      </Box>
    </Box>
  );
}
