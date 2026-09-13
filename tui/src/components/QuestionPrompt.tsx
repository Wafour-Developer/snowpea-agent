/**
 * The `ask_user` picker: the agent's questions, answered with the arrow keys.
 *
 * This is what the tool exists for. An agent that writes "(a) three.js (b) raw
 * WebGL2" into the transcript has not asked a question, it has hoped the user
 * types the right letter back. Here the options are rows, the cursor is always
 * visible, and nothing is sent until the user says so.
 *
 * Two rules come from watching people use the first version:
 *
 *   - **Confirming is a row, not a convention.** Enter on an option marks it
 *     `●` and moves the cursor to a separated "확인 / Confirm" row; Enter there
 *     submits. A prompt whose only visible action is "pick something" leaves
 *     the user unsure whether picking already committed them.
 *   - **A batch is tabs, not a queue.** Several questions arrive together, so
 *     ←/→ (or Tab) walk between them, each keeps its own selection, answered
 *     ones are ticked, and the Confirm row on the last tab submits the lot.
 *     The user can go back and change their mind first, which is the whole
 *     reason the daemon sends the batch in one request.
 *
 * It is not `ConfirmMenu` because of all of the above, plus multi-select
 * (Space toggles), a free-text "기타 / Other…" row, and a preview pane for an
 * option easier shown than told.
 *
 * While it is up it owns the keyboard, so nothing leaks into the chat draft.
 */

import React, { useMemo, useState } from "react";
import { Box, Text, useInput } from "ink";

import type { QuestionEntry } from "../state/store.js";

/** One question's answer, as `question.respond` carries it. */
export interface QuestionAnswerItem {
  selected: string[];
  text: string | null;
}

/** The free-text row, when `allowOther` is set. Not an option: it has no label. */
export const OTHER_LABEL = "기타 / Other…";
/** The row that actually sends. */
export const CONFIRM_LABEL = "확인 / Confirm";

export interface QuestionPromptProps {
  request: QuestionEntry;
  onAnswer: (answers: QuestionAnswerItem[]) => void;
  isActive?: boolean;
}

/** A blank answer; the shape the daemon reads as "not answered". */
const blank = (): QuestionAnswerItem => ({ selected: [], text: null });

/** Rows the prompt occupies, so the full-screen layout can reserve them. */
export function questionPromptRows(request: QuestionEntry): number {
  const questions = request.questions ?? [];
  const first = questions[0];
  const options = first?.options ?? [];
  const described = options.filter((option) => option.description).length;
  const other = first?.allowOther === false ? 0 : 1;
  const tabs = questions.length > 1 ? 1 : 0;
  // border(2) + tabs-or-header + question + options + other + blank + confirm + hint
  return 4 + tabs + options.length + described + other + 2 + 1;
}

export function QuestionPrompt({
  request,
  onAnswer,
  isActive = true,
}: QuestionPromptProps): React.ReactElement {
  const questions = useMemo(() => request.questions ?? [], [request]);
  const [tab, setTab] = useState(0);
  const [answers, setAnswers] = useState<QuestionAnswerItem[]>(() => questions.map(() => blank()));
  const [cursor, setCursor] = useState(0);
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");

  const at = Math.min(tab, Math.max(questions.length - 1, 0));
  const current = questions[at];
  const options = current?.options ?? [];
  const allowOther = current?.allowOther !== false;
  const multi = current?.multi === true;
  const answer = answers[at] ?? blank();

  // Rows the cursor can sit on: the options, then Other, then Confirm.
  const otherRow = allowOther ? options.length : -1;
  const confirmRow = allowOther ? options.length + 1 : options.length;
  const rows = confirmRow + 1;
  const last = at === questions.length - 1;

  const answeredAt = (index: number): boolean => {
    const entry = answers[index];
    return Boolean(entry && (entry.selected.length > 0 || (entry.text ?? "").length > 0));
  };

  const patch = (next: QuestionAnswerItem): void =>
    setAnswers((current) => current.map((entry, index) => (index === at ? next : entry)));

  const goto = (target: number): void => {
    setTab((target + questions.length) % questions.length);
    setTyping(false);
    setDraft("");
    setCursor(0);
  };

  const pick = (index: number): void => {
    const label = options[index]?.label ?? "";
    if (!label) return;
    if (multi) {
      const held = answer.selected.includes(label)
        ? answer.selected.filter((entry) => entry !== label)
        : [...answer.selected, label];
      // Keep the order the options were offered in.
      const ordered = options.map((option) => option.label).filter((one) => held.includes(one));
      patch({ selected: ordered, text: answer.text });
      return;
    }
    patch({ selected: [label], text: null });
    // Single-select: choosing is done, so put the cursor where sending is.
    setCursor(confirmRow);
  };

  /** Enter on the bottom row: the last tab sends, an earlier one moves on. */
  const confirm = (): void => {
    if (!last) {
      goto(at + 1);
      return;
    }
    onAnswer(answers);
  };

  useInput(
    (input, key) => {
      if (typing) {
        if (key.return) {
          const text = draft.trim();
          patch({ selected: answer.selected, text: text.length > 0 ? text : null });
          setTyping(false);
          setDraft("");
          setCursor(confirmRow);
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
        // Esc is a real answer: "I am not choosing." The daemon reports an
        // empty answer set as declined rather than letting the turn guess.
        onAnswer([]);
        return;
      }
      if (questions.length > 1 && (key.leftArrow || (key.tab && key.shift))) {
        goto(at - 1);
        return;
      }
      if (questions.length > 1 && (key.rightArrow || key.tab)) {
        goto(at + 1);
        return;
      }
      if (key.upArrow || input === "k") {
        setCursor((index) => (index + rows - 1) % rows);
        return;
      }
      if (key.downArrow || input === "j") {
        setCursor((index) => (index + 1) % rows);
        return;
      }
      if (input === " " && multi && cursor < options.length) {
        pick(cursor);
        return;
      }
      if (key.return) {
        if (cursor === confirmRow) {
          confirm();
          return;
        }
        if (cursor === otherRow || options.length === 0) {
          setTyping(true);
          return;
        }
        pick(cursor);
        return;
      }
      // 1–9 marks that row without sending: jumping to an option is not the
      // same as agreeing to submit, and only the Confirm row submits.
      if (/^[1-9]$/.test(input)) {
        const index = Number(input) - 1;
        if (index < options.length) {
          setCursor(index);
          pick(index);
        }
        return;
      }
      // Anything else is swallowed; a prompt that is up owns the keyboard.
    },
    { isActive },
  );

  const preview = options[cursor]?.preview ?? "";
  const confirmText = last ? CONFIRM_LABEL : "다음 질문 / Next question";
  const hint = [
    multi ? "Space 선택" : null,
    "↑↓ 이동",
    questions.length > 1 ? "←→ 질문 이동" : null,
    "Enter 확인",
    "1-9 고르기",
    "Esc 취소",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      {questions.length > 1 ? (
        <Box>
          {questions.map((question, index) => {
            const here = index === at;
            const tick = answeredAt(index) ? "✓ " : "";
            return (
              <Text
                key={`${request.requestId}-t${index}`}
                inverse={here}
                bold={here}
                color={here ? "cyan" : undefined}
                dimColor={!here}
              >
                {` ${tick}${question.header || `Q${index + 1}`} `}
              </Text>
            );
          })}
        </Box>
      ) : (
        <Text bold color="cyan">
          {current?.header || "질문 / Question"}
        </Text>
      )}
      <Text wrap="wrap">{current?.question ?? ""}</Text>
      <Box flexDirection="row" marginTop={1}>
        <Box flexDirection="column" flexGrow={1}>
          {options.map((option, index) => {
            const here = index === cursor;
            const held = answer.selected.includes(option.label);
            const mark = multi ? (held ? "[x] " : "[ ] ") : held ? "● " : "○ ";
            return (
              <Box key={`${request.requestId}-o${index}`} flexDirection="column">
                <Box>
                  <Text color={here ? "cyan" : undefined} bold={here}>
                    {here ? "❯ " : "  "}
                  </Text>
                  <Text inverse={here} color={here ? "cyan" : undefined} dimColor={!here && !held}>
                    {` ${index + 1}. ${mark}${option.label} `}
                  </Text>
                </Box>
                {option.description ? <Text dimColor>{`      ${option.description}`}</Text> : null}
              </Box>
            );
          })}
          {allowOther ? (
            <Box>
              <Text color={cursor === otherRow ? "cyan" : undefined} bold={cursor === otherRow}>
                {cursor === otherRow ? "❯ " : "  "}
              </Text>
              <Text
                inverse={cursor === otherRow}
                color={cursor === otherRow ? "cyan" : undefined}
                dimColor={cursor !== otherRow && !answer.text}
              >
                {` ${answer.text ? `● ${answer.text}` : OTHER_LABEL} `}
              </Text>
            </Box>
          ) : null}
        </Box>
        {preview ? (
          <Box
            flexDirection="column"
            marginLeft={2}
            borderStyle="single"
            borderColor="gray"
            paddingX={1}
          >
            {preview.split("\n").map((line, index) => (
              <Text key={`${request.requestId}-p${index}`} dimColor wrap="truncate-end">
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
          <Text dimColor>{"  Enter to keep · Esc to go back"}</Text>
        </Box>
      ) : (
        <>
          {/* The bottom row is the only thing that sends, and it looks like it. */}
          <Box marginTop={1}>
            <Text color={cursor === confirmRow ? "cyan" : undefined} bold={cursor === confirmRow}>
              {cursor === confirmRow ? "❯ " : "  "}
            </Text>
            <Text
              inverse={cursor === confirmRow}
              bold={cursor === confirmRow}
              color={cursor === confirmRow ? "cyan" : undefined}
              dimColor={cursor !== confirmRow}
            >
              {` ${confirmText} `}
            </Text>
            {questions.length > 1 ? (
              <Text dimColor>
                {`  ${questions.filter((_, index) => answeredAt(index)).length}/${questions.length}`}
              </Text>
            ) : null}
          </Box>
          <Text dimColor>{hint}</Text>
        </>
      )}
    </Box>
  );
}
