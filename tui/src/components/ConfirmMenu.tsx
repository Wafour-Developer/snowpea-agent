/**
 * A list of choices with a cursor on it, confirmed with Enter.
 *
 * Every yes/no question in the TUI goes through this: the tool approval, the
 * update confirmation, anything else that needs a decision. A prompt that can
 * only be answered by guessing which letter it wants is the thing this exists
 * to stop, so Enter always takes the highlighted row and the row is always
 * visible.
 *
 * It is now a thin shell over {@link ChoiceList} and {@link useChoiceKeys}, so
 * it moves, numbers and cancels exactly like every other picker (M15b §2).
 *
 * While it is up it owns the keyboard. The caller disables the chat line, and
 * the shortcut letters are matched here so they cannot leak into the draft.
 */

import React, { useState } from "react";
import { Box } from "ink";

import { currentAccent } from "../layout/palette.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import { ChoiceList, type ChoiceOption } from "./ChoiceList.js";

export interface ConfirmOption<T> {
  /** What the row says. */
  label: string;
  /** What choosing it means to the caller. */
  value: T;
  /** Single-key shortcut, matched case-insensitively. */
  shortcut?: string;
  /** Drawn in red; used for the refusing option. */
  danger?: boolean;
  /** Extra words under the row, for options that need explaining. */
  hint?: string;
}

export interface ConfirmMenuProps<T> {
  options: ConfirmOption<T>[];
  onChoose: (value: T) => void;
  /** Row the cursor starts on. */
  initialIndex?: number;
  /** Escape and Ctrl+C pick this one, when there is one. */
  escapeValue?: T;
  isActive?: boolean;
  /**
   * Rows drawn at once. Without it every option is drawn, which is right for a
   * handful of choices and wrong for a list of saved sessions: past the height
   * of the terminal the top of the menu scrolled away and could not be reached.
   */
  windowSize?: number;
}

/** Rows the menu occupies, so the full-screen layout can reserve them. */
export function confirmMenuRows(options: { hint?: string }[]): number {
  return options.reduce((total, option) => total + (option.hint ? 2 : 1), 0) + 1;
}

export function ConfirmMenu<T>({
  options,
  onChoose,
  initialIndex = 0,
  escapeValue,
  isActive = true,
  windowSize,
}: ConfirmMenuProps<T>): React.ReactElement {
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(0, initialIndex), Math.max(0, options.length - 1)),
  );

  const shortcuts: Record<string, () => void> = {};
  for (const option of options) {
    if (option.shortcut) shortcuts[option.shortcut.toLowerCase()] = () => onChoose(option.value);
  }

  useChoiceKeys({
    count: options.length,
    index,
    onIndex: setIndex,
    onEnter: (at) => {
      if (options.length > 0) onChoose(options[at].value);
    },
    onCancel: () => {
      if (escapeValue !== undefined) onChoose(escapeValue);
    },
    // Tab has always stepped down this menu; keeping it costs nothing and
    // people who learned it would notice its absence.
    onTab: () => {
      if (options.length > 0) setIndex((i) => (i + 1) % options.length);
    },
    onDigit: setIndex,
    shortcuts,
    // The shortcut letters own the alphabet here, so j/k would be ambiguous.
    vim: false,
    isActive,
  });

  const rows: ChoiceOption[] = options.map((option) => ({
    label: option.label,
    description: option.hint,
    shortcut: option.shortcut,
    danger: option.danger,
  }));

  return (
    <Box flexDirection="column">
      <ChoiceList
        options={rows}
        selectedIndex={index}
        color={currentAccent()}
        windowSize={windowSize}
        hint={choiceHint({
          enter: "confirm",
          extra: windowSize && rows.length > windowSize ? ["PgUp/PgDn page"] : [],
        })}
      />
    </Box>
  );
}
