/**
 * `/external`: the sessions of the working directory's external CLIs.
 *
 * Typing searches; the keys that would otherwise be letters are Tab (this
 * folder / all projects), Ctrl+N (new session) and Ctrl+L (older sessions).
 * Each row says which CLI, the title, its status and where its process lives —
 * "other terminal" rows open view-only.
 */

import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";

import { currentAccent } from "../layout/palette.js";
import { fit } from "../layout/fit.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ExternalCliInfo, ExternalSessionRow } from "../rpc/sdk.js";
import { PENDING_PATH_ROWS, pendingPathLines, pickerLabel, searchRows } from "../state/external.js";
import { ChoiceList } from "./ChoiceList.js";

/** Rows shown at once. */
export const EXTERNAL_PICKER_ROWS = 8;

/** Rows the picker occupies at most, so the full-screen layout can reserve them. */
export const EXTERNAL_PICKER_TOTAL_ROWS = 2 + 1 + 1 + EXTERNAL_PICKER_ROWS + 2 + 1 + 1 + PENDING_PATH_ROWS;

export interface ExternalPickerProps {
  rows: ExternalSessionRow[];
  /** The folder being listed, for the header. */
  folder: string;
  allProjects: boolean;
  loading: boolean;
  /** An older page can be fetched. */
  hasMore: boolean;
  /** Shown when the list could not be read. */
  error?: string | null;
  /** CLIs that cannot be started, e.g. `Codex (not installed)`. */
  unavailable?: string[];
  /** CLIs whose configured path is not confirmed yet (`pendingPath`). */
  pending?: ExternalCliInfo[];
  width: number;
  onOpen: (row: ExternalSessionRow) => void;
  onNew: () => void;
  onToggleAll: () => void;
  onMore: () => void;
  onCancel: () => void;
  isActive?: boolean;
}

export function ExternalPicker({
  rows,
  folder,
  allProjects,
  loading,
  hasMore,
  error = null,
  unavailable = [],
  pending = [],
  width,
  onOpen,
  onNew,
  onToggleAll,
  onMore,
  onCancel,
  isActive = true,
}: ExternalPickerProps): React.ReactElement {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const shown = searchRows(rows, query);

  useEffect(() => {
    setIndex((at) => Math.min(at, Math.max(0, shown.length - 1)));
  }, [shown.length]);

  useChoiceKeys({
    count: shown.length,
    index,
    onIndex: setIndex,
    onEnter: (at) => {
      if (shown[at]) onOpen(shown[at]);
    },
    onCancel: () => {
      // Esc clears a search first, then closes.
      if (query) {
        setQuery("");
        return;
      }
      onCancel();
    },
    digits: false,
    vim: false,
    onKey: (input, key) => {
      if (key.tab) {
        onToggleAll();
        return true;
      }
      if (key.ctrl && input === "n") {
        onNew();
        return true;
      }
      if (key.ctrl && input === "l") {
        onMore();
        return true;
      }
      if (key.backspace || key.delete) {
        setQuery((value) => value.slice(0, -1));
        return true;
      }
      if (input && !key.ctrl && !key.meta && !key.upArrow && !key.downArrow && !key.return && !key.escape) {
        if (input === " " && query === "") return true;
        setQuery((value) => value + input);
        return true;
      }
      return false;
    },
    isActive,
  });

  const inner = Math.max(20, width - 8);
  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="blue" paddingX={1}>
      <Text bold color="blue" wrap="truncate-end">
        {fit(`External sessions · ${allProjects ? "all projects" : folder}`, width - 4)}
      </Text>
      <Text wrap="truncate-end">
        <Text dimColor>search: </Text>
        <Text>{fit(query, Math.max(1, width - 16))}</Text>
        <Text inverse> </Text>
      </Text>
      {error ? (
        <Text color="red" wrap="truncate-end">{fit(error, width - 4)}</Text>
      ) : shown.length === 0 ? (
        <Text dimColor wrap="truncate-end">
          {loading
            ? "loading…"
            : rows.length > 0
              ? "no session matches the search"
              : "no sessions here yet — Ctrl+N starts one"}
        </Text>
      ) : (
        <ChoiceList
          options={shown.map((row) => ({
            label: pickerLabel(row, inner, allProjects),
            danger: false,
          }))}
          selectedIndex={index}
          color={currentAccent()}
          windowSize={EXTERNAL_PICKER_ROWS}
          hint={null}
        />
      )}
      {unavailable.length > 0 ? (
        <Text dimColor wrap="truncate-end">{fit(`unavailable: ${unavailable.join(", ")}`, width - 4)}</Text>
      ) : null}
      {pendingPathLines(pending, width - 4).map((line, at) => (
        <Text key={`pending-${at}`} color="yellow" wrap="truncate-end">{line}</Text>
      ))}
      <Text dimColor wrap="truncate-end">
        {fit(
          `${choiceHint({ enter: "open", digits: false, cancel: query ? "clear search" : "close", extra: ["Tab all projects", "Ctrl+N new", hasMore ? "Ctrl+L older" : ""] })}`,
          width - 4,
        )}
      </Text>
    </Box>
  );
}
