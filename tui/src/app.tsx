/**
 * snowpea TUI root.
 *
 * Thin client: every decision (commands, tools, permissions, modes) lives in
 * the daemon. This component only renders `session.event` streams and forwards
 * input as `session.prompt` / `command.run`.
 *
 * The default layout is inline, the way Claude Code draws: the logo is printed
 * once, finished transcript entries are handed to Ink's `<Static>` so they land
 * in the terminal's own scrollback and are never redrawn, and the only live
 * region is the tail — whatever is still streaming, the input box and the HUD
 * under it. The screen therefore fills from the bottom up and the terminal
 * keeps the scrollback. `--fullscreen` opts into the alternate-buffer layout in
 * `FullscreenLayout` instead.
 */

import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Box, Static, Text, useApp, useInput } from "ink";

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
import {
  bannerText,
  cancel as cancelUpdate,
  confirm as confirmUpdate,
  fromCheck,
  initialUpdateState,
  progress as updateProgress,
  type UpdateState,
} from "./state/update.js";
import { initialState, reducer, type State, type TimelineItem } from "./state/store.js";
import { cycleMode } from "./state/mode.js";
import { useTerminalSize } from "./hooks/useTerminalSize.js";
import { useDaemonInfo } from "./hooks/useDaemonInfo.js";
import { useElapsed } from "./hooks/useElapsed.js";
import { TUI_VERSION } from "./version.js";
import { transcriptLines } from "./layout/transcript.js";
import {
  HEADER_ROWS,
  bottomRows as reserveBottomRows,
  clampScroll,
  computeLayout,
  halfPageStep,
  pageStep,
  scrollIndicator,
  sliceViewport,
  usableRows,
} from "./layout/viewport.js";
import { buildHudSegments, layoutHud } from "./layout/hud.js";
import { settledCount } from "./layout/statics.js";
import { FullscreenLayout } from "./components/FullscreenLayout.js";
import { Chat } from "./components/Chat.js";
import { MessageView } from "./components/MessageStream.js";
import { ToolCall } from "./components/ToolCall.js";
import { DiffView } from "./components/DiffView.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import { ApprovalQueue } from "./components/ApprovalQueue.js";
import { StatusHud } from "./components/StatusHud.js";
import { Logo, logoRows } from "./components/Logo.js";
import { HelpPanel } from "./components/HelpPanel.js";
import { SubagentTree } from "./components/SubagentTree.js";
import { UpdateBanner } from "./components/UpdateBanner.js";

export const PLACEHOLDER_TEXT = "snowpea tui placeholder";

/** How often the unattended queue is re-read while it is not empty. */
export const APPROVAL_POLL_MS = 5000;

/** How long "Updated to vX — restarting…" stays on screen before the restart. */
export const RESTART_DELAY_MS = 1200;

export interface AppProps {
  client: TuiClient;
  sessionId: string;
  mode: Mode;
  workdir: string;
  provider?: string;
  model?: string;
  /**
   * Draw as a full-screen app on the alternate screen buffer. `index.tsx`
   * turns this off for `--no-fullscreen` and `SNOWPEA_TUI_INLINE=1`, which
   * fall back to rendering inline in the scrollback.
   */
  fullscreen?: boolean;
  /**
   * Called once an update finished, so `index.tsx` can exit with
   * `TUI_RESTART_EXIT` (75) and let `cli/main.py` re-exec `snowpea`.
   */
  onRestart?: () => void;
}

/** One transcript entry — a message, a tool call or a diff. */
function TimelineEntry({
  state,
  item,
  expandedCall,
}: {
  state: State;
  item: TimelineItem;
  expandedCall: string | null;
}): React.ReactElement | null {
  if (item.kind === "message") {
    const message = state.messages.find((m) => m.id === item.id);
    return message ? <MessageView message={message} /> : null;
  }
  if (item.kind === "tool") {
    const call = state.toolCalls.find((c) => c.callId === item.id);
    return call ? <ToolCall call={call} expanded={expandedCall === item.id} /> : null;
  }
  const diff = state.diffs.find((d) => d.id === item.id);
  return diff ? <DiffView diff={diff} /> : null;
}

/**
 * What `<Static>` has been given, in order.
 *
 * The logo is the first item, so it is printed once at the top of the session
 * and then scrolls away like any other output.
 */
type StaticEntry =
  | { key: "logo"; kind: "logo" }
  | { key: string; kind: "entry"; item: TimelineItem };

export function App({
  client,
  sessionId,
  mode,
  workdir,
  provider,
  model,
  fullscreen = false,
  onRestart,
}: AppProps): React.ReactElement {
  const { exit } = useApp();
  const [state, dispatch] = useReducer(reducer, initialState);
  const [showHelp, setShowHelp] = useState(false);
  const [draft, setDraft] = useState("");
  const [expandedCall, setExpandedCall] = useState<string | null>(null);
  /** True while the unattended queue holds the keyboard (Ctrl+A toggles it). */
  const [queueFocused, setQueueFocused] = useState(false);
  /** Shown once in the status line until the shortcut is used or it times out. */
  const [modeHintVisible, setModeHintVisible] = useState(true);
  /** Transient "mode: X" toast shown in the status line after a change. */
  const [modeToast, setModeToast] = useState<string | null>(null);
  /** `/ralph` while a slash command owns the turn; the HUD names it. */
  const [runningCommand, setRunningCommand] = useState<string | null>(null);
  /**
   * How many timeline entries have been handed to `<Static>`. Only ever grows:
   * an entry that reached the scrollback cannot be taken back.
   */
  const [staticCursor, setStaticCursor] = useState(0);
  /**
   * Lines the transcript is scrolled back from its newest line. 0 follows the
   * stream; PgUp / Ctrl+U walk it upwards. Full-screen only — inline rendering
   * leaves scrolling to the terminal.
   */
  const [scrollOffset, setScrollOffset] = useState(0);
  const modeToastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const registryRef = useRef<SlashRegistry>(new SlashRegistry(client, sessionId));
  /** Update banner state; see state/update.ts. */
  const [update, setUpdate] = useState<UpdateState>(initialUpdateState);
  /**
   * Whether `system.checkUpdate` said a newer release exists. The banner state
   * cannot answer this on its own: dismissing the banner puts it back to idle,
   * and the HUD should still offer the upgrade.
   */
  const [updateAvailable, setUpdateAvailable] = useState(false);
  /** Resolver for the approval promise the SDK is awaiting. */
  const approvalResolver = useRef<((response: ApprovalResponse) => void) | null>(null);

  const refreshApprovals = useCallback(() => {
    void client
      .listApprovals(sessionId)
      .then((result) => dispatch({ type: "approval/list", requests: result.requests ?? [] }))
      .catch(() => {
        /* approval.list is advisory; a failure must not block the session. */
      });
  }, [client, sessionId]);

  useEffect(() => {
    dispatch({ type: "session/ready", sessionId, mode, provider, model });
    dispatch({ type: "status", status: client.getStatus() });

    client.setListeners({
      onSessionEvent: (event) => dispatch({ type: "session/event", event }),
      onStatus: (status) => dispatch({ type: "status", status }),
      // An unattended turn raised a request the daemon broadcast to every
      // surface; the queue is re-read rather than trusted from the payload.
      onApprovalPending: () => refreshApprovals(),
      // A plugin install or `skill.reload` moved the server-side table.
      onCommandsChanged: () => {
        void registryRef.current
          .refresh()
          .then((commands: CommandInfo[]) => dispatch({ type: "commands", commands }))
          .catch(() => {
            /* the table is advisory; a failed refresh must not break the UI. */
          });
      },
      onUpdateProgress: ({ phase, message }) =>
        setUpdate((current) => {
          const next = (phase ?? "started") as "started" | "done" | "failed";
          return updateProgress(current, next, message ?? "");
        }),
      onApprovalResolved: ({ requestId }) => {
        dispatch({ type: "approval/resolved", requestId });
        // Another surface may have answered one of ours, or freed a slot that
        // lets a queued turn raise its own request; re-read the backlog.
        refreshApprovals();
      },
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

    refreshApprovals();

    // The daemon answers from its 24h cache, so this costs nothing on most
    // launches and never blocks the first render.
    void client
      .checkUpdate()
      .then((check) => {
        setUpdateAvailable(Boolean(check.available) && !check.error);
        setUpdate((current) => fromCheck(current, check));
      })
      .catch(() => {
        /* the check is advisory; a failure must never disturb the session. */
      });
  }, [client, sessionId, mode, provider, model, refreshApprovals]);

  // An upgrade that finished: tell the daemon to go, then ask to be restarted.
  useEffect(() => {
    if (update.phase !== "done") return;
    let cancelled = false;
    const timer = setTimeout(() => {
      void client
        .restartDaemon()
        .catch(() => undefined)
        .then(() => {
          if (!cancelled) {
            onRestart?.();
            exit();
          }
        });
    }, RESTART_DELAY_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [update.phase, client, onRestart, exit]);

  // Poll while something is waiting: unattended requests are raised by turns
  // this surface never sees, so there is no event to hang the refresh off.
  useEffect(() => {
    if (state.approvalQueue.length === 0) return;
    const timer = setInterval(refreshApprovals, APPROVAL_POLL_MS);
    return () => clearInterval(timer);
  }, [state.approvalQueue.length, refreshApprovals]);

  // Release finished entries into the scrollback as soon as they settle.
  useEffect(() => {
    setStaticCursor((cursor) => settledCount(state, cursor));
  }, [state]);

  // The turn that carried the command is over; the HUD stops naming it.
  useEffect(() => {
    if (!state.turnActive) setRunningCommand(null);
  }, [state.turnActive]);

  // Nothing left to answer: give the keyboard back to the chat line.
  useEffect(() => {
    if (state.approvalQueue.length === 0 && queueFocused) setQueueFocused(false);
  }, [state.approvalQueue.length, queueFocused]);

  // The Shift+Tab hint is a one-time nudge; it fades on its own if unused.
  useEffect(() => {
    const timer = setTimeout(() => setModeHintVisible(false), 6000);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => () => {
    if (modeToastTimer.current) clearTimeout(modeToastTimer.current);
  }, []);

  /** Optimistically applies a mode change, then confirms it with the daemon. */
  const changeMode = useCallback(
    (next: Mode) => {
      setModeHintVisible(false);
      dispatch({ type: "mode", mode: next });
      setModeToast(`mode: ${next.toUpperCase()}`);
      if (modeToastTimer.current) clearTimeout(modeToastTimer.current);
      modeToastTimer.current = setTimeout(() => setModeToast(null), 2000);
      void client
        .setMode(sessionId, next)
        .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
    },
    [client, sessionId],
  );

  const completions = useMemo(
    () => (draft.startsWith("/") ? registryRef.current.complete(draft) : []),
    [draft, state.commands],
  );

  // --- full-screen geometry -------------------------------------------------
  // Everything below is inert while `fullscreen` is false: the inline layout
  // lets Ink and the terminal do the measuring and the scrolling.
  const terminal = useTerminalSize();
  /** The root box pads one column on each side. */
  const contentWidth = Math.max(1, terminal.columns - 2);

  const daemon = useDaemonInfo(client);
  const sessionElapsedMs = useElapsed();
  /** Daemon version once it answered; the bundled constant until then. */
  const version = update.current || TUI_VERSION;
  const hudSegments = useMemo(
    () =>
      buildHudSegments({
        status: state.status,
        version,
        latestVersion: updateAvailable ? update.latest : null,
        workdir,
        sessionId: state.sessionId,
        provider: state.provider,
        model: state.model,
        mode: state.mode,
        usage: state.usage,
        sessionMs: sessionElapsedMs,
        daemonPid: daemon.pid,
        daemonSummary: daemon.summary,
        pendingApprovals: state.approvalQueue.length,
        runningCommand,
        turnActive: state.turnActive,
        toast: modeToast,
        modeHint: modeHintVisible,
      }),
    [
      state.status,
      version,
      update.latest,
      updateAvailable,
      workdir,
      state.sessionId,
      state.provider,
      state.model,
      state.mode,
      state.usage,
      sessionElapsedMs,
      daemon.pid,
      daemon.summary,
      state.approvalQueue.length,
      runningCommand,
      state.turnActive,
      modeToast,
      modeHintVisible,
    ],
  );
  const hudRows = useMemo(
    () => layoutHud(hudSegments, contentWidth),
    [hudSegments, contentWidth],
  );

  const layout = computeLayout({
    // One row short of the terminal on purpose; see `RESERVED_FRAME_ROW`.
    rows: usableRows(terminal.rows),
    columns: terminal.columns,
    // Logo block, then the workdir row and the rule row.
    headerRows: logoRows(terminal.rows) + HEADER_ROWS,
    // The mode bar, then however many rows the HUD packed itself into.
    statusRows: 1 + hudRows.length,
    bottomRows: reserveBottomRows({
      paletteCommands: draft.startsWith("/") ? completions.length : 0,
      approvalArgs: state.pendingApproval
        ? Object.keys(state.pendingApproval.args ?? {}).length
        : null,
      queueRequests: state.approvalQueue.length,
      queueFocused,
      errorVisible: state.errors.length > 0,
    }),
  });
  const lines = useMemo(
    () => (fullscreen ? transcriptLines(state, contentWidth, { expandedCall }) : []),
    [fullscreen, state, contentWidth, expandedCall],
  );
  // Memoized so typing, which touches neither the transcript nor the scroll
  // position, hands `TranscriptView` the very same array and its memo holds.
  const viewport = useMemo(
    () => sliceViewport(lines, layout.transcriptRows, scrollOffset),
    [lines, layout.transcriptRows, scrollOffset],
  );

  // Growing the transcript must not slide the window out from under a reader
  // who has scrolled up: the offset counts from the bottom, so it has to grow
  // with it. At offset 0 the view simply keeps following the newest line.
  const previousLineCount = useRef(0);
  useEffect(() => {
    const grown = lines.length - previousLineCount.current;
    previousLineCount.current = lines.length;
    if (grown > 0) setScrollOffset((offset) => (offset > 0 ? offset + grown : 0));
  }, [lines.length]);

  // A resize changes how much fits, so an offset from the old height may now
  // point past the end of the transcript.
  useEffect(() => {
    setScrollOffset((offset) => clampScroll(offset, lines.length, layout.transcriptRows));
  }, [layout.transcriptRows, lines.length]);

  const scrollBy = useCallback(
    (delta: number) =>
      setScrollOffset((offset) => clampScroll(offset + delta, lines.length, layout.transcriptRows)),
    [lines.length, layout.transcriptRows],
  );

  const submit = useCallback(
    (text: string) => {
      // `/update` is a core builtin (headless and IDE run it as a command), but
      // in the TUI it opens the confirmation banner instead of firing blind.
      if (/^\/update\s*$/.test(text.trim())) {
        setUpdate(confirmUpdate);
        return;
      }
      dispatch({ type: "user/message", text });
      // Name the command in the HUD for as long as its turn owns the session.
      const commandName = text.startsWith("/") ? `/${text.slice(1).split(/\s/)[0]}` : null;
      setRunningCommand(commandName);
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
      void run.catch((error: unknown) => {
        setRunningCommand(null);
        dispatch({ type: "error", message: String(error) });
      });
    },
    [client, sessionId],
  );

  /** Answer the y/n banner prompt: start the upgrade, or put the banner away. */
  const answerUpdate = useCallback(
    (accepted: boolean) => {
      if (!accepted) {
        setUpdate(cancelUpdate);
        return;
      }
      setUpdate((current) => updateProgress(current, "started", "starting the update…"));
      void client
        .startUpdate()
        .then((result) => {
          if (!result.started) {
            setUpdate((current) =>
              updateProgress(current, "failed", result.error ?? "the update did not start"),
            );
          }
        })
        .catch((error: unknown) =>
          setUpdate((current) => updateProgress(current, "failed", String(error))),
        );
    },
    [client],
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

  const respondQueued = useCallback(
    (requestId: string, decision: ApprovalDecision, scope: ApprovalScope) => {
      dispatch({ type: "approval/resolved", requestId });
      void client
        .respondApproval(requestId, decision, scope)
        .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
    },
    [client],
  );

  useInput((input, key) => {
    if (key.ctrl && input === "c") {
      exit();
      return;
    }
    if (key.ctrl && input === "a") {
      setQueueFocused((focused) => !focused && state.approvalQueue.length > 0);
      return;
    }
    if (key.ctrl && input === "o") {
      const last = state.toolCalls[state.toolCalls.length - 1];
      setExpandedCall((current) => (current ? null : (last?.callId ?? null)));
      return;
    }
    // Scrolling the transcript is ours only in full-screen mode; inline, the
    // terminal's own scrollback already holds the history.
    if (fullscreen) {
      if (key.pageUp) {
        scrollBy(pageStep(layout.transcriptRows));
        return;
      }
      if (key.pageDown) {
        scrollBy(-pageStep(layout.transcriptRows));
        return;
      }
      if (key.ctrl && input === "u") {
        scrollBy(halfPageStep(layout.transcriptRows));
        return;
      }
      if (key.ctrl && input === "d") {
        scrollBy(-halfPageStep(layout.transcriptRows));
        return;
      }
    }
    // Shift+Tab cycles accept -> auto -> plan -> accept, like Claude Code.
    // Ink 5 reports this as key.tab + key.shift; some terminals instead send
    // the raw "[Z" (or a bare "[Z") escape, so both are handled.
    if ((key.tab && key.shift) || input === "[Z" || input === "[Z") {
      changeMode(cycleMode(state.mode));
      return;
    }
    // The update banner owns y/n while it is asking.
    if (update.phase === "confirm") {
      if (input === "y" || input === "Y") {
        answerUpdate(true);
        return;
      }
      if (input === "n" || input === "N" || key.escape) {
        answerUpdate(false);
        return;
      }
    }
    // U opens the same confirmation the /update command does.
    if ((input === "U" || input === "u") && update.phase === "available") {
      setUpdate(confirmUpdate);
      return;
    }
    // Ctrl+P: cheap on/off toggle for plan mode.
    if (key.ctrl && input === "p") {
      changeMode(state.mode === "plan" ? "accept" : "plan");
    }
  });

  const approvalActive = state.pendingApproval !== null;

  /** Input block: the error row, the backlog, and the chat line or prompt. */
  const bottomNode = (
    <>
      {state.errors.length > 0 ? (
        <Text color="red" wrap="truncate-end">
          {state.errors[state.errors.length - 1]}
        </Text>
      ) : null}

      <ApprovalQueue
        requests={state.approvalQueue}
        onRespond={respondQueued}
        isActive={queueFocused && state.pendingApproval === null}
        onBlur={() => setQueueFocused(false)}
      />

      {state.pendingApproval ? (
        <ApprovalPrompt request={state.pendingApproval} onDecide={decideApproval} />
      ) : (
        <Chat
          onSubmit={submit}
          completions={completions}
          onChange={setDraft}
          onInterrupt={() => void client.interrupt(sessionId).catch(() => undefined)}
          onToggleHelp={() => setShowHelp((v) => !v)}
          disabled={approvalActive || queueFocused}
        />
      )}
    </>
  );

  // Mode, workdir and session all live in the HUD now, so it is the whole
  // status block.
  const statusNode = <StatusHud rows={hudRows} />;

  /** The logo, then every entry that has settled, in order. */
  const staticItems = useMemo<StaticEntry[]>(() => {
    const items: StaticEntry[] = [{ key: "logo", kind: "logo" }];
    for (const item of state.timeline.slice(0, staticCursor)) {
      items.push({ key: `${item.kind}-${item.id}`, kind: "entry", item });
    }
    return items;
  }, [state.timeline, staticCursor]);

  const helpNode = showHelp ? (
    <HelpPanel
      commands={state.commands}
      runningSubagents={state.subagents.filter((agent) => agent.status === "running").length}
    />
  ) : null;

  if (fullscreen) {
    return (
      <FullscreenLayout
        rows={layout.rows}
        terminalRows={terminal.rows}
        version={version}
        columns={layout.columns}
        transcriptRows={layout.transcriptRows}
        lines={viewport.lines}
        banner={bannerText(update)}
        scrollIndicator={scrollIndicator(viewport)}
        overlay={helpNode}
        bottom={bottomNode}
        status={statusNode}
      />
    );
  }

  // --- inline layout, the default ------------------------------------------
  // Everything before `staticCursor` is already in the terminal's scrollback;
  // what follows is the live tail that a keystroke is allowed to repaint.
  const live = state.timeline.slice(staticCursor);

  return (
    <Box flexDirection="column" paddingX={1}>
      <Static items={staticItems}>
        {(entry) =>
          entry.kind === "logo" ? (
            <Box key="logo" flexDirection="column" marginBottom={1}>
              <Logo terminalRows={terminal.rows} version={version} width={contentWidth} />
            </Box>
          ) : (
            <Box key={entry.key} flexDirection="column">
              <TimelineEntry state={state} item={entry.item} expandedCall={expandedCall} />
            </Box>
          )
        }
      </Static>

      {live.map((item) => (
        <TimelineEntry
          key={`${item.kind}-${item.id}`}
          state={state}
          item={item}
          expandedCall={expandedCall}
        />
      ))}

      <SubagentTree subagents={state.subagents} />

      <UpdateBanner update={update} />

      {helpNode}

      {bottomNode}

      {statusNode}
    </Box>
  );
}
