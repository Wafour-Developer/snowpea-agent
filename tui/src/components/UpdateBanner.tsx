/** Top-of-screen update nudge (CORE-update). Renders nothing when idle. */

import React from "react";
import { Box, Text } from "ink";

import { bannerText, type UpdateState } from "../state/update.js";

const PHASE_COLOR: Record<string, string> = {
  available: "cyan",
  confirm: "yellow",
  running: "yellow",
  done: "green",
  failed: "red",
};

export function UpdateBanner({ update }: { update: UpdateState }): React.ReactElement | null {
  const text = bannerText(update);
  if (text === null) return null;
  return (
    <Box marginBottom={1}>
      <Text color={PHASE_COLOR[update.phase] ?? "cyan"}>{text}</Text>
    </Box>
  );
}
