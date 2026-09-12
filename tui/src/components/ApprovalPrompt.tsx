/**
 * Interactive approval prompt (contract §7).
 *
 * Shown for the server→client `approval.request` on the session's origin
 * surface. The four answers are the four things a person actually wants to say
 * — yes once, yes for the session, yes for this project, no — so each is a row
 * of a `ConfirmMenu` rather than a scope the user has to assemble out of arrow
 * keys before pressing a letter. Enter takes the highlighted row; `y`, `a` and
 * `n` still work for anyone who knows them.
 */

import React from "react";
import { Box, Text } from "ink";

import type { ApprovalDecision, ApprovalScope } from "../rpc/sdk.js";
import { type ApprovalEntry } from "../state/store.js";
import { ConfirmMenu, type ConfirmOption } from "./ConfirmMenu.js";

const RISK_COLOR: Record<string, string> = {
  low: "green",
  medium: "yellow",
  high: "red",
};

export interface ApprovalAnswer {
  decision: ApprovalDecision;
  scope: ApprovalScope;
}

/** The four answers, in the order they are offered. */
export const APPROVAL_OPTIONS: ConfirmOption<ApprovalAnswer>[] = [
  { label: "Yes", value: { decision: "allow", scope: "once" }, shortcut: "y" },
  {
    label: "Yes, and don't ask again this session",
    value: { decision: "allow", scope: "session" },
    shortcut: "a",
  },
  {
    label: "Yes for this project",
    value: { decision: "allow", scope: "project" },
    shortcut: "p",
    hint: "adds an allowlist rule the daemon keeps",
  },
  { label: "No", value: { decision: "deny", scope: "once" }, shortcut: "n", danger: true },
];

export function formatArgs(args: Record<string, unknown> | undefined, max = 72): string[] {
  return Object.entries(args ?? {}).map(([key, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    const flat = String(text).replace(/\s+/g, " ");
    return `${key}: ${flat.length > max ? `${flat.slice(0, max - 1)}…` : flat}`;
  });
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
  // A daemon that suggests a scope moves the cursor to it; otherwise the
  // cursor sits on plain Yes, which is what Enter should mean by default.
  const suggested = APPROVAL_OPTIONS.findIndex(
    (option) => option.value.decision === "allow" && option.value.scope === request.scopeHint,
  );

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
      <Text bold color="yellow">
        Approval required
      </Text>
      <Text>
        <Text bold>{request.tool}</Text>
        <Text dimColor> risk=</Text>
        <Text color={RISK_COLOR[request.risk ?? ""] ?? "white"}>{request.risk ?? "unknown"}</Text>
        {request.timeoutSec ? <Text dimColor> timeout={request.timeoutSec}s</Text> : null}
      </Text>
      {request.note ? (
        <Text bold color="red">
          {"  ⚠ "}
          {request.note}
        </Text>
      ) : null}
      {formatArgs(request.args).map((line, index) => (
        <Text key={`${request.requestId}-a${index}`} dimColor wrap="truncate-end">
          {"  "}
          {line}
        </Text>
      ))}
      <Box marginTop={1} flexDirection="column">
        <ConfirmMenu<ApprovalAnswer>
          options={APPROVAL_OPTIONS}
          initialIndex={suggested === -1 ? 0 : suggested}
          escapeValue={{ decision: "deny", scope: "once" }}
          isActive={isActive}
          onChoose={(answer) => onDecide(answer.decision, answer.scope)}
        />
      </Box>
    </Box>
  );
}
