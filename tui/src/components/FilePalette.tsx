/**
 * Autocomplete popup shown while the caret is inside an '@' file reference.
 * Reuses ChoiceList, formatted similarly to SlashCommandPalette.
 */

import React from "react";
import { Box, Text } from "ink";

import { choiceHint } from "../hooks/useChoiceKeys.js";
import { ChoiceList, type ChoiceOption } from "./ChoiceList.js";
import { formatSize } from "../state/attachments.js";
import type { FileCompletionEntry } from "../state/fileRefs.js";

export interface FilePaletteProps {
  entries: readonly FileCompletionEntry[];
  selectedIndex?: number;
  maxRows?: number;
  truncated?: boolean;
}

export function FilePalette({
  entries,
  selectedIndex = 0,
  maxRows = 8,
  truncated = false,
}: FilePaletteProps): React.ReactElement | null {
  if (entries.length === 0) return null;

  const options: ChoiceOption[] = entries.map((entry) => {
    const isDir = entry.kind === "dir" || entry.path.endsWith("/");
    const label = isDir && !entry.path.endsWith("/") ? `${entry.path}/` : entry.path;
    const description = !isDir && entry.size != null ? formatSize(entry.size) : undefined;
    return {
      label,
      description,
    };
  });

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      <ChoiceList
        options={options}
        selectedIndex={selectedIndex}
        color="cyan"
        windowSize={maxRows}
        descriptionMode="inline"
        hint={choiceHint({ enter: "select", digits: false, extra: ["Tab complete"] })}
        showPreview={false}
      />
      {truncated ? <Text dimColor>… more</Text> : null}
    </Box>
  );
}
