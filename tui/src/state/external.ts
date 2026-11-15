/**
 * External CLI sessions (1.53.0, beta) as the TUI keeps them.
 *
 * Claude Code, Codex, agy and Cursor agent keep sessions of their own. The
 * daemon lists them, opens them over `external.*` and streams `external.event`
 * with the same kinds and payloads a snowpea session uses, so the existing
 * transcript reducer and renderer draw them: all that is new here is the thin
 * converter, the view-only/mode bookkeeping and the picker's rows.
 *
 * Pure, so `test/external.test.ts` covers it without a terminal or a daemon.
 */

import { fit, spread } from "../layout/fit.js";
import type {
  ExternalCli,
  ExternalCliInfo,
  ExternalEvent,
  ExternalModeName,
  ExternalRef,
  ExternalSessionRow,
  ExternalState,
  ExternalTimelineEvent,
  SessionEvent,
} from "../rpc/sdk.js";
import { initialState, reducer, type State } from "./store.js";

export const EXTERNAL_CLIS: readonly ExternalCli[] = ["claude", "codex", "agy", "cursor"];

const LABELS: Record<ExternalCli, string> = {
  claude: "Claude Code",
  codex: "Codex",
  agy: "agy",
  cursor: "Cursor",
};

export function cliLabel(cli: string): string {
  return LABELS[cli as ExternalCli] ?? cli;
}

export function refKey(ref: ExternalRef): string {
  return `${ref.cli}:${ref.id}`;
}

export function sameRef(a: ExternalRef, b: ExternalRef): boolean {
  return a.cli === b.cli && a.id === b.id;
}

/** The id approvals use for a hosted external session (`ApprovalRequest.sessionId`). */
export function extSessionId(ref: ExternalRef): string {
  return `ext:${ref.cli}:${ref.id}`;
}

// --- where it runs, and what that allows ---------------------------------------

/** The words the picker and the screen header use for `host`. */
export function hostLabel(session: Pick<ExternalSessionRow, "host" | "tmux">): string {
  switch (session.host) {
    case "terminal":
      return "other terminal";
    case "tmux":
      return session.tmux ? `tmux ${session.tmux.session}` : "tmux";
    case "snowpea":
      return "opened by snowpea";
    default:
      return "not open";
  }
}

/** Why `external.send` would be refused, or null when it is not. */
export function viewOnlyReason(
  session: Pick<ExternalSessionRow, "canSend" | "host" | "tmux" | "cwd"> & { ref?: ExternalRef },
): string | null {
  if (session.canSend !== false) return null;
  if (session.host === "terminal") {
    // A Claude Code session in the person's own tmux pane can be adopted.
    return session.ref?.cli === "claude"
      ? "Open in another terminal — view only · t types into its tmux pane · or wait until it exits"
      : "Open in another terminal — view only until that process exits.";
  }
  if (session.host === "tmux") {
    const where = session.tmux ? ` (${session.tmux.session} ${session.tmux.pane})` : "";
    return `Running in a tmux pane${where} — view only here.`;
  }
  return "This session is view only.";
}

/**
 * A line for a session snowpea can type into but whose permission prompts the
 * person answers: a tmux pane (`approvals: person`). Null for any other.
 */
export function externalNote(session: Pick<ExternalSessionRow, "approvals" | "host" | "canSend">): string | null {
  if (session.approvals !== "person" || session.canSend === false) return null;
  return "tmux pane — answer permission prompts in the terminal; snowpea types only while you are not";
}

/** Why Esc cannot stop the running turn of a view-only session, or null when it can. */
export function interruptRefusal(session: Pick<ExternalSessionRow, "host" | "canSend">): string | null {
  if (session.host !== "terminal") return null;
  return "This session runs in another terminal — stop it there. Esc again leaves the screen.";
}

export function modeLabel(mode: ExternalModeName | null | undefined): string {
  switch (mode) {
    case "readonly":
      return "read-only";
    case "unrestricted":
      return "UNRESTRICTED";
    default:
      return "default";
  }
}

/** True while snowpea cannot gate what the session runs. */
export function isUngated(session: Pick<ExternalSessionRow, "mode" | "controlled">): boolean {
  return session.mode === "unrestricted" || session.controlled === false;
}

/** The persistent warning for the header and the status line; null when gated. */
export function ungatedWarning(session: Pick<ExternalSessionRow, "mode" | "controlled" | "ref">): string | null {
  if (session.mode === "unrestricted") {
    return `UNRESTRICTED — snowpea does not control what this ${cliLabel(session.ref.cli)} session runs`;
  }
  if (session.controlled === false) {
    return `not gated — ${cliLabel(session.ref.cli)} runs without snowpea's approvals`;
  }
  return null;
}

/** The word to type before a session may be switched to unrestricted. */
export const UNRESTRICTED_WORD = "unrestricted";

/** The explanation shown above the typed confirmation. */
export function unrestrictedPrompt(input: { cli: string; title?: string | null; cwd: string }): string[] {
  return [
    `Switch ${cliLabel(input.cli)}${input.title ? ` "${input.title}"` : ""} to UNRESTRICTED mode?`,
    `Folder: ${input.cwd}`,
    "snowpea does not control what this session runs: no approvals are asked, and",
    "Jarvis's guarded-class rules do not apply inside it. This is recorded.",
    `Type "${UNRESTRICTED_WORD}" and press Enter to confirm, Esc to cancel.`,
  ];
}

export function confirmsUnrestricted(typed: string): boolean {
  return typed.trim().toLowerCase() === UNRESTRICTED_WORD;
}

/** The modes a CLI offers for a new session, default first. */
export function modesFor(info: ExternalCliInfo | undefined): ExternalModeName[] {
  // `modes` is what a new session may use (agy has no read-only; Cursor starts
  // read-only); the capability list is the older spelling of it.
  const listed = info?.modes?.length ? info.modes : info?.capabilities?.modes;
  const modes: ExternalModeName[] =
    listed && listed.length > 0
      ? [...listed]
      : info?.id === "agy"
        ? ["default", "unrestricted"]
        : ["default", "readonly", "unrestricted"];
  const first = info?.defaultMode;
  if (first && modes.includes(first)) return [first, ...modes.filter((mode) => mode !== first)];
  return modes;
}

/** Whether a CLI can be started: installed and enabled. */
export function usable(info: ExternalCliInfo): boolean {
  return info.installed && info.enabled !== false;
}

/** Why a CLI cannot be started, in a few words. */
export function unavailableReason(info: ExternalCliInfo): string {
  if (!info.installed) return "not installed";
  if (info.enabled === false) return "turned off in settings";
  return (info.problems ?? [])[0] ?? "";
}

// --- the picker ------------------------------------------------------------------

function statusWord(row: ExternalSessionRow): string {
  if (row.status === "running") return "running";
  if (row.status === "error") return "error";
  return "idle";
}

/** Filter by what the person typed: matches the title, preview, CLI, folder, model and id. */
export function searchRows(rows: readonly ExternalSessionRow[], query: string): ExternalSessionRow[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...rows];
  return rows.filter((row) => {
    const hay = [
      cliLabel(row.ref.cli),
      row.ref.cli,
      row.title ?? "",
      row.preview ?? "",
      row.cwd,
      row.project ?? "",
      row.model ?? "",
      row.ref.id,
    ]
      .join(" ")
      .toLowerCase();
    return words.every((word) => hay.includes(word));
  });
}

/** `[Codex] title … running · other terminal · view only`, cut to `width` cells. */
export function pickerLabel(row: ExternalSessionRow, width: number, showFolder: boolean): string {
  const title = (row.title ?? "").trim() || (row.preview ?? "").trim() || row.ref.id.slice(0, 8);
  const folder = showFolder ? ` (${row.cwd.split(/[\\/]/).filter(Boolean).pop() ?? row.cwd})` : "";
  const bits = [statusWord(row), hostLabel(row)];
  if (row.canSend === false) bits.push("view only");
  if (row.mode === "unrestricted") bits.push("UNRESTRICTED");
  return spread(`[${cliLabel(row.ref.cli)}] ${title}${folder}`, bits.join(" · "), width);
}

// --- the converter -----------------------------------------------------------------

/** An `external.event` (or one entry of an `external.open` page) as a session event. */
export function toSessionEvent(
  ref: ExternalRef,
  event: Pick<ExternalTimelineEvent, "kind" | "payload" | "seq" | "ts" | "turnId">,
): SessionEvent {
  const payload = { ...(event.payload ?? {}) };
  // Session events carry the turn in their payload where it is known.
  if (event.turnId && payload["turnId"] === undefined) payload["turnId"] = event.turnId;
  return { sessionId: extSessionId(ref), seq: event.seq, kind: event.kind, ts: event.ts, payload };
}

// --- the open session -----------------------------------------------------------------

/** Raw events kept for re-folding when an older page is loaded. */
export const MAX_KEPT_EVENTS = 4000;

export interface ExternalView {
  ref: ExternalRef;
  session: ExternalSessionRow;
  /** The transcript, folded by the ordinary session reducer. */
  state: State;
  events: ExternalTimelineEvent[];
  hasMore: boolean;
  loadingOlder: boolean;
  /** One-off line for the status slot (the app shows and clears it). */
  notice: string | null;
  /** Set by `external.state` with status error. */
  error: string | null;
}

export type ExternalAction =
  | { type: "opened"; session: ExternalSessionRow; events: ExternalTimelineEvent[]; hasMore: boolean }
  | { type: "older"; events: ExternalTimelineEvent[]; hasMore: boolean }
  | { type: "loadingOlder"; on: boolean }
  | { type: "event"; event: ExternalEvent }
  | { type: "state"; payload: ExternalState }
  | { type: "session"; session: ExternalSessionRow }
  | { type: "sent"; text: string }
  | { type: "interrupted" }
  | { type: "note"; text: string }
  | { type: "failed"; message: string }
  | { type: "notice/clear" };

function byseq(events: readonly ExternalTimelineEvent[]): ExternalTimelineEvent[] {
  const seen = new Map<number, ExternalTimelineEvent>();
  for (const event of events) seen.set(event.seq, event);
  return [...seen.values()].sort((a, b) => a.seq - b.seq);
}

function fold(ref: ExternalRef, events: readonly ExternalTimelineEvent[], base: State): State {
  const converted = events.map((event) => toSessionEvent(ref, event));
  return reducer(base, { type: "session/replay", events: converted });
}

export function openExternal(
  session: ExternalSessionRow,
  events: ExternalTimelineEvent[],
  hasMore: boolean,
): ExternalView {
  const ordered = byseq(events);
  const base: State = { ...initialState, sessionId: extSessionId(session.ref), status: "connected" };
  return {
    ref: session.ref,
    session,
    state: fold(session.ref, ordered, base),
    events: ordered.slice(-MAX_KEPT_EVENTS),
    hasMore,
    loadingOlder: false,
    notice: null,
    error: null,
  };
}

/** A mode the view should say out loud when it changes under the person. */
function flipNotice(before: ExternalSessionRow, after: ExternalSessionRow): string | null {
  if (before.host !== "tmux" && after.host === "tmux" && after.canSend !== false) {
    return "snowpea types into the tmux pane now — answer permission prompts there.";
  }
  if (before.canSend === false && after.canSend !== false) {
    return "The other terminal closed — you can send from here now.";
  }
  if (before.canSend !== false && after.canSend === false) {
    return after.host === "terminal"
      ? "Another terminal opened this session — view only until it exits."
      : "This session is view only now.";
  }
  if (before.mode !== after.mode && after.mode === "unrestricted") {
    return "Mode is now UNRESTRICTED.";
  }
  return null;
}

export function externalReducer(view: ExternalView, action: ExternalAction): ExternalView {
  switch (action.type) {
    case "opened":
      return openExternal(action.session, action.events, action.hasMore);

    case "older": {
      const events = byseq([...action.events, ...view.events]).slice(-MAX_KEPT_EVENTS);
      const base: State = { ...initialState, sessionId: view.state.sessionId, status: "connected" };
      return { ...view, events, hasMore: action.hasMore, loadingOlder: false, state: fold(view.ref, events, base) };
    }

    case "loadingOlder":
      return { ...view, loadingOlder: action.on };

    case "event": {
      if (!sameRef(action.event.ref, view.ref)) return view;
      const known = view.events[view.events.length - 1]?.seq ?? 0;
      if (action.event.seq <= known) return view;
      const entry: ExternalTimelineEvent = {
        kind: action.event.kind,
        payload: action.event.payload,
        seq: action.event.seq,
        ts: action.event.ts,
        turnId: action.event.turnId ?? null,
      };
      const state = reducer(view.state, {
        type: "session/event",
        event: toSessionEvent(view.ref, entry),
      });
      let next: State = state;
      // A turn that ended for any reason but "complete" says so once.
      if (entry.kind === "turn.done") {
        const reason = String(entry.payload?.["reason"] ?? "complete");
        if (reason !== "complete" && reason !== "stopped") {
          next = reducer(next, { type: "note", text: `turn ended: ${reason}` });
        }
      }
      return { ...view, state: next, events: [...view.events, entry].slice(-MAX_KEPT_EVENTS) };
    }

    case "state": {
      if (!sameRef(action.payload.ref, view.ref)) return view;
      const p = action.payload;
      const session: ExternalSessionRow = {
        ...view.session,
        status: p.status,
        host: p.host,
        canSend: p.canSend,
        approvals: p.approvals,
        controlled: p.controlled ?? view.session.controlled,
        mode: p.mode ?? view.session.mode,
        title: p.title ?? view.session.title,
        tmux: p.tmux ?? null,
      };
      let state = view.state;
      let error = view.error;
      const diedMidTurn = p.status === "error" && view.state.turnActive;
      if (p.status === "error") {
        error = p.error ?? "the session stopped";
        if (view.session.status !== "error" || diedMidTurn) {
          state = reducer(state, {
            type: "session/event",
            event: {
              sessionId: state.sessionId ?? "",
              seq: state.lastSeq + 1,
              kind: "turn.done",
              ts: new Date().toISOString(),
              payload: { reason: "error" },
            },
          });
          state = reducer(state, {
            type: "note",
            text: `${cliLabel(session.ref.cli)} stopped${diedMidTurn ? " mid-turn" : ""}: ${error}. Send a message to continue.`,
          });
        }
      } else if (view.session.status === "error") {
        error = null;
      }
      return { ...view, session, state, error, notice: flipNotice(view.session, session) ?? view.notice };
    }

    case "session":
      return { ...view, session: action.session };

    case "sent":
      return { ...view, state: reducer(view.state, { type: "user/message", text: action.text }) };

    case "interrupted":
      return view;

    case "note":
      return { ...view, state: reducer(view.state, { type: "note", text: action.text }) };

    case "failed": {
      // A send that never started leaves nothing running: settle the turn the
      // optimistic echo opened, or the input would wait for an end that is not coming.
      const failed = reducer(view.state, { type: "error", message: action.message });
      const settled = reducer(failed, {
        type: "session/event",
        event: {
          sessionId: failed.sessionId ?? "",
          seq: failed.lastSeq + 1,
          kind: "turn.done",
          ts: new Date().toISOString(),
          payload: { reason: "error" },
        },
      });
      return { ...view, state: settled };
    }

    case "notice/clear":
      return view.notice === null ? view : { ...view, notice: null };

    default:
      return view;
  }
}

// --- errors -------------------------------------------------------------------------

/** What the person was doing when a call failed, for the codes that mean different things. */
export type ExternalCallKind = "send" | "interrupt" | "adopt" | "open";

/** The line to show for a failed `external.*` call. */
export function describeExternalError(error: unknown, cli?: string, kind: ExternalCallKind = "send"): string {
  const raw = error as { message?: string; data?: { code?: string }; code?: string | number } | null;
  const code = raw?.data?.code ?? (typeof raw?.code === "string" ? raw.code : undefined);
  const message = raw?.message ?? String(error);
  switch (code) {
    case "external_unavailable":
      return `${cli ? cliLabel(cli) : "That CLI"} is not available: ${message}`;
    case "external_view_only":
      if (kind === "interrupt") {
        return "This session runs in another terminal — stop it there; snowpea cannot interrupt it.";
      }
      if (kind === "adopt") return "Typing into that tmux pane was not allowed — the session stays view only.";
      return "This session is open in another terminal — view only until it exits.";
    case "session_busy":
      // The daemon says why: a turn is running, a prompt is open in the pane,
      // or someone is typing there. Its words are the reason.
      return message && message !== "session_busy" ? message : "The session is busy — wait for the turn to end.";
    case "jarvis_person_only":
      return "Only a person can do that here (not Jarvis).";
    case "jarvis_disabled":
      return "Jarvis mode is off — /jarvis on";
    case "not_found":
      return "That session does not exist (anymore).";
    case "outside_root":
      return "That session is not in a registered project.";
    default:
      return message;
  }
}

/** A short title for the header. */
export function headerTitle(session: ExternalSessionRow, width: number): string {
  const title = (session.title ?? "").trim() || session.ref.id.slice(0, 8);
  return fit(`${cliLabel(session.ref.cli)} · ${title}`, width);
}
