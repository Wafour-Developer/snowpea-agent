/** Connection state, short session id, provider/model, token usage. */

import React from "react";
import { Box, Text } from "ink";

import type { ConnectionStatus } from "../rpc/client.js";
import type { Usage } from "../state/store.js";

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "yellow",
  connected: "green",
  reconnecting: "yellow",
  closed: "red",
};

export function shortSessionId(sessionId: string | null): string {
  if (!sessionId) return "—";
  return sessionId.length <= 8 ? sessionId : sessionId.slice(0, 8);
}

export function StatusLine({
  status,
  sessionId,
  provider,
  model,
  usage,
  turnActive = false,
}: {
  status: ConnectionStatus;
  sessionId: string | null;
  provider: string | null;
  model: string | null;
  usage: Usage;
  turnActive?: boolean;
}): React.ReactElement {
  const providerLabel = [provider, model].filter(Boolean).join("/") || "default";
  return (
    <Box>
      <Text color={STATUS_COLOR[status]}>● {status}</Text>
      <Text dimColor> · session {shortSessionId(sessionId)}</Text>
      <Text dimColor> · {providerLabel}</Text>
      <Text dimColor>
        {" "}
        · tokens {usage.inputTokens}↑/{usage.outputTokens}↓
      </Text>
      {turnActive ? <Text color="yellow"> · working (esc to interrupt)</Text> : null}
    </Box>
  );
}
