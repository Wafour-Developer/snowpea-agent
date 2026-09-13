/**
 * Input line with history. Enter submits. A line starting with `/` goes through
 * the slash registry (→ `command.run`); anything else is `session.prompt`.
 *
 * Ink 5 ships no text input, so the line editor is implemented on `useInput`
 * to avoid pulling another dependency into the bundled artifact.
 */

import React, { useEffect, useState } from "react";
import { Box, Text, useInput } from "ink";

import type { CommandInfo } from "../rpc/sdk.js";
import { SlashCommandPalette } from "./SlashCommandPalette.js";

export interface ChatProps {
  onSubmit: (text: string) => void;
  /** Prompts from previous runs, oldest first; ↑ walks back through them. */
  initialHistory?: string[];
  /** ↓ at the newest entry with nothing drafted: the cursor leaves the input. */
  onFocusDown?: () => void;
  /** Paths pasted or dropped into the input; the owner turns them into chips. */
  onPaste?: (text: string) => boolean;
  /** Backspace on an empty input takes the newest chip off. */
  onBackspaceEmpty?: () => boolean;
  /** Ctrl+X clears every chip. */
  onClearAttachments?: () => void;
  /** Ctrl+Space starts or stops a recording while voice input is armed. */
  onToggleRecording?: () => void;
  /** Ctrl+V with nothing pasteable as text: try an image from the clipboard. */
  onClipboard?: () => void;
  /** Text to put in the draft, e.g. what a recording transcribed to. */
  insert?: string | null;
  /** Called once `insert` has been taken, so it is not applied twice. */
  onInserted?: () => void;
  /** Literal text typed while focus was on a row below the input. */
  append?: string | null;
  /** Called once `append` has been restored to the draft. */
  onAppended?: () => void;
  /**
   * `R` on an untouched input reopens the session the launch screen offered.
   *
   * Handled here rather than in the app so the character never lands in the
   * draft, and offered only while the input is empty and nothing has been said
   * yet — after that, R is just a letter.
   */
  onQuickResume?: () => void;
  /** U on empty input opens update confirmation without entering the draft. */
  onQuickUpdate?: () => void;
  /** Autocomplete candidates for the current input; owner calls registry.complete(). */
  completions: CommandInfo[];
  disabled?: boolean;
  placeholder?: string;
  onChange?: (value: string) => void;
  onInterrupt?: () => void;
}

export function Chat({
  onSubmit,
  initialHistory = [],
  onFocusDown,
  onQuickResume,
  onQuickUpdate,
  onPaste,
  onBackspaceEmpty,
  onClearAttachments,
  onToggleRecording,
  onClipboard,
  insert = null,
  onInserted,
  append = null,
  onAppended,
  completions,
  disabled = false,
  placeholder = "ask anything, or /command",
  onChange,
  onInterrupt,
}: ChatProps): React.ReactElement {
  const [value, setValue] = useState("");
  // Seeded from the file on disk, so ↑ reaches prompts from previous runs.
  const [history, setHistory] = useState<string[]>(initialHistory);
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const [selected, setSelected] = useState(0);

  const showPalette = value.startsWith("/") && completions.length > 0;

  // Text produced elsewhere — a transcription, for now — lands in the draft for
  // the user to read before it is sent.
  useEffect(() => {
    if (!insert) return;
    update(value.length > 0 ? `${value} ${insert}` : insert);
    onInserted?.();
    // Only a new `insert` matters; the draft it is appended to is read live.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insert]);

  useEffect(() => {
    if (!append) return;
    update(value + append);
    onAppended?.();
    // Only a new `append` matters; the current draft is read live.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [append]);

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
        if (showPalette && !value.includes(" ")) {
          const completion = completions[selected];
          if (completion && value !== `/${completion.name}`) {
            update(`/${completion.name} `);
            return;
          }
        }
        const text = value.trim();
        if (text.length === 0) return;
        setHistory((h) => [...h, text]);
        setHistoryIndex(null);
        update("");
        onSubmit(text);
        return;
      }

      if (key.backspace || key.delete) {
        // With nothing typed, backspace takes the newest attachment off instead
        // of doing nothing at all.
        if (value.length === 0 && onBackspaceEmpty?.()) return;
        update(value.slice(0, -1));
        return;
      }

      if (key.ctrl && input === "v") {
        onClipboard?.();
        return;
      }

      if (key.ctrl && input === "x") {
        onClearAttachments?.();
        return;
      }

      // Ctrl+Space is a NUL byte on the wire; Ink's key parser turns that into
      // ctrl + "`", which is the form that actually arrives here.
      if (key.ctrl && (input === " " || input === "`")) {
        onToggleRecording?.();
        return;
      }

      if (key.ctrl || key.meta || input.length === 0) return;
      // A paste arrives as one chunk: if it names files, it becomes chips
      // rather than a wall of text in the draft.
      if (input.length > 1 && onPaste?.(input)) return;
      if (input === "U" && value.length === 0 && onQuickUpdate) {
        onQuickUpdate();
        return;
      }
      if (input === "R" && value.length === 0 && onQuickResume) {
        onQuickResume();
        return;
      }
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
