/**
 * Unattended approval backlog, seeded from `approval.list` (contract §7).
 *
 * These requests came from turns with no human attached — a scheduled job or a
 * gateway message — so any authenticated surface may answer them. The panel is
 * read-only until it is focused (Ctrl+R in the app), which keeps its single-key
 * actions from stealing characters from the chat line.
 *
 * Focused keys: ↑/↓ pick a request, 1-9 jump to one, ←/→ pick the scope, `a`
 * allows, `d` denies, Esc or Ctrl+R leaves. A `project` or `always` scope also
 * stores an allowlist pattern on the daemon, so the same command stops asking.
 */

import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";

import { useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ApprovalDecision, ApprovalScope } from "../rpc/sdk.js";
import { APPROVAL_SCOPES, type ApprovalEntry } from "../state/store.js";
import { ChoiceList } from "./ChoiceList.js";

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
  /** True while the queue holds the keyboard; the app toggles it with Ctrl+R. */
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

  const answer = (decision: ApprovalDecision): void => {
    if (current) onRespond?.(current.requestId, decision, APPROVAL_SCOPES[scopeIndex]);
  };

  useChoiceKeys({
    count: requests.length,
    index: Math.min(selected, Math.max(requests.length - 1, 0)),
    onIndex: setSelected,
    onLeft: () => setScopeIndex((i) => (i + APPROVAL_SCOPES.length - 1) % APPROVAL_SCOPES.length),
    onRight: () => setScopeIndex((i) => (i + 1) % APPROVAL_SCOPES.length),
    onTab: () => setScopeIndex((i) => (i + 1) % APPROVAL_SCOPES.length),
    onCancel: () => onBlur?.(),
    // Enter is the safe half of the pair: it allows nothing on its own, and
    // `a`/`d` stay the two answers this backlog has always taken.
    shortcuts: {
      a: () => answer("allow"),
      d: () => answer("deny"),
    },
    vim: false,
    isActive: isActive && requests.length > 0,
  });

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
      <ChoiceList
        options={requests.map((request) => ({
          label: `${request.tool} · risk=${request.risk ?? "unknown"} · ${request.requestId.slice(0, 8)} · ${summarise(request)}`,
        }))}
        selectedIndex={isActive ? Math.min(selected, requests.length - 1) : -1}
        color="yellow"
        hint={null}
      />
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
          <Text dimColor>
            {"↑↓ move · 1-9 pick · ←→ scope · a allow · d deny · Esc or Ctrl+R leave"}
          </Text>
        </>
      ) : (
        <Text dimColor>Ctrl+R to answer them here.</Text>
      )}
    </Box>
  );
}
