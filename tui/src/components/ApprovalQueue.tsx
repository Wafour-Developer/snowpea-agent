/**
 * Unattended approval backlog, seeded from `approval.list` (contract §7).
 *
 * M1 scope is read-only: the list shows what is waiting so the operator knows
 * a background turn is blocked. Responding from the queue lands with M4's
 * allowlist work.
 */

import React from "react";
import { Box, Text } from "ink";

import type { ApprovalEntry } from "../state/store.js";

export function ApprovalQueue({
  requests,
}: {
  requests: ApprovalEntry[];
}): React.ReactElement | null {
  if (requests.length === 0) return null;
  return (
    <Box flexDirection="column" borderStyle="single" borderColor="gray" paddingX={1}>
      <Text bold dimColor>
        Unattended approvals ({requests.length})
      </Text>
      {requests.map((request) => (
        <Text key={request.requestId} dimColor>
          {"  "}
          {request.tool} · risk={request.risk ?? "unknown"} · {request.requestId.slice(0, 8)}
        </Text>
      ))}
      <Text dimColor>respond with: snowpea approvals respond {"<id>"}</Text>
    </Box>
  );
}
