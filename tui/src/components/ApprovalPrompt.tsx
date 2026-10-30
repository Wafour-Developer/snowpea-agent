/**
 * Interactive approval prompt (contract §7, M15b §1).
 *
 * Shown for the server→client `approval.request` on the session's origin
 * surface. Each row is one line: yes once, yes for the session, yes for this
 * project, switch to auto, no, or no with a reason. Enter takes the highlighted
 * row; letter shortcuts still work for anyone who knows them.
 *
 * A bare "no" tells the model nothing, so the reason row sends what you type
 * back as the tool's refusal.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";

import type { ApprovalDecision, ApprovalScope, Mode } from "../rpc/sdk.js";
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
  /** True for the row that asks for a reason before it answers. */
  withReason?: boolean;
  /**
   * True for "Allow Everything": auto mode, or — in plan mode — planning with
   * every approval answered yes and auto once the plan is done. app.tsx picks
   * which by the session's mode.
   */
  switchToAuto?: boolean;
}

/** The six answers, in the order they are offered. */
export const APPROVAL_OPTIONS: ConfirmOption<ApprovalAnswer>[] = [
  { label: "Yes", value: { decision: "allow", scope: "once" }, shortcut: "y" },
  {
    label: "Yes, this session only",
    value: { decision: "allow", scope: "session" },
    shortcut: "a",
  },
  {
    label: "Yes, add to project allowlist",
    value: { decision: "allow", scope: "project" },
    shortcut: "p",
  },
  {
    label: "Allow Everything (Auto mode)",
    value: { decision: "allow", scope: "once", switchToAuto: true },
    shortcut: "e",
  },
  { label: "No", value: { decision: "deny", scope: "once" }, shortcut: "n", danger: true },
  {
    label: "No, with reason",
    value: { decision: "deny", scope: "once", withReason: true },
    shortcut: "r",
    danger: true,
  },
];

/** The row label "Allow Everything" carries in plan mode (1.32.0). */
export const PLAN_ALLOW_EVERYTHING = "Allow Everything (plan, then Auto)";

/** Longest rule text the project row shows, so it stays one line. */
const REMEMBER_DISPLAY_MAX = 40;

/**
 * The project row for a shell request (1.34.0, `shellRemember`): it names the
 * rule the daemon would store, or is left out when only "once" can be kept.
 * `undefined` keeps the row as it was: an older daemon sends no `remember`.
 */
function projectRow(
  request: Pick<ApprovalEntry, "args" | "remember"> | undefined,
): ConfirmOption<ApprovalAnswer> | null | undefined {
  if (!request || request.remember === undefined) return undefined;
  if (request.remember === null) {
    return typeof request.args?.command === "string" ? null : undefined;
  }
  const display = request.remember.display;
  const shown =
    display.length > REMEMBER_DISPLAY_MAX
      ? `${display.slice(0, REMEMBER_DISPLAY_MAX - 1)}…`
      : display;
  return {
    label: `Yes, allow "${shown}" in this project`,
    value: { decision: "allow", scope: "project" },
    shortcut: "p",
  };
}

/**
 * The rows for a session in `mode`: plan mode names what "e" does there, and
 * a shell request's project row says what it remembers. The rows are fixed
 * once the request is known, so the prompt never changes height while shown.
 */
export function approvalOptions(
  mode?: Mode,
  request?: Pick<ApprovalEntry, "args" | "remember">,
): ConfirmOption<ApprovalAnswer>[] {
  const project = projectRow(request);
  if (mode !== "plan" && project === undefined) return APPROVAL_OPTIONS;
  return APPROVAL_OPTIONS.flatMap((option) => {
    if (option.value.scope === "project" && project !== undefined) {
      return project === null ? [] : [project];
    }
    if (mode === "plan" && option.value.switchToAuto) {
      return [{ ...option, label: PLAN_ALLOW_EVERYTHING }];
    }
    return [option];
  });
}

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
  mode,
}: {
  request: ApprovalEntry;
  onDecide: (answer: ApprovalAnswer, reason?: string) => void;
  isActive?: boolean;
  /** The session's mode, which decides what "Allow Everything" means. */
  mode?: Mode;
}): React.ReactElement {
  const options = approvalOptions(mode, request);
  // A daemon that suggests a scope moves the cursor to it; otherwise the
  // cursor sits on plain Yes, which is what Enter should mean by default.
  const suggested = options.findIndex(
    (option) => option.value.decision === "allow" && option.value.scope === request.scopeHint,
  );
  const [reason, setReason] = useState<string | null>(null);

  // The reason field owns the keyboard once it is open: Enter sends the
  // refusal, Esc goes back to the menu, and nothing leaks into the chat draft.
  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (reason === null) return;
      if (key.return) {
        onDecide({ decision: "deny", scope: "once" }, reason.trim() || undefined);
        return;
      }
      if (key.escape) {
        setReason(null);
        return;
      }
      if (key.backspace || key.delete) {
        setReason((value) => (value ?? "").slice(0, -1));
        return;
      }
      if (input && !key.ctrl && !key.meta) setReason((value) => (value ?? "") + input);
    },
    { isActive: isActive && reason !== null },
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
        {reason === null ? (
          <ConfirmMenu<ApprovalAnswer>
            options={options}
            initialIndex={suggested === -1 ? 0 : suggested}
            escapeValue={{ decision: "deny", scope: "once" }}
            isActive={isActive}
            onChoose={(answer) => {
              if (answer.withReason) {
                setReason("");
                return;
              }
              onDecide(answer);
            }}
          />
        ) : (
          <Box flexDirection="column">
            <Text color="red">Why are you refusing? The model is told exactly this.</Text>
            <Box>
              <Text color="red">{"› "}</Text>
              <Text>{reason}</Text>
              <Text inverse> </Text>
            </Box>
            <Text dimColor>Enter deny with this reason · Esc back to the choices</Text>
          </Box>
        )}
      </Box>
    </Box>
  );
}
