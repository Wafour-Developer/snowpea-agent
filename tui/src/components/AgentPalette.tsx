/**
 * The agent-name completion list.
 *
 * Same shape as the slash palette, because it answers the same question in the
 * same place: what can I type here. Each row is a name, what that agent is for,
 * and the model it is assigned when the settings assign one — which is the
 * thing worth knowing before handing it a task.
 */

import React from "react";
import { Box, Text } from "ink";

import { candidateTag, type AgentCandidate } from "../state/agent-completion.js";

export function AgentPalette({
  candidates,
  selectedIndex = 0,
  maxRows = 8,
  prefix = "",
}: {
  candidates: AgentCandidate[];
  selectedIndex?: number;
  maxRows?: number;
  /** What has been typed so far; shown when nothing matches it. */
  prefix?: string;
}): React.ReactElement | null {
  if (candidates.length === 0) {
    if (prefix.length === 0) return null;
    return (
      <Box borderStyle="round" borderColor="magenta" paddingX={1}>
        <Text dimColor>{`no agent starts with "${prefix}"`}</Text>
      </Box>
    );
  }

  const start = Math.max(0, Math.min(selectedIndex - maxRows + 1, candidates.length - maxRows));
  const shown = candidates.slice(Math.max(0, start), Math.max(0, start) + maxRows);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="magenta" paddingX={1}>
      {shown.map((candidate) => {
        const active = candidates.indexOf(candidate) === selectedIndex;
        const tag = candidateTag(candidate);
        return (
          <Text key={candidate.name} inverse={active} wrap="truncate-end">
            <Text color="magenta">{candidate.name}</Text>
            <Text dimColor>{candidate.description ? ` ${candidate.description}` : ""}</Text>
            {tag ? <Text dimColor>{` [${tag}]`}</Text> : null}
          </Text>
        );
      })}
      <Text dimColor>↑/↓ select · Tab or Enter accept · Esc close</Text>
    </Box>
  );
}
