/** A terminal-width divider between the persistent bottom regions. */
import React from "react";
import { Box, Text } from "ink";

export function SectionRule({ width }: { width: number }): React.ReactElement {
  return (
    <Box width={width} flexShrink={0} overflow="hidden">
      <Text dimColor wrap="truncate-end">{"─".repeat(Math.max(1, width))}</Text>
    </Box>
  );
}
