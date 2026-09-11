/** PLAN / ACCEPT / AUTO indicator; follows `mode.changed` (contract §1). */

import React from "react";
import { Box, Text } from "ink";

import type { Mode } from "../rpc/sdk.js";

export const MODES: Mode[] = ["plan", "accept", "auto"];

const MODE_COLOR: Record<Mode, string> = {
  plan: "cyan",
  accept: "green",
  auto: "red",
};

export function ModeBar({ mode }: { mode: Mode }): React.ReactElement {
  return (
    <Box>
      {MODES.map((m) => (
        <Text
          key={m}
          bold={m === mode}
          inverse={m === mode}
          color={m === mode ? MODE_COLOR[m] : undefined}
          dimColor={m !== mode}
        >
          {` ${m.toUpperCase()} `}
        </Text>
      ))}
      <Text dimColor> (/mode plan|accept|auto)</Text>
    </Box>
  );
}
