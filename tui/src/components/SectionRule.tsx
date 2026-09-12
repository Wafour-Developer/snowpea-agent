/** A terminal-width divider between the persistent bottom regions. */
import React from "react";
import { Box, Text } from "ink";

export function SectionRule({
  width,
  color,
  label,
}: {
  width: number;
  color?: string;
  label?: string;
}): React.ReactElement {
  const suffix = label ? ` ${label}` : "";
  const rule = `${"─".repeat(Math.max(1, width - suffix.length))}${suffix}`.slice(0, Math.max(1, width));
  return (
    <Box width={width} flexShrink={0} overflow="hidden">
      <Text color={color} dimColor={!color} wrap="truncate-end">
        {rule}
      </Text>
    </Box>
  );
}
