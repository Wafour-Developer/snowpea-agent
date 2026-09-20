/**
 * Input line with history. Enter submits. A line starting with `/` goes through
 * the slash registry (→ `command.run`); anything else is `session.prompt`.
 *
 * Ink 5 ships no text input, so the line editor is implemented on `useInput`
 * to avoid pulling another dependency into the bundled artifact.
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";

import { currentAccent } from "../layout/palette.js";
import type { CommandInfo } from "../rpc/sdk.js";
import {
  agentQuery,
  applyAgentCompletion,
  filterAgents,
  type AgentCandidate,
} from "../state/agent-completion.js";
import {
  backspace,
  del,
  down,
  end,
  home,
  insert as insertText,
  layout as layoutEditor,
  left,
  right,
  up,
  wordLeft,
  wordRight,
  type EditorState,
} from "../state/editor.js";
import { shouldShowSlashPalette } from "../state/slash-completion.js";
import {
  findFileRefToken,
  applyFileCompletion,
  type FileCompletionEntry,
} from "../state/fileRefs.js";
import { AgentPalette } from "./AgentPalette.js";
import { FilePalette } from "./FilePalette.js";
import { SlashCommandPalette } from "./SlashCommandPalette.js";
import type { FileCompleteResult } from "../rpc/client.js";

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
  /** Pressing R when daemon is not running starts daemon. */
  onStartDaemon?: () => void;
  /** U on empty input opens update confirmation without entering the draft. */
  onQuickUpdate?: () => void;
  /** Autocomplete candidates for the current input; owner calls registry.complete(). */
  completions: CommandInfo[];
  /**
   * Every agent that could be named, for `/delegate <agent>` and `$agent`.
   *
   * The list is unfiltered: the draft decides which of them are offered, and
   * the draft lives here.
   */
  agents?: AgentCandidate[];
  /** Cells available to the draft, excluding the `> ` prompt. */
  draftWidth?: number;
  disabled?: boolean;
  placeholder?: string;
  onChange?: (value: string) => void;
  onInterrupt?: () => void;
  /** Function to complete file paths (calls file.complete via RPC). */
  onFileComplete?: (query: string) => Promise<FileCompleteResult>;
  /** Debounce milliseconds for file completion (defaults to 80). */
  fileCompleteDebounceMs?: number;
}

export function Chat({
  onSubmit,
  initialHistory = [],
  onFocusDown,
  onQuickResume,
  onStartDaemon,
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
  agents = [],
  draftWidth,
  disabled = false,
  placeholder = "ask anything, or /command",
  onChange,
  onInterrupt,
  onFileComplete,
  fileCompleteDebounceMs = 80,
}: ChatProps): React.ReactElement {
  const [editor, setEditor] = useState<EditorState>({ text: "", cursor: 0 });
  const value = editor.text;
  const cursor = editor.cursor;
  const { stdout } = useStdout();
  const wrapWidth = Math.max(
    1,
    Math.floor(draftWidth ?? Math.max(1, (stdout?.columns ?? 80) - 2)),
  );
  const draft = useMemo(
    () => layoutEditor(value, wrapWidth, cursor),
    [value, wrapWidth, cursor],
  );
  const stickyColumn = useRef<number | null>(null);
  // Seeded from the file on disk, so ↑ reaches prompts from previous runs.
  const [history, setHistory] = useState<string[]>(initialHistory);
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  /** Draft present before ↑ entered history; ↓ past the newest entry restores it. */
  const historyDraft = useRef("");
  const [selected, setSelected] = useState(0);
  /** True while Esc has closed the agent list for the name being typed. */
  const [agentsDismissed, setAgentsDismissed] = useState(false);

  // File completion state
  const [fileCompletions, setFileCompletions] = useState<FileCompletionEntry[]>([]);
  const [fileTruncated, setFileTruncated] = useState(false);
  const [selectedFile, setSelectedFile] = useState(0);
  const [dismissedTokenStart, setDismissedTokenStart] = useState<number | null>(null);

  const showPalette = shouldShowSlashPalette(value, completions);

  const fileToken = findFileRefToken(value, cursor);
  const isFileDismissed =
    fileToken !== null && dismissedTokenStart === fileToken.start;
  const showFilePopup =
    fileToken !== null && !isFileDismissed && !showPalette;

  useEffect(() => {
    if (dismissedTokenStart !== null) {
      if (!fileToken || fileToken.start !== dismissedTokenStart) {
        setDismissedTokenStart(null);
      }
    }
  }, [fileToken, dismissedTokenStart]);

  useEffect(() => {
    if (!showFilePopup || !onFileComplete) {
      setFileCompletions([]);
      setFileTruncated(false);
      setSelectedFile(0);
      return;
    }

    const query = fileToken.query;
    let cancelled = false;

    const timer = setTimeout(() => {
      onFileComplete(query)
        .then((result) => {
          if (cancelled) return;
          setFileCompletions((result.entries ?? []) as FileCompletionEntry[]);
          setFileTruncated(Boolean(result.truncated));
          setSelectedFile(0);
        })
        .catch(() => {
          if (cancelled) return;
          setFileCompletions([]);
          setFileTruncated(false);
        });
    }, fileCompleteDebounceMs);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [showFilePopup, fileToken?.query, fileToken?.start, onFileComplete, fileCompleteDebounceMs]);

  // `$name` or `/delegate name`: the same list, in the place the name goes.
  const query = agentQuery(value, cursor);
  const agentMatches = query ? filterAgents(agents, query.prefix) : [];
  const showAgents = query !== null && !agentsDismissed && !showPalette && !showFilePopup;

  const update = (next: EditorState, options: { keepColumn?: boolean } = {}) => {
    const safe = {
      text: next.text,
      cursor: Math.max(0, Math.min(next.text.length, next.cursor)),
    };
    setEditor(safe);
    if (!options.keepColumn) stickyColumn.current = null;
    setSelected(0);
    setAgentsDismissed(false);
    onChange?.(safe.text);
  };

  const replace = (text: string, nextCursor = text.length) =>
    update({ text, cursor: nextCursor });

  // Text produced elsewhere — a transcription, for now — lands in the draft for
  // the user to read before it is sent.
  useEffect(() => {
    if (!insert) return;
    const text = value.length > 0 ? ` ${insert}` : insert;
    update(insertText(editor, text));
    onInserted?.();
    // Only a new `insert` matters; the draft it is appended to is read live.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insert]);

  useEffect(() => {
    if (!append) return;
    update(insertText(editor, append));
    onAppended?.();
    // Only a new `append` matters; the current draft is read live.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [append]);

  useInput(
    (input, key) => {
      const keyPlus = key as typeof key & {
        shift?: boolean;
        alt?: boolean;
        home?: boolean;
        end?: boolean;
      };

      if (key.escape) {
        if (showFilePopup) {
          setDismissedTokenStart(fileToken!.start);
          setFileCompletions([]);
          return;
        }
        // Esc closes the agent list first; only an open turn is interrupted.
        if (showAgents) {
          setAgentsDismissed(true);
          return;
        }
        onInterrupt?.();
        return;
      }

      if (showFilePopup && fileCompletions.length > 0) {
        if (key.upArrow || key.downArrow) {
          const delta = key.downArrow ? 1 : -1;
          setSelectedFile((i) => (i + delta + fileCompletions.length) % fileCompletions.length);
          return;
        }
        if (key.tab) {
          const candidate = fileCompletions[Math.min(selectedFile, fileCompletions.length - 1)];
          if (candidate) {
            const next = applyFileCompletion(value, fileToken!, candidate);
            replace(next.text, next.cursor);
            return;
          }
        }
      }

      if (showAgents && agentMatches.length > 0) {
        if (key.upArrow || key.downArrow) {
          const delta = key.downArrow ? 1 : -1;
          setSelected((i) => (i + delta + agentMatches.length) % agentMatches.length);
          return;
        }
        if (key.tab || key.return) {
          const candidate = agentMatches[Math.min(selected, agentMatches.length - 1)];
          const next = applyAgentCompletion(value, query!, candidate.name);
          replace(next.text, next.cursor);
          return;
        }
      }
      if (showPalette && (key.upArrow || key.downArrow)) {
        const delta = key.downArrow ? 1 : -1;
        setSelected((i) => (i + delta + completions.length) % completions.length);
        return;
      }
      if (showPalette && key.tab) {
        replace(`/${completions[selected].name} `);
        return;
      }

      // Tab (plain or Shift+Tab) never becomes a literal character in the
      // draft: plain Tab has no other binding here, and Shift+Tab is the
      // mode-cycle shortcut handled by the parent's own `useInput`.
      if (key.tab || input === "[Z" || input === "[Z") return;

      if ((key.ctrl || key.meta) && key.leftArrow) {
        setHistoryIndex(null);
        update(wordLeft(editor));
        return;
      }

      if ((key.ctrl || key.meta) && key.rightArrow) {
        setHistoryIndex(null);
        update(wordRight(editor));
        return;
      }

      if (key.ctrl && input === "a") {
        setHistoryIndex(null);
        update(home(editor));
        return;
      }

      if (key.ctrl && input === "e") {
        setHistoryIndex(null);
        update(end(editor));
        return;
      }

      if (keyPlus.home) {
        setHistoryIndex(null);
        update(home(editor));
        return;
      }

      if (keyPlus.end) {
        setHistoryIndex(null);
        update(end(editor));
        return;
      }

      if (key.leftArrow || key.rightArrow) {
        setHistoryIndex(null);
        update(key.leftArrow ? left(editor) : right(editor));
        return;
      }

      if (key.upArrow || key.downArrow) {
        const target = stickyColumn.current ?? draft.cursorCol;
        if (key.upArrow && draft.cursorRow > 0) {
          stickyColumn.current = target;
          setHistoryIndex(null);
          update(up(editor, wrapWidth, target), { keepColumn: true });
          return;
        }
        if (key.downArrow && draft.cursorRow < draft.lines.length - 1) {
          stickyColumn.current = target;
          setHistoryIndex(null);
          update(down(editor, wrapWidth, target), { keepColumn: true });
          return;
        }

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
        if (key.upArrow && historyIndex === null) historyDraft.current = value;
        const current = historyIndex ?? history.length;
        const next = key.upArrow ? Math.max(0, current - 1) : Math.min(history.length, current + 1);
        setHistoryIndex(next === history.length ? null : next);
        replace(next === history.length ? historyDraft.current : history[next]);
        return;
      }

      if (key.return && (keyPlus.shift || key.meta || keyPlus.alt)) {
        setHistoryIndex(null);
        update(insertText(editor, "\n"));
        return;
      }

      if (key.return) {
        if (showFilePopup && fileCompletions.length > 0) {
          const candidate = fileCompletions[Math.min(selectedFile, fileCompletions.length - 1)];
          if (candidate) {
            const next = applyFileCompletion(value, fileToken!, candidate);
            replace(next.text, next.cursor);
            return;
          }
        }
        // Enter accepts the highlighted completion while the draft is still a
        // name — including a sub-action like `/skill create`, whose name has
        // a space in it, and including the exact name with no trailing space,
        // which becomes `/name ` so arguments can follow. Only a draft that
        // has been accepted (`/name `) or carries arguments runs on Enter.
        if (showPalette) {
          const completion = completions[selected];
          const full = completion ? `/${completion.name}` : "";
          const trimmed = value.trimEnd();
          if (completion && full.startsWith(trimmed)) {
            const accepted = full === trimmed && value.length > trimmed.length;
            if (!accepted) {
              replace(`${full} `);
              return;
            }
          }
        }
        const text = value.trim();
        if (text.length === 0) return;
        setHistory((h) => [...h, text]);
        setHistoryIndex(null);
        replace("");
        onSubmit(text);
        return;
      }

      if (key.ctrl && input === "d") {
        setHistoryIndex(null);
        update(del(editor));
        return;
      }

      // Most terminals report the Backspace byte (DEL, 0x7f) as `key.delete`.
      // Treat both Ink spellings as backward deletion so attachment removal and
      // editing remain consistent across terminals.
      if (key.backspace || key.delete) {
        // With nothing typed, backspace takes the newest attachment off instead
        // of doing nothing at all.
        if (value.length === 0 && onBackspaceEmpty?.()) return;
        setHistoryIndex(null);
        update(backspace(editor));
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
      const typed = input.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      // A paste arrives as one chunk: if it names files, it becomes chips
      // rather than a wall of text in the draft.
      if (typed.length > 1 && onPaste?.(typed)) return;
      if ((typed === "R" || typed === "r") && onStartDaemon) {
        onStartDaemon();
        return;
      }
      if (typed === "U" && value.length === 0 && onQuickUpdate) {
        onQuickUpdate();
        return;
      }
      if (typed === "R" && value.length === 0 && onQuickResume) {
        onQuickResume();
        return;
      }
      setHistoryIndex(null);
      if (
        fileToken &&
        fileToken.quoted &&
        value[fileToken.end - 1] === '"' &&
        cursor === fileToken.end &&
        typed !== '"'
      ) {
        const before = value.slice(0, cursor - 1);
        const after = value.slice(cursor - 1);
        update({ text: before + typed + after, cursor: cursor - 1 + typed.length });
        return;
      }
      update(insertText(editor, typed));
    },
    { isActive: !disabled },
  );

  const cursorEnd = useMemo(() => right(editor).cursor, [editor]);
  const promptColor = disabled ? "gray" : currentAccent();

  return (
    <Box flexDirection="column">
      {showFilePopup && fileCompletions.length > 0 ? (
        <FilePalette
          entries={fileCompletions}
          selectedIndex={Math.min(selectedFile, Math.max(0, fileCompletions.length - 1))}
          truncated={fileTruncated}
        />
      ) : null}
      {showAgents ? (
        <AgentPalette
          candidates={agentMatches}
          selectedIndex={Math.min(selected, Math.max(0, agentMatches.length - 1))}
          prefix={query?.prefix ?? ""}
        />
      ) : null}
      {showPalette ? (
        <SlashCommandPalette commands={completions} selectedIndex={selected} />
      ) : null}
      {value.length === 0 ? (
        <Box>
          <Text color={promptColor}>{"> "}</Text>
          <Text dimColor>{placeholder}</Text>
          <Text inverse>{" "}</Text>
        </Box>
      ) : (
        <Box flexDirection="column">
          {draft.lines.map((line, row) => {
            const prefix = row === 0 ? "> " : "  ";
            if (row !== draft.cursorRow) {
              return (
                <Box key={`draft-${row}`} flexWrap="nowrap" overflow="hidden">
                  <Text color={promptColor}>{prefix}</Text>
                  <Text wrap="truncate-end">{value.slice(line.start, line.end)}</Text>
                </Box>
              );
            }
            const hasCursorText = cursor >= line.start && cursor < line.end && cursorEnd > cursor;
            const before = value.slice(line.start, cursor);
            const mark = hasCursorText ? value.slice(cursor, cursorEnd) : " ";
            const after = hasCursorText
              ? value.slice(cursorEnd, line.end)
              : value.slice(cursor, line.end);
            // The layout already wrapped by display cells; Ink must not wrap
            // again, or a wide character near the edge would put the caret on
            // a row the model does not know about.
            return (
              <Box key={`draft-${row}`} flexWrap="nowrap" overflow="hidden">
                <Text color={promptColor}>{prefix}</Text>
                <Text>{before}</Text>
                <Text inverse>{mark}</Text>
                <Text wrap="truncate-end">{after}</Text>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
