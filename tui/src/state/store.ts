/**
 * TUI state: a plain reducer driven by `session.event` notifications plus a few
 * local actions. No store library — `useReducer` in `app.tsx` owns an instance.
 *
 * Event kinds and payload shapes follow m1-core-contract §1.
 */

import type {
  ApprovalRequestParams,
  ApprovalScope,
  CommandInfo,
  Mode,
  SessionEvent,
} from "../rpc/sdk.js";
import type { ConnectionStatus } from "../rpc/client.js";

export type MessageRole = "user" | "assistant" | "system";

export interface Message {
  id: string;
  role: MessageRole;
  text: string;
  /** True while `message.delta` chunks are still being appended. */
  streaming: boolean;
}

export type ToolCallState = "running" | "ok" | "error";

export interface ToolCallEntry {
  callId: string;
  name: string;
  args: Record<string, unknown>;
  state: ToolCallState;
  output?: string;
  error?: string;
}

export interface DiffEntry {
  id: string;
  path: string;
  patch: string;
}

export type ApprovalSource = "interactive" | "queue";

export interface ApprovalEntry extends ApprovalRequestParams {
  source: ApprovalSource;
}

export interface Usage {
  inputTokens: number;
  outputTokens: number;
}

/** Ordered render list; each item points at one of the collections below. */
export type TimelineItem =
  | { kind: "message"; id: string }
  | { kind: "tool"; id: string }
  | { kind: "diff"; id: string };

export interface State {
  sessionId: string | null;
  status: ConnectionStatus;
  mode: Mode;
  provider: string | null;
  model: string | null;
  messages: Message[];
  toolCalls: ToolCallEntry[];
  diffs: DiffEntry[];
  timeline: TimelineItem[];
  /** Interactive prompt (from `onRequest`), at most one at a time. */
  pendingApproval: ApprovalEntry | null;
  /** Unattended backlog, seeded from `approval.list`. */
  approvalQueue: ApprovalEntry[];
  commands: CommandInfo[];
  usage: Usage;
  lastSeq: number;
  turnActive: boolean;
  errors: string[];
}

export const initialState: State = {
  sessionId: null,
  status: "connecting",
  mode: "accept",
  provider: null,
  model: null,
  messages: [],
  toolCalls: [],
  diffs: [],
  timeline: [],
  pendingApproval: null,
  approvalQueue: [],
  commands: [],
  usage: { inputTokens: 0, outputTokens: 0 },
  lastSeq: 0,
  turnActive: false,
  errors: [],
};

export type Action =
  | { type: "session/ready"; sessionId: string; mode: Mode; provider?: string; model?: string }
  | { type: "status"; status: ConnectionStatus }
  | { type: "mode"; mode: Mode }
  | { type: "commands"; commands: CommandInfo[] }
  | { type: "user/message"; text: string }
  | { type: "session/event"; event: SessionEvent }
  | { type: "approval/request"; request: ApprovalRequestParams }
  | { type: "approval/list"; requests: ApprovalRequestParams[] }
  | { type: "approval/resolved"; requestId: string }
  | { type: "error"; message: string };

let counter = 0;
function nextId(prefix: string): string {
  counter += 1;
  return `${prefix}-${counter}`;
}

/** Exposed for deterministic ids in tests. */
export function __resetIdCounter(): void {
  counter = 0;
}

function pushTimeline(state: State, item: TimelineItem): TimelineItem[] {
  return [...state.timeline, item];
}

function appendDelta(state: State, text: string): State {
  const last = state.messages[state.messages.length - 1];
  if (last && last.role === "assistant" && last.streaming) {
    const messages = state.messages.slice(0, -1).concat({ ...last, text: last.text + text });
    return { ...state, messages };
  }
  const message: Message = { id: nextId("msg"), role: "assistant", text, streaming: true };
  return {
    ...state,
    messages: [...state.messages, message],
    timeline: pushTimeline(state, { kind: "message", id: message.id }),
  };
}

function finishMessage(state: State, payload: Record<string, unknown>): State {
  const text = typeof payload.text === "string" ? payload.text : undefined;
  const role = (typeof payload.role === "string" ? payload.role : "assistant") as MessageRole;
  const last = state.messages[state.messages.length - 1];
  if (last && last.streaming && last.role === role) {
    const messages = state.messages
      .slice(0, -1)
      .concat({ ...last, text: text ?? last.text, streaming: false });
    return { ...state, messages };
  }
  if (text === undefined) return state;
  const message: Message = { id: nextId("msg"), role, text, streaming: false };
  return {
    ...state,
    messages: [...state.messages, message],
    timeline: pushTimeline(state, { kind: "message", id: message.id }),
  };
}

function applySessionEvent(state: State, event: SessionEvent): State {
  const payload = (event.payload ?? {}) as Record<string, any>;
  const base: State =
    typeof event.seq === "number" && event.seq > state.lastSeq
      ? { ...state, lastSeq: event.seq }
      : state;

  switch (event.kind) {
    case "message.delta":
      return appendDelta(base, String(payload.text ?? ""));

    case "message.done":
      return finishMessage(base, payload);

    case "tool.call": {
      const entry: ToolCallEntry = {
        callId: String(payload.callId ?? nextId("call")),
        name: String(payload.name ?? "unknown"),
        args: (payload.args ?? {}) as Record<string, unknown>,
        state: "running",
      };
      return {
        ...base,
        toolCalls: [...base.toolCalls, entry],
        timeline: pushTimeline(base, { kind: "tool", id: entry.callId }),
      };
    }

    case "tool.result": {
      const callId = String(payload.callId ?? "");
      const ok = payload.ok !== false;
      const idx = base.toolCalls.findIndex((c) => c.callId === callId);
      const patch = {
        state: (ok ? "ok" : "error") as ToolCallState,
        output: typeof payload.output === "string" ? payload.output : undefined,
        error: typeof payload.error === "string" ? payload.error : undefined,
      };
      if (idx === -1) {
        const entry: ToolCallEntry = {
          callId: callId || nextId("call"),
          name: String(payload.name ?? "unknown"),
          args: {},
          ...patch,
        };
        return {
          ...base,
          toolCalls: [...base.toolCalls, entry],
          timeline: pushTimeline(base, { kind: "tool", id: entry.callId }),
        };
      }
      const toolCalls = base.toolCalls.slice();
      toolCalls[idx] = { ...toolCalls[idx], ...patch };
      return { ...base, toolCalls };
    }

    case "diff": {
      const entry: DiffEntry = {
        id: nextId("diff"),
        path: String(payload.path ?? ""),
        patch: String(payload.patch ?? ""),
      };
      return {
        ...base,
        diffs: [...base.diffs, entry],
        timeline: pushTimeline(base, { kind: "diff", id: entry.id }),
      };
    }

    case "mode.changed":
      return { ...base, mode: (payload.mode ?? base.mode) as Mode };

    case "usage":
      return {
        ...base,
        usage: {
          inputTokens: base.usage.inputTokens + Number(payload.inputTokens ?? 0),
          outputTokens: base.usage.outputTokens + Number(payload.outputTokens ?? 0),
        },
      };

    case "error":
      return {
        ...base,
        errors: [...base.errors, `${payload.code ?? "error"}: ${payload.message ?? ""}`],
      };

    case "turn.done": {
      const messages = base.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m));
      return { ...base, messages, turnActive: false };
    }

    default:
      // subagent.*, team.task.update and future kinds are accepted silently.
      return base;
  }
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "session/ready":
      return {
        ...state,
        sessionId: action.sessionId,
        mode: action.mode,
        provider: action.provider ?? state.provider,
        model: action.model ?? state.model,
      };

    case "status":
      return { ...state, status: action.status };

    case "mode":
      return { ...state, mode: action.mode };

    case "commands":
      return { ...state, commands: action.commands };

    case "user/message": {
      const message: Message = {
        id: nextId("msg"),
        role: "user",
        text: action.text,
        streaming: false,
      };
      return {
        ...state,
        messages: [...state.messages, message],
        timeline: pushTimeline(state, { kind: "message", id: message.id }),
        turnActive: true,
      };
    }

    case "session/event":
      return applySessionEvent(state, action.event);

    case "approval/request":
      return { ...state, pendingApproval: { ...action.request, source: "interactive" } };

    case "approval/list": {
      const pendingId = state.pendingApproval?.requestId;
      return {
        ...state,
        approvalQueue: action.requests
          .filter((r) => r.requestId !== pendingId)
          .map((r) => ({ ...r, source: "queue" as const })),
      };
    }

    case "approval/resolved":
      return {
        ...state,
        pendingApproval:
          state.pendingApproval?.requestId === action.requestId ? null : state.pendingApproval,
        approvalQueue: state.approvalQueue.filter((r) => r.requestId !== action.requestId),
      };

    case "error":
      return { ...state, errors: [...state.errors, action.message] };

    default:
      return state;
  }
}

export const APPROVAL_SCOPES: ApprovalScope[] = ["once", "session", "project", "always"];
