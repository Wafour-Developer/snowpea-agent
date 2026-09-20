/**
 * `/model` with nothing after it: a list to pick from.
 *
 * The daemon's own `/model` prints its listing into the transcript, which is
 * fine in a pipe and poor in front of a person — you have to read it, then type
 * the name back. Here the same information is a list with a cursor on it, and
 * picking a row runs `/model <ref>` for you.
 */

import React, { useState } from "react";
import { Box, Text } from "ink";

import { currentAccent } from "../layout/palette.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ModelOption } from "../state/models.js";
import { ChoiceList } from "./ChoiceList.js";

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

  useChoiceKeys({
    count: options.length,
    index,
    onIndex: setIndex,
    onEnter: (row) => {
      if (options.length > 0) onChoose(options[row]);
    },
    onCancel,
    onTab: () => {
      if (options.length > 0) setIndex((i) => (i + 1) % options.length);
    },
    isActive,
  });

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        Model
      </Text>
      {options.length === 0 ? (
        <>
          <Text dimColor>no profiles configured and the vendor listed nothing</Text>
          <Text dimColor>{choiceHint({ enter: "pick", digits: false })}</Text>
        </>
      ) : (
        <ChoiceList
          options={options.map((option) => ({
            label: option.label,
            description: option.detail,
            badge: option.current ? "← in use" : undefined,
            badgeColor: currentAccent(),
            bold: option.current,
          }))}
          selectedIndex={index}
          color={currentAccent()}
          windowSize={MODEL_PICKER_ROWS}
          descriptionMode="inline"
          hint={choiceHint({ enter: "pick" })}
        />
      )}
    </Box>
  );
}
