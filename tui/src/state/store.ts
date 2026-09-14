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
  CompactionStarted,
  Mode,
  QuestionRequestParams,
  SessionEvent,
  ToolProgress,
  TurnStarted,
} from "../rpc/sdk.js";
import type { ConnectionStatus } from "../rpc/client.js";
import type { FileDiagnostics, LspServer } from "./lsp.js";
import { replayTurnSummaryLine } from "./working.js";

/**
 * `note` is this surface talking, not the conversation: the `✓ Done` line a
 * replayed turn leaves behind, drawn without a speaker's marker so it reads as
 * the same aside a live turn leaves in the scrollback.
 */
export type MessageRole = "user" | "assistant" | "system" | "note";

export interface Message {
  id: string;
  role: MessageRole;
  text: string;
  /** True while `message.delta` chunks are still being appended. */
  streaming: boolean;
  /** Files sent with the prompt, shown under it in the transcript. */
  attachments?: { name: string; mime?: string; size?: number }[];
}

export type ToolCallState = "running" | "ok" | "error";

/** One line of a running tool's output. */
export interface ProgressLine {
  text: string;
  /** stderr is drawn apart from stdout; a delegate's text counts as stdout. */
  stream: "stdout" | "stderr";
}

/** Lines of live output kept per call. */
export const PROGRESS_TAIL = 8;

export interface ToolCallEntry {
  callId: string;
  name: string;
  args: Record<string, unknown>;
  state: ToolCallState;
  output?: string;
  error?: string;
  /** Wall-clock start, so a long-running call can say how long it has been. */
  startedAt?: number;
  /**
   * Live output while the call runs, newest last.
   *
   * The daemon streams `tool.progress` for the shell and for a delegate; this
   * is the tail of it, kept short because it is a glimpse of a running job, not
   * a log. The final `tool.result` replaces it.
   */
  progress?: ProgressLine[];
  /** True once the daemon said it stopped sending output for this call. */
  progressTruncated?: boolean;
}

export interface DiffEntry {
  id: string;
  path: string;
  patch: string;
  /** True when the patch creates the file rather than changing it. */
  created?: boolean;
}

/**
 * How much of the model's context window the session is using.
 *
 * Filled from the `context` event. The daemon may not know the window size, in
 * which case only `used` is meaningful.
 */
export interface ContextUsage {
  used: number;
  window: number | null;
  percent: number | null;
  /** True when the daemon counted tokens approximately rather than exactly. */
  estimated: boolean;
}

/** One compaction, so the transcript can show where history was folded away. */
export interface CompactionEntry {
  id: string;
  before: number;
  after: number;
}

/** Team task states, counted for the board line (contract §1, team.task.update). */
export type TeamTaskStatus =
  | "pending"
  | "queued"
  | "claimed"
  | "running"
  | "done"
  | "conflict"
  | "merged"
  | "failed";

export interface TeamTaskEntry {
  taskId: string;
  teamId: string;
  status: TeamTaskStatus;
  assignee: string | null;
}

/** Lifecycle of one delegated subagent, mirroring `SubagentStatus`. */
export type SubagentStatus = "queued" | "running" | "done" | "error";

export interface SubagentEntry {
  agentId: string;
  /** Agent definition it runs as, when it was given one. */
  name: string;
  task: string;
  /**
   * One-line label the delegating model wrote, in the user's language.
   *
   * Empty when it wrote none; the panel and the summary line then fall back to
   * their own wording, which is how every delegation before this field looked.
   */
  title: string;
  status: SubagentStatus;
  /** Most recent thing it said or did, shown while it runs. */
  lastText: string;
  /** Its final answer, once `subagent.done` has arrived. */
  summary: string;
  /** The child's own session, for clients that follow both streams. */
  sessionId: string | null;
  inputTokens: number;
  outputTokens: number;
  /** Wall-clock start and finish, so the panel can time each delegate. */
  startedAt: number;
  endedAt: number | null;
}

export type ApprovalSource = "interactive" | "queue";

export interface ApprovalEntry extends ApprovalRequestParams {
  source: ApprovalSource;
}

/** A question from `ask_user`, waiting on this surface. */
export type QuestionEntry = QuestionRequestParams;

export interface Usage {
  inputTokens: number;
  outputTokens: number;
}

/**
 * A prompt the daemon took but has not started yet.
 *
 * Sending while a turn is running queues the prompt rather than refusing it,
 * and the daemon says so with `turn.queued`. The text is not in that event —
 * this surface is the one that knows what it sent — so it is attached when
 * `session.prompt` answers with the same turn id.
 */
export interface QueuedPrompt {
  turnId: string;
  text: string;
  /** 1-based place behind the running turn. */
  position: number;
}

/** Ordered render list; each item points at one of the collections below. */
export type TimelineItem =
  | { kind: "message"; id: string }
  | { kind: "tool"; id: string }
  | { kind: "diff"; id: string }
  | { kind: "compaction"; id: string };

export interface State {
  sessionId: string | null;
  status: ConnectionStatus;
  mode: Mode;
  provider: string | null;
  model: string | null;
  /** Where the model came from — pin, profile, project, global — when told. */
  modelSource: string | null;
  messages: Message[];
  toolCalls: ToolCallEntry[];
  diffs: DiffEntry[];
  timeline: TimelineItem[];
  /** Interactive prompt (from `onRequest`), at most one at a time. */
  pendingApproval: ApprovalEntry | null;
  /** Unattended backlog, seeded from `approval.list`. */
  approvalQueue: ApprovalEntry[];
  /** The `ask_user` question this surface is being asked, at most one.
   *  It is drawn ahead of any approval: the daemon is blocked on it, and the
   *  question is the thing the user was in the middle of thinking about. */
  pendingQuestion: QuestionEntry | null;
  commands: CommandInfo[];
  /** Delegated children of this session, in the order they were spawned. */
  subagents: SubagentEntry[];
  /** Team board, keyed by task; only present while a `/team` run is going. */
  teamTasks: TeamTaskEntry[];
  usage: Usage;
  /** Context-window usage, once the daemon has reported any. */
  context: ContextUsage | null;
  /** Compaction markers, drawn as dividers in the transcript. */
  compactions: CompactionEntry[];
  /** Tools the session has available, from `tool.list`. */
  toolCount: number | null;
  /** Prompts accepted but not started, oldest first. */
  queued: QueuedPrompt[];
  /**
   * User prompts typed while the model was mid-answer, waiting for a place in
   * the transcript.
   *
   * A queued prompt must not be spliced into the timeline while an assistant
   * message is still streaming: the deltas that arrive after it would start a
   * second message, leaving the answer as two entries with the user's line
   * between them. Neither half ever closes, so both stay in the live region;
   * `message.done` then replaces only the newer half with the complete text and
   * the prefix is on screen twice. The prompt is visible in the queued list the
   * whole time, and joins the transcript after the message it interrupted.
   */
  deferredPrompts: Message[];
  /**
   * Prompts this surface drew itself, each waiting for its `message.user`.
   *
   * The daemon publishes the prompt when it joins the history, which is what
   * lets a resumed session show the user's own words. A live session has
   * already drawn that line — the moment Enter was pressed, rather than when
   * the turn reached the model — so the event would be the same prompt twice.
   * The local entry is kept (it is what makes typing feel immediate, and it is
   * what a queued prompt has instead of an event for the minutes it waits) and
   * its event is swallowed here.
   */
  pendingEchoes: string[];
  /** Language servers the daemon has running, from `lsp.status`. */
  lsp: LspServer[];
  /** Diagnostics counts per file, from `lsp.diagnostics` events. */
  diagnostics: Record<string, FileDiagnostics>;
  /**
   * Text of prompts by turn id, until their turn finishes.
   *
   * `session.prompt` answers with the turn id and the `turn.queued` event can
   * arrive either side of that answer, so the text is parked here and picked up
   * by whichever of the two comes second.
   */
  promptTexts: Record<string, string>;
  /**
   * Transcripts of the child sessions delegates run in, keyed by session id.
   *
   * A subagent is a session of its own; `session.resume` replays it and the
   * connection then receives its events live. They are kept apart from the main
   * transcript so opening an agent shows its conversation, not a mixture.
   */
  children: Record<string, State>;
  lastSeq: number;
  turnActive: boolean;
  /**
   * When the running turn actually began.
   *
   * `turn.started` says so; until the daemon sends it the surface stamps the
   * moment the prompt was accepted, which is the same thing for a turn that
   * did not wait.
   */
  turnStartedAt: number | null;
  /** True when the running turn sat in the queue before it began. */
  turnWaited: boolean;
  /** Set while a compaction is running; cleared by the `compaction` event. */
  compacting: { reason: NonNullable<CompactionStarted["reason"]>; before: number } | null;
  /**
   * Characters of hidden reasoning the model has streamed in this turn.
   *
   * Reasoning never joins the transcript — it is not the answer, and it counts
   * against the output budget — so the working line is the only place it shows.
   * Reset when a turn starts.
   */
  reasoningChars: number;
  errors: string[];
}

export const initialState: State = {
  sessionId: null,
  status: "connecting",
  mode: "accept",
  provider: null,
  model: null,
  modelSource: null,
  messages: [],
  toolCalls: [],
  diffs: [],
  timeline: [],
  pendingApproval: null,
  approvalQueue: [],
  pendingQuestion: null,
  commands: [],
  subagents: [],
  teamTasks: [],
  usage: { inputTokens: 0, outputTokens: 0 },
  context: null,
  compactions: [],
  toolCount: null,
  queued: [],
  deferredPrompts: [],
  pendingEchoes: [],
  promptTexts: {},
  lsp: [],
  diagnostics: {},
  children: {},
  lastSeq: 0,
  turnActive: false,
  turnStartedAt: null,
  turnWaited: false,
  compacting: null,
  reasoningChars: 0,
  errors: [],
};

export type Action =
  | { type: "session/ready"; sessionId: string; mode: Mode; provider?: string; model?: string }
  | { type: "status"; status: ConnectionStatus }
  | { type: "mode"; mode: Mode }
  | { type: "commands"; commands: CommandInfo[] }
  | { type: "tools"; count: number }
  | { type: "lsp/status"; servers: LspServer[] }
  /** A line this surface wrote itself, e.g. the `/lsp` table. */
  | { type: "note"; text: string }
  /** `session.prompt` answered: this turn id is the text we just sent. */
  | { type: "prompt/turn"; turnId: string; text: string }
  | {
      type: "user/message";
      text: string;
      attachments?: { name: string; mime?: string; size?: number }[];
      /**
       * False when the text will not come back as a `message.user` event.
       *
       * A slash command is not a model turn: the daemon runs it instead of
       * appending it to the history, so nothing is published and there is no
       * echo to wait for.
       */
      expectEvent?: boolean;
    }
  | { type: "session/reset"; sessionId: string }
  | { type: "session/event"; event: SessionEvent }
  /**
   * A whole `session.resume` backlog, applied in one pass.
   *
   * Dispatching the events one at a time is one reducer pass and one Ink frame
   * each, and every finished entry is promoted into `<Static>` on its own
   * frame — so a long session appears to type its own history back out. Folding
   * them here means the reconstructed transcript reaches the scrollback in a
   * single render.
   */
  | { type: "session/replay"; events: SessionEvent[] }
  | { type: "child/event"; sessionId: string; event: SessionEvent }
  | { type: "child/replay"; sessionId: string; events: SessionEvent[] }
  | { type: "approval/request"; request: ApprovalRequestParams }
  | { type: "approval/list"; requests: ApprovalRequestParams[] }
  | { type: "approval/resolved"; requestId: string }
  | { type: "question/request"; request: QuestionRequestParams }
  | { type: "question/resolved"; requestId: string }
  | { type: "errors/clear" }
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

/**
 * True when a unified diff adds a file rather than editing one.
 *
 * `--- /dev/null` is the conventional marker; a patch with no removals and no
 * old-file header is treated the same way, because not every producer writes
 * the marker.
 */
export function isCreation(patch: string): boolean {
  if (/^---\s+\/dev\/null/m.test(patch)) return true;
  const lines = patch.split("\n").filter((line) => line.length > 0);
  if (lines.length === 0) return false;
  const body = lines.filter((line) => !/^(\+\+\+|---|@@|diff |index )/.test(line));
  if (body.length === 0) return false;
  return body.every((line) => line.startsWith("+"));
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

/**
 * What to say when the model ran out of room.
 *
 * A turn that was resumed still delivered a whole answer, so it only gets a
 * footnote; one that is still cut off after the daemon's two continuations has
 * to say so, or a half review reads as a short one.
 */
export function outputLimitNote(
  truncated: boolean,
  continuations: number,
): string | null {
  if (truncated) return "response hit the output limit and is incomplete";
  if (continuations > 0) return "response hit the output limit; continued";
  return null;
}

function withLimitNote(state: State, payload: Record<string, unknown>): State {
  const note = outputLimitNote(
    Boolean(payload.truncated),
    Number(payload.continuations ?? 0),
  );
  if (!note) return state;
  const message: Message = { id: nextId("msg"), role: "system", text: note, streaming: false };
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
    return withLimitNote({ ...state, messages }, payload);
  }
  if (text === undefined) return state;
  const message: Message = { id: nextId("msg"), role, text, streaming: false };
  return withLimitNote(
    {
      ...state,
      messages: [...state.messages, message],
      timeline: pushTimeline(state, { kind: "message", id: message.id }),
    },
    payload,
  );
}

/**
 * Move prompts that waited for the answer into the transcript, in order.
 *
 * Called wherever a message stops streaming, so the user's line lands after the
 * message it interrupted rather than inside it.
 */
function flushDeferred(state: State): State {
  if (state.deferredPrompts.length === 0) return state;
  let next: State = { ...state, deferredPrompts: [] };
  for (const message of state.deferredPrompts) {
    next = {
      ...next,
      messages: [...next.messages, message],
      timeline: pushTimeline(next, { kind: "message", id: message.id }),
    };
  }
  return next;
}

/** Replace one subagent entry in place; unknown ids are ignored. */
function patchSubagent(
  state: State,
  agentId: string,
  patch: (entry: SubagentEntry) => SubagentEntry,
): State {
  const index = state.subagents.findIndex((entry) => entry.agentId === agentId);
  if (index === -1) return state;
  const subagents = state.subagents.slice();
  subagents[index] = patch(subagents[index]);
  return { ...state, subagents };
}

/**
 * How a replayed event differs from a live one.
 *
 * Only two things: a live turn's `✓ Done` line is written by the surface as it
 * watches the turn end, so a replay has to rebuild it here, and the only clock
 * it has is the timestamps on the events themselves.
 */
interface ApplyOptions {
  replay?: boolean;
  /** Timestamp of the first event of the turn being replayed. */
  turnStartedAt?: string | null;
}

function applySessionEvent(
  state: State,
  event: SessionEvent,
  options: ApplyOptions = {},
): State {
  const payload = (event.payload ?? {}) as Record<string, any>;
  // An event this session has already accounted for is dropped rather than
  // applied twice. The daemon may re-send the tail it just replayed — a resume
  // races with the live subscription it sets up — and applying a delta twice
  // would duplicate text that is already in the transcript.
  if (typeof event.seq === "number" && state.lastSeq > 0 && event.seq <= state.lastSeq) {
    return state;
  }
  const base: State =
    typeof event.seq === "number" && event.seq > state.lastSeq
      ? { ...state, lastSeq: event.seq }
      : state;

  switch (event.kind) {
    // The prompt that opened the turn. Live, this surface drew it already; on a
    // replay it is the only record of what was asked.
    case "message.user": {
      const text = String(payload.text ?? "");
      const echo = base.pendingEchoes.indexOf(text);
      if (echo !== -1) {
        const pendingEchoes = base.pendingEchoes.slice();
        pendingEchoes.splice(echo, 1);
        return { ...base, pendingEchoes };
      }
      const files = Array.isArray(payload.attachments)
        ? payload.attachments
          .map((entry: any) => ({ name: String(entry?.name ?? "") }))
          .filter((entry: { name: string }) => entry.name.length > 0)
        : [];
      const message: Message = {
        id: nextId("msg"),
        role: "user",
        text,
        streaming: false,
        attachments: files.length > 0 ? files : undefined,
      };
      return {
        ...base,
        messages: [...base.messages, message],
        timeline: pushTimeline(base, { kind: "message", id: message.id }),
      };
    }

    case "message.delta":
      return appendDelta(base, String(payload.text ?? ""));

    // Thinking, not an answer: counted for the working line, never appended.
    case "message.reasoning":
      return { ...base, reasoningChars: Number(payload.chars ?? base.reasoningChars) };

    case "message.done":
      return flushDeferred(finishMessage(base, payload));

    case "tool.call": {
      const entry: ToolCallEntry = {
        callId: String(payload.callId ?? nextId("call")),
        name: String(payload.name ?? "unknown"),
        args: (payload.args ?? {}) as Record<string, unknown>,
        state: "running",
        startedAt: Number(payload.at ?? Date.now()),
      };
      // A tool call closes whatever the model was saying: it stopped writing to
      // go and do something, so that message will never grow again. The daemon
      // does not send `message.done` here — it would reach the chat gateways
      // and forward a model's intermediate muttering to people's phones — so
      // the inference is made here. It matters because an open message can
      // never leave the live region, and entries may only reach the scrollback
      // in order: one unclosed message pins every tool card and diff behind it
      // on screen for the rest of the turn, until the region is taller than the
      // terminal and Ink starts clearing the screen for every frame.
      const last = base.messages[base.messages.length - 1];
      const messages =
        last && last.streaming && last.role === "assistant"
          ? base.messages.slice(0, -1).concat({ ...last, streaming: false })
          : base.messages;
      // The message just closed, so anything that was waiting on it can land —
      // before the tool card, which is where it happened in time.
      const settled = flushDeferred({ ...base, messages });
      return {
        ...settled,
        toolCalls: [...settled.toolCalls, entry],
        timeline: pushTimeline(settled, { kind: "tool", id: entry.callId }),
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
      const patch = String(payload.patch ?? "");
      const entry: DiffEntry = {
        id: nextId("diff"),
        path: String(payload.path ?? ""),
        patch,
        created: payload.created === true || isCreation(patch),
      };
      return {
        ...base,
        diffs: [...base.diffs, entry],
        timeline: pushTimeline(base, { kind: "diff", id: entry.id }),
      };
    }

    case "subagent.spawn": {
      const entry: SubagentEntry = {
        agentId: String(payload.agentId ?? nextId("agent")),
        name: String(payload.name ?? ""),
        task: String(payload.task ?? ""),
        title: String(payload.title ?? ""),
        status: (payload.status ?? "queued") as SubagentStatus,
        lastText: "",
        summary: "",
        sessionId: typeof payload.sessionId === "string" ? payload.sessionId : null,
        inputTokens: 0,
        outputTokens: 0,
        // Stamped here because the event carries no clock of its own; the panel
        // needs a start to count from. `at` lets tests pin it.
        startedAt: Number(payload.at ?? Date.now()),
        endedAt: null,
      };
      // A spawn for an id we already track is a replay, not a second child.
      if (base.subagents.some((s) => s.agentId === entry.agentId)) return base;
      return { ...base, subagents: [...base.subagents, entry] };
    }

    case "subagent.update":
      return patchSubagent(base, String(payload.agentId ?? ""), (entry) => ({
        ...entry,
        status: (payload.status ?? entry.status) as SubagentStatus,
        lastText: String(payload.lastText ?? payload.text ?? entry.lastText),
        name: String(payload.name ?? entry.name),
        title: String(payload.title ?? entry.title),
        outputTokens: Number(payload.usage?.outputTokens ?? entry.outputTokens),
        sessionId:
          typeof payload.sessionId === "string" ? payload.sessionId : entry.sessionId,
      }));

    case "subagent.done":
      return patchSubagent(base, String(payload.agentId ?? ""), (entry) => ({
        ...entry,
        status: (payload.status ?? (payload.ok === false ? "error" : "done")) as SubagentStatus,
        summary: String(payload.summary ?? payload.result ?? ""),
        title: String(payload.title ?? entry.title),
        lastText: "",
        inputTokens: Number(payload.usage?.inputTokens ?? entry.inputTokens),
        outputTokens: Number(payload.usage?.outputTokens ?? entry.outputTokens),
        endedAt: Number(payload.at ?? Date.now()),
      }));

    // The daemon re-routed the session: a pin, a profile change, or a project
    // default that just took effect. It is the authority, so the HUD follows it
    // rather than what this surface last asked for.
    case "model.changed": {
      // A field that is present but null means "cleared" — the daemon sends
      // that when a pin is dropped — while an absent field means "unchanged".
      // Keeping the old model after a clear would name a pin that is gone.
      const model = "model" in payload ? (payload.model ?? null) : base.model;
      const provider = "provider" in payload ? (payload.provider ?? null) : base.provider;
      const source =
        typeof payload.source === "string" && payload.source.length > 0
          ? payload.source
          : "model" in payload && payload.model === null
            ? null
            : base.modelSource;
      return { ...base, model, provider, modelSource: source };
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

    // The daemon reports context usage after every turn and after a compaction.
    // Older daemons never send it, which is why the HUD hides the segment until
    // the first one arrives.
    // The daemon names these `used`/`window`; `usedTokens`/`windowTokens` are
    // accepted too so a differently-shaped emitter still lights the segment up.
    case "context": {
      const used = Number(payload.used ?? payload.usedTokens ?? 0);
      const rawWindow = payload.window ?? payload.windowTokens ?? null;
      const window = rawWindow === null || rawWindow === undefined ? null : Number(rawWindow);
      const percent =
        payload.percent === null || payload.percent === undefined
          ? window && window > 0
            ? (used / window) * 100
            : null
          : Number(payload.percent);
      return {
        ...base,
        context: { used, window, percent, estimated: payload.estimated === true },
      };
    }

    case "compaction": {
      // Whatever started it has finished; the divider says what it did.
      const entry: CompactionEntry = {
        id: nextId("compaction"),
        before: Number(payload.before ?? payload.from ?? 0),
        after: Number(payload.after ?? payload.to ?? 0),
      };
      return {
        ...base,
        compacting: null,
        compactions: [...base.compactions, entry],
        timeline: pushTimeline(base, { kind: "compaction", id: entry.id }),
      };
    }

    case "team.task.update": {
      const taskId = String(payload.taskId ?? "");
      if (taskId.length === 0) return base;
      const entry: TeamTaskEntry = {
        taskId,
        teamId: String(payload.teamId ?? ""),
        status: (payload.status ?? "pending") as TeamTaskStatus,
        assignee: typeof payload.assignee === "string" ? payload.assignee : null,
      };
      const index = base.teamTasks.findIndex((task) => task.taskId === taskId);
      if (index === -1) return { ...base, teamTasks: [...base.teamTasks, entry] };
      const teamTasks = base.teamTasks.slice();
      teamTasks[index] = entry;
      return { ...base, teamTasks };
    }

    case "error":
      return {
        ...base,
        errors: [...base.errors, `${payload.code ?? "error"}: ${payload.message ?? ""}`],
      };

    // A language server published for a file this session touched. Only the
    // counts arrive: the text of every diagnostic is already in the tool result
    // the model saw.
    case "lsp.diagnostics": {
      const path = String(payload.path ?? "");
      if (path.length === 0) return base;
      const entry: FileDiagnostics = {
        count: Number(payload.count ?? 0),
        errors: Number(payload.errors ?? 0),
        warnings: Number(payload.warnings ?? 0),
      };
      // A file that came back clean drops out rather than sitting at zero.
      if (entry.count <= 0) {
        if (!(path in base.diagnostics)) return base;
        const diagnostics = { ...base.diagnostics };
        delete diagnostics[path];
        return { ...base, diagnostics };
      }
      return { ...base, diagnostics: { ...base.diagnostics, [path]: entry } };
    }

    // The turn is running now, which is when its clock starts. A turn that sat
    // in the queue says so: the elapsed time on screen would otherwise look
    // like the model taking its time.
    case "turn.started": {
      const started = payload as TurnStarted;
      const turnId = started.turnId ?? "";
      // The daemon says whether the turn was queued; a `turn.dequeued` may have
      // already emptied the local queue, so its own flag is asked first.
      const waited =
        typeof started.queued === "boolean"
          ? started.queued
          : base.queued.some((entry) => entry.turnId === turnId);
      // Replay has no live clock; the event's own timestamp is the only one.
      const stamped = Date.parse(typeof event.ts === "string" ? event.ts : "");
      const text = started.prompt ?? "";
      return {
        ...base,
        turnActive: true,
        turnStartedAt: Number.isFinite(stamped) ? stamped : Date.now(),
        turnWaited: waited,
        promptTexts:
          text.length > 0 && turnId.length > 0
            ? { ...base.promptTexts, [turnId]: text }
            : base.promptTexts,
        queued: base.queued.filter((entry) => entry.turnId !== turnId),
      };
    }

    // Output from a tool while it is still running.
    case "tool.progress": {
      const progressed = payload as ToolProgress;
      const index = base.toolCalls.findIndex((call) => call.callId === progressed.callId);
      if (index === -1) return base;
      const stream: ProgressLine["stream"] = progressed.stream === "stderr" ? "stderr" : "stdout";
      const arriving = (progressed.chunk ?? "").split("\n").filter((line) => line.length > 0);
      if (arriving.length === 0 && progressed.truncated !== true) return base;
      const call = base.toolCalls[index];
      const progress = [...(call.progress ?? []), ...arriving.map((text) => ({ text, stream }))];
      const toolCalls = base.toolCalls.slice();
      toolCalls[index] = {
        ...call,
        progress: progress.slice(-PROGRESS_TAIL),
        progressTruncated: call.progressTruncated || progressed.truncated === true,
      };
      return { ...base, toolCalls };
    }

    // Compaction takes a moment and a turn cannot run during it; saying so
    // beats a screen that looks stuck.
    case "compaction.started": {
      const compaction = payload as CompactionStarted;
      return {
        ...base,
        compacting: {
          reason: compaction.reason ?? "auto",
          before: compaction.before ?? 0,
        },
      };
    }

    case "turn.queued": {
      const turnId = String(payload.turnId ?? "");
      if (turnId.length === 0) return base;
      const position = Number(payload.position ?? base.queued.length + 1);
      const index = base.queued.findIndex((entry) => entry.turnId === turnId);
      if (index !== -1) {
        const queued = base.queued.slice();
        queued[index] = { ...queued[index], position };
        return { ...base, queued };
      }
      // The prompt call may already have said what this turn is.
      const text = base.promptTexts[turnId] ?? "";
      return { ...base, queued: [...base.queued, { turnId, text, position }] };
    }

    case "turn.dequeued": {
      const turnId = String(payload.turnId ?? "");
      return { ...base, queued: base.queued.filter((entry) => entry.turnId !== turnId) };
    }

    case "turn.done": {
      const finished = String(payload.turnId ?? "");
      const promptTexts = { ...base.promptTexts };
      if (finished) delete promptTexts[finished];
      const messages = base.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m));
      // Nothing may be left stranded by a turn that ended without a final
      // message — an interrupt, or an error.
      const settled = {
        ...flushDeferred({ ...base, messages }),
        promptTexts,
        turnActive: false,
        turnStartedAt: null,
        turnWaited: false,
      };
      if (!options.replay) return settled;
      // A live turn's summary is written by `app.tsx`, which watched it run. A
      // replayed one has to be rebuilt, and the event timestamps are the only
      // clock there is.
      const started = Date.parse(options.turnStartedAt ?? "");
      const ended = Date.parse(typeof event.ts === "string" ? event.ts : "");
      const elapsed =
        Number.isFinite(started) && Number.isFinite(ended) ? ended - started : null;
      const note: Message = {
        id: nextId("msg"),
        role: "note",
        text: replayTurnSummaryLine(
          String(payload.reason ?? "complete") === "complete",
          elapsed,
        ),
        streaming: false,
      };
      return {
        ...settled,
        messages: [...settled.messages, note],
        timeline: pushTimeline(settled, { kind: "message", id: note.id }),
      };
    }

    default:
      // team.task.update and future kinds are accepted silently.
      return base;
  }
}

/**
 * Fold a whole `session.resume` backlog in one pass.
 *
 * A turn's wall-clock span is the gap between its first event and its
 * `turn.done`; the first event of a turn is whatever follows the previous one's
 * `turn.done`, which is the prompt itself for anything recorded since
 * `message.user` existed and the first token of the answer for a log older
 * than that.
 */
function replayEvents(state: State, events: SessionEvent[]): State {
  let turnStartedAt: string | null = null;
  let next = state;
  for (const event of events) {
    if (turnStartedAt === null && typeof event.ts === "string") turnStartedAt = event.ts;
    next = applySessionEvent(next, event, { replay: true, turnStartedAt });
    if (event.kind === "turn.done") turnStartedAt = null;
  }
  return next;
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "session/reset":
      return { ...initialState, status: state.status, sessionId: action.sessionId };

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

    case "tools":
      return { ...state, toolCount: action.count };

    case "lsp/status":
      return { ...state, lsp: action.servers };

    case "note": {
      const message: Message = {
        id: nextId("msg"),
        role: "system",
        text: action.text,
        streaming: false,
      };
      return {
        ...state,
        messages: [...state.messages, message],
        timeline: pushTimeline(state, { kind: "message", id: message.id }),
      };
    }

    case "prompt/turn": {
      const promptTexts = { ...state.promptTexts, [action.turnId]: action.text };
      const index = state.queued.findIndex((entry) => entry.turnId === action.turnId);
      if (index === -1) return { ...state, promptTexts };
      const queued = state.queued.slice();
      queued[index] = { ...queued[index], text: action.text };
      return { ...state, queued, promptTexts };
    }

    case "errors/clear":
      return state.errors.length === 0 ? state : { ...state, errors: [] };

    case "user/message": {
      const message: Message = {
        id: nextId("msg"),
        role: "user",
        text: action.text,
        streaming: false,
        attachments: action.attachments?.length ? action.attachments : undefined,
      };
      // The daemon will publish this same prompt as `message.user` when the
      // turn reaches the model; that copy is for a resume, and is dropped here.
      const pendingEchoes =
        action.expectEvent === false
          ? state.pendingEchoes
          : [...state.pendingEchoes, action.text];
      // Mid-answer, this prompt waits: see `deferredPrompts`. The queued list
      // under the input is what shows it in the meantime.
      if (state.messages.some((m) => m.streaming)) {
        return {
          ...state,
          pendingEchoes,
          deferredPrompts: [...state.deferredPrompts, message],
          reasoningChars: 0,
        };
      }
      return {
        ...state,
        pendingEchoes,
        messages: [...state.messages, message],
        timeline: pushTimeline(state, { kind: "message", id: message.id }),
        turnActive: true,
        reasoningChars: 0,
      };
    }

    case "session/event":
      return applySessionEvent(state, action.event);

    case "session/replay":
      return replayEvents(state, action.events);

    case "child/replay": {
      const current = state.children[action.sessionId] ?? initialState;
      const next = replayEvents(current, action.events);
      if (next === current) return state;
      return { ...state, children: { ...state.children, [action.sessionId]: next } };
    }

    case "child/event": {
      const current = state.children[action.sessionId] ?? initialState;
      const next = applySessionEvent(current, action.event);
      if (next === current) return state;
      return { ...state, children: { ...state.children, [action.sessionId]: next } };
    }

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

    case "question/request":
      return { ...state, pendingQuestion: action.request };

    case "question/resolved":
      return {
        ...state,
        pendingQuestion:
          state.pendingQuestion?.requestId === action.requestId ? null : state.pendingQuestion,
      };

    case "error":
      return { ...state, errors: [...state.errors, action.message] };

    default:
      return state;
  }
}

export const APPROVAL_SCOPES: ApprovalScope[] = ["once", "session", "project", "always"];
