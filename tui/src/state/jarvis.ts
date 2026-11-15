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
  JarvisDecisionEvent,
  JarvisDelegationEvent,
  JarvisEscalationEvent,
  JarvisLedgerEntry,
  JarvisOverviewRow,
  JarvisStateEvent,
  JarvisStatus,
  QuestionRequestParams,
} from "../rpc/sdk.js";
import { fit, spread } from "../layout/fit.js";
import { cliLabel } from "./external.js";

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
  request: ApprovalRequestParams | QuestionRequestParams;
}

export interface JarvisState {
  /** `jarvis.status`; null until asked or on a daemon without the feature. */
  status: JarvisStatus | null;
  rows: JarvisOverviewRow[];
  waiting: WaitingItem[];
  /** Requests routed to Jarvis that nobody has answered yet. */
  deciding: number;
  decisions: JarvisLedgerEntry[];
  escalations: JarvisEscalationEvent[];
  /** The latest thing worth a toast; the app shows and clears it. */
  notice: string | null;
  loadedAt: number | null;
}

export const initialJarvis: JarvisState = {
  status: null,
  rows: [],
  waiting: [],
  deciding: 0,
  decisions: [],
  escalations: [],
  notice: null,
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
  | { type: "decisions"; entries: JarvisLedgerEntry[] }
  | { type: "event/state"; payload: JarvisStateEvent }
  | { type: "event/decision"; payload: JarvisDecisionEvent }
  | { type: "event/escalation"; payload: JarvisEscalationEvent }
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

    case "waiting": {
      const items = [
        ...action.approvals.map((request) => waitingFrom("approval", request, state.rows)),
        ...action.questions.map((request) => waitingFrom("question", request, state.rows)),
      ];
      const routedToJarvis = items.filter((item) => routeOf(item.request) === "jarvis").length;
      return {
        ...state,
        waiting: items.filter((item) => needsPerson(item.request)),
        deciding: routedToJarvis,
      };
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
      const known = state.escalations.some((entry) => entry.id === action.payload.id);
      if (known) return state;
      const importance = action.payload.importance ?? "normal";
      return {
        ...state,
        escalations: [action.payload, ...state.escalations].slice(0, MAX_ESCALATIONS),
        // Low-importance news stays in the panel; the rest also gets a toast.
        notice: importance === "low" ? state.notice : `Jarvis: ${action.payload.summary}`,
      };
    }

    case "event/delegation":
      return { ...state, notice: `Jarvis delegation ${action.payload.reason}` };

    case "resolved":
      return {
        ...state,
        waiting: state.waiting.filter((item) => item.requestId !== action.requestId),
      };

    case "notice/clear":
      return state.notice === null ? state : { ...state, notice: null };

    default:
      return state;
  }
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
  const route = routeOf(request);
  return route === null || route === "person";
}

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

/** The name a session goes by on this screen: title, else project, else id. */
export function sessionLabel(
  row: Pick<JarvisOverviewRow, "title" | "project" | "external" | "sessionId">,
): string {
  const where = baseName(row.project);
  const title = (row.title ?? "").trim();
  const name = title && where ? `${where} · ${title}` : title || where;
  if (row.external) {
    const cli = cliLabel(row.external.cli);
    return name ? `${cli} · ${name}` : `${cli} ${row.external.id.slice(0, 8)}`;
  }
  return name || (row.sessionId ?? "").slice(0, 8) || "session";
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
      ? `${cliLabel(external.cli)} ${external.id.slice(0, 8)}`
      : (request.sessionId ?? "").slice(0, 8);
  return {
    requestId: request.requestId,
    kind,
    sessionId: request.sessionId,
    external,
    label,
    summary: kind === "approval" ? approvalSummary(request as ApprovalRequestParams) : questionSummary(request as QuestionRequestParams),
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
  if (row.takenOver) return { glyph: "✋", color: "cyan", text: "yours" };
  if (left) return { glyph: "⏸", color: "cyan", text: `paused ${left}` };
  switch (row.status) {
    case "running":
      return { glyph: "✳", color: "green", text: "running" };
    case "awaiting_approval":
    case "awaiting_question":
      return { glyph: "◐", color: "yellow", text: "Jarvis deciding" };
    case "error":
      return { glyph: "✗", color: "red", text: "error" };
    default:
      return { glyph: "◯", color: undefined, text: "idle" };
  }
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
    lines.push({ key: `problem-${problem}`, segments: [{ text: fit(`⚠ ${problem}`, w), color: "yellow" }] });
  }

  const rows = state.rows;
  const waiting = state.waiting;
  const decisions = state.decisions;

  // Space left for sections after the header; one row per section title.
  let room = Math.max(0, maxRows - lines.length);
  const waitingShown = budget(waiting.length, Math.min(waiting.length, Math.max(1, Math.floor(room / 3))));
  const decisionShown = budget(decisions.length, Math.min(decisions.length, Math.max(0, Math.floor(room / 4))));
  const waitingRows = waiting.length > 0 ? 1 + waitingShown + (waiting.length > waitingShown ? 1 : 0) : 0;
  const decisionRows = decisions.length > 0 && decisionShown > 0 ? 1 + decisionShown : 0;
  const sessionRoom = Math.max(0, room - waitingRows - decisionRows - (rows.length > 0 ? 1 : 0));
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
      const chips: string[] = [];
      if (row.external) chips.push(hostChip(row.host) ?? "external");
      if (row.level && w >= 44) chips.push(row.level);
      const right = [st.text, ...chips].join(" · ");
      const left = `${String(index + 1).padStart(2)} ${st.glyph} ${sessionLabel(row)}`;
      lines.push({ key: `row-${index}`, segments: [{ text: spread(left, right, w), color: st.color }] });
    });
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
      const mark = item.kind === "approval" ? "⚠" : "?";
      lines.push({
        key: `wait-${index}`,
        segments: [{ text: fit(` ${mark} ${item.label} — ${item.summary}`, w), color: "yellow" }],
      });
    });
    if (waiting.length > waitingShown) {
      lines.push({
        key: "waiting-more",
        segments: [{ text: fit(`   … ${waiting.length - waitingShown} more`, w), dimColor: true }],
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

/** `jarvis allowed shell: pytest (snowpea)` */
export function decisionLine(entry: JarvisLedgerEntry): string {
  const who = entry.decidedBy === "auto" ? "auto" : entry.decidedBy;
  const where = baseName(entry.project);
  return `${who}: ${entry.summary}${where ? ` (${where})` : ""}`;
}

// --- the status-line marker ----------------------------------------------------

/**
 * The marker for an ordinary session Jarvis supervises, or null when it does
 * not: "Jarvis deciding" while its authority holds, "paused, returns in N min"
 * after the person typed there, "yours" after a takeover.
 */
export function jarvisMarker(
  status: JarvisStatus | null,
  row: Pick<JarvisOverviewRow, "level" | "pausedUntil" | "takenOver"> | null | undefined,
  now: number,
): string | null {
  if (!status?.enabled || !row) return null;
  if (row.takenOver) return "Jarvis: you have this session · /jarvis handback";
  const left = formatRemaining(row.pausedUntil, now);
  if (left) return `Jarvis paused, returns in ${left}`;
  return row.level ? `Jarvis deciding · ${row.level}` : "Jarvis deciding";
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

/** The request is Jarvis's `delegation_set` proposal. */
export function isDelegationProposal(request: ApprovalRequestParams): boolean {
  return /(^|[.:/])delegation_set$/.test(request.tool);
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

// --- /jarvis status ---------------------------------------------------------------

/** The lines `/jarvis status` writes into the transcript. */
export function statusLines(status: JarvisStatus, now: number): string[] {
  const lines = [
    `Jarvis (beta): ${status.enabled ? (status.running ? "on, running" : "on, not running") : "off"}`,
  ];
  if (!status.enabled) return [...lines, "Turn it on with /jarvis on."];
  if (status.model) lines.push(`model: ${status.model}`);
  lines.push(`default delegation: ${status.delegationDefault ?? "staged"}`);
  lines.push(`folder: ${status.folder}`);
  const budget = status.budget;
  if (budget) {
    const tokens = budget.dailyTokens > 0 ? `${budget.tokensToday ?? 0}/${budget.dailyTokens} tokens today` : "no daily token cap";
    lines.push(`budget: ${budget.wakesThisHour ?? 0}/${budget.wakesPerHour} wakes this hour, ${tokens}${budget.exhausted ? " — USED UP, everything goes to you" : ""}`);
  }
  for (const pause of status.paused ?? []) {
    const who = pause.external ? `${cliLabel(pause.external.cli)} ${pause.external.id.slice(0, 8)}` : (pause.sessionId ?? "").slice(0, 8);
    const left = formatRemaining(pause.until, now);
    lines.push(
      `${pause.reason === "typing" ? "paused (you typed)" : "taken over"}: ${who}${left ? `, returns in ${left}` : pause.reason === "takeover" ? " until /jarvis handback" : ""}`,
    );
  }
  if ((status.hiddenProjects ?? []).length > 0) lines.push(`hidden from Jarvis: ${(status.hiddenProjects ?? []).map(baseName).join(", ")}`);
  for (const problem of status.problems ?? []) lines.push(`⚠ ${problem}`);
  return lines;
}
