/**
 * `/resume` and `/sessions`: saved sessions with the agent each belongs to.
 *
 * A plain snowpea session has a blank tag; external CLI sessions carry `[cc]`
 * `[cx]` `[ag]` `[cu]` `[ext]` in one colour and browser sessions `[web]` in
 * another. `e` adds the current folder's external sessions to the same list
 * (the parent remembers the choice), `f` cycles the filter, and a legend names
 * the tags that are on screen.
 */

import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";

import { currentAccent } from "../layout/palette.js";
import { fit } from "../layout/fit.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ExternalSessionRow } from "../rpc/sdk.js";
import {
  filterChoices,
  filterLabel,
  legendLine,
  nextFilter,
  tagCell,
  tagColumnWidth,
  type AgentKind,
} from "../state/agent-tag.js";
import type { SessionRecord } from "../state/history.js";
import {
  applyFilter,
  externalToggleLabel,
  itemText,
  pickerItems,
} from "../state/session-picker.js";
import { ChoiceList, type ChoiceOption } from "./ChoiceList.js";

export interface SessionPickerProps {
  records: SessionRecord[];
  /** The current folder's external sessions; only listed while `includeExternal` is on. */
  externals: ExternalSessionRow[];
  /** True while the external list is being fetched. */
  externalsLoading?: boolean;
  /** Why the external list could not be read. */
  externalsError?: string | null;
  includeExternal: boolean;
  onToggleExternal: () => void;
  workdir: string;
  showElsewhere: boolean;
  onRevealElsewhere: () => void;
  onOpenSession: (sessionId: string) => void;
  onOpenExternal: (row: ExternalSessionRow) => void;
  onCancel: () => void;
  width: number;
  windowSize: number;
  now?: number;
}

/** Rows the picker draws around the list: header, legend and hint. */
export const SESSION_PICKER_CHROME_ROWS = 3;

export function SessionPicker({
  records,
  externals,
  externalsLoading = false,
  externalsError = null,
  includeExternal,
  onToggleExternal,
  workdir,
  showElsewhere,
  onRevealElsewhere,
  onOpenSession,
  onOpenExternal,
  onCancel,
  width,
  windowSize,
  now = Date.now(),
}: SessionPickerProps): React.ReactElement {
  const [filter, setFilter] = useState<"all" | AgentKind>("all");
  const [index, setIndex] = useState(0);

  const view = pickerItems(records, includeExternal ? externals : [], workdir, showElsewhere);
  const identities = view.items.map((item) => item.identity);
  // A filter whose kind has left the list (external hidden again) falls back to all.
  const active = filterChoices(identities).includes(filter) ? filter : "all";
  const shown = applyFilter(view.items, active);
  const column = tagColumnWidth(shown.map((item) => item.identity));
  // Cursor, tag column, label padding: the cells the list itself uses.
  const textWidth = Math.max(16, width - 6 - (column > 0 ? column : 0));

  const options: ChoiceOption[] = shown.map((item) => ({
    tag: column > 0 ? tagCell(item.identity, column) : undefined,
    tagColor: item.identity.color,
    label: itemText(item, textWidth, now),
  }));
  const revealAt = options.length;
  if (active === "all" && view.hiddenElsewhere > 0) {
    options.push({ label: `▸ Sessions from other directories (${view.hiddenElsewhere})…` });
  }
  const cancelAt = options.length;
  options.push({ label: "Cancel" });

  useEffect(() => {
    setIndex((at) => Math.min(at, options.length - 1));
  }, [options.length]);

  const choose = (at: number): void => {
    if (at === cancelAt) return onCancel();
    if (at === revealAt && at < cancelAt) return onRevealElsewhere();
    const item = shown[at];
    if (!item) return;
    if (item.external) onOpenExternal(item.external);
    else if (item.session) onOpenSession(item.session.sessionId);
  };

  useChoiceKeys({
    count: options.length,
    index,
    onIndex: setIndex,
    onEnter: choose,
    onCancel,
    onDigit: setIndex,
    digitLimit: shown.length,
    shortcuts: {
      e: onToggleExternal,
      f: () => {
        setFilter(nextFilter(active, identities));
        setIndex(0);
      },
    },
    vim: false,
  });

  const legend = legendLine(shown.map((item) => item.identity), width - 2);
  const header = [
    `filter: ${filterLabel(active)}`,
    externalsLoading ? "external sessions: loading…" : externalsError ? `external: ${externalsError}` : externalToggleLabel(includeExternal),
  ].join(" · ");
  const canFilter = filterChoices(identities).length > 1;

  return (
    <Box flexDirection="column">
      <Text dimColor wrap="truncate-end">{fit(header, width - 2)}</Text>
      <ChoiceList
        options={options}
        selectedIndex={index}
        color={currentAccent()}
        windowSize={windowSize}
        hint={null}
      />
      {legend ? <Text dimColor wrap="truncate-end">{legend}</Text> : null}
      <Text dimColor wrap="truncate-end">
        {fit(
          choiceHint({
            enter: "resume",
            cancel: "cancel",
            extra: ["e external", canFilter ? "f filter" : ""],
          }),
          width - 2,
        )}
      </Text>
    </Box>
  );
}
