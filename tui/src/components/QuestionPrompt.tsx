/**
 * The `ask_user` picker: the agent's question, answered with the arrow keys.
 *
 * This is what the tool exists for. An agent that writes "(a) three.js (b) raw
 * WebGL2" into the transcript has not asked a question, it has hoped the user
 * types the right letter back. Here the options are rows, the cursor is always
 * visible, and Enter takes the highlighted one — the same contract
 * `ConfirmMenu` makes for approvals.
 *
 * It is not `ConfirmMenu` itself because a question needs three things an
 * approval does not: multi-select (Space toggles, Enter submits the set), a
 * free-text "기타 / Other…" row that turns into a one-line input, and a preview
 * pane beside the list for an option easier shown than told.
 *
 * While it is up it owns the keyboard, so nothing leaks into the chat draft.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { QuestionEntry } from "../state/store.js";

/** What the caller sends back to the daemon. */
export interface QuestionAnswer {
  selected: string[];
  text: string | null;
}

/** The free-text row, when `allowOther` is set. Not an option: it has no label. */
export const OTHER_LABEL = "기타 / Other…";

export interface QuestionPromptProps {
  request: QuestionEntry;
  onAnswer: (answer: QuestionAnswer) => void;
  isActive?: boolean;
}

/** Rows the prompt occupies, so the full-screen layout can reserve them. */
export function questionPromptRows(request: QuestionEntry): number {
  const options = request.options ?? [];
  const described = options.filter((option) => option.description).length;
  const other = request.allowOther === false ? 0 : 1;
  // border ×2 + header + question + rows + hint
  return 4 + options.length + described + other + 1;
}

export function QuestionPrompt({
  request,
  onAnswer,
  isActive = true,
}: QuestionPromptProps): React.ReactElement {
  const options = request.options ?? [];
  const allowOther = request.allowOther !== false;
  const multi = request.multi === true;
  const rows = allowOther ? options.length + 1 : options.length;

  const [index, setIndex] = useState(0);
  const [chosen, setChosen] = useState<Set<number>>(() => new Set());
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");

  const otherRow = allowOther ? options.length : -1;

  const submit = (): void => {
    if (index === otherRow && !multi) {
      setTyping(true);
      return;
    }
    if (multi) {
      const picked = [...chosen].sort((a, b) => a - b).map((at) => options[at]?.label ?? "");
      const wanted = picked.filter((label) => label.length > 0);
      // Nothing ticked means the cursor row is what they meant; a picker that
      // answers "nothing" on Enter is a picker that wasted the user's time.
      if (wanted.length === 0 && index !== otherRow) {
        onAnswer({ selected: [options[index]?.label ?? ""], text: null });
        return;
      }
      if (index === otherRow || wanted.length === 0) {
        setTyping(true);
        return;
      }
      onAnswer({ selected: wanted, text: null });
      return;
    }
    if (options.length === 0) {
      setTyping(true);
      return;
    }
    onAnswer({ selected: [options[index]?.label ?? ""], text: null });
  };

  useInput(
    (input, key) => {
      if (typing) {
        if (key.return) {
          const text = draft.trim();
          const picked = multi
            ? [...chosen].sort((a, b) => a - b).map((at) => options[at]?.label ?? "")
            : [];
          onAnswer({ selected: picked.filter(Boolean), text: text.length > 0 ? text : null });
          return;
        }
        if (key.escape) {
          setTyping(false);
          setDraft("");
          return;
        }
        if (key.backspace || key.delete) {
          setDraft((value) => value.slice(0, -1));
          return;
        }
        if (input && !key.ctrl && !key.meta) setDraft((value) => value + input);
        return;
      }

      if (key.escape) {
        // Esc is a real answer: "I am not choosing." The tool reports it as
        // declined rather than letting the turn guess.
        onAnswer({ selected: [], text: null });
        return;
      }
      if (rows > 0 && (key.upArrow || input === "k")) {
        setIndex((at) => (at + rows - 1) % rows);
        return;
      }
      if (rows > 0 && (key.downArrow || key.tab || input === "j")) {
        setIndex((at) => (at + 1) % rows);
        return;
      }
      if (input === " " && multi && index !== otherRow) {
        setChosen((current) => {
          const next = new Set(current);
          if (next.has(index)) next.delete(index);
          else next.add(index);
          return next;
        });
        return;
      }
      if (key.return) {
        submit();
        return;
      }
      // 1–9 jumps straight to a row; with multi it ticks it instead, because
      // the point of the number keys is to answer without aiming.
      if (/^[1-9]$/.test(input)) {
        const at = Number(input) - 1;
        if (at < options.length) {
          setIndex(at);
          if (multi) {
            setChosen((current) => {
              const next = new Set(current);
              if (next.has(at)) next.delete(at);
              else next.add(at);
              return next;
            });
          } else {
            onAnswer({ selected: [options[at].label], text: null });
          }
        }
        return;
      }
      // Anything else is swallowed; a prompt that is up owns the keyboard.
    },
    { isActive },
  );

  const counter = (request.total ?? 1) > 1 ? ` (${request.index ?? 1}/${request.total})` : "";
  const preview = options[index]?.preview ?? "";
  const hint = multi
    ? "↑↓ move · Space toggle · Enter confirm · 1-9 pick · Esc cancel"
    : "↑↓ move · Enter choose · 1-9 pick · Esc cancel";

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        {request.header ? `${request.header}${counter}` : `질문 / Question${counter}`}
      </Text>
      <Text wrap="wrap">{request.question}</Text>
      <Box flexDirection="row" marginTop={1}>
        <Box flexDirection="column" flexGrow={1}>
          {options.map((option, at) => {
            const selected = at === index;
            const ticked = multi && chosen.has(at);
            const box = multi ? (ticked ? "[x] " : "[ ] ") : "";
            return (
              <Box key={`${request.requestId}-o${at}`} flexDirection="column">
                <Box>
                  <Text color={selected ? "cyan" : undefined} bold={selected}>
                    {selected ? "❯ " : "  "}
                  </Text>
                  <Text inverse={selected} color={selected ? "cyan" : undefined} dimColor={!selected}>
                    {` ${at + 1}. ${box}${option.label} `}
                  </Text>
                </Box>
                {option.description ? (
                  <Text dimColor>{`      ${option.description}`}</Text>
                ) : null}
              </Box>
            );
          })}
          {allowOther ? (
            <Box>
              <Text color={index === otherRow ? "cyan" : undefined} bold={index === otherRow}>
                {index === otherRow ? "❯ " : "  "}
              </Text>
              <Text
                inverse={index === otherRow}
                color={index === otherRow ? "cyan" : undefined}
                dimColor={index !== otherRow}
              >
                {` ${OTHER_LABEL} `}
              </Text>
            </Box>
          ) : null}
        </Box>
        {preview ? (
          <Box flexDirection="column" marginLeft={2} borderStyle="single" borderColor="gray" paddingX={1}>
            {preview.split("\n").map((line, at) => (
              <Text key={`${request.requestId}-p${at}`} dimColor wrap="truncate-end">
                {line}
              </Text>
            ))}
          </Box>
        ) : null}
      </Box>
      {typing ? (
        <Box marginTop={1}>
          <Text color="cyan">{"› "}</Text>
          <Text>{draft}</Text>
          <Text inverse> </Text>
          <Text dimColor>{"  Enter to send · Esc to go back"}</Text>
        </Box>
      ) : (
        <Text dimColor>{hint}</Text>
      )}
    </Box>
  );
}
