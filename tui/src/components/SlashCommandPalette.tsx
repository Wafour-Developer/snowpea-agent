/**
 * Autocomplete popup shown while the input starts with `/`.
 * Entries come from `command.list` only — the TUI has no command table.
 */

import React from "react";
import { Box } from "ink";

import { currentAccent } from "../layout/palette.js";
import { choiceHint } from "../hooks/useChoiceKeys.js";
import type { SlashCompletion } from "../state/slash-completion.js";
import { ChoiceList } from "./ChoiceList.js";

export function SlashCommandPalette({
  commands,
  selectedIndex = 0,
  maxRows = 8,
}: {
  commands: SlashCompletion[];
  selectedIndex?: number;
  maxRows?: number;
}): React.ReactElement | null {
  if (commands.length === 0) return null;
  const hasPreview = commands.some((command) => Boolean(command.preview));
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={currentAccent()} paddingX={1}>
      <ChoiceList
        options={commands.map((command) => ({
          label: `/${command.name}`,
          description: command.summary,
          preview: command.preview,
        }))}
        selectedIndex={selectedIndex}
        color={currentAccent()}
        windowSize={maxRows}
        descriptionMode="inline"
        hint={choiceHint({ enter: "select", digits: false, extra: ["Tab complete"] })}
        showPreview={hasPreview}
      />
    </Box>
  );
}
