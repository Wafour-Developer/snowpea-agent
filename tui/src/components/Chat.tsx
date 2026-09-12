/**
 * Input line with history. Enter submits. A line starting with `/` goes through
 * the slash registry (→ `command.run`); anything else is `session.prompt`.
 *
 * Ink 5 ships no text input, so the line editor is implemented on `useInput`
 * to avoid pulling another dependency into the bundled artifact.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { CommandInfo } from "../rpc/sdk.js";
import { SlashCommandPalette } from "./SlashCommandPalette.js";

/** VT100 / xterm encodings of F1. */
const F1_SEQUENCES = new Set(["\u001bOP", "\u001b[11~"]);

export interface ChatProps {
  onSubmit: (text: string) => void;
  /** Prompts from previous runs, oldest first; ↑ walks back through them. */
  initialHistory?: string[];
  /** ↓ at the newest entry with nothing drafted: the cursor leaves the input. */
  onFocusDown?: () => void;
  /** Autocomplete candidates for the current input; owner calls registry.complete(). */
  completions: CommandInfo[];
  disabled?: boolean;
  placeholder?: string;
  onChange?: (value: string) => void;
  onInterrupt?: () => void;
  onToggleHelp?: () => void;
}

export function Chat({
  onSubmit,
  initialHistory = [],
  onFocusDown,
  completions,
  disabled = false,
  placeholder = "ask anything, or /command",
  onChange,
  onInterrupt,
  onToggleHelp,
}: ChatProps): React.ReactElement {
  const [value, setValue] = useState("");
  // Seeded from the file on disk, so ↑ reaches prompts from previous runs.
  const [history, setHistory] = useState<string[]>(initialHistory);
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const [selected, setSelected] = useState(0);

  const showPalette = value.startsWith("/") && completions.length > 0;

  const update = (next: string) => {
    setValue(next);
    setSelected(0);
    onChange?.(next);
  };

  useInput(
    (input, key) => {
      if (key.escape) {
        onInterrupt?.();
        return;
      }
      // Ink's Key has no F1; terminals send it as an escape sequence.
      if (F1_SEQUENCES.has(input)) {
        onToggleHelp?.();
        return;
      }

      if (showPalette && (key.upArrow || key.downArrow)) {
        const delta = key.downArrow ? 1 : -1;
        setSelected((i) => (i + delta + completions.length) % completions.length);
        return;
      }
      if (showPalette && key.tab) {
        update(`/${completions[selected].name} `);
        return;
      }

      // Tab (plain or Shift+Tab) never becomes a literal character in the
      // draft: plain Tab has no other binding here, and Shift+Tab is the
      // mode-cycle shortcut handled by the parent's own `useInput`.
      if (key.tab || input === "[Z" || input === "[Z") return;

      if (key.upArrow || key.downArrow) {
        // Down with nothing left to go forward to hands the keyboard to the
        // rows under the input, the way Claude Code does.
        if (key.downArrow && historyIndex === null) {
          onFocusDown?.();
          return;
        }
        if (history.length === 0) {
          if (key.downArrow) onFocusDown?.();
          return;
        }
        const current = historyIndex ?? history.length;
        const next = key.upArrow ? Math.max(0, current - 1) : Math.min(history.length, current + 1);
        setHistoryIndex(next === history.length ? null : next);
        update(next === history.length ? "" : history[next]);
        return;
      }

      if (key.return) {
        const text = value.trim();
        if (text.length === 0) return;
        setHistory((h) => [...h, text]);
        setHistoryIndex(null);
        update("");
        onSubmit(text);
        return;
      }

      if (key.backspace || key.delete) {
        update(value.slice(0, -1));
        return;
      }

      if (key.ctrl || key.meta || input.length === 0) return;
      update(value + input);
    },
    { isActive: !disabled },
  );

  return (
    <Box flexDirection="column">
      {showPalette ? (
        <SlashCommandPalette commands={completions} selectedIndex={selected} />
      ) : null}
      <Box>
        <Text color={disabled ? "gray" : "green"}>{"> "}</Text>
        {value.length === 0 ? (
          <Text dimColor>{placeholder}</Text>
        ) : (
          <Text>{value}</Text>
        )}
        <Text inverse>{" "}</Text>
      </Box>
    </Box>
  );
}
