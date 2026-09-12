/**
 * What is going to be sent with the next prompt, shown above the input.
 *
 * A chip is the only evidence that a pasted path became an attachment rather
 * than text, so it says the name and the size and nothing else.
 */

import React from "react";
import { Box, Text } from "ink";

import { chipLabel, type Attachment } from "../state/attachments.js";

export function AttachmentChips({
  attachments,
  width,
}: {
  attachments: Attachment[];
  width: number;
}): React.ReactElement | null {
  if (attachments.length === 0) return null;
  return (
    <Box width={width} flexWrap="wrap">
      {attachments.map((attachment) => (
        <Text key={attachment.id} color="cyan">
          {`[${chipLabel(attachment)}] `}
        </Text>
      ))}
      <Text dimColor>(backspace removes the last · Ctrl+X clears)</Text>
    </Box>
  );
}
