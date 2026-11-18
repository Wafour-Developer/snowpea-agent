/**
 * `/feedback` (`/개선하기`): what would be sent, an optional comment, the
 * consent line and an explicit Send.
 *
 * Steps: the comment (type, Enter goes on, Esc closes without sending), then
 * the choice (Send / Back to the comment / Cancel), then the answer — the
 * report id, or the error with when to try again. Nothing is sent before Send
 * is picked.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import { mouseReports } from "../input/mouse.js";
import { say } from "../state/errors.js";
import {
  FEEDBACK_CONSENT,
  previewMessageLines,
  previewSummary,
  type FeedbackPreviewView,
  type FeedbackSendOutcome,
} from "../state/feedback.js";
import { ChoiceList } from "./ChoiceList.js";

/** The server takes a comment of at most this many characters. */
const COMMENT_MAX = 20000;

export interface FeedbackScreenProps {
  preview: FeedbackPreviewView;
  initialComment?: string;
  width: number;
  onSend: (comment: string) => Promise<FeedbackSendOutcome>;
  onClose: () => void;
  isActive?: boolean;
}

type Step = "comment" | "confirm" | "sending" | "done";

export function FeedbackScreen({
  preview,
  initialComment = "",
  width,
  onSend,
  onClose,
  isActive = true,
}: FeedbackScreenProps): React.ReactElement {
  const [step, setStep] = useState<Step>("comment");
  const [comment, setComment] = useState(initialComment.slice(0, COMMENT_MAX));
  const [choice, setChoice] = useState(0);
  const [outcome, setOutcome] = useState<FeedbackSendOutcome | null>(null);
  const inner = Math.max(10, width - 4);

  const options = [
    { label: say("Send", "보내기"), description: say("upload this conversation now", "이 대화를 지금 보내요") },
    { label: say("Edit the comment", "의견 고치기"), description: "" },
    { label: say("Cancel", "취소"), description: say("nothing is sent", "아무것도 보내지 않아요") },
  ];

  const pick = (row: number) => {
    if (row === 0) {
      setStep("sending");
      void onSend(comment).then((result) => {
        setOutcome(result);
        setStep("done");
      });
    } else if (row === 1) {
      setStep("comment");
    } else {
      onClose();
    }
  };

  useChoiceKeys({
    count: options.length,
    index: choice,
    onIndex: setChoice,
    onEnter: pick,
    onCancel: onClose,
    isActive: isActive && step === "confirm",
  });

  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (step === "done") {
        if (key.return || key.escape) onClose();
        return;
      }
      if (step !== "comment") return;
      if (key.escape) {
        onClose();
        return;
      }
      if (key.return) {
        setChoice(0);
        setStep("confirm");
        return;
      }
      if (key.backspace || key.delete) {
        setComment((text) => text.slice(0, -1));
        return;
      }
      if (key.tab || key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return;
      if (key.ctrl || key.meta || input.length === 0) return;
      setComment((text) => (text + input.replace(/[\r\n]+/g, " ")).slice(0, COMMENT_MAX));
    },
    { isActive: isActive && (step === "comment" || step === "done") },
  );

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        {say("Improve snowpea — send this conversation", "개선하기 — 이 대화 보내기")}
      </Text>
      {previewSummary(preview).map((line, i) => (
        <Text key={`s${i}`} dimColor wrap="wrap">
          {line}
        </Text>
      ))}
      <Box flexDirection="column" marginTop={1}>
        {previewMessageLines(preview, inner).map((line, i) => (
          <Text key={`m${i}`} wrap="truncate">
            {line}
          </Text>
        ))}
      </Box>
      <Box marginTop={1}>
        <Text wrap="wrap">
          {say("Comment (optional): ", "의견 (선택): ")}
          <Text>{comment}</Text>
          {step === "comment" ? <Text inverse>{" "}</Text> : null}
        </Text>
      </Box>
      <Box marginTop={1}>
        <Text color="yellow" wrap="wrap">
          {FEEDBACK_CONSENT()}
        </Text>
      </Box>
      {step === "comment" ? (
        <Text dimColor>{say("Enter: continue · Esc: close without sending", "Enter: 계속 · Esc: 보내지 않고 닫기")}</Text>
      ) : null}
      {step === "confirm" ? (
        <ChoiceList options={options} selectedIndex={choice} descriptionMode="inline" hint={choiceHint({})} />
      ) : null}
      {step === "sending" ? <Text>{say("Sending…", "보내는 중…")}</Text> : null}
      {step === "done" && outcome ? (
        <>
          <Text color={outcome.ok ? "green" : "red"} wrap="wrap">
            {outcome.ok
              ? say(`Sent. Thank you! Report id: ${outcome.id}`, `보냈어요. 고마워요! 보고서 id: ${outcome.id}`)
              : outcome.message}
          </Text>
          <Text dimColor>{say("Enter or Esc: close", "Enter 또는 Esc: 닫기")}</Text>
        </>
      ) : null}
    </Box>
  );
}
