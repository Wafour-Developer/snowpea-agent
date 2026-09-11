/**
 * snowpea TUI root.
 *
 * Thin client: every decision (commands, tools, permissions, modes) lives in
 * the daemon. This component only renders `session.event` streams and forwards
 * input as `session.prompt` / `command.run`.
 */

import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";

import type { TuiClient } from "./rpc/client.js";
import type {
  ApprovalDecision,
  ApprovalRequestParams,
  ApprovalResponse,
  ApprovalScope,
  CommandInfo,
  Mode,
} from "./rpc/sdk.js";
import { SlashRegistry } from "./slash/registry.js";
import { initialState, reducer, type State } from "./state/store.js";
import { Chat } from "./components/Chat.js";
import { MessageStream } from "./components/MessageStream.js";
import { ToolCall } from "./components/ToolCall.js";
import { DiffView } from "./components/DiffView.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import { ApprovalQueue } from "./components/ApprovalQueue.js";
import { ModeBar } from "./components/ModeBar.js";
import { StatusLine } from "./components/StatusLine.js";
import { HelpPanel } from "./components/HelpPanel.js";

export const PLACEHOLDER_TEXT = "snowpea tui placeholder";

export interface AppProps {
  client: TuiClient;
  sessionId: string;
  mode: Mode;
  workdir: string;
  provider?: string;
  model?: string;
}

/** Renders the ordered transcript: messages, tool calls and diffs interleaved. */
function Timeline({ state, expandedCall }: { state: State; expandedCall: string | null }) {
  return (
    <Box flexDirection="column">
      {state.timeline.map((item, index) => {
        if (item.kind === "message") {
          const message = state.messages.find((m) => m.id === item.id);
          return message ? (
            <MessageStream key={`${item.id}-${index}`} messages={[message]} />
          ) : null;
        }
        if (item.kind === "tool") {
          const call = state.toolCalls.find((c) => c.callId === item.id);
          return call ? (
            <ToolCall key={`${item.id}-${index}`} call={call} expanded={expandedCall === item.id} />
          ) : null;
        }
        const diff = state.diffs.find((d) => d.id === item.id);
        return diff ? <DiffView key={`${item.id}-${index}`} diff={diff} /> : null;
      })}
    </Box>
  );
}

export function App({
  client,
  sessionId,
  mode,
  workdir,
  provider,
  model,
}: AppProps): React.ReactElement {
  const { exit } = useApp();
  const [state, dispatch] = useReducer(reducer, initialState);
  const [showHelp, setShowHelp] = useState(false);
  const [draft, setDraft] = useState("");
  const [expandedCall, setExpandedCall] = useState<string | null>(null);
  const registryRef = useRef<SlashRegistry>(new SlashRegistry(client, sessionId));
  /** Resolver for the approval promise the SDK is awaiting. */
  const approvalResolver = useRef<((response: ApprovalResponse) => void) | null>(null);

  useEffect(() => {
    dispatch({ type: "session/ready", sessionId, mode, provider, model });
    dispatch({ type: "status", status: client.getStatus() });

    client.setListeners({
      onSessionEvent: (event) => dispatch({ type: "session/event", event }),
      onStatus: (status) => dispatch({ type: "status", status }),
      onApprovalResolved: ({ requestId }) =>
        dispatch({ type: "approval/resolved", requestId }),
    });

    client.onApprovalRequest(
      (request: ApprovalRequestParams) =>
        new Promise<ApprovalResponse>((resolve) => {
          approvalResolver.current = resolve;
          dispatch({ type: "approval/request", request });
        }),
    );

    void registryRef.current
      .load()
      .then((commands: CommandInfo[]) => dispatch({ type: "commands", commands }))
      .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));

    void client
      .listApprovals(sessionId)
      .then((result) => dispatch({ type: "approval/list", requests: result.requests ?? [] }))
      .catch(() => {
        /* approval.list is advisory; a failure must not block the session. */
      });
  }, [client, sessionId, mode, provider, model]);

  const completions = useMemo(
    () => (draft.startsWith("/") ? registryRef.current.complete(draft) : []),
    [draft, state.commands],
  );

  const submit = useCallback(
    (text: string) => {
      dispatch({ type: "user/message", text });
      const registry = registryRef.current;
      const run = text.startsWith("/")
        ? registry.dispatch(text).then(async (result) => {
            // Skills and plugins can change the command table; re-read it.
            if (/^(help|skill|plugin)/.test(text.slice(1))) {
              const commands = await registry.refresh();
              dispatch({ type: "commands", commands });
            }
            if (text.slice(1).startsWith("help")) setShowHelp(true);
            return result;
          })
        : client.prompt(sessionId, text);
      void run.catch((error: unknown) =>
        dispatch({ type: "error", message: String(error) }),
      );
    },
    [client, sessionId],
  );

  const decideApproval = useCallback(
    (decision: ApprovalDecision, scope: ApprovalScope) => {
      const resolve = approvalResolver.current;
      const requestId = state.pendingApproval?.requestId;
      approvalResolver.current = null;
      if (requestId) dispatch({ type: "approval/resolved", requestId });
      resolve?.({ decision, scope });
    },
    [state.pendingApproval],
  );

  useInput((input, key) => {
    if (key.ctrl && input === "c") {
      exit();
      return;
    }
    if (key.ctrl && input === "o") {
      const last = state.toolCalls[state.toolCalls.length - 1];
      setExpandedCall((current) => (current ? null : (last?.callId ?? null)));
    }
  });

  const approvalActive = state.pendingApproval !== null;

  return (
    <Box flexDirection="column" paddingX={1}>
      <Box marginBottom={1}>
        <Text dimColor>snowpea · {workdir}</Text>
      </Box>

      <Timeline state={state} expandedCall={expandedCall} />

      {state.errors.length > 0 ? (
        <Text color="red">{state.errors[state.errors.length - 1]}</Text>
      ) : null}

      {showHelp ? <HelpPanel commands={state.commands} /> : null}

      <ApprovalQueue requests={state.approvalQueue} />

      {state.pendingApproval ? (
        <ApprovalPrompt request={state.pendingApproval} onDecide={decideApproval} />
      ) : (
        <Chat
          onSubmit={submit}
          completions={completions}
          onChange={setDraft}
          onInterrupt={() => void client.interrupt(sessionId).catch(() => undefined)}
          onToggleHelp={() => setShowHelp((v) => !v)}
          disabled={approvalActive}
        />
      )}

      <ModeBar mode={state.mode} />
      <StatusLine
        status={state.status}
        sessionId={state.sessionId}
        provider={state.provider}
        model={state.model}
        usage={state.usage}
        turnActive={state.turnActive}
      />
    </Box>
  );
}
