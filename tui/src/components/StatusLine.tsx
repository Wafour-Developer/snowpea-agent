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
  hint = null,
  toast = null,
}: {
  status: ConnectionStatus;
  sessionId: string | null;
  provider: string | null;
  model: string | null;
  usage: Usage;
  turnActive?: boolean;
  /** One-time nudge, e.g. "⇧Tab: mode"; shown until dismissed or used. */
  hint?: string | null;
  /** Transient confirmation, e.g. "mode: AUTO"; clears itself after a beat. */
  toast?: string | null;
}): React.ReactElement {
  const providerLabel = [provider, model].filter(Boolean).join("/") || "default";
  return (
    <Box flexDirection="column">
      <Box>
        <Text color={STATUS_COLOR[status]}>● {status}</Text>
        <Text dimColor> · session {shortSessionId(sessionId)}</Text>
        <Text dimColor> · {providerLabel}</Text>
        <Text dimColor>
          {" "}
          · tokens {usage.inputTokens}↑/{usage.outputTokens}↓
        </Text>
        {turnActive ? <Text color="yellow"> · working (esc to interrupt)</Text> : null}
        {toast ? <Text color="cyan"> · {toast}</Text> : hint ? <Text dimColor> · {hint}</Text> : null}
      </Box>
    </Box>
  );
}
