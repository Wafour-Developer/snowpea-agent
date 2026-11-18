/**
 * The question an untrusted folder gets once, at startup, before anything else:
 * "Do you trust this folder?". Yes trusts it (the default scopes, the digests
 * that were shown) and the session goes on; No leaves snowpea.
 *
 * Arrows or 1/2 choose, Enter confirms, Esc is No. When core will not take
 * trust from this client (a remote or connector login) the reason is shown and
 * Enter goes on without trust; the project's own files stay off.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";
import type { TrustGate } from "../hooks/useTrustScreen.js";
import { say } from "../state/errors.js";

export interface TrustPromptProps {
  gate: TrustGate;
  width: number;
  height: number;
  onYes: () => void;
  onNo: () => void;
  /** Go on without trust (only offered after core refused). */
  onContinue: () => void;
}

export function TrustPrompt({ gate, width, height, onYes, onNo, onContinue }: TrustPromptProps): React.ReactElement {
  const [choice, setChoice] = useState<0 | 1>(0);

  useInput((input, key) => {
    if (mouseReports(input).length > 0 || gate.busy) return;
    if (gate.refused) {
      if (key.return || key.escape) onContinue();
      return;
    }
    if (key.upArrow || key.downArrow || key.tab) setChoice((value) => (value === 0 ? 1 : 0));
    else if (input === "1") setChoice(0);
    else if (input === "2") setChoice(1);
    else if (key.escape) onNo();
    else if (key.return) (choice === 0 ? onYes : onNo)();
  });

  const options = [say("1. Yes, trust this folder", "1. 예, 이 폴더를 신뢰합니다"), say("2. No, exit", "2. 아니요, 종료합니다")];

  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor="yellow" paddingX={1} overflow="hidden">
      <Text bold color="yellow">
        {say("Do you trust this folder?", "이 폴더를 신뢰하시겠습니까?")}
      </Text>
      <Text> </Text>
      <Text bold>{gate.status.workdir}</Text>
      <Text> </Text>
      <Text wrap="wrap">
        {say(
          "snowpea may run tools and read and write files here, and this folder's settings, hooks and skills become active.",
          "snowpea가 이 폴더에서 도구를 실행하고 파일을 읽고 쓸 수 있으며, 이 폴더의 설정, 훅, 스킬이 활성화됩니다.",
        )}
      </Text>
      <Text> </Text>
      {gate.note ? (
        <Text color={gate.refused ? "yellow" : "red"} wrap="wrap">
          {gate.note}
        </Text>
      ) : null}
      {gate.busy ? (
        <Text dimColor>{say("Trusting…", "신뢰하는 중…")}</Text>
      ) : gate.refused ? (
        <Text dimColor>{say("Enter to continue without trusting it", "Enter: 신뢰하지 않고 계속")}</Text>
      ) : (
        <>
          {options.map((label, index) => (
            <Text key={label} color={choice === index ? "cyan" : undefined} bold={choice === index}>
              {choice === index ? "❯ " : "  "}
              {label}
            </Text>
          ))}
          <Text> </Text>
          <Text dimColor>{say("↑↓ or 1/2 choose · Enter confirm · Esc no", "↑↓ 또는 1/2 선택 · Enter 확인 · Esc 아니요")}</Text>
        </>
      )}
    </Box>
  );
}
