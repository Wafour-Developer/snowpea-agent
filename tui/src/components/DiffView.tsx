/** Colored unified diff for `diff{path, patch}` events. */

import React from "react";
import { Box, Text } from "ink";

import type { DiffEntry } from "../state/store.js";

export function diffLineColor(line: string): string | undefined {
  if (line.startsWith("+++") || line.startsWith("---")) return "cyan";
  if (line.startsWith("@@")) return "magenta";
  if (line.startsWith("+")) return "green";
  if (line.startsWith("-")) return "red";
  return undefined;
}

export function DiffView({
  diff,
  maxLines = 40,
}: {
  diff: DiffEntry;
  maxLines?: number;
}): React.ReactElement {
  const lines = diff.patch.split("\n");
  const shown = lines.slice(0, maxLines);
  const hidden = lines.length - shown.length;
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text bold color="yellow">
        ± {diff.path}
      </Text>
      {shown.map((line, index) => (
        <Text key={`${diff.id}-d${index}`} color={diffLineColor(line)}>
          {line}
        </Text>
      ))}
      {hidden > 0 ? <Text dimColor>{`… ${hidden} more diff lines`}</Text> : null}
    </Box>
  );
}
