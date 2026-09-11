/**
 * Interactive approval prompt (contract §7).
 *
 * Shown for the server→client `approval.request` on the session's origin
 * surface. `y` allows, `n` denies, left/right pick the scope. The decision
 * resolves the promise the SDK is awaiting.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { ApprovalDecision, ApprovalScope } from "../rpc/sdk.js";
import { APPROVAL_SCOPES, type ApprovalEntry } from "../state/store.js";

const RISK_COLOR: Record<string, string> = {
  low: "green",
  medium: "yellow",
  high: "red",
};

export function formatArgs(args: Record<string, unknown>): string[] {
  return Object.entries(args).map(
    ([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`,
  );
}

export function ApprovalPrompt({
  request,
  onDecide,
  isActive = true,
}: {
  request: ApprovalEntry;
  onDecide: (decision: ApprovalDecision, scope: ApprovalScope) => void;
  isActive?: boolean;
}): React.ReactElement {
  const initialScope = request.scopeHint ?? "once";
  const [scopeIndex, setScopeIndex] = useState(() => {
    const index = APPROVAL_SCOPES.indexOf(initialScope);
    return index === -1 ? 0 : index;
  });

  useInput(
    (input, key) => {
      if (key.leftArrow) {
        setScopeIndex((i) => (i + APPROVAL_SCOPES.length - 1) % APPROVAL_SCOPES.length);
        return;
      }
      if (key.rightArrow || key.tab) {
        setScopeIndex((i) => (i + 1) % APPROVAL_SCOPES.length);
        return;
      }
      const lower = input.toLowerCase();
      if (lower === "y") onDecide("allow", APPROVAL_SCOPES[scopeIndex]);
      else if (lower === "n" || key.escape) onDecide("deny", APPROVAL_SCOPES[scopeIndex]);
    },
    { isActive },
  );

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
      <Text bold color="yellow">
        Approval required
      </Text>
      <Text>
        <Text bold>{request.tool}</Text>
        <Text dimColor> risk=</Text>
        <Text color={RISK_COLOR[request.risk] ?? "white"}>{request.risk}</Text>
        {request.timeoutSec ? <Text dimColor> timeout={request.timeoutSec}s</Text> : null}
      </Text>
      {formatArgs(request.args).map((line, index) => (
        <Text key={`${request.requestId}-a${index}`} dimColor>
          {"  "}
          {line}
        </Text>
      ))}
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
      <Text dimColor>[y] allow [n] deny ←/→ change scope</Text>
    </Box>
  );
}
