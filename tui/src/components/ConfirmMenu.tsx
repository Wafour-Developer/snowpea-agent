/**
 * A list of choices with a cursor on it, confirmed with Enter.
 *
 * Every yes/no question in the TUI goes through this: the tool approval, the
 * update confirmation, anything else that needs a decision. A prompt that can
 * only be answered by guessing which letter it wants is the thing this exists
 * to stop, so Enter always takes the highlighted row and the row is always
 * visible.
 *
 * While it is up it owns the keyboard. The caller disables the chat line, and
 * the shortcut letters are matched here so they cannot leak into the draft.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

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
}: ConfirmMenuProps<T>): React.ReactElement {
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(0, initialIndex), Math.max(0, options.length - 1)),
  );

  useInput(
    (input, key) => {
      if (options.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i + options.length - 1) % options.length);
        return;
      }
      if (key.downArrow || key.tab) {
        setIndex((i) => (i + 1) % options.length);
        return;
      }
      if (key.return) {
        onChoose(options[index].value);
        return;
      }
      if (key.escape && escapeValue !== undefined) {
        onChoose(escapeValue);
        return;
      }
      const typed = input.toLowerCase();
      const match = options.find((option) => option.shortcut?.toLowerCase() === typed);
      if (match) onChoose(match.value);
      // Anything else is swallowed: a prompt that is up owns the keyboard, and
      // a stray character must never reach the chat draft behind it.
    },
    { isActive },
  );

  return (
    <Box flexDirection="column">
      {options.map((option, at) => {
        const selected = at === index;
        return (
          <Box key={option.label} flexDirection="column">
            <Box>
              <Text color={selected ? (option.danger ? "red" : "green") : undefined} bold={selected}>
                {selected ? "❯ " : "  "}
              </Text>
              <Text
                inverse={selected}
                color={option.danger ? "red" : selected ? "green" : undefined}
                dimColor={!selected && !option.danger}
              >
                {` ${option.label} `}
              </Text>
              {option.shortcut ? <Text dimColor>{`  (${option.shortcut})`}</Text> : null}
            </Box>
            {option.hint ? <Text dimColor>{`     ${option.hint}`}</Text> : null}
          </Box>
        );
      })}
      <Text dimColor>↑↓ move · Enter confirm · Esc cancel</Text>
    </Box>
  );
}
