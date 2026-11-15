/**
 * Switching an external session to unrestricted mode takes a typed word.
 *
 * Unrestricted means snowpea stops gating what the CLI runs, so a single key
 * would be too easy to hit: the person types `unrestricted` and presses Enter.
 * Anything else, or Esc, leaves the session as it was.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";
import { fit } from "../layout/fit.js";
import { UNRESTRICTED_WORD, confirmsUnrestricted } from "../state/external.js";

export function unrestrictedConfirmRows(lines: number): number {
  return 2 + lines + 2;
}

export function UnrestrictedConfirm({
  lines,
  width,
  onConfirm,
  onCancel,
  isActive = true,
}: {
  lines: string[];
  width: number;
  onConfirm: () => void;
  onCancel: () => void;
  isActive?: boolean;
}): React.ReactElement {
  const [typed, setTyped] = useState("");
  const [wrong, setWrong] = useState(false);
  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (key.escape) {
        onCancel();
        return;
      }
      if (key.return) {
        if (confirmsUnrestricted(typed)) onConfirm();
        else setWrong(true);
        return;
      }
      if (key.backspace || key.delete) {
        setTyped((value) => value.slice(0, -1));
        setWrong(false);
        return;
      }
      if (input && !key.ctrl && !key.meta && !key.tab) {
        setTyped((value) => value + input);
        setWrong(false);
      }
    },
    { isActive },
  );
  const inner = Math.max(10, width - 4);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="red" paddingX={1} width={width}>
      {lines.map((line, index) => (
        <Text key={`uc-${index}`} bold={index === 0} color={index === 0 ? "red" : undefined} wrap="truncate-end">
          {fit(line, inner)}
        </Text>
      ))}
      <Box>
        <Text color="red">{"› "}</Text>
        <Text>{fit(typed, Math.max(1, inner - 4))}</Text>
        <Text inverse> </Text>
      </Box>
      {wrong ? (
        <Text color="red" wrap="truncate-end">{`Type exactly "${UNRESTRICTED_WORD}", or Esc to cancel.`}</Text>
      ) : null}
    </Box>
  );
}
