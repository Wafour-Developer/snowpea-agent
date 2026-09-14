/**
 * `/mcp catalog`: the curated presets, as a list with a cursor on it.
 *
 * The daemon's own listing prints ids you then have to type back into
 * `--preset`. Picking a row here opens the add form already filled in with the
 * preset's command and the variables it needs (M14 §3, §5).
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { McpCatalogEntry } from "../state/mcp.js";

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

  useInput(
    (_input, key) => {
      if (key.escape) {
        onCancel();
        return;
      }
      if (entries.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i + entries.length - 1) % entries.length);
        return;
      }
      if (key.downArrow || key.tab) {
        setIndex((i) => (i + 1) % entries.length);
        return;
      }
      if (key.return) onChoose(entries[index]);
    },
    { isActive },
  );

  const start = Math.max(0, Math.min(index - CATALOG_ROWS + 2, entries.length - CATALOG_ROWS));
  const shown = entries.slice(start, start + CATALOG_ROWS);

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        MCP catalog
      </Text>
      {entries.length === 0 ? (
        <Text dimColor>this daemon ships no presets</Text>
      ) : (
        shown.map((entry) => {
          const active = entries[index] === entry;
          const needs = entry.needs.length > 0 ? `  needs ${entry.needs.join(", ")}` : "";
          return (
            <Text key={entry.id} wrap="truncate-end">
              <Text color={active ? "green" : undefined}>{active ? "❯ " : "  "}</Text>
              <Text inverse={active}>{entry.label}</Text>
              <Text dimColor>{`  ${entry.description}${needs}`}</Text>
            </Text>
          );
        })
      )}
      <Text dimColor>↑↓ move · Enter fills the add form · Esc cancel</Text>
    </Box>
  );
}
