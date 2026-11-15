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
  ApprovalDecision,
  ApprovalRequestParams,
  ApprovalScope,
  ExternalCliInfo,
  ExternalRef,
  ExternalSessionRow,
  JarvisOverviewRow,
  QuestionRequestParams,
} from "../rpc/sdk.js";
import {
  externalReducer,
  describeExternalError,
  modeLabel,
  openExternal,
  sameRef,
  unrestrictedPrompt,
  usable,
  viewOnlyReason,
  type ExternalAction,
  type ExternalView,
} from "../state/external.js";
import {
  initialJarvis,
  jarvisMarker,
  jarvisReducer,
  panelLines,
  requiresLine,
  sessionLabel,
  statusLines,
  supportOf,
  type JarvisState,
  type PanelLine,
  type Support,
  type WaitingItem,
} from "../state/jarvis.js";
import { parseExternalCommand, parseJarvisCommand } from "../slash/jarvis.js";
import { cliLabel } from "../state/external.js";

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
    }
  | { kind: "new"; infos: ExternalCliInfo[] }
  | { kind: "sessions" }
  | { kind: "answer"; requestId: string }
  | { kind: "unrestricted"; ref: ExternalRef; lines: string[] };

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

  const refresh = useCallback(async () => {
    if (!support.jarvis || refreshing.current) return;
    refreshing.current = true;
    try {
      const status = await client.call("jarvis.status", {}).catch(() => null);
      if (status) jarvisDispatch({ type: "status", status });
      if (!status?.enabled) return;
      const [overview, approvals, questions, ledger] = await Promise.all([
        client.call("jarvis.overview", { includeIdle: true, includeExternal: true, limit: 100 }).catch(() => null),
        client.listApprovals().catch(() => null),
        client.call("question.list", {}).catch(() => null),
        support.delegation ? client.call("jarvis.ledger", { limit: 12 }).catch(() => null) : Promise.resolve(null),
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
    } finally {
      refreshing.current = false;
    }
  }, [client, support.jarvis, support.delegation]);

  useEffect(() => {
    void refresh();
  }, [refresh, sessionId]);

  useEffect(() => {
    if (!jarvisOn) return;
    const timer = setInterval(() => void refresh(), onJarvisScreen ? POLL_VISIBLE_MS : POLL_BACKGROUND_MS);
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
      onQuestionPending: () => void refresh(),
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
    [refresh],
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

  const openExternalRef = useCallback(
    (ref: ExternalRef, mode?: string) => {
      void client
        .call("external.open", { ref, limit: 200, ...(mode ? { mode } : {}) })
        .then((result) => {
          viewDispatch({ type: "opened", session: result.session, events: result.events ?? [], hasMore: result.hasMore === true });
          setUi(null);
        })
        .catch((error: unknown) => showToast(describeExternalError(error, ref.cli)));
    },
    [client, showToast],
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
          const code = (error as { data?: { code?: string } } | null)?.data?.code;
          note(
            code === "jarvis_disabled"
              ? "Jarvis is off — /jarvis on"
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
    return clisRef.current;
  }, [client]);

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
    setUi({ kind: "picker", rows: [], nextBefore: null, allProjects: false, loading: true, error: null, unavailable: [] });
    void loadClis()
      .then((infos) => {
        const unavailable = infos.filter((info) => !usable(info)).map((info) => `${cliLabel(info.id)} (${info.installed ? "off" : "not installed"})`);
        setUi((current) => (current?.kind === "picker" ? { ...current, unavailable } : current));
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
      void client
        .call("external.create", {
          cli: choice.cli,
          project: workdir,
          mode: choice.mode,
          transport: "structured",
          ...(choice.model ? { model: choice.model } : {}),
        })
        .then((result) => {
          viewDispatch({ type: "opened", session: result.session, events: [], hasMore: false });
          setUi(null);
        })
        .catch((error: unknown) => {
          setUi(null);
          note(`external: ${describeExternalError(error, choice.cli)}`);
        });
    },
    [client, note, workdir],
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
      viewDispatch({ type: "sent", text });
      void client.call("external.send", { ref: view.ref, text }).catch((error: unknown) => {
        const message = describeExternalError(error, view.ref.cli);
        viewDispatch({ type: "failed", message });
      });
    },
    [client, showToast],
  );

  const interruptExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    void client
      .call("external.interrupt", { ref: view.ref })
      .catch((error: unknown) => showToast(describeExternalError(error, view.ref.cli)));
  }, [client, showToast]);

  const leaveExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    viewDispatch({ type: "close" });
    void client.call("external.close", { ref: view.ref }).catch(() => undefined);
  }, [client]);

  /** Esc in an external session: stop the running turn, else go back. */
  const escapeExternal = useCallback(() => {
    const view = externalRef.current;
    if (!view) return;
    if (view.state.turnActive) interruptExternal();
    else leaveExternal();
  }, [interruptExternal, leaveExternal]);

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
        case "close":
          if (externalRef.current) leaveExternal();
          else showToast("no external session is open");
          break;
        case "mode":
          changeMode(command.mode);
          break;
        case "more":
          loadOlder();
          break;
      }
      return true;
    },
    [changeMode, gate, leaveExternal, loadOlder, note, openNewForm, openPicker, showToast],
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
    return jarvisMarker(jarvis.status, paused, now);
  }, [jarvis.rows, jarvis.status, jarvisOn, now, onJarvisScreen, sessionId]);

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
    confirmUnrestricted,
    marker,
    panel,
  };
}

export type JarvisScreens = ReturnType<typeof useJarvisScreens>;
