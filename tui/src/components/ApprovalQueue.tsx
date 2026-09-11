/**
 * Unattended approval backlog, seeded from `approval.list` (contract §7).
 *
 * These requests came from turns with no human attached — a scheduled job or a
 * gateway message — so any authenticated surface may answer them. The panel is
 * read-only until it is focused (Ctrl+A in the app), which keeps its single-key
 * actions from stealing characters from the chat line.
 *
 * Focused keys: ↑/↓ pick a request, ←/→ pick the scope, `a` allows, `d` (or
 * Esc) denies, Ctrl+A leaves. A `project` or `always` scope also stores an
 * allowlist pattern on the daemon, so the same command stops asking.
 */

import React, { useEffect, useState } from "react";
import { Box, Text, useInput } from "ink";

import type { ApprovalDecision, ApprovalScope } from "../rpc/sdk.js";
import { APPROVAL_SCOPES, type ApprovalEntry } from "../state/store.js";

export type ApprovalQueueRespond = (
  requestId: string,
  decision: ApprovalDecision,
  scope: ApprovalScope,
) => void;

function summarise(request: ApprovalEntry): string {
  const command = (request.args ?? {})["command"];
  if (typeof command === "string" && command.length > 0) return command;
  const args = Object.keys(request.args ?? {});
  return args.length > 0 ? args.join(", ") : "no arguments";
}

export function ApprovalQueue({
  requests,
  onRespond,
  isActive = false,
  onBlur,
}: {
  requests: ApprovalEntry[];
  onRespond?: ApprovalQueueRespond;
  /** True while the queue holds the keyboard; the app toggles it with Ctrl+A. */
  isActive?: boolean;
  onBlur?: () => void;
}): React.ReactElement | null {
  const [selected, setSelected] = useState(0);
  const [scopeIndex, setScopeIndex] = useState(0);

  // Keep the cursor inside the list as entries are added and resolved.
  useEffect(() => {
    setSelected((index) => (requests.length === 0 ? 0 : Math.min(index, requests.length - 1)));
  }, [requests.length]);

  const current = requests[Math.min(selected, Math.max(requests.length - 1, 0))];

  useInput(
    (input, key) => {
      if (requests.length === 0) return;
      if (key.upArrow) {
        setSelected((i) => (i + requests.length - 1) % requests.length);
        return;
      }
      if (key.downArrow) {
        setSelected((i) => (i + 1) % requests.length);
        return;
      }
      if (key.leftArrow) {
        setScopeIndex((i) => (i + APPROVAL_SCOPES.length - 1) % APPROVAL_SCOPES.length);
        return;
      }
      if (key.rightArrow || key.tab) {
        setScopeIndex((i) => (i + 1) % APPROVAL_SCOPES.length);
        return;
      }
      const decision: ApprovalDecision | null =
        input.toLowerCase() === "a" ? "allow" : input.toLowerCase() === "d" ? "deny" : null;
      if (decision && current) {
        onRespond?.(current.requestId, decision, APPROVAL_SCOPES[scopeIndex]);
        return;
      }
      if (key.escape) onBlur?.();
    },
    { isActive: isActive && requests.length > 0 },
  );

  if (requests.length === 0) return null;

  return (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor={isActive ? "yellow" : "gray"}
      paddingX={1}
    >
      <Text bold dimColor={!isActive} color={isActive ? "yellow" : undefined}>
        Unattended approvals ({requests.length})
      </Text>
      {requests.map((request) => {
        const picked = isActive && request.requestId === current?.requestId;
        return (
          <Text key={request.requestId} dimColor={!picked} inverse={picked}>
            {picked ? "> " : "  "}
            {request.tool} · risk={request.risk ?? "unknown"} · {request.requestId.slice(0, 8)} ·{" "}
            {summarise(request)}
          </Text>
        );
      })}
      {isActive ? (
        <>
          <Box marginTop={1}>
            <Text>scope: </Text>
            {APPROVAL_SCOPES.map((scope, index) => (
              <Text
                key={scope}
                inverse={index === scopeIndex}
                color={index === scopeIndex ? "yellow" : undefined}
                dimColor={index !== scopeIndex}
              >
                {` ${scope} `}
              </Text>
            ))}
          </Box>
          <Text dimColor>[a] allow [d] deny ↑/↓ pick ←/→ scope · Ctrl+A leave</Text>
        </>
      ) : (
        <Text dimColor>Ctrl+A to answer them here.</Text>
      )}
    </Box>
  );
}
