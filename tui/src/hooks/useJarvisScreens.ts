/**
 * Everything the Jarvis screen and the external-CLI screens need from the app.
 *
 * `app.tsx` is already large, so these features live here: the hook owns the
 * Jarvis state, the open external session, the pickers and every call to
 * `jarvis.*` / `external.*`, and hands the app a few values to draw and a few
 * handlers to call. The listeners it returns are merged into the app's
 * `client.setListeners`, and nothing here runs against a daemon that did not
 * advertise the capabilities (an older one keeps its old behaviour).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import type { QuestionAnswerItem } from "../components/QuestionPrompt.js";
import { useClock } from "./useClock.js";
import type { TuiClient, TuiClientListeners } from "../rpc/client.js";
import type {
  ExternalTimelineEvent,
  ApprovalDecision,
  ApprovalRequestParams,
  ApprovalScope,
  ExternalCliInfo,
  ExternalRef,
  ExternalSessionRow,
  JarvisOverviewRow,
  QuestionRequestParams,
  SessionJarvisState,
} from "../rpc/sdk.js";
import {
  externalReducer,
  confirmPathPrompt,
  describeExternalError,
  extSessionId,
  isUntrustedProject,
  personOnlyLine,
  rpcErrorFacts,
  trustPrompt,
  interruptRefusal,
  modeLabel,
  modesFor,
  openExternal,
  sameRef,
  unrestrictedPrompt,
  usable,
  viewOnlyReason,
  type ExternalAction,
  type ExternalView,
} from "../state/external.js";
import {
  accessLine,
  externalOfRequest,
  initialJarvis,
  MAX_ESCALATIONS,
  jarvisMarker,
  jarvisReducer,
  panelLines,
  requiresLine,
  sessionLabel,
  statusLines,
  supportOf,
  type JarvisState,
  type PanelLine,
  type PersonOnlyView,
  type Support,
  type WaitingItem,
} from "../state/jarvis.js";
import { parseExternalCommand, parseJarvisCommand } from "../slash/jarvis.js";
import { cliLabel } from "../state/external.js";

/** True when the CLI adds a message to a running turn instead of refusing it. */
export function steerOf(infos: readonly ExternalCliInfo[] | null, cli: string): boolean {
  return infos?.find((info) => info.id === cli)?.capabilities?.steer === true;
}

/**
 * What the picker says about the CLIs it cannot start. A CLI whose path awaits
 * confirmation shows as not installed; it gets its own line instead.
 */
export function unavailableOf(infos: readonly ExternalCliInfo[]): { unavailable: string[]; pending: ExternalCliInfo[] } {
  return {
    unavailable: infos
      .filter((info) => !usable(info) && !info.pendingPath)
      .map((info) => `${cliLabel(info.id)} (${info.installed ? "off" : "not installed"})`),
    pending: infos.filter((info) => info.pendingPath),
  };
}

/** Which overlay replaces the input line, if any. */
export type ScreenUi =
  | {
      kind: "picker";
      rows: ExternalSessionRow[];
      nextBefore: string | null;
      allProjects: boolean;
      loading: boolean;
      error: string | null;
      unavailable: string[];
      /** CLIs whose configured path awaits the person's confirmation. */
      pending: ExternalCliInfo[];
    }
  | { kind: "new"; infos: ExternalCliInfo[] }
  | { kind: "sessions" }
  | { kind: "answer"; requestId: string }
  | { kind: "unrestricted"; ref: ExternalRef; lines: string[] }
  /** The person-only confirmation for adopting a tmux pane (`tmux.adopt`). */
  | { kind: "adopt"; ref: ExternalRef; request: ApprovalRequestParams }
  /** A one-key yes/no this screen asks itself: trusting a project, confirming a CLI path. */
  | { kind: "confirm"; view: PersonOnlyView; label: string; onYes: () => void; onNo: () => void };

type ViewAction = ExternalAction | { type: "close" };

function viewReducer(view: ExternalView | null, action: ViewAction): ExternalView | null {
  if (action.type === "close") return null;
  if (action.type === "opened") return openExternal(action.session, action.events, action.hasMore);
  return view ? externalReducer(view, action) : null;
}

export interface ScreensDeps {
  client: TuiClient;
  workdir: string;
  /** The session the main transcript shows. */
  sessionId: string;
  turnActive: boolean;
  resumeSession: (target: string, into: "main" | "child") => void;
  showToast: (text: string) => void;
  /** A line in the main transcript. */
  note: (text: string) => void;
  /** Open this screen as soon as the daemon has answered (`snowpea jarvis`). */
  start?: "jarvis" | "external";
}

function errorText(error: unknown): string {
  const raw = error as { message?: string } | null;
  return raw?.message ?? String(error);
}

/** Jarvis reading a session again within this long says nothing new. */
const ACCESS_QUIET_MS = 30_000;
/** A second Esc within this long leaves a session whose turn cannot be stopped from here. */
const ESC_AGAIN_MS = 3000;
const POLL_VISIBLE_MS = 4000;
const POLL_BACKGROUND_MS = 15_000;
const PAGE = 40;

export function useJarvisScreens(deps: ScreensDeps) {
  const { client, workdir, sessionId, turnActive, resumeSession, showToast, note, start } = deps;
  const support: Support = supportOf(client.serverCapabilities?.());
  const [jarvis, jarvisDispatch] = useReducer(jarvisReducer, initialJarvis);
  const [external, viewDispatch] = useReducer(viewReducer, null);
  const [ui, setUi] = useState<ScreenUi | null>(null);
  const uiRef = useRef<ScreenUi | null>(null);
  uiRef.current = ui;
  const returnTo = useRef<string | null>(null);
  const refreshing = useRef(false);
  const clisRef = useRef<ExternalCliInfo[] | null>(null);
  const [clis, setClis] = useState<ExternalCliInfo[] | null>(null);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const loadClisRef = useRef<(() => Promise<ExternalCliInfo[]>) | null>(null);
  const lastAccess = useRef(new Map<string, number>());
  const lastRefusal = useRef(0);
  /** The session `/external adopt` is waiting on a confirmation for. */
  const adopting = useRef<ExternalRef | null>(null);
  const externalRef = useRef<ExternalView | null>(null);
  externalRef.current = external;
  const jarvisRef = useRef<JarvisState>(jarvis);
  jarvisRef.current = jarvis;

  const jarvisSessionId = jarvis.status?.sessionId ?? null;
  const jarvisOn = support.jarvis && jarvis.status?.enabled === true;
  // Remaining pause times are shown in minutes; the clock only needs to move
  // while Jarvis is on, and not faster than that.
  const now = useClock(jarvisOn, 15_000);
  /** True while the open session is the Jarvis session. */
  const onJarvisScreen = jarvisOn && jarvisSessionId !== null && sessionId === jarvisSessionId;

  // --- reading Jarvis ---------------------------------------------------------

  /**
   * Read the daemon's state. The waiting requests are not part of the timer's
   * refresh: `approval.pending`, `question.pending` and the two route events
   * keep them, so they are listed only when something asks for it (start-up,
   * a reconnect, a resolved request).
   */
  const refresh = useCallback(async (options: { periodic?: boolean } = {}) => {
    if (!support.jarvis || refreshing.current) return;
    refreshing.current = true;
    try {
      const status = await client.call("jarvis.status", {}).catch(() => null);
      if (status) jarvisDispatch({ type: "status", status });
      if (!status?.enabled) return;
      const [overview, approvals, questions, ledger, notices, sessions] = await Promise.all([
        client.call("jarvis.overview", { includeIdle: true, includeExternal: true, limit: 100 }).catch(() => null),
        options.periodic ? Promise.resolve(null) : client.listApprovals().catch(() => null),
        options.periodic ? Promise.resolve(null) : client.call("question.list", {}).catch(() => null),
        support.delegation ? client.call("jarvis.ledger", { limit: 12 }).catch(() => null) : Promise.resolve(null),
        client.call("jarvis.escalations.list", { limit: MAX_ESCALATIONS }).catch(() => null),
        client.call("session.list", {}).catch(() => null),
      ]);
      if (overview) {
        jarvisDispatch({ type: "overview", rows: overview.rows ?? [], at: Date.now() });
      }
      if (approvals || questions) {
        jarvisDispatch({
          type: "waiting",
          approvals: (approvals?.requests ?? []) as ApprovalRequestParams[],
          questions: (questions?.requests ?? []) as QuestionRequestParams[],
        });
      }
      if (ledger) jarvisDispatch({ type: "decisions", entries: ledger.entries ?? [] });
      if (notices) jarvisDispatch({ type: "escalations", entries: notices.escalations ?? [] });
      if (sessions) {
        const states: Record<string, SessionJarvisState> = {};
        for (const entry of (sessions.sessions ?? []) as Array<{ sessionId: string; jarvis?: SessionJarvisState | null }>) {
          if (entry.jarvis) states[entry.sessionId] = entry.jarvis;
        }
        jarvisDispatch({ type: "sessionStates", states });
      }
    } finally {
      refreshing.current = false;
    }
  }, [client, support.jarvis, support.delegation]);

  useEffect(() => {
    void refresh();
  }, [refresh, sessionId]);

  useEffect(() => {
    if (!jarvisOn) return;
    const timer = setInterval(() => void refresh({ periodic: true }), onJarvisScreen ? POLL_VISIBLE_MS : POLL_BACKGROUND_MS);
    return () => clearInterval(timer);
  }, [jarvisOn, onJarvisScreen, refresh]);

  // News worth a line in the status slot.
  useEffect(() => {
    if (jarvis.notice === null) return;
    showToast(jarvis.notice);
    jarvisDispatch({ type: "notice/clear" });
  }, [jarvis.notice, showToast]);
  useEffect(() => {
    if (!external?.notice) return;
    showToast(external.notice);
    viewDispatch({ type: "notice/clear" });
  }, [external?.notice, showToast]);

  // --- notifications ------------------------------------------------------------

  const listeners = useMemo<Partial<TuiClientListeners>>(
    () => ({
      onJarvisState: (payload) => jarvisDispatch({ type: "event/state", payload }),
      onJarvisDecision: (payload) => jarvisDispatch({ type: "event/decision", payload }),
      onJarvisEscalation: (payload) => {
        jarvisDispatch({ type: "event/escalation", payload });
        void refresh();
      },
      onJarvisDelegation: (payload) => {
        jarvisDispatch({ type: "event/delegation", payload });
        void refresh();
      },
      onApprovalPending: (payload) => {
        const request = (payload as { request?: ApprovalRequestParams } | undefined)?.request;
        if (!request) return;
        jarvisDispatch({ type: "pending", kind: "approval", request });
        // The confirmation for the adoption this screen started.
        const wanted = adopting.current;
        const asked = request.tool === "tmux.adopt" ? externalOfRequest(request) : null;
        if (wanted && asked && sameRef(asked, wanted)) setUi({ kind: "adopt", ref: wanted, request });
      },
      onQuestionPending: (payload) => {
        const request = (payload as { request?: QuestionRequestParams } | undefined)?.request;
        if (request) jarvisDispatch({ type: "pending", kind: "question", request });
        else void refresh();
      },
      // Jarvis took a request, or gave it back: the "deciding" count and the
      // prompt list follow the event.
      onApprovalRouteChanged: (payload) =>
        jarvisDispatch({ type: "route", requestId: payload.requestId, route: payload.route }),
      onQuestionRouteChanged: (payload) =>
        jarvisDispatch({ type: "route", requestId: payload.requestId, route: payload.route }),
      onJarvisEscalationDismissed: (payload) => jarvisDispatch({ type: "event/dismissed", payload }),
      onSessionsChanged: (payload) => {
        if (payload?.sessionId && payload.jarvis !== undefined) {
          jarvisDispatch({
            type: "sessionState",
            sessionId: payload.sessionId,
            state: (payload.jarvis as SessionJarvisState | null) ?? null,
          });
        }
      },
      // One quiet line in the session Jarvis read or instructed, not one per read.
      onJarvisAccess: (event) => {
        const view = externalRef.current;
        const onView =
          view !== null &&
          (event.sessionId === extSessionId(view.ref) || (event.external != null && sameRef(event.external, view.ref)));
        if (!onView && event.sessionId !== sessionIdRef.current) return;
        const key = `${event.sessionId}:${event.action === "read" ? "read" : "instruct"}`;
        const at = Date.now();
        if (at - (lastAccess.current.get(key) ?? 0) < ACCESS_QUIET_MS) return;
        lastAccess.current.set(key, at);
        if (onView) viewDispatch({ type: "note", text: accessLine(event) });
        else note(accessLine(event));
      },
      onExternalClisChanged: (payload) => {
        const next = (payload?.clis ?? []) as ExternalCliInfo[];
        clisRef.current = next;
        setClis(next);
        setUi((current) =>
          current?.kind === "new"
            ? { ...current, infos: next }
            : current?.kind === "picker"
              ? { ...current, ...unavailableOf(next) }
              : current,
        );
      },
      onExternalEvent: (payload) => viewDispatch({ type: "event", event: payload }),
      onExternalState: (payload) => {
        viewDispatch({ type: "state", payload });
        setUi((current) =>
          current?.kind === "picker"
            ? {
                ...current,
                rows: current.rows.map((row) =>
                  sameRef(row.ref, payload.ref)
                    ? {
                        ...row,
                        status: payload.status,
                        host: payload.host,
                        canSend: payload.canSend,
                        mode: payload.mode ?? row.mode,
                        title: payload.title ?? row.title,
                      }
                    : row,
                ),
              }
            : current,
        );
      },
    }),
    [refresh, note],
  );

  /** After a reconnect the daemon forgot who listens to the open session. */
  const reconnected = useCallback(() => {
    const view = externalRef.current;
    void refresh();
    if (!view) return;
    void client
      .call("external.open", { ref: view.ref, limit: 200 })
      .then((result) =>
        viewDispatch({ type: "opened", session: result.session, events: result.events ?? [], hasMore: result.hasMore === true }),
      )
      .catch((error: unknown) => note(`external: could not reopen the session — ${errorText(error)}`));
  }, [client, note, refresh]);

  // --- Jarvis commands ------------------------------------------------------------

  const gate = useCallback(
    (feature: "jarvis" | "external"): boolean => {
      const ok = feature === "jarvis" ? support.jarvis : support.external;
      if (!ok) note(requiresLine(feature));
      return ok;
    },
    [note, support.external, support.jarvis],
  );

  const enterJarvis = useCallback(() => {
    if (!gate("jarvis")) return;
    const status = jarvisRef.current.status;
    if (!status?.enabled) {
      showToast("Jarvis is off — /jarvis on");
      return;
    }
    if (!status.sessionId) {
      showToast("Jarvis has no session yet — try again in a moment");
      void refresh();
      return;
    }
    if (status.sessionId === sessionId) {
      void refresh();
      showToast("you are in the Jarvis session");
      return;
    }
    if (turnActive) {
      showToast("interrupt the current turn before opening another session");
      return;
    }
    returnTo.current = sessionId;
    resumeSession(status.sessionId, "main");
  }, [gate, refresh, resumeSession, sessionId, showToast, turnActive]);

  const goBack = useCallback(() => {
    const target = returnTo.current;
    if (!target || target === sessionId) {
      showToast("no earlier session to go back to — /sessions lists them");
      return;
    }
    if (turnActive) {
      showToast("interrupt the current turn before opening another session");
      return;
    }
    const from = sessionId;
    resumeSession(target, "main");
    returnTo.current = from;
  }, [resumeSession, sessionId, showToast, turnActive]);

  const resolveRow = useCallback((target: string): JarvisOverviewRow | null => {
    const rows = jarvisRef.current.rows;
    if (/^\d+$/.test(target)) return rows[Number(target) - 1] ?? null;
    const lower = target.toLowerCase();
    return (
      rows.find((row) => row.sessionId?.startsWith(target) || row.external?.id.startsWith(target)) ??
      rows.find((row) => sessionLabel(row).toLowerCase().includes(lower)) ??
      null
    );
  }, []);

  /** Stop this connection's `external.event` stream for a session; its process keeps running. */
  const unwatch = useCallback(
    (ref: ExternalRef) => {
      void client.call("external.unwatch", { ref }).catch(() => undefined);
    },
    [client],
  );

  /** Show a session the daemon just opened or created; the one it replaces stops being watched. */
  const showOpened = useCallback(
    (session: ExternalSessionRow, events: ExternalTimelineEvent[], hasMore: boolean) => {
      const previous = externalRef.current;
      if (previous && !sameRef(previous.ref, session.ref)) unwatch(previous.ref);
      viewDispatch({ type: "opened", session, events, hasMore });
      setUi(null);
      // Whether the CLI takes a message mid-turn is in its capabilities.
      void loadClisRef.current?.().catch(() => undefined);
    },
    [unwatch],
  );

  /**
   * `external.open` / `external.create` with the one-time trust question: a
   * project no person has trusted yet answers `external_unavailable`
   * (`untrusted_project`); after a "y" the same call is made with `trust: true`.
   * `untrusted_project_person_only` is a message, never a question.
   */
  const callTrusted = useCallback(
    (
      method: "external.open" | "external.create",
      params: Record<string, unknown>,
      options: { cli: string; project: string; onResult: (result: any) => void; onError: (error: unknown) => void },
    ) => {
      const run = (trust: boolean): void => {
        void client
          .call(method, trust ? { ...params, trust: true } : params)
          .then(options.onResult)
          .catch((error: unknown) => {
            if (!trust && isUntrustedProject(error)) {
              const project = rpcErrorFacts(error).project ?? options.project;
              setUi({
                kind: "confirm",
                view: { title: "Trust this project for external CLIs?", from: cliLabel(options.cli), lines: trustPrompt(project) },
                label: cliLabel(options.cli),
                onYes: () => {
                  setUi(null);
                  run(true);
                },
                onNo: () => {
                  setUi(null);
                  showToast("project not trusted — no external session was opened");
                },
              });
              return;
            }
            options.onError(error);
          });
      };
      run(false);
    },
    [client, showToast],
  );

  const openExternalRef = useCallback(
    (ref: ExternalRef, mode?: string) => {
      callTrusted(
        "external.open",
        { ref, limit: 200, ...(mode ? { mode } : {}) },
        {
          cli: ref.cli,
          project: workdir,
          onResult: (result) => showOpened(result.session, result.events ?? [], result.hasMore === true),
          onError: (error) => showToast(describeExternalError(error, ref.cli, "open")),
        },
      );
    },
    [callTrusted, showOpened, showToast, workdir],
  );

  /** `/external open <cli>:<id>`: look the row up first so a stale handle says why. */
  const openByRef = useCallback(
    (ref: ExternalRef) => {
      void client
        .call("external.get", { ref })
        .then((result) => openExternalRef(result.session.ref))
        .catch((error: unknown) => showToast(describeExternalError(error, ref.cli, "open")));
    },
    [client, openExternalRef, showToast],
  );

  const openRow = useCallback(
    (row: JarvisOverviewRow) => {
      setUi(null);
      if (row.external) {
        if (!gate("external")) return;
        openExternalRef(row.external);
        return;
      }
      if (!row.sessionId) return;
      if (turnActive) {
        showToast("interrupt the current turn before opening another session");
        return;
      }
      if (row.sessionId === sessionId) return;
      returnTo.current = sessionId;
      resumeSession(row.sessionId, "main");
    },
    [gate, openExternalRef, resumeSession, sessionId, showToast, turnActive],
  );

  /** The session `/jarvis handback|takeover` names: a row, else the one on screen. */
  const targetParams = useCallback(
    (target: string | null): Record<string, unknown> | string => {
      if (target) {
        const row = resolveRow(target);
        if (!row) return `no supervised session "${target}" — /jarvis sessions lists them`;
        return row.external ? { external: row.external } : { sessionId: row.sessionId };
      }
      const view = externalRef.current;
      if (view) return { external: view.ref };
      if (jarvisSessionId && sessionId === jarvisSessionId) {
        return "name a session: /jarvis handback <n> (/jarvis sessions lists them)";
      }
      return { sessionId };
    },
    [jarvisSessionId, resolveRow, sessionId],
  );

  const jarvisCall = useCallback(
    (method: string, params: Record<string, unknown>, done: (result: any) => string | null) => {
      void client
        .call(method, params)
        .then((result) => {
          const text = done(result);
          if (text) showToast(text);
          void refresh();
        })
        .catch((error: unknown) => {
          const code = rpcErrorFacts(error).code;
          note(
            code === "jarvis_disabled"
              ? "Jarvis is off — /jarvis on"
              : code === "jarvis_person_only"
                ? personOnlyLine(error)
                : `${method} failed: ${errorText(error)}`,
          );
        });
    },
    [client, note, refresh, showToast],
  );

  const handleJarvis = useCallback(
    (line: string): boolean => {
      const command = parseJarvisCommand(line);
      if (!command) return false;
      if (!gate("jarvis")) return true;
      switch (command.kind) {
        case "invalid":
          note(`usage: ${command.usage}`);
          break;
        case "enter":
          enterJarvis();
          break;
        case "back":
          goBack();
          break;
        case "status":
          void client
            .call("jarvis.status", {})
            .then((status) => {
              jarvisDispatch({ type: "status", status });
              note(statusLines(status, Date.now()).join("\n"));
            })
            .catch((error: unknown) => note(`jarvis.status failed: ${errorText(error)}`));
          break;
        case "on":
          jarvisCall("jarvis.enable", {}, (result) => {
            jarvisDispatch({ type: "status", status: result });
            return result.running ? "Jarvis is on (beta) — /jarvis opens it" : "Jarvis is starting…";
          });
          break;
        case "off":
          jarvisCall("jarvis.disable", {}, (result) => {
            jarvisDispatch({ type: "status", status: result });
            return "Jarvis is off — requests come to you again";
          });
          break;
        case "sessions":
          if (jarvisRef.current.rows.length === 0) showToast("Jarvis supervises no sessions yet");
          else setUi({ kind: "sessions" });
          break;
        case "open": {
          const row = resolveRow(command.target);
          if (!row) showToast(`no supervised session "${command.target}"`);
          else openRow(row);
          break;
        }
        case "dismiss": {
          const list = jarvisRef.current.escalations;
          if (list.length === 0) {
            showToast("no notices to dismiss");
            break;
          }
          const picked = command.target === "all" ? list : [list[command.target - 1]];
          if (picked[0] === undefined) {
            showToast(`no notice ${command.target} — /jarvis lists them`);
            break;
          }
          const ids = picked.map((entry) => entry.id);
          jarvisCall(
            "jarvis.escalations.dismiss",
            command.target === "all" ? { all: true } : { ids },
            (result) => {
              jarvisDispatch({ type: "event/dismissed", payload: { ids: result?.dismissed ?? ids } });
              return ids.length === 1 ? "notice dismissed" : `${ids.length} notices dismissed`;
            },
          );
          break;
        }
        case "waiting":
          if (jarvisRef.current.waiting.length === 0) showToast("nothing is waiting for you");
          else setUi({ kind: "answer", requestId: jarvisRef.current.waiting[0].requestId });
          break;
        case "handback":
        case "takeover": {
          const params = targetParams(command.target);
          if (typeof params === "string") {
            showToast(params);
            break;
          }
          if (command.kind === "handback") {
            jarvisCall("jarvis.handback", params, () => "Jarvis has this session again");
          } else {
            jarvisCall(
              "jarvis.takeover",
              { ...params, ...(command.minutes !== null ? { minutes: command.minutes } : {}) },
              () =>
                command.minutes !== null
                  ? `you have this session for ${command.minutes} min — /jarvis handback returns it`
                  : "you have this session — /jarvis handback returns it",
            );
          }
          break;
        }
      }
      return true;
    },
    [client, enterJarvis, gate, goBack, jarvisCall, note, openRow, resolveRow, showToast, targetParams],
  );

  // --- answering what waits for the person ---------------------------------------

  const waitingItem: WaitingItem | null =
    ui?.kind === "answer" ? (jarvis.waiting.find((item) => item.requestId === ui.requestId) ?? null) : null;

  // The item may have been answered elsewhere while the prompt was up.
  useEffect(() => {
    if (ui?.kind !== "answer") return;
    if (waitingItem) return;
    const next = jarvis.waiting[0];
    setUi(next ? { kind: "answer", requestId: next.requestId } : null);
  }, [ui, waitingItem, jarvis.waiting]);

  const afterAnswer = useCallback(
    (requestId: string) => {
      jarvisDispatch({ type: "resolved", requestId });
      const rest = jarvisRef.current.waiting.filter((item) => item.requestId !== requestId);
      setUi(rest.length > 0 ? { kind: "answer", requestId: rest[0].requestId } : null);
      void refresh();
    },
    [refresh],
  );

  const answerApproval = useCallback(
    (requestId: string, decision: ApprovalDecision, scope: ApprovalScope, reason?: string) => {
      afterAnswer(requestId);
      void client
        .respondApproval(requestId, decision, scope, reason)
        .catch((error: unknown) => note(`approval.respond failed: ${errorText(error)}`));
    },
    [afterAnswer, client, note],
  );

  const answerQuestion = useCallback(
    (requestId: string, answers: QuestionAnswerItem[]) => {
      afterAnswer(requestId);
      void client
        .respondQuestion(requestId, answers)
        .catch((error: unknown) => note(`question.respond failed: ${errorText(error)}`));
    },
    [afterAnswer, client, note],
  );

  const openWaiting = useCallback(() => {
    const first = jarvisRef.current.waiting[0];
    if (first) setUi({ kind: "answer", requestId: first.requestId });
  }, []);

  // --- external sessions ---------------------------------------------------------

  const loadClis = useCallback(async (): Promise<ExternalCliInfo[]> => {
    if (clisRef.current) return clisRef.current;
    const result = await client.call("external.clis", {});
    clisRef.current = (result?.clis ?? []) as ExternalCliInfo[];
    setClis(clisRef.current);
    return clisRef.current;
  }, [client]);

  loadClisRef.current = loadClis;

  const loadPicker = useCallback(
    async (allProjects: boolean, before: string | null, keep: ExternalSessionRow[]) => {
      try {
        const result = await client.call("external.list", {
          cwd: workdir,
          allProjects,
          limit: PAGE,
          ...(before ? { before } : {}),
        });
        const rows = [...keep, ...((result?.sessions ?? []) as ExternalSessionRow[])];
        setUi((current) =>
          current?.kind === "picker" && current.allProjects === allProjects
            ? { ...current, rows, nextBefore: result?.nextBefore ?? null, loading: false, error: null }
            : current,
        );
      } catch (error) {
        setUi((current) =>
          current?.kind === "picker"
            ? { ...current, loading: false, error: describeExternalError(error) }
            : current,
        );
      }
    },
    [client, workdir],
  );

  const openPicker = useCallback(() => {
    if (!gate("external")) return;
    setUi({ kind: "picker", rows: [], nextBefore: null, allProjects: false, loading: true, error: null, unavailable: [], pending: [] });
    void loadClis()
      .then((infos) => {
        setUi((current) => (current?.kind === "picker" ? { ...current, ...unavailableOf(infos) } : current));
      })
      .catch(() => undefined);
    void loadPicker(false, null, []);
  }, [gate, loadClis, loadPicker]);

  const pickerToggleAll = useCallback(() => {
    const current = uiRef.current;
    if (current?.kind !== "picker") return;
    const allProjects = !current.allProjects;
    setUi({ ...current, allProjects, rows: [], nextBefore: null, loading: true, error: null });
    void loadPicker(allProjects, null, []);
  }, [loadPicker]);

  const pickerMore = useCallback(() => {
    const current = uiRef.current;
    if (current?.kind !== "picker" || !current.nextBefore || current.loading) return;
    setUi({ ...current, loading: true });
    void loadPicker(current.allProjects, current.nextBefore, current.rows);
  }, [loadPicker]);

  const openNewForm = useCallback(() => {
    void loadClis()
      .then((infos) => setUi({ kind: "new", infos }))
      .catch((error: unknown) => showToast(describeExternalError(error)));
  }, [loadClis, showToast]);

  const createExternal = useCallback(
    (choice: { cli: ExternalRef["cli"]; mode: string; model: string | null }) => {
      callTrusted(
        "external.create",
        {
          cli: choice.cli,
          project: workdir,
          mode: choice.mode,
          transport: "structured",
          ...(choice.model ? { model: choice.model } : {}),
        },
        {
          cli: choice.cli,
          project: workdir,
          onResult: (result) => showOpened(result.session, [], false),
          onError: (error) => {
            setUi(null);
            note(`external: ${describeExternalError(error, choice.cli)}`);
          },
        },
      );
    },
    [callTrusted, note, showOpened, workdir],
  );

  const loadOlder = useCallback(() => {
    const view = externalRef.current;
    if (!view) {
      showToast("open an external session first");
      return;
    }
    if (!view.hasMore || view.loadingOlder) {
      showToast("that is the start of the session");
      return;
    }
    const first = view.events[0]?.seq;
    viewDispatch({ type: "loadingOlder", on: true });
    void client
      .call("external.read", { ref: view.ref, beforeSeq: first ?? null, limit: 200 })
      .then((result) => viewDispatch({ type: "older", events: result.events ?? [], hasMore: result.hasMore === true }))
      .catch((error: unknown) => {
        viewDispatch({ type: "loadingOlder", on: false });
        showToast(describeExternalError(error));
      });
  }, [client, showToast]);

  const sendExternal = useCallback(
    (text: string) => {
      const view = externalRef.current;
      if (!view) return;
      const why = viewOnlyReason(view.session);
      if (why) {
        showToast(why);
        return;
      }
      // A running turn takes a message only where the CLI can steer it (Codex);
      // elsewhere the daemon would answer session_busy, so say it before echoing.
      if (view.state.turnActive && !steerOf(clisRef.current, view.ref.cli)) {
        showToast(`${cliLabel(view.ref.cli)} is still answering — Esc stops the turn, then send`);
        return;
      }
      viewDispatch({ type: "sent", text });
      void client.call("external.send", { ref: view.ref, text }).catch((error: unknown) => {
        const message = describeExternalError(error, view.ref.cli);
        viewDispatch({ type: "failed", message });
        // The reason (a turn is running, a prompt is open in the pane, someone is typing) is what to act on.
        showToast(message);
      });
    },
    [client, showToast],
  );

  const interruptExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    void client
      .call("external.interrupt", { ref: view.ref })
      .catch((error: unknown) => showToast(describeExternalError(error, view.ref.cli, "interrupt")));
  }, [client, showToast]);

  /** Leave the screen; the process keeps running and an idle one is stopped by the daemon later. */
  const leaveExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    viewDispatch({ type: "close" });
    unwatch(view.ref);
  }, [unwatch]);

  /** `/external stop`: also stop the process snowpea runs for the session. */
  const stopExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) {
      showToast("no external session is open");
      return;
    }
    viewDispatch({ type: "close" });
    void client
      .call("external.close", { ref: view.ref })
      .then(() => showToast(`${cliLabel(view.ref.cli)} session stopped — its transcript stays`))
      .catch((error: unknown) => showToast(describeExternalError(error, view.ref.cli)));
  }, [client, showToast]);

  /**
   * Esc in an external session: stop the running turn, else go back. A turn in
   * another terminal cannot be stopped from here: the first Esc says so, a
   * second one right after leaves.
   */
  const escapeExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    if (!view.state.turnActive) {
      leaveExternal();
      return;
    }
    const why = interruptRefusal(view.session);
    if (why) {
      const at = Date.now();
      if (at - lastRefusal.current < ESC_AGAIN_MS) {
        leaveExternal();
        return;
      }
      lastRefusal.current = at;
      showToast(why);
      return;
    }
    interruptExternal();
  }, [interruptExternal, leaveExternal, showToast]);

  /**
   * `/external adopt`: let snowpea type into the tmux pane a Claude Code
   * session runs in. The call waits for a confirmation only a person can give;
   * it is found in the queue and drawn here, and the call's own answer closes it.
   */
  const adoptExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) {
      showToast("open an external session first — /external");
      return;
    }
    const { ref, session } = view;
    if (session.host === "tmux") {
      showToast("snowpea already types into this tmux pane");
      return;
    }
    if (ref.cli !== "claude" || session.host !== "terminal") {
      showToast("adopting needs a Claude Code session open in your own tmux pane");
      return;
    }
    // The confirmation reaches every surface as `approval.pending`; the
    // listener above picks it up for this ref while the call waits.
    adopting.current = ref;
    showToast("Confirm in the prompt — snowpea types into the pane only after you say yes");
    void client
      .call("external.open", { ref, limit: 200, transport: "tmux" })
      .then((result) => {
        showToast("snowpea types into the tmux pane now — answer permission prompts there");
        showOpened(result.session, result.events ?? [], result.hasMore === true);
      })
      .catch((error: unknown) => showToast(describeExternalError(error, ref.cli, "adopt")))
      .finally(() => {
        adopting.current = null;
        setUi((current) => (current?.kind === "adopt" ? null : current));
      });
  }, [client, showOpened, showToast]);

  const answerAdopt = useCallback(
    (confirmed: boolean) => {
      const current = uiRef.current;
      if (current?.kind !== "adopt") return;
      setUi(null);
      void client
        .respondApproval(current.request.requestId, confirmed ? "allow" : "deny", "once", confirmed ? undefined : "declined by the person")
        .catch((error: unknown) => note(`approval.respond failed: ${errorText(error)}`));
    },
    [client, note],
  );

  /**
   * `/external confirm-path <cli>`: the person confirms the resolved path of a
   * configured executable, shown exactly, before snowpea may run it.
   */
  const confirmCliPath = useCallback(
    (cli: string) => {
      void client
        .call("external.clis", {})
        .then((result) => {
          const infos = (result?.clis ?? []) as ExternalCliInfo[];
          clisRef.current = infos;
          setClis(infos);
          const info = infos.find((entry) => entry.id === cli);
          if (!info?.pendingPath) {
            showToast(`${cliLabel(cli)}: no path is waiting for confirmation`);
            return;
          }
          const path = info.pendingPath;
          setUi({
            kind: "confirm",
            view: { title: `Run this ${cliLabel(cli)} executable?`, from: "Settings", lines: confirmPathPrompt(info) },
            label: cliLabel(cli),
            onYes: () => {
              setUi(null);
              void client
                .call("external.clis.confirmPath", { cli, path })
                .then((confirmed) => {
                  const next = (confirmed?.clis ?? clisRef.current ?? []) as ExternalCliInfo[];
                  clisRef.current = next;
                  setClis(next);
                  showToast(`${cliLabel(cli)}: path confirmed`);
                })
                .catch((error: unknown) => {
                  const facts = rpcErrorFacts(error);
                  showToast(
                    facts.code === "jarvis_person_only"
                      ? personOnlyLine(error)
                      : facts.code === "invalid_params"
                        ? `${cliLabel(cli)}: the configured path changed — run /external confirm-path ${cli} again`
                        : describeExternalError(error, cli),
                  );
                });
            },
            onNo: () => {
              setUi(null);
              showToast(`${cliLabel(cli)}: path not confirmed`);
            },
          });
        })
        .catch((error: unknown) => showToast(describeExternalError(error)));
    },
    [client, showToast],
  );

  const changeMode = useCallback(
    (mode: "default" | "readonly" | "unrestricted") => {
      const view = externalRef.current;
      if (!view) {
        showToast("open an external session first — /external");
        return;
      }
      if (view.session.mode === mode) {
        showToast(`already ${modeLabel(mode)}`);
        return;
      }
      const info = clisRef.current?.find((entry) => entry.id === view.ref.cli);
      if (info && !modesFor(info).includes(mode)) {
        showToast(`${cliLabel(view.ref.cli)} has no ${modeLabel(mode)} mode`);
        return;
      }
      if (mode === "unrestricted") {
        setUi({
          kind: "unrestricted",
          ref: view.ref,
          lines: unrestrictedPrompt({ cli: view.ref.cli, title: view.session.title, cwd: view.session.cwd }),
        });
        return;
      }
      void client
        .call("external.setMode", { ref: view.ref, mode })
        .then((result) => {
          viewDispatch({ type: "session", session: result.session });
          showToast(`mode: ${modeLabel(mode)}`);
        })
        .catch((error: unknown) => showToast(describeExternalError(error, view.ref.cli)));
    },
    [client, showToast],
  );

  const confirmUnrestricted = useCallback(() => {
    if (ui?.kind !== "unrestricted") return;
    const ref = ui.ref;
    setUi(null);
    void client
      .call("external.setMode", { ref, mode: "unrestricted" })
      .then((result) => {
        viewDispatch({ type: "session", session: result.session });
        showToast("mode: UNRESTRICTED");
      })
      .catch((error: unknown) => showToast(describeExternalError(error, ref.cli)));
  }, [client, showToast, ui]);

  const handleExternal = useCallback(
    (line: string): boolean => {
      const command = parseExternalCommand(line);
      if (!command) return false;
      if (!gate("external")) return true;
      switch (command.kind) {
        case "invalid":
          note(`usage: ${command.usage}`);
          break;
        case "picker":
          openPicker();
          break;
        case "new":
          openNewForm();
          break;
        case "open":
          openByRef(command.ref as ExternalRef);
          break;
        case "close":
          if (externalRef.current) leaveExternal();
          else showToast("no external session is open");
          break;
        case "stop":
          stopExternal();
          break;
        case "adopt":
          adoptExternal();
          break;
        case "mode":
          changeMode(command.mode);
          break;
        case "confirmPath":
          confirmCliPath(command.cli);
          break;
        case "more":
          loadOlder();
          break;
      }
      return true;
    },
    [adoptExternal, changeMode, confirmCliPath, gate, leaveExternal, loadOlder, note, openByRef, openNewForm, openPicker, showToast, stopExternal],
  );

  /** `/jarvis …` and `/external …`; false for any other line. */
  const handleCommand = useCallback(
    (line: string): boolean => handleJarvis(line) || handleExternal(line),
    [handleExternal, handleJarvis],
  );

  // `snowpea jarvis` / `snowpea external` land on their screen once the first
  // answer from the daemon is in.
  const started = useRef(false);
  useEffect(() => {
    if (!start || started.current) return;
    if (start === "external") {
      started.current = true;
      openPicker();
      return;
    }
    if (!support.jarvis) {
      started.current = true;
      note(requiresLine("jarvis"));
      return;
    }
    if (jarvis.status === null) return;
    started.current = true;
    if (!jarvis.status.enabled) {
      note("Jarvis is off — turn it on with /jarvis on, then open it with /jarvis.");
      return;
    }
    enterJarvis();
  }, [start, support.jarvis, jarvis.status, openPicker, enterJarvis, note]);

  // --- what the app draws -----------------------------------------------------------

  const marker = useMemo(() => {
    if (!jarvisOn || onJarvisScreen) return null;
    const row = jarvis.rows.find((entry) => entry.sessionId === sessionId);
    const pause = (jarvis.status?.paused ?? []).find((entry) => entry.sessionId === sessionId);
    const merged =
      row ??
      (pause
        ? { level: jarvis.status?.delegationDefault ?? null, pausedUntil: pause.until ?? null, takenOver: pause.reason === "takeover" && !pause.until }
        : null);
    // A pause the status knows about wins over a stale overview row.
    const paused = pause && merged ? { ...merged, pausedUntil: pause.until ?? merged.pausedUntil, takenOver: merged.takenOver || (pause.reason === "takeover" && !pause.until) } : merged;
    const scoped = paused ? { ...paused, levelScope: jarvis.sessionStates[sessionId]?.levelScope ?? null } : paused;
    return jarvisMarker(jarvis.status, scoped, now);
  }, [jarvis.rows, jarvis.sessionStates, jarvis.status, jarvisOn, now, onJarvisScreen, sessionId]);

  const panel = useCallback(
    (width: number, maxRows: number): PanelLine[] => (onJarvisScreen ? panelLines(jarvis, width, maxRows, now) : []),
    [jarvis, now, onJarvisScreen],
  );

  return {
    support,
    jarvis,
    onJarvisScreen,
    external,
    ui,
    setUi,
    listeners,
    reconnected,
    refresh,
    now,
    handleCommand,
    enterJarvis,
    openRow,
    openExternalRef,
    openWaiting,
    waitingItem,
    answerApproval,
    answerQuestion,
    pickerToggleAll,
    pickerMore,
    openNewForm,
    createExternal,
    sendExternal,
    interruptExternal,
    escapeExternal,
    leaveExternal,
    stopExternal,
    adoptExternal,
    answerAdopt,
    /** True when the open session's CLI takes a message during a running turn. */
    canSteer: external ? steerOf(clis, external.ref.cli) : false,
    confirmUnrestricted,
    marker,
    panel,
  };
}

export type JarvisScreens = ReturnType<typeof useJarvisScreens>;
