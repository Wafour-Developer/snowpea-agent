/** A terminal-width divider between the persistent bottom regions. */
import React from "react";
import { Box, Text } from "ink";

export function SectionRule({ width, color }: { width: number; color?: string }): React.ReactElement {
  return (
    <Box width={width} flexShrink={0} overflow="hidden">
      <Text color={color} dimColor={!color} wrap="truncate-end">
        {"─".repeat(Math.max(1, width))}
      </Text>
    </Box>
  );
}
