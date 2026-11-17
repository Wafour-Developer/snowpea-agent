/**
 * Jarvis mode (1.53.0, beta) as the TUI keeps it.
 *
 * Jarvis is one persistent session that supervises every other session. This
 * module holds what the screen needs about it: whether the daemon can do it at
 * all, the status, the overview rows, what is waiting for the person, and the
 * latest decisions; and the pure helpers that turn those into text. Nothing here
 * touches the terminal or the network, so `test/jarvis.test.ts` covers it all.
 */

import type {
  ApprovalRequestParams,
  ExternalRef,
  JarvisAccessEvent,
  JarvisDecisionEvent,
  JarvisDelegationEvent,
  JarvisEscalationDismissed,
  JarvisEscalationEvent,
  JarvisLedgerEntry,
  JarvisOverviewRow,
  JarvisStateEvent,
  JarvisStatus,
  QuestionRequestParams,
  SessionJarvisState,
} from "../rpc/sdk.js";
import { fit, spread } from "../layout/fit.js";
import { textWidth } from "../layout/text-width.js";
import { cliLabel } from "./external.js";
import { agentOfExternal, agentOfSession, type AgentIdentity } from "./agent-tag.js";
import { NOT_APPLIED_NOTE } from "./security.js";
import { ALWAYS_ASKS, alwaysAsksReason, alwaysAsksYou } from "./person-only.js";

// --- capability gating -------------------------------------------------------

/** What the daemon advertised in `system.hello`, narrowed to this feature. */
export interface Support {
  /** `jarvis.*` methods, the Jarvis session and `jarvis.state`. */
  jarvis: boolean;
  /** The ledger, grants and the decision/escalation events. */
  delegation: boolean;
  /** `external.*` methods. */
  external: boolean;
}

export function supportOf(capabilities: readonly string[] | undefined): Support {
  const caps = new Set(capabilities ?? []);
  return {
    jarvis: caps.has("jarvis"),
    delegation: caps.has("jarvisDelegation"),
    external: caps.has("externalSessions"),
  };
}

/** The one line an older daemon gets instead of the feature. */
export function requiresLine(feature: "jarvis" | "external"): string {
  const what = feature === "jarvis" ? "Jarvis mode (beta)" : "External CLI sessions (beta)";
  return `${what} requires core 1.53 — this daemon is older. Run /update.`;
}

// --- state -------------------------------------------------------------------

export const MAX_DECISIONS = 12;
export const MAX_ESCALATIONS = 20;

/** One thing a person has to answer, from any session. */
export interface WaitingItem {
  requestId: string;
  kind: "approval" | "question";
  sessionId: string;
  external: ExternalRef | null;
  /** The session's name as the person knows it. */
  label: string;
  /** One line: the tool and what it wants, or the first question. */
  summary: string;
  /** Only a person may decide it, whatever the mode (`neverDelegable`, `personOnly`, a mode picker). */
  alwaysAsks: boolean;
  request: ApprovalRequestParams | QuestionRequestParams;
}

export interface JarvisState {
  /** `jarvis.status`; null until asked or on a daemon without the feature. */
  status: JarvisStatus | null;
  rows: JarvisOverviewRow[];
  /** Every request the daemon lists, whoever it is routed to; route events edit it in place. */
  requests: WaitingItem[];
  /** The part of `requests` that waits on the person. */
  waiting: WaitingItem[];
  /** Requests routed to Jarvis that nobody has answered yet. */
  deciding: number;
  decisions: JarvisLedgerEntry[];
  /** Open notices (finished, error, stalled, budget, jarvis); they persist until dismissed. */
  escalations: JarvisEscalationEvent[];
  /** `session.list`'s `jarvis` per session, for where a level comes from. */
  sessionStates: Record<string, SessionJarvisState>;
  /** The latest thing worth a toast; the app shows and clears it. */
  notice: string | null;
  /** Dedupe keys of the notices already toasted (see noticeKey), newest last. */
  announced: string[];
  loadedAt: number | null;
}

export const initialJarvis: JarvisState = {
  status: null,
  rows: [],
  requests: [],
  waiting: [],
  deciding: 0,
  decisions: [],
  escalations: [],
  sessionStates: {},
  notice: null,
  announced: [],
  loadedAt: null,
};

export type JarvisAction =
  | { type: "status"; status: JarvisStatus }
  | { type: "overview"; rows: JarvisOverviewRow[]; at: number }
  | {
      type: "waiting";
      approvals: ApprovalRequestParams[];
      questions: QuestionRequestParams[];
    }
  /** `approval.pending` / `question.pending`: one request joined the shared queue. */
  | { type: "pending"; kind: "approval" | "question"; request: ApprovalRequestParams | QuestionRequestParams }
  /** `approval.route.changed` / `question.route.changed`. */
  | { type: "route"; requestId: string; route: ApprovalRequestParams["route"] }
  | { type: "decisions"; entries: JarvisLedgerEntry[] }
  | { type: "event/state"; payload: JarvisStateEvent }
  | { type: "event/decision"; payload: JarvisDecisionEvent }
  /** `onScreen`: the Jarvis screen is open; `sessionId`: the session the person is in. Outside the screen nothing is toasted. */
  | { type: "event/escalation"; payload: JarvisEscalationEvent; onScreen?: boolean; sessionId?: string }
  | { type: "escalations"; entries: JarvisEscalationEvent[] }
  | { type: "event/dismissed"; payload: JarvisEscalationDismissed }
  | { type: "sessionStates"; states: Record<string, SessionJarvisState> }
  | { type: "sessionState"; sessionId: string; state: SessionJarvisState | null }
  | { type: "event/delegation"; payload: JarvisDelegationEvent }
  | { type: "resolved"; requestId: string }
  | { type: "notice/clear" }
  | { type: "reset" };

function newestFirst(entries: JarvisLedgerEntry[]): JarvisLedgerEntry[] {
  return [...entries].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts)).slice(0, MAX_DECISIONS);
}

export function jarvisReducer(state: JarvisState, action: JarvisAction): JarvisState {
  switch (action.type) {
    case "reset":
      return initialJarvis;

    case "status":
      return action.status.enabled ? { ...state, status: action.status } : { ...initialJarvis, status: action.status };

    case "overview":
      return { ...state, rows: action.rows, loadedAt: action.at };

    case "waiting":
      return withRequests(state, [
        ...action.approvals.map((request) => waitingFrom("approval", request, state.rows)),
        ...action.questions.map((request) => waitingFrom("question", request, state.rows)),
      ]);

    case "pending": {
      const item = waitingFrom(action.kind, action.request, state.rows);
      return withRequests(state, [...state.requests.filter((known) => known.requestId !== item.requestId), item]);
    }

    case "route": {
      if (!state.requests.some((item) => item.requestId === action.requestId)) return state;
      return withRequests(
        state,
        state.requests.map((item) =>
          item.requestId === action.requestId ? { ...item, request: { ...item.request, route: action.route } } : item,
        ),
      );
    }

    case "decisions":
      return { ...state, decisions: newestFirst(action.entries) };

    case "event/state":
      return jarvisReducer(state, { type: "status", status: action.payload.status });

    case "event/decision": {
      const entry = action.payload.entry;
      const rest = state.decisions.filter((known) => known.id !== entry.id);
      return { ...state, decisions: newestFirst([entry, ...rest]) };
    }

    case "event/escalation": {
      const key = noticeKey(action.payload);
      const known =
        state.escalations.some((entry) => entry.id === action.payload.id || noticeKey(entry) === key) ||
        state.announced.includes(key);
      if (known) return state;
      const importance = action.payload.importance ?? "normal";
      // Only the Jarvis screen toasts, and never for the session the person is in (it shows its own prompt).
      const quiet = !action.onScreen || importance === "low" || (!!action.payload.sessionId && action.payload.sessionId === action.sessionId);
      // A waiting approval or question is answered, not dismissed: only the
      // other reasons are kept as notices (what `jarvis.escalations.list` holds).
      const kept = isNotice(action.payload);
      return {
        ...state,
        escalations: kept
          ? [action.payload, ...state.escalations].slice(0, MAX_ESCALATIONS)
          : state.escalations,
        notice: quiet ? state.notice : `Jarvis: ${action.payload.summary}`,
        announced: quiet ? state.announced : [...state.announced, key].slice(-200),
      };
    }

    case "escalations":
      // The daemon keeps them until dismissed, so its list is the truth after a
      // reload or reconnect; the next refresh heals a notice that raced it.
      return { ...state, escalations: action.entries.slice(0, MAX_ESCALATIONS) };

    case "event/dismissed": {
      const gone = new Set(action.payload.ids);
      if (!state.escalations.some((entry) => gone.has(entry.id))) return state;
      return { ...state, escalations: state.escalations.filter((entry) => !gone.has(entry.id)) };
    }

    case "sessionStates":
      return { ...state, sessionStates: action.states };

    case "sessionState": {
      const next = { ...state.sessionStates };
      if (action.state) next[action.sessionId] = action.state;
      else delete next[action.sessionId];
      return { ...state, sessionStates: next };
    }

    case "event/delegation":
      return { ...state, notice: `Jarvis delegation ${action.payload.reason}` };

    case "resolved":
      return withRequests(
        state,
        state.requests.filter((item) => item.requestId !== action.requestId),
      );

    case "notice/clear":
      return state.notice === null ? state : { ...state, notice: null };

    default:
      return state;
  }
}

function withRequests(state: JarvisState, requests: WaitingItem[]): JarvisState {
  return {
    ...state,
    requests,
    waiting: requests.filter((item) => needsPerson(item.request)),
    deciding: requests.filter((item) => routeOf(item.request) === "jarvis" && !alwaysAsksYou(item.request)).length,
  };
}

/** The escalations that persist as notices; the rest are requests waiting in the queue. */
const NOTICE_REASONS = new Set(["finished", "error", "stalled", "budget", "jarvis"]);

/** One need, however often the daemon repeats it: the request id, else the summary within its session. */
export function noticeKey(entry: Pick<JarvisEscalationEvent, "requestId" | "sessionId" | "summary">): string {
  return entry.requestId ? `r:${entry.requestId}` : `s:${entry.sessionId ?? ""}:${entry.summary}`;
}

export function isNotice(escalation: Pick<JarvisEscalationEvent, "reason">): boolean {
  return NOTICE_REASONS.has(escalation.reason);
}

// --- who is waiting, and what for ---------------------------------------------

function routeOf(request: ApprovalRequestParams | QuestionRequestParams): string | null {
  return request.route ?? null;
}

/**
 * A request is the person's when the engine routed it to them, or when Jarvis
 * mode was off for it. `jarvis` means the Jarvis session is deciding (the
 * person may still answer, but it is not waiting on them) and `auto` was
 * allowed already.
 */
export function needsPerson(request: ApprovalRequestParams | QuestionRequestParams): boolean {
  // What only a person may settle is theirs whatever its route says.
  if (alwaysAsksYou(request)) return true;
  const route = routeOf(request);
  return route === null || route === "person";
}

export { ALWAYS_ASKS, alwaysAsksReason, alwaysAsksYou };

function rowFor(
  rows: readonly JarvisOverviewRow[],
  sessionId: string,
  external: ExternalRef | null,
): JarvisOverviewRow | undefined {
  return rows.find((row) =>
    external
      ? row.external?.cli === external.cli && row.external.id === external.id
      : row.sessionId === sessionId,
  );
}

/** A folder's last segment: how people name a project. */
export function baseName(path: string | null | undefined): string {
  if (!path) return "";
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).pop() || trimmed;
}

type LabelRow = Pick<JarvisOverviewRow, "title" | "project" | "external" | "sessionId"> &
  Partial<Pick<JarvisOverviewRow, "hostToolsFrom" | "originSurface">>;

/** Who a row belongs to: an external CLI, a browser host (`[web]`) or plain snowpea (no tag). */
export function rowAgent(row: LabelRow): AgentIdentity {
  return row.external ? agentOfExternal(row.external.cli) : agentOfSession(row);
}

/** The name a session goes by, without its agent tag: title, else project, else id. */
export function sessionName(row: LabelRow): string {
  const where = baseName(row.project);
  const title = (row.title ?? "").trim();
  const name = title && where ? `${where} · ${title}` : title || where;
  if (row.external) return name || row.external.id.slice(0, 8);
  return name || (row.sessionId ?? "").slice(0, 8) || "session";
}

/** The name a session goes by on this screen; `[cc]` / `[web]` first when it has one. */
export function sessionLabel(row: LabelRow): string {
  const tag = rowAgent(row).tag;
  return tag ? `${tag} ${sessionName(row)}` : sessionName(row);
}

export function externalOfRequest(
  request: ApprovalRequestParams | QuestionRequestParams,
): ExternalRef | null {
  if (request.external) return { cli: request.external.cli, id: request.external.id };
  const match = /^ext:([a-z]+):(.+)$/.exec(request.sessionId ?? "");
  return match ? { cli: match[1] as ExternalRef["cli"], id: match[2] } : null;
}

function waitingFrom(
  kind: "approval" | "question",
  request: ApprovalRequestParams | QuestionRequestParams,
  rows: readonly JarvisOverviewRow[],
): WaitingItem {
  const external = externalOfRequest(request);
  const row = rowFor(rows, request.sessionId, external);
  const label = row
    ? sessionLabel(row)
    : external
      ? `${agentOfExternal(external.cli).tag} ${external.id.slice(0, 8)}`
      : (request.sessionId ?? "").slice(0, 8);
  return {
    requestId: request.requestId,
    kind,
    sessionId: request.sessionId,
    external,
    label,
    summary: kind === "approval" ? approvalSummary(request as ApprovalRequestParams) : questionSummary(request as QuestionRequestParams),
    alwaysAsks: alwaysAsksYou(request),
    request,
  };
}

/**
 * The name to show on a prompt that is not about the session the person is in:
 * an external CLI session, or another snowpea session. Null for the session on
 * screen, where the name would only repeat the header.
 */
export function requestLabel(
  request: ApprovalRequestParams | QuestionRequestParams | null,
  rows: readonly JarvisOverviewRow[],
  currentSessionId: string,
): string | null {
  if (!request) return null;
  const external = externalOfRequest(request);
  // A request that names no session (older daemons) is the one on screen.
  if (!external && (!request.sessionId || request.sessionId === currentSessionId)) return null;
  return waitingFrom("approval", request, rows).label;
}

export function approvalSummary(request: ApprovalRequestParams): string {
  const args = request.args ?? {};
  const command = args["command"];
  const detail =
    typeof command === "string" && command
      ? command
      : typeof args["path"] === "string"
        ? (args["path"] as string)
        : "";
  return detail ? `${request.tool}: ${detail}` : request.tool;
}

export function questionSummary(request: QuestionRequestParams): string {
  const first = request.questions?.[0]?.question ?? "";
  return first ? `asks: ${first}` : "asks a question";
}

// --- the overview, as lines ----------------------------------------------------

/** `7 min`, `1 h 5 min`, `<1 min`; null once the time has passed. */
export function formatRemaining(until: string | null | undefined, now: number): string | null {
  if (!until) return null;
  const ms = Date.parse(until) - now;
  if (!Number.isFinite(ms) || ms <= 0) return null;
  if (ms < 60_000) return "<1 min";
  const minutes = Math.ceil(ms / 60_000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

/** Where an external session's process lives, in the words the pickers use. */
export function hostChip(host: JarvisOverviewRow["host"]): string | null {
  switch (host) {
    case "terminal":
      return "other terminal";
    case "tmux":
      return "tmux";
    case "snowpea":
      return "snowpea";
    case "none":
      return "not open";
    default:
      return null;
  }
}

// The glyphs below are one cell in every terminal and in Ink's own measure;
// ✳ ⏸ ⚠ are two cells to Ink and would push a full-width row past the edge.
export interface RowStatus {
  /** One glyph. */
  glyph: string;
  color: string | undefined;
  /** Short words for the right-hand column. */
  text: string;
}

/** What a row says about the session and whom it waits on. */
export function rowStatus(row: JarvisOverviewRow, now: number): RowStatus {
  const waiting = row.waitingOnPerson ?? 0;
  const left = formatRemaining(row.pausedUntil, now);
  if (waiting > 0) return { glyph: "!", color: "yellow", text: `waits for you (${waiting})` };
  if (row.takenOver) return { glyph: "◉", color: "cyan", text: "yours" };
  if (left) return { glyph: "‖", color: "cyan", text: `paused ${left}` };
  switch (row.status) {
    case "running":
      return { glyph: "●", color: "green", text: "running" };
    case "awaiting_approval":
    case "awaiting_question":
      return { glyph: "◐", color: "yellow", text: "Jarvis deciding" };
    case "error":
      return { glyph: "✗", color: "red", text: "error" };
    default:
      return { glyph: "◯", color: undefined, text: "idle" };
  }
}

/** What UNCONTROLLED means for Jarvis: it is shown the session but cannot gate or instruct it. */
export const UNCONTROLLED_NOTE = "   UNCONTROLLED: Jarvis can read but not instruct";

/** The chips after a row's status: gating and approvals first, since they are warnings. */
export function rowChips(row: JarvisOverviewRow, width: number): string[] {
  const chips: string[] = [];
  if (row.exposed === false) chips.push("hidden from Jarvis");
  if (row.external) {
    if (row.controlled === false) chips.push("UNCONTROLLED");
    else if (row.mode === "readonly") chips.push("read-only");
    chips.push(hostChip(row.host) ?? "external");
    if (row.approvals === "person") chips.push("you answer");
  }
  // The lists cannot reach what this session runs (core's `securityListsApply`).
  if (row.securityListsApply === false) chips.push("no security lists");
  if (row.level && width >= 44) chips.push(row.level);
  return chips;
}

/** `parts` joined with ` · `, the last ones dropped until it fits `max` cells. */
export function fitChips(parts: string[], max: number): string {
  const kept = [...parts];
  while (kept.length > 1 && textWidth(kept.join(" · ")) > max) kept.pop();
  return kept.join(" · ");
}

const NOTICE_GLYPH: Record<string, string> = {
  finished: "✓",
  error: "✗",
  stalled: "…",
  budget: "$",
  jarvis: "◆",
};

/**
 * A tamper notice (something outside snowpea changed Jarvis's delegation rules on disk): core's
 * `kind: "tamper"` first, else the `je-tamper-` id an older core gives it. Its reason stays "error".
 */
export function isTamperNotice(entry: Pick<JarvisEscalationEvent, "id"> & { kind?: string | null }): boolean {
  if (entry.kind === "tamper") return true;
  return typeof entry.id === "string" && entry.id.startsWith("je-tamper-");
}

function noticeColor(entry: JarvisEscalationEvent): string | undefined {
  if (isTamperNotice(entry)) return "yellow";
  if (entry.reason === "error") return "red";
  if (entry.reason === "budget" || entry.reason === "stalled") return "yellow";
  return undefined;
}

/** `✓ snowpea · fix login — tests pass` */
export function noticeLine(entry: JarvisEscalationEvent): string {
  const name = (entry.title ?? "").trim() || baseName(entry.project) || (entry.sessionId ?? "").slice(0, 8);
  const who = entry.external ? `${agentOfExternal(entry.external.cli).tag} ${name}`.trim() : name;
  // A tamper notice is a security notice, not a failed turn.
  const glyph = isTamperNotice(entry) ? "!" : (NOTICE_GLYPH[entry.reason] ?? "•");
  const head = `${glyph} ${who ? `${who} — ` : ""}`;
  return `${head}${entry.summary}`;
}

/** Rows the panel may show of one section before saying "+N more". */
function budget(total: number, room: number): number {
  return Math.max(0, Math.min(total, room));
}

export interface PanelLine {
  key: string;
  segments: { text: string; color?: string; dimColor?: boolean; bold?: boolean }[];
}

/**
 * `text` as segments with the external agent tag it contains (if any) drawn in
 * the external colour; the rest keeps `color`. The text is unchanged, so the
 * tag reads without colour.
 */
export function tagSegments(text: string, external: ExternalRef | AgentIdentity | null | undefined, color?: string): PanelLine["segments"] {
  const identity: AgentIdentity | null = !external ? null : "kind" in external ? external : agentOfExternal(external.cli);
  const at = identity && identity.tag ? text.indexOf(identity.tag) : -1;
  if (!identity || at < 0) return [{ text, color }];
  const end = at + identity.tag.length;
  return [
    ...(at > 0 ? [{ text: text.slice(0, at), color }] : []),
    { text: identity.tag, color: identity.color, bold: true },
    ...(end < text.length ? [{ text: text.slice(end), color }] : []),
  ];
}

/**
 * The Jarvis screen's panel: header, supervised sessions, what waits for the
 * person, recent decisions. `maxRows` bounds the whole panel; sections shrink
 * in that order of importance (waiting items last) and say how many they hid.
 */
export function panelLines(
  state: JarvisState,
  width: number,
  maxRows: number,
  now: number,
): PanelLine[] {
  const lines: PanelLine[] = [];
  const status = state.status;
  const w = Math.max(20, width);

  const level = status?.delegationDefault ?? "staged";
  const headParts = [status?.running ? "running" : "stopped", `default ${level}`];
  if (status?.model) headParts.push(status.model);
  if (state.deciding > 0) headParts.push(`deciding ${state.deciding}`);
  if (status?.budget?.exhausted) headParts.push("budget used up");
  lines.push({
    key: "head",
    segments: [
      { text: "◆ Jarvis", color: "magenta", bold: true },
      { text: fit(` · ${headParts.join(" · ")}`, w - 8), dimColor: true },
    ],
  });
  for (const problem of status?.problems ?? []) {
    if (lines.length >= maxRows) break;
    lines.push({ key: `problem-${problem}`, segments: [{ text: fit(`! ${problem}`, w), color: "yellow" }] });
  }

  const rows = state.rows;
  const waiting = state.waiting;
  const decisions = state.decisions;
  const notices = state.escalations;

  // Space left for sections after the header; one row per section title.
  let room = Math.max(0, maxRows - lines.length);
  const waitingShown = budget(waiting.length, Math.min(waiting.length, Math.max(1, Math.floor(room / 3))));
  const decisionShown = budget(decisions.length, Math.min(decisions.length, Math.max(0, Math.floor(room / 4))));
  const waitingRows = waiting.length > 0 ? 1 + waitingShown + (waiting.length > waitingShown ? 1 : 0) : 0;
  const decisionRows = decisions.length > 0 && decisionShown > 0 ? 1 + decisionShown : 0;
  const noticeShown = budget(notices.length, Math.min(notices.length, Math.max(0, Math.floor(room / 4))));
  const noticeRows = noticeShown > 0 ? 1 + noticeShown + (notices.length > noticeShown ? 1 : 0) : 0;
  const sessionRoomBefore = Math.max(
    0,
    room - waitingRows - decisionRows - noticeRows - (rows.length > 0 ? 1 : 0),
  );
  // One line saying what an uncontrolled session means for Jarvis, when there is room.
  const uncontrolledNote = rows.some((row) => row.external && row.controlled === false) && sessionRoomBefore >= 3;
  // Core says which sessions the security lists cannot reach (`securityListsApply: false`).
  const securityNote = rows.some((row) => row.securityListsApply === false) && sessionRoomBefore >= (uncontrolledNote ? 4 : 3);
  const sessionRoom = sessionRoomBefore - (uncontrolledNote ? 1 : 0) - (securityNote ? 1 : 0);
  const hasMore = rows.length > sessionRoom;
  const sessionShown = budget(rows.length, hasMore ? Math.max(0, sessionRoom - 1) : sessionRoom);

  if (rows.length === 0) {
    lines.push({ key: "none", segments: [{ text: fit("No supervised sessions yet.", w), dimColor: true }] });
  } else if (sessionShown > 0) {
    lines.push({
      key: "sessions-title",
      segments: [{ text: fit(`Sessions (${rows.length}) · /jarvis open <n>`, w), dimColor: true }],
    });
    rows.slice(0, sessionShown).forEach((row, index) => {
      const st = rowStatus(row, now);
      const chips = rowChips(row, w);
      const right = fitChips([st.text, ...chips], Math.max(12, w - 24));
      const left = `${String(index + 1).padStart(2)} ${st.glyph} ${sessionLabel(row)}`;
      // A session snowpea does not gate is red unless it waits on the person.
      const color = row.controlled === false && st.color !== "yellow" ? "red" : st.color;
      lines.push({ key: `row-${index}`, segments: tagSegments(spread(left, right, w), rowAgent(row), color) });
    });
    if (uncontrolledNote) {
      lines.push({ key: "uncontrolled-note", segments: [{ text: fit(UNCONTROLLED_NOTE, w), color: "red" }] });
    }
    if (securityNote) {
      lines.push({ key: "security-note", segments: [{ text: fit(`   ${NOT_APPLIED_NOTE}`, w), color: "red" }] });
    }
    if (hasMore) {
      lines.push({
        key: "sessions-more",
        segments: [{ text: fit(`   … ${rows.length - sessionShown} more (/jarvis sessions)`, w), dimColor: true }],
      });
    }
  }

  if (waiting.length > 0) {
    lines.push({
      key: "waiting-title",
      segments: [
        { text: fit(`Waiting for you (${waiting.length}) · Ctrl+W answers`, w), color: "yellow", bold: true },
      ],
    });
    waiting.slice(0, waitingShown).forEach((item, index) => {
      const mark = item.kind === "approval" ? "!" : "?";
      const asks = item.alwaysAsks ? `[${ALWAYS_ASKS}] ` : "";
      lines.push({
        key: `wait-${index}`,
        segments: tagSegments(fit(` ${mark} ${item.label} — ${asks}${item.summary}`, w), item.external, "yellow"),
      });
    });
    if (waiting.length > waitingShown) {
      lines.push({
        key: "waiting-more",
        segments: [{ text: fit(`   … ${waiting.length - waitingShown} more`, w), dimColor: true }],
      });
    }
  }

  if (noticeRows > 0) {
    lines.push({
      key: "notices-title",
      segments: [{ text: fit(`Notices (${notices.length}) · /jarvis dismiss`, w), dimColor: true }],
    });
    notices.slice(0, noticeShown).forEach((entry, index) => {
      lines.push({
        key: `notice-${index}`,
        segments: tagSegments(fit(` ${index + 1}. ${noticeLine(entry)}`, w), entry.external, noticeColor(entry)),
      });
    });
    if (notices.length > noticeShown) {
      lines.push({
        key: "notices-more",
        segments: [{ text: fit(`   … ${notices.length - noticeShown} more`, w), dimColor: true }],
      });
    }
  }

  if (decisionRows > 0) {
    lines.push({ key: "decisions-title", segments: [{ text: fit("Recent decisions", w), dimColor: true }] });
    decisions.slice(0, decisionShown).forEach((entry, index) => {
      lines.push({
        key: `decision-${index}`,
        segments: [{ text: fit(` ${decisionLine(entry)}`, w), dimColor: true }],
      });
    });
  }

  return lines.slice(0, Math.max(1, maxRows));
}

/** Who answered, in words: `jarvis` and `jarvis:auto` are Jarvis's (1.53). */
export function byLabel(by: string | null | undefined): string {
  if (by === "jarvis:auto" || by === "auto") return "auto";
  return by ?? "";
}

/** The status-line text while a request of this session waits on Jarvis. */
export const JARVIS_DECIDING = "Jarvis is deciding";

/**
 * The transcript line for an approval Jarvis answered (`approval.resolved.by`
 * `jarvis` / `jarvis:auto`), or null for any other answerer or an unknown request.
 */
export function resolvedLine(
  by: string | undefined,
  decision: string | undefined,
  request: ApprovalRequestParams | undefined,
): string | null {
  if ((by !== "jarvis" && by !== "jarvis:auto") || !request) return null;
  const who = by === "jarvis:auto" ? "Jarvis (auto)" : "Jarvis";
  return `${who} ${decision === "deny" ? "denied" : "allowed"} ${approvalSummary(request)}`;
}

const SCOPE_RANK: Record<string, number> = { once: 0, session: 1, project: 2, site: 2, always: 3 };
const SCOPE_WORDS: Record<string, string> = {
  once: "once only",
  session: "for this session only",
  project: "for this project only",
  site: "for this site only",
};

/** What the person chose for each request they allowed, until core says what it applied. */
const chosenScopes = new Map<string, string>();

export function rememberChosenScope(requestId: string, decision: string, scope: string): void {
  if (decision !== "allow") return;
  chosenScopes.set(requestId, scope);
  // A request that is never reported back must not pile up.
  if (chosenScopes.size > 200) chosenScopes.delete(chosenScopes.keys().next().value as string);
}

export function takeChosenScope(requestId: string): string | undefined {
  const scope = chosenScopes.get(requestId);
  chosenScopes.delete(requestId);
  return scope;
}

/** True when the scope core applied is narrower than the one the person chose. */
export function scopeNarrowed(chosen: string | undefined, applied: string | null | undefined): boolean {
  if (!chosen || !applied) return false;
  const a = SCOPE_RANK[applied];
  const c = SCOPE_RANK[chosen];
  return a !== undefined && c !== undefined && a < c;
}

/**
 * The transcript line for `approval.resolved`, or null when nothing needs
 * saying. A withdrawn request (no decision, outcome `withdrawn`) was never
 * decided: it closes quietly. Jarvis's answers are named; a person's allow that
 * core kept narrower than chosen says "Allowed once only".
 */
export function approvalResolvedNote(
  payload: { by?: string; decision?: string | null; outcome?: string | null; scope?: string | null },
  request: ApprovalRequestParams | undefined,
  chosenScope?: string,
): string | null {
  if (payload.outcome === "withdrawn" || payload.decision === null || payload.decision === undefined) return null;
  const jarvis = resolvedLine(payload.by, payload.decision, request);
  if (jarvis) return jarvis;
  if (payload.decision === "allow" && scopeNarrowed(chosenScope, payload.scope)) {
    const words = SCOPE_WORDS[payload.scope as string] ?? `as ${payload.scope}`;
    return `Allowed ${words} — core kept it narrower than "${chosenScope}"${request ? ` (${approvalSummary(request)})` : ""}`;
  }
  return null;
}

/** A high-importance Jarvis notice, as the transcript line that stays (the toast alone would be gone). */
export function highEscalationLine(summary: string): string {
  return summary.replace(/\s+/g, " ").trim();
}

/**
 * The note for the result of `approval.respond` (1.55.0): a remote web login's
 * "always" is applied as "session", and the result's `scope` says so. Null when
 * the applied scope is not narrower than chosen, or when `approval.resolved`
 * already told it (the chosen scope is taken by whichever comes first).
 */
export function respondResultNote(
  requestId: string,
  result: { scope?: string | null } | null | undefined,
  request: ApprovalRequestParams | undefined,
): string | null {
  const chosen = takeChosenScope(requestId);
  if (!scopeNarrowed(chosen, result?.scope)) return null;
  const words = SCOPE_WORDS[result?.scope as string] ?? `as ${result?.scope}`;
  return `Allowed ${words} — core kept it narrower than "${chosen}"${request ? ` (${approvalSummary(request)})` : ""}`;
}

/** The one quiet line a supervised session shows when Jarvis looked at or instructed it. */
export function accessLine(event: Pick<JarvisAccessEvent, "action" | "summary">): string {
  const verb = event.action === "read" ? "read" : "instructed";
  const raw = (event.summary ?? "").replace(/\s+/g, " ").trim();
  const what = raw.length > 80 ? `${raw.slice(0, 79)}…` : raw;
  return `Jarvis ${verb} this session${what ? ` — ${what}` : ""}`;
}

/** `jarvis allowed shell: pytest (snowpea)` */
export function decisionLine(entry: JarvisLedgerEntry): string {
  const who = byLabel(entry.decidedBy);
  const where = baseName(entry.project);
  return `${who}: ${entry.summary}${where ? ` (${where})` : ""}`;
}

// --- the status-line marker ----------------------------------------------------

/** Where a level comes from, when it is not the session's own. */
export function levelSource(scope: SessionJarvisState["levelScope"]): string | null {
  switch (scope) {
    case "project":
      return "from the project";
    case "global":
      return "from the global setting";
    default:
      return null;
  }
}

/**
 * The marker for an ordinary session Jarvis supervises, or null when it does
 * not: "Jarvis deciding" while its authority holds (saying when the level was
 * inherited from the project or the global setting), "paused, returns in N min"
 * after the person typed there, "yours" after a takeover.
 */
export function jarvisMarker(
  status: JarvisStatus | null,
  row:
    | (Pick<JarvisOverviewRow, "level" | "pausedUntil" | "takenOver"> & Pick<SessionJarvisState, "levelScope">)
    | null
    | undefined,
  now: number,
): string | null {
  if (!status?.enabled || !row) return null;
  if (row.takenOver) return "Jarvis: you have this session · /jarvis handback";
  const left = formatRemaining(row.pausedUntil, now);
  if (left) return `Jarvis paused, returns in ${left}`;
  if (!row.level) return "Jarvis deciding";
  const source = levelSource(row.levelScope);
  return `Jarvis deciding · ${row.level}${source ? ` (${source})` : ""}`;
}

// --- the delegation confirmation -----------------------------------------------

export interface DelegationProposal {
  scope: "global" | "project" | "session";
  target: string | null;
  level: "staged" | "full";
  /** Guarded classes Jarvis would also decide. */
  allow: string[];
  expiresAt: string | null;
  note: string;
}

const GUARDED_WORDS: Record<string, string> = {
  deploy: "deploy, release, push",
  outside_destructive: "outside the project, destructive",
  secret_send: "secrets, sending data out",
};

export function guardedWords(name: string): string {
  return GUARDED_WORDS[name] ?? name;
}

/**
 * The request is Jarvis's `delegation_set` proposal: the daemon marks it
 * `personOnly` with `tool: "delegation_set"` (1.53). Nothing else is read as one.
 */
export function isDelegationProposal(request: ApprovalRequestParams): boolean {
  return request.personOnly === true && request.tool === "delegation_set";
}

/** The request is the confirmation for adopting a tmux pane (`external.open` with `transport: "tmux"`). */
/** A line starting with this is drawn red in a person-only confirmation. */
const UNRESTRICTED_WARN = "UNRESTRICTED";

export function isTmuxAdopt(request: ApprovalRequestParams): boolean {
  return request.personOnly === true && request.tool === "tmux.adopt";
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Read a proposal out of the approval's arguments. The shape is the one
 * `jarvis.delegation.set` takes (`scope`, `target`, `level`, `allow`,
 * `expiresAt`, `note`); a missing field reads as the safest value, so a
 * proposal that arrives short can only look narrower than it is, never wider.
 */
export function parseProposal(request: ApprovalRequestParams): DelegationProposal | null {
  if (!isDelegationProposal(request)) return null;
  const args = request.args ?? {};
  const scopeRaw = text(args["scope"]);
  const scope: DelegationProposal["scope"] =
    scopeRaw === "global" || scopeRaw === "project" || scopeRaw === "session" ? scopeRaw : "session";
  const allowRaw = args["allow"];
  const allow = Array.isArray(allowRaw)
    ? allowRaw.filter((item): item is string => typeof item === "string")
    : typeof allowRaw === "string" && allowRaw
      ? allowRaw.split(/[,\s]+/).filter(Boolean)
      : [];
  return {
    scope,
    target: text(args["target"]),
    level: args["level"] === "full" ? "full" : "staged",
    allow,
    expiresAt: text(args["expiresAt"]),
    note: text(args["note"]) ?? "",
  };
}

/** The lines the one-press confirmation shows. */
export function proposalLines(proposal: DelegationProposal, now: number): string[] {
  const scope =
    proposal.scope === "global"
      ? "every project and session"
      : proposal.scope === "project"
        ? `project ${proposal.target ?? "(unnamed)"}`
        : `session ${proposal.target ?? "(this one)"}`;
  const level =
    proposal.level === "full"
      ? "full — Jarvis decides risky requests and stalls too"
      : "staged — only safe approvals and how-to-proceed questions";
  const guarded =
    proposal.allow.length > 0
      ? `Jarvis may ALSO decide: ${proposal.allow.map(guardedWords).join("; ")}`
      : "none — deploy, outside-project and secret/send actions still ask you";
  const left = formatRemaining(proposal.expiresAt, now);
  const expiry = proposal.expiresAt
    ? `${proposal.expiresAt}${left ? ` (in ${left})` : " (already past)"}`
    : proposal.scope === "session"
      ? "until the session ends"
      : proposal.scope === "project"
        ? "24 h (the default)"
        : "until you revoke it";
  const lines = [`Scope    ${scope}`, `Level    ${level}`, `Guarded  ${guarded}`, `Expires  ${expiry}`];
  if (proposal.note) lines.push(`Note     ${proposal.note}`);
  return lines;
}

/** What the one-key confirmation of a person-only request shows. */
export interface PersonOnlyView {
  title: string;
  /** Who asks, when the request names no session. */
  from: string;
  lines: string[];
  /** Lines starting with this are drawn as a warning. */
  warnPrefix?: string;
}

/**
 * The confirmation for a request only a person may answer, or null for any
 * other request (and for a person-only one of a kind this build does not know,
 * which gets the ordinary prompt reduced to yes/no once).
 */
export function personOnlyView(request: ApprovalRequestParams, now: number): PersonOnlyView | null {
  const proposal = parseProposal(request);
  if (proposal) {
    return {
      title: "Jarvis asks to change what it may decide",
      from: "Jarvis",
      lines: proposalLines(proposal, now),
      warnPrefix: proposal.allow.length > 0 ? "Guarded" : undefined,
    };
  }
  if (isTmuxAdopt(request)) {
    const args = request.args ?? {};
    // `args.tmux {session, pane}` is the contract's shape; the flat form is an older core's.
    const tmux = (args["tmux"] ?? {}) as Record<string, unknown>;
    const where = [text(tmux["session"]) ?? text(args["session"]), text(tmux["pane"]) ?? text(args["pane"])]
      .filter(Boolean)
      .join(" ");
    const unrestricted = args["unrestricted"] === true;
    const lines = [`Pane     ${where || "(unnamed)"}`];
    // What tmux says runs there and where: the person checks it is the session they mean.
    const running = text(args["paneCommand"]);
    if (running) lines.push(`Running  ${running}`);
    const folder = text(args["paneCwd"]) ?? text(args["cwd"]);
    if (folder) lines.push(`Folder   ${folder}`);
    lines.push(
      "Does     snowpea types your prompts into that pane",
      "Stays    permission prompts remain yours, answered in the terminal",
    );
    if (unrestricted) {
      lines.push(
        `${UNRESTRICTED_WARN}  the pane runs with a permission bypass`,
        `${UNRESTRICTED_WARN}  snowpea cannot control what it runs`,
      );
    }
    if (request.note) lines.push(`Note     ${request.note}`);
    return {
      title: "Let snowpea type into your tmux pane?",
      from: "tmux",
      lines,
      warnPrefix: unrestricted ? UNRESTRICTED_WARN : undefined,
    };
  }
  return null;
}

// --- /jarvis status ---------------------------------------------------------------

/** The lines `/jarvis status` writes into the transcript. */
export function statusLines(status: JarvisStatus, now: number): string[] {
  const lines = [
    `Jarvis (beta): ${status.enabled ? (status.running ? "on, running" : "on, not running") : "off"}`,
  ];
  if (!status.enabled) return [...lines, "Turn it on with /jarvis on."];
  lines.push("Requests Jarvis may never decide always ask you.");
  if (status.model) lines.push(`model: ${status.model}`);
  lines.push(`default delegation: ${status.delegationDefault ?? "staged"}`);
  lines.push(`folder: ${status.folder}`);
  const budget = status.budget;
  if (budget) {
    const tokens = budget.dailyTokens > 0 ? `${budget.tokensToday ?? 0}/${budget.dailyTokens} tokens today` : "no daily token cap";
    lines.push(`budget: ${budget.wakesThisHour ?? 0}/${budget.wakesPerHour} wakes this hour, ${tokens}${budget.exhausted ? " — USED UP, everything goes to you" : ""}`);
  }
  for (const pause of status.paused ?? []) {
    const who = pause.external ? `${agentOfExternal(pause.external.cli).tag} ${pause.external.id.slice(0, 8)}` : (pause.sessionId ?? "").slice(0, 8);
    const left = formatRemaining(pause.until, now);
    lines.push(
      `${pause.reason === "typing" ? "paused (you typed)" : "taken over"}: ${who}${left ? `, returns in ${left}` : pause.reason === "takeover" ? " until /jarvis handback" : ""}`,
    );
  }
  if ((status.hiddenProjects ?? []).length > 0) lines.push(`hidden from Jarvis: ${(status.hiddenProjects ?? []).map(baseName).join(", ")}`);
  for (const problem of status.problems ?? []) lines.push(`! ${problem}`);
  return lines;
}
