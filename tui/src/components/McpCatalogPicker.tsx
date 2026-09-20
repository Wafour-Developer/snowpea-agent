/**
 * `/mcp catalog`: the curated presets, as a list with a cursor on it.
 *
 * The daemon's own listing prints ids you then have to type back into
 * `--preset`. Picking a row here opens the add form already filled in with the
 * preset's command and the variables it needs (M14 §3, §5).
 */

import React, { useState } from "react";
import { Box, Text } from "ink";

import { currentAccent } from "../layout/palette.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { McpCatalogEntry } from "../state/mcp.js";
import { ChoiceList } from "./ChoiceList.js";

/** Rows shown at once; the list scrolls inside this. */
export const CATALOG_ROWS = 8;

export interface McpCatalogPickerProps {
  entries: readonly McpCatalogEntry[];
  onChoose: (entry: McpCatalogEntry) => void;
  onCancel: () => void;
  isActive?: boolean;
  width: number;
}

export function McpCatalogPicker({
  entries,
  onChoose,
  onCancel,
  isActive = true,
  width,
}: McpCatalogPickerProps): React.ReactElement {
  const [index, setIndex] = useState(0);

  useChoiceKeys({
    count: entries.length,
    index,
    onIndex: setIndex,
    onEnter: (row) => {
      if (entries.length > 0) onChoose(entries[row]);
    },
    onCancel,
    onTab: () => {
      if (entries.length > 0) setIndex((i) => (i + 1) % entries.length);
    },
    isActive,
  });

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        MCP catalog
      </Text>
      {entries.length === 0 ? (
        <>
          <Text dimColor>this daemon ships no presets</Text>
          <Text dimColor>{choiceHint({ enter: "fills the add form", digits: false })}</Text>
        </>
      ) : (
        <ChoiceList
          options={entries.map((entry) => ({
            label: entry.label,
            description: `${entry.description}${entry.needs.length > 0 ? `  needs ${entry.needs.join(", ")}` : ""}`,
          }))}
          selectedIndex={index}
          color={currentAccent()}
          windowSize={CATALOG_ROWS}
          descriptionMode="inline"
          hint={choiceHint({ enter: "fills the add form" })}
        />
      )}
    </Box>
  );
}
