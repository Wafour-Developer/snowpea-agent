/**
 * `/model` with nothing after it: a list to pick from.
 *
 * The daemon's own `/model` prints its listing into the transcript, which is
 * fine in a pipe and poor in front of a person — you have to read it, then type
 * the name back. Here the same information is a list with a cursor on it, and
 * picking a row runs `/model <ref>` for you.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { ModelOption } from "../state/models.js";

/** Rows shown at once; the list scrolls inside this. */
export const MODEL_PICKER_ROWS = 8;

export interface ModelPickerProps {
  options: ModelOption[];
  onChoose: (option: ModelOption) => void;
  onCancel: () => void;
  isActive?: boolean;
  width: number;
}

export function ModelPicker({
  options,
  onChoose,
  onCancel,
  isActive = true,
  width,
}: ModelPickerProps): React.ReactElement {
  const [index, setIndex] = useState(() => {
    const current = options.findIndex((option) => option.current);
    return current === -1 ? 0 : current;
  });

  useInput(
    (input, key) => {
      if (key.escape) {
        onCancel();
        return;
      }
      if (options.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i + options.length - 1) % options.length);
        return;
      }
      if (key.downArrow || key.tab) {
        setIndex((i) => (i + 1) % options.length);
        return;
      }
      if (key.return) onChoose(options[index]);
      // Everything else is swallowed: the picker owns the keyboard while it is
      // up, the way the approval prompt does.
    },
    { isActive },
  );

  // Keep the cursor inside the window without scrolling short lists.
  const start = Math.max(
    0,
    Math.min(index - MODEL_PICKER_ROWS + 2, options.length - MODEL_PICKER_ROWS),
  );
  const shown = options.slice(start, start + MODEL_PICKER_ROWS);

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        Model
      </Text>
      {options.length === 0 ? (
        <Text dimColor>no profiles configured and the vendor listed nothing</Text>
      ) : (
        shown.map((option) => {
          const selected = options[index] === option;
          return (
            <Box key={`${option.origin}-${option.ref}`}>
              <Text color={selected ? "green" : undefined}>{selected ? "❯ " : "  "}</Text>
              <Text inverse={selected} bold={option.current}>
                {option.label}
              </Text>
              <Text dimColor>{option.detail ? `  ${option.detail}` : ""}</Text>
              {option.current ? <Text color="green">{"  ← in use"}</Text> : null}
            </Box>
          );
        })
      )}
      <Text dimColor>↑↓ move · Enter pick · Esc cancel</Text>
    </Box>
  );
}
