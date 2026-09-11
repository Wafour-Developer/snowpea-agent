/**
 * snowpea TUI root component — M0 placeholder.
 *
 * M1 replaces this with the Chat / MessageStream / ToolCall / ApprovalPrompt /
 * ModeBar tree that talks to the daemon through @snowpea/sdk.
 */

import React from "react";
import { Box, Text } from "ink";

export const PLACEHOLDER_TEXT = "snowpea tui placeholder";

export function App(): React.ReactElement {
  return (
    <Box paddingX={1}>
      <Text color="green">{PLACEHOLDER_TEXT}</Text>
    </Box>
  );
}
