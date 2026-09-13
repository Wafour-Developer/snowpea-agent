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
import { Box, Static, Text, useApp, useInput, useStdin } from "ink";

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
import {
  initialState,
  reducer,
  type State,
  type TimelineItem,
  type ToolCallEntry,
} from "./state/store.js";
import { derivePhase, queuedLabel, turnSummaryLine, workingLine } from "./state/working.js";
import { cycleMode } from "./state/mode.js";
import { useTerminalSize } from "./hooks/useTerminalSize.js";
import { useDaemonInfo } from "./hooks/useDaemonInfo.js";
import { useElapsed } from "./hooks/useElapsed.js";
import { useSpinner } from "./hooks/useSpinner.js";
import { useKnownAgents } from "./hooks/useKnownAgents.js";
import { clampFocus, focusDown, focusUp, isInput, INPUT_FOCUS, type Focus } from "./state/focus.js";
import { offerSession } from "./state/history.js";
import {
  beginRecording,
  endRecording,
  speak,
  stopSpeaking,
  type AudioRuntime,
  type RecordingHandle,
  type SpeechHandle,
} from "./state/audio-runtime.js";
import { createAudioClient, describeAudioError, type AudioClient } from "./rpc/audio.js";
import { INHERIT_REF, modelOptions, modelSource, type ModelOption } from "./state/models.js";
import { delegationHint, delegationLabel } from "./state/delegation.js";
import type { LocalAudio } from "./util/audio-tools.js";
import {
  addAttachments,
  removeLast,
  scanAttachments,
  type Attachment,
  type FileProbe,
} from "./state/attachments.js";
import {
  SPEAKING_LABEL,
  initialVoice,
  noAudio,
  recordingLabel,
  setTts,
  startRecording,
  stopRecording,
  toggleVoiceInput,
  type AudioCapabilities,
  type VoiceState,
} from "./state/voice.js";
import type { SessionMemory, SessionRecord, TuiHistory } from "./state/history.js";
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
import { buildHudSegments, formatTokens, layoutHud } from "./layout/hud.js";
import { settledCount } from "./layout/statics.js";
import { groupCalls, toolKind } from "./layout/summary.js";
import { agentStatusText, buildAgentRows } from "./layout/agents.js";
import { compactionDivider, contextWarning, summaryLine } from "./layout/bottom.js";
import { FullscreenLayout } from "./components/FullscreenLayout.js";
import { Chat } from "./components/Chat.js";
import { MessageView } from "./components/MessageStream.js";
import { ToolCall } from "./components/ToolCall.js";
import { DiffView } from "./components/DiffView.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import { ApprovalQueue } from "./components/ApprovalQueue.js";
import { ConfirmMenu, type ConfirmOption } from "./components/ConfirmMenu.js";
import { StatusHud } from "./components/StatusHud.js";
import { AgentPanel } from "./components/AgentPanel.js";
import { AttachmentChips } from "./components/AttachmentChips.js";
import { ModelPicker } from "./components/ModelPicker.js";
import { QueuedPrompts } from "./components/QueuedPrompts.js";
import { AgentTranscript } from "./components/AgentTranscript.js";
import { SectionRule } from "./components/SectionRule.js";
import { LaunchBanner } from "./components/LaunchBanner.js";
import { ShellList } from "./components/ShellList.js";
import { ToolSummary } from "./components/ToolSummary.js";
import { WorkingIndicator } from "./components/WorkingIndicator.js";
import { Logo, logoRows } from "./components/Logo.js";
import { HelpPanel } from "./components/HelpPanel.js";
import { UpdateBanner } from "./components/UpdateBanner.js";

export const PLACEHOLDER_TEXT = "snowpea tui placeholder";

/** The answers to "update now?", offered the same way approvals are. */
const UPDATE_OPTIONS: ConfirmOption<boolean>[] = [
  { label: "Update and restart", value: true, shortcut: "y" },
  { label: "Not now", value: false, shortcut: "n", danger: true },
];

/** How often the unattended queue is re-read while it is not empty. */
export const APPROVAL_POLL_MS = 5000;

/**
 * Longest message the status line will carry.
 *
 * A daemon's explanation — "no transcription backend: install the whisper CLI,
 * set an OpenAI API key, or configure audio.stt.command" — is far too long for
 * a slot beside six other segments, and squeezing it in there costs the user
 * every other thing the line says. Anything longer gets its own row above the
 * input, where it fits and can be read.
 */
export const TOAST_INLINE_MAX = 48;

/** Rows the open agent transcript is given, and what PgUp/PgDn move by. */
export const AGENT_TRANSCRIPT_ROWS = 12;

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
  /** Prompt history on disk; absent in tests, which need no files. */
  history?: TuiHistory;
  /** What was last open in this directory, and where to record this one. */
  sessions?: SessionMemory;
  /**
   * The newest session `session.list` still has open for this directory, read
   * before the first render so the launch banner can offer it.
   */
  priorSession?: SessionRecord | null;
  /** Resolves pasted paths against the file system; absent in tests. */
  probe?: FileProbe;
  /** Saves an image from the system clipboard, or null when it cannot. */
  captureClipboard?: () => string | null;
  /**
   * What the daemon says it can do with audio. Read from
   * `audio.capabilities` after connecting; this prop seeds it, which is what
   * the tests use.
   */
  audio?: AudioCapabilities;
  /** Recording and playback on this machine, for what the daemon cannot do. */
  localAudio?: LocalAudio | null;
  /** Where a local recording is written. */
  recordingPath?: string;
}

/** One transcript entry — a message, a tool call, a diff or a compaction. */
function TimelineEntry({
  state,
  item,
  expandedId,
  width,
}: {
  state: State;
  item: TimelineItem;
  /** The entry Ctrl+O opened, if it is this one. */
  expandedId: string | null;
  width: number;
}): React.ReactElement | null {
  if (item.kind === "message") {
    const message = state.messages.find((m) => m.id === item.id);
    return message ? <MessageView message={message} width={width} /> : null;
  }
  if (item.kind === "tool") {
    const call = state.toolCalls.find((c) => c.callId === item.id);
    return call ? <ToolCall call={call} expanded={expandedId === item.id} /> : null;
  }
  if (item.kind === "compaction") {
    const entry = state.compactions.find((c) => c.id === item.id);
    return entry ? (
      <Box marginBottom={1}>
        <Text dimColor>{compactionDivider(entry.before, entry.after, width)}</Text>
      </Box>
    ) : null;
  }
  const diff = state.diffs.find((d) => d.id === item.id);
  return diff ? <DiffView diff={diff} expanded={expandedId === item.id} /> : null;
}

/**
 * What `<Static>` has been given, in order.
 *
 * The logo is the first item, so it is printed once at the top of the session
 * and then scrolls away like any other output.
 */
type StaticEntry =
  | { key: "launch"; kind: "launch" }
  | { key: string; kind: "entry"; item: TimelineItem }
  /** A run of successful tool calls, folded into one line. */
  | { key: string; kind: "tools"; calls: ToolCallEntry[] }
  /** The `✓ Done in 12s` line a finished turn leaves behind. */
  | { key: string; kind: "note"; text: string; ok: boolean };

/**
 * Turn a slice of settled timeline entries into what the scrollback shows.
 *
 * Consecutive successful tool calls fold into one summary line; a failed call
 * breaks the run and keeps its own card, so a failure is never summarised away.
 */
function releaseEntries(state: State, items: TimelineItem[]): StaticEntry[] {
  const out: StaticEntry[] = [];
  let run: ToolCallEntry[] = [];

  const flush = (): void => {
    for (const block of groupCalls(run)) {
      if (block.kind === "single") {
        out.push({
          key: `tool-${block.call.callId}`,
          kind: "entry",
          item: { kind: "tool", id: block.call.callId },
        });
        continue;
      }
      out.push({
        key: `tools-${block.calls[0].callId}-${block.calls.length}`,
        kind: "tools",
        calls: block.calls,
      });
    }
    run = [];
  };

  for (const item of items) {
    if (item.kind === "tool") {
      const call = state.toolCalls.find((c) => c.callId === item.id);
      if (call) {
        run.push(call);
        continue;
      }
    }
    flush();
    out.push({ key: `${item.kind}-${item.id}`, kind: "entry", item });
  }
  flush();
  return out;
}

export function App({
  client,
  sessionId: initialSessionId,
  mode,
  workdir,
  provider,
  model,
  fullscreen = false,
  onRestart,
  history,
  sessions,
  priorSession = null,
  probe,
  captureClipboard,
  audio = noAudio,
  localAudio = null,
  recordingPath,
}: AppProps): React.ReactElement {
  const { exit } = useApp();
  const [sessionId, setSessionId] = useState(initialSessionId);
  const activeSessionRef = useRef(initialSessionId);
  const resumingRef = useRef(false);
  const [state, dispatch] = useReducer(reducer, initialState);
  const [showHelp, setShowHelp] = useState(false);
  const { stdin } = useStdin();
  const [draft, setDraft] = useState("");
  /** Id of the transcript entry Ctrl+O opened: a tool call or a diff. */
  const [expandedId, setExpandedId] = useState<string | null>(null);
  /** True while the unattended queue holds the keyboard (Ctrl+R toggles it). */
  const [queueFocused, setQueueFocused] = useState(false);
  /** Ctrl+A opens the agent panel out past its collapsing rules. */
  const [agentsExpanded, setAgentsExpanded] = useState(false);
  const [agentRosterVersion, setAgentRosterVersion] = useState(0);
  /** Files the next prompt will carry. */
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  /** Voice input and speech; see state/voice.ts. */
  const [voice, setVoice] = useState<VoiceState>(initialVoice);
  /** Text waiting to be put in the draft, e.g. a transcription. */
  const [insert, setInsert] = useState<string | null>(null);
  /** Keystroke that moves focus back from a status/agent row into the draft. */
  const [append, setAppend] = useState<string | null>(null);
  /**
   * Where the session's model came from, when the daemon says.
   *
   * `session.list` does not carry a source tag yet; `modelSource` reads one if
   * a later daemon adds it and answers null until then, which is why the HUD
   * simply omits the tag rather than guessing "global".
   */
  const [sessionModelSource, setSessionModelSource] = useState<string | null>(null);
  /** Options for the `/model` picker, or null while it is closed. */
  const [modelPicker, setModelPicker] = useState<ModelOption[] | null>(null);
  /** What the daemon can do with audio; the prop is the starting point. */
  const [capabilities, setCapabilities] = useState<AudioCapabilities>(audio);
  /** The recording in flight, and the reply being spoken. */
  const recordingRef = useRef<RecordingHandle | null>(null);
  const speechRef = useRef<SpeechHandle | null>(null);
  /** Assistant messages already spoken, so a re-render cannot repeat one. */
  const spokenRef = useRef<Set<string>>(new Set());
  /** Where the keyboard is: the input, the footer row, or an agent row. */
  const [focus, setFocus] = useState<Focus>(INPUT_FOCUS);
  /** True while the footer row is showing what is running. */
  const [shellsOpen, setShellsOpen] = useState(false);
  /** Child session whose transcript replaced the live area, if any. */
  const [openAgent, setOpenAgent] = useState<{ sessionId: string; name: string } | null>(null);
  /** Saved sessions offered after a bare `/resume`. */
  const [resumeChoices, setResumeChoices] = useState<SessionRecord[] | null>(null);
  const [modePicker, setModePicker] = useState(false);
  /** Lines the open agent's transcript is scrolled back from its newest line. */
  const [agentScroll, setAgentScroll] = useState(0);
  /** Prompts from previous runs, read once at start. */
  const [pastPrompts] = useState<string[]>(() => {
    history?.load();
    return history?.prompts() ?? [];
  });
  /** The session last open in this directory, for the launch banner. */
  const [lastSession] = useState<SessionRecord | null>(() => {
    sessions?.load();
    return offerSession(sessions?.last(workdir) ?? null, priorSession);
  });
  /** Shown once in the status line until the shortcut is used or it times out. */
  const [modeHintVisible, setModeHintVisible] = useState(true);
  /** Transient "mode: X" toast shown in the status line after a change. */
  const [modeToast, setModeToast] = useState<string | null>(null);
  /** `/ralph` while a slash command owns the turn; the HUD names it. */
  const [runningCommand, setRunningCommand] = useState<string | null>(null);
  /**
   * How many timeline entries have been handed to `<Static>`. Only ever grows:
   * an entry that reached the scrollback cannot be taken back.
   *
   * Derived during render rather than in an effect on purpose. An effect would
   * draw the just-finished entry once in the live region and only move it to
   * `<Static>` on the next pass, which writes it to the terminal twice.
   * `settledCount` is monotonic, so recomputing it mid-render is stable.
   */
  const staticCursorRef = useRef(0);
  /** Everything already written to the scrollback, in the order it went there. */
  const staticBlocksRef = useRef<StaticEntry[]>([{ key: "launch", kind: "launch" }]);
  /**
   * The turn in flight: when it started and what the session had spent by then,
   * so the indicator can report this turn rather than the whole session.
   */
  const turnRef = useRef<{
    startedAt: number;
    inputTokens: number;
    outputTokens: number;
    errors: number;
  } | null>(null);
  const turnActiveRef = useRef(false);
  /** Bumped per finished turn so each summary line gets its own Static key. */
  const turnCountRef = useRef(0);
  /**
   * Lines the transcript is scrolled back from its newest line. 0 follows the
   * stream; PgUp / Ctrl+U walk it upwards. Full-screen only — inline rendering
   * leaves scrolling to the terminal.
   */
  const [scrollOffset, setScrollOffset] = useState(0);
  const modeToastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Dropped queue entries, counted so one notice covers the whole flush. */
  const droppedRef = useRef(0);
  const droppedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  const audioClient = useMemo<AudioClient>(() => createAudioClient(client), [client]);
  /**
   * True when the daemon advertised audio at all in `system.hello`.
   *
   * Without it there are no audio methods to call, so the commands say that
   * instead of failing one RPC at a time.
   */
  const audioOffered = useCallback(
    () => client.serverCapabilities?.().includes("audio") ?? true,
    [client],
  );

  // What the daemon can do is read once at the start and again whenever a
  // setting moves, because installing a backend or naming a voice is a setting.
  const refreshCapabilities = useCallback(() => {
    if (!audioOffered()) {
      setCapabilities(noAudio);
      return;
    }
    void audioClient
      .capabilities()
      .then(setCapabilities)
      .catch(() => {
        // A daemon with no audio at all answers with an error; that is not a
        // failure, it is the answer, and `noAudio` already says it.
        setCapabilities(noAudio);
      });
  }, [audioClient, audioOffered]);

  useEffect(() => {
    refreshCapabilities();
  }, [refreshCapabilities]);


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
      // A delegate's events arrive on its own session; they belong to that
      // agent's transcript, never appended to this one.
      onSessionEvent: (event) => {
        // Interrupting drops whatever was waiting; the daemon says so one event
        // at a time, and one line about all of them is what a person wants.
        if (event.kind === "turn.dequeued" && (event.payload as any)?.reason === "dropped") {
          droppedRef.current += 1;
          if (droppedTimer.current) clearTimeout(droppedTimer.current);
          droppedTimer.current = setTimeout(() => {
            const count = droppedRef.current;
            droppedRef.current = 0;
            if (count > 0) showToast(`${count} queued prompt${count === 1 ? "" : "s"} dropped`);
          }, 120);
        }
        if (event.sessionId && event.sessionId !== activeSessionRef.current) {
          dispatch({ type: "child/event", sessionId: event.sessionId, event });
          return;
        }
        dispatch({ type: "session/event", event });
      },
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
      // Installing a backend or naming a voice is a setting; re-ask.
      onSettingsChanged: () => refreshCapabilities(),
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

    // Where the model came from, when the daemon reports it.
    void client
      .call("session.list", {})
      .then((result) => {
        const sessions = Array.isArray(result?.sessions) ? result.sessions : [];
        const mine = sessions.find((entry: any) => entry?.sessionId === sessionId);
        setSessionModelSource(modelSource(mine));
      })
      .catch(() => {
        /* advisory: the HUD just leaves the tag off. */
      });

    // How many tools this session has; the HUD shows the count.
    void client
      .call("tool.list", { sessionId })
      .then((result) => {
        const tools = Array.isArray(result?.tools) ? result.tools.length : 0;
        if (tools > 0) dispatch({ type: "tools", count: tools });
      })
      .catch(() => {
        /* advisory: an older daemon may not answer at all. */
      });
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

  }, [client, sessionId, mode, provider, model, refreshApprovals, refreshCapabilities]);

  // One fresh, non-blocking check per launch, independent of session/mode changes.
  useEffect(() => {
    let cancelled = false;
    void client.checkUpdate(true).then((check) => {
      if (cancelled) return;
      setUpdateAvailable(Boolean(check.available) && !check.error);
      setUpdate((current) => fromCheck(current, check));
    }).catch(() => { /* Offline startup still works. */ });
    return () => { cancelled = true; };
  }, [client]);

  // Ink 5 removes F1 from useInput entirely. Read only its raw sequences here.
  useEffect(() => {
    const onData = (data: Buffer | string) => {
      if (state.pendingApproval || update.phase === "confirm" || update.phase === "running") return;
      if (["\u001bOP", "\u001b[11~", "\u001b[[A"].includes(data.toString())) {
        setShowHelp(current => !current);
        setFocus(INPUT_FOCUS);
      }
    };
    stdin.on("data", onData);
    return () => { stdin.off("data", onData); };
  }, [stdin, state.pendingApproval, update.phase]);

  // An upgrade that finished: tell the daemon to go, then ask to be restarted.
  useEffect(() => {
    if (update.phase !== "done") return;
    let cancelled = false;
    const timer = setTimeout(() => {
      void client
        .restartDaemon()
        .then(() => {
          if (!cancelled) {
            onRestart?.();
            exit();
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) setUpdate((current) => updateProgress(current, "failed", `restart failed: ${String(error)}`));
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

  // Speech follows the transcript: every assistant message that finishes while
  // `/tts` is on is read out once, and never twice however often this renders.
  const lastMessage = state.messages[state.messages.length - 1];
  useEffect(() => {
    if (!voice.tts || !lastMessage) return;
    if (lastMessage.role !== "assistant" || lastMessage.streaming) return;
    if (spokenRef.current.has(lastMessage.id)) return;
    spokenRef.current.add(lastMessage.id);
    setVoice((current) => ({ ...current, speaking: true }));
    void speak(runtime, lastMessage.text).then((handle) => {
      speechRef.current = handle;
      if (!handle) setVoice((current) => ({ ...current, speaking: false }));
    });
    // Only a newly finished message matters, not every change around it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.tts, lastMessage?.id, lastMessage?.streaming]);

  // A compaction is easy to miss in the scrollback, so it also says so in the
  // status line for a beat.
  const compactionCount = state.compactions.length;
  useEffect(() => {
    const latest = state.compactions[compactionCount - 1];
    if (!latest) return;
    showToast(`compacted: ${formatTokens(latest.before)} → ${formatTokens(latest.after)}`);
    // Only the arrival of a new compaction matters, not the state it arrived in.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compactionCount]);

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

  /** Put a line in the status slot for a couple of seconds. */
  const showToast = useCallback((text: string) => {
    setModeToast(text);
    if (modeToastTimer.current) clearTimeout(modeToastTimer.current);
    modeToastTimer.current = setTimeout(() => setModeToast(null), 2500);
  }, []);

  /**
   * Replay a session's events into a transcript.
   *
   * `session.resume` hands back everything from `afterSeq` on and subscribes
   * this connection to the session, so live events follow by the usual route.
   */
  const resumeSession = useCallback(
    (target: string, into: "main" | "child") => {
      if (into === "main") {
        if (resumingRef.current || state.turnActive) return;
        if (target === activeSessionRef.current) return;
        resumingRef.current = true;
      }
      void client
        .call("session.resume", { sessionId: target, afterSeq: 0 })
        .then((result) => {
          if (into === "main") {
            activeSessionRef.current = target;
            registryRef.current = new SlashRegistry(client, target);
            staticCursorRef.current = 0;
            turnRef.current = null;
            turnActiveRef.current = false;
            setOpenAgent(null);
            setScrollOffset(0);
            setExpandedId(null);
            setRunningCommand(null);
            dispatch({ type: "session/reset", sessionId: target });
            setSessionId(target);
            sessions?.remember({ sessionId: target, workdir, firstPrompt: "", at: Date.now() });
          }
          const events = Array.isArray(result?.events) ? result.events : [];
          for (const event of events) {
            if (into === "child") dispatch({ type: "child/event", sessionId: target, event });
            else dispatch({ type: "session/event", event });
          }
        })
        .catch((error: unknown) =>
          dispatch({ type: "error", message: `resume failed: ${String(error)}` }),
        )
        .finally(() => { if (into === "main") resumingRef.current = false; });
    },
    [client, state.turnActive, sessions, workdir],
  );

  /** Optimistically applies a mode change, then confirms it with the daemon. */
  const changeMode = useCallback(
    (next: Mode) => {
      setModeHintVisible(false);
      dispatch({ type: "mode", mode: next });
      showToast(`mode: ${next.toUpperCase()}`);
      void client
        .setMode(sessionId, next)
        .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
    },
    [client, sessionId, showToast],
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
        modelSource: state.modelSource ?? sessionModelSource,
        mode: state.mode,
        usage: state.usage,
        context: state.context,
        toolCount: state.toolCount,
        speaking: voice.tts,
        sessionMs: sessionElapsedMs,
        daemonPid: daemon.pid,
        daemonSummary: daemon.summary,
        pendingApprovals: state.approvalQueue.length,
        runningCommand,
        turnActive: state.turnActive,
        toast: modeToast && modeToast.length <= TOAST_INLINE_MAX ? modeToast : null,
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
      state.modelSource,
      sessionModelSource,
      state.mode,
      state.usage,
      state.context,
      state.toolCount,
      voice.tts,
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

  // --- what has reached the scrollback --------------------------------------
  // Appended during render, not from an effect: an effect would draw the
  // just-finished entry once in the live region and move it to `<Static>` on
  // the next pass, writing it to the terminal twice. Every step below is
  // monotonic, so running it again for the same state changes nothing.
  const released = settledCount(state, staticCursorRef.current);
  if (released > staticCursorRef.current) {
    staticBlocksRef.current = staticBlocksRef.current.concat(
      releaseEntries(state, state.timeline.slice(staticCursorRef.current, released)),
    );
    staticCursorRef.current = released;
  }

  // A turn beginning or ending is also scrollback bookkeeping: the indicator
  // measures from the start, and the end leaves one line behind.
  const now = Date.now();
  if (state.turnActive && !turnActiveRef.current) {
    turnRef.current = {
      startedAt: now,
      inputTokens: state.usage.inputTokens,
      outputTokens: state.usage.outputTokens,
      errors: state.errors.length,
    };
  }
  if (!state.turnActive && turnActiveRef.current && turnRef.current) {
    const turn = turnRef.current;
    turnCountRef.current += 1;
    staticBlocksRef.current = staticBlocksRef.current.concat({
      key: `turn-${turnCountRef.current}`,
      kind: "note",
      ok: state.errors.length === turn.errors,
      text: turnSummaryLine({
        ok: state.errors.length === turn.errors,
        elapsedMs: now - turn.startedAt,
        inputTokens: state.usage.inputTokens - turn.inputTokens,
        outputTokens: state.usage.outputTokens - turn.outputTokens,
      }),
    });
    turnRef.current = null;
  }
  turnActiveRef.current = state.turnActive;

  const staticCursor = staticCursorRef.current;
  const staticItems = staticBlocksRef.current;

  // --- who is working for this session ---------------------------------------
  const knownAgents = useKnownAgents(client, undefined, agentRosterVersion);
  const activeTeam = knownAgents.find((agent) => agent.kind === "team")?.name;
  const agentRows = useMemo(
    () =>
      buildAgentRows({
        state,
        known: knownAgents.filter((agent) => agent.kind !== "team"),
        now,
        expanded: agentsExpanded,
        currentLabel: "main",
      }),
    // `now` deliberately left out: the panel should follow the session, not the
    // clock. The spinner's own tick is what refreshes the elapsed columns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.subagents, state.teamTasks, knownAgents, activeTeam, agentsExpanded, now],
  );

  // Rows come and go as delegates finish; the cursor must stay on one.
  const agentRowCount = agentRows.length;
  useEffect(() => {
    setFocus((current) => clampFocus(current, agentRowCount));
  }, [agentRowCount]);

  // --- the open agent's transcript, when one is open -------------------------
  const openAgentEntry = openAgent
    ? state.subagents.find((agent) => agent.sessionId === openAgent.sessionId)
    : undefined;
  const agentLines = useMemo(
    () =>
      openAgent && state.children[openAgent.sessionId]
        ? transcriptLines(state.children[openAgent.sessionId], contentWidth - 4)
        : [],
    [openAgent, state.children, contentWidth],
  );
  const agentViewport = useMemo(
    () => sliceViewport(agentLines, AGENT_TRANSCRIPT_ROWS, agentScroll),
    [agentLines, agentScroll],
  );

  // --- the working indicator -------------------------------------------------
  const phase = derivePhase(state, { runningCommand });
  const spinnerFrame = useSpinner(
    voice.recording || (phase.kind !== "idle" && phase.kind !== "approval"),
  );
  const turn = turnRef.current;
  const queuedSuffix = state.queued.length > 0 ? ` · ${queuedLabel(state.queued.length)}` : "";
  const workingText = voice.recording
    ? recordingLabel(voice.startedAt, now)
    : voice.speaking
      ? SPEAKING_LABEL
      : workingLine({
        phase,
        elapsedMs: turn ? now - turn.startedAt : 0,
        inputTokens: turn ? state.usage.inputTokens - turn.inputTokens : 0,
        outputTokens: turn ? state.usage.outputTokens - turn.outputTokens : 0,
        frame: spinnerFrame,
        verbOffset: state.messages.length,
      });
  const indicatorText = workingText === null ? null : `${workingText}${queuedSuffix}`;

  // `$agent …` in the draft: say who it is about to go to.
  const delegation = useMemo(
    () => delegationHint(draft, knownAgents.map((agent) => agent.name)),
    [draft, knownAgents],
  );

  const layout = computeLayout({
    // One row short of the terminal on purpose; see `RESERVED_FRAME_ROW`.
    rows: usableRows(terminal.rows),
    columns: terminal.columns,
    // Logo block, then the workdir row and the rule row.
    headerRows: logoRows(terminal.rows) + HEADER_ROWS,
    // The HUD's rows, the context warning when there is one, the summary line
    // and every row of the agent panel.
    statusRows:
      hudRows.length +
      (contextWarning(state.context) ? 1 : 0) +
      1 +
      agentRows.length +
      3,
    bottomRows: reserveBottomRows({
      paletteCommands: draft.startsWith("/") ? completions.length : 0,
      approvalArgs: state.pendingApproval
        ? Object.keys(state.pendingApproval.args ?? {}).length
        : null,
      queueRequests: state.approvalQueue.length,
      queueFocused,
      errorVisible: state.errors.length > 0,
      workingVisible: workingText !== null,
      delegationVisible: delegation !== null,
      queuedRows: Math.min(state.queued.length, 4),
      noticeVisible: Boolean(modeToast && modeToast.length > TOAST_INLINE_MAX),
    }),
  });
  const lines = useMemo(
    () => (fullscreen ? transcriptLines(state, contentWidth, { expandedCall: expandedId }) : []),
    [fullscreen, state, contentWidth, expandedId],
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

  /** Turn whatever was pasted into chips; false means it was ordinary text. */
  const takePaste = useCallback(
    (text: string): boolean => {
      if (!probe) return false;
      const { attachments: found, rejected } = scanAttachments(text, probe);
      for (const entry of rejected) showToast(`${entry.path}: ${entry.reason}`);
      if (found.length === 0) return rejected.length > 0;
      setAttachments((current) => addAttachments(current, found));
      return true;
    },
    [probe, showToast],
  );

  /**
   * `/model` with no argument: ask the daemon what there is and offer a list.
   *
   * Both sources are advisory — an older daemon, a vendor endpoint that refuses
   * to list — so a failure still opens the picker with whatever came back,
   * including the model already in use.
   */
  const openModelPicker = useCallback(() => {
    const settings = client
      .call("settings.get", { scope: "global" })
      .catch(() => ({ settings: {} }));
    const projectSettings = client
      .call("settings.get", { scope: "project", workdir })
      .catch(() => ({ settings: {} }));
    const discovered = client
      .call("provider.models", state.provider ? { vendor: state.provider } : {})
      .catch(() => ({ models: [], current: null }));

    void Promise.all([settings, projectSettings, discovered]).then(
      ([settingsResult, projectResult, modelsResult]) => {
      const document = (settingsResult?.settings ?? {}) as Record<string, any>;
      const project = (projectResult?.settings ?? {}) as Record<string, any>;
      const options = modelOptions({
        profiles: document.models?.profiles ?? null,
        projectProfiles: project.models?.profiles ?? null,
        defaultProfile: document.models?.default ?? null,
        agentModels: document.agents?.models ?? null,
        discovered: modelsResult?.models ?? null,
        current: state.model ?? modelsResult?.current ?? null,
        vendor: state.provider ?? modelsResult?.vendor ?? null,
      });
        setModelPicker(options);
      },
    );
  }, [client, workdir, state.provider, state.model]);

  /**
   * Pin the session to a model, or clear the pin.
   *
   * `session.setModel` is the method that persists it. An older daemon does not
   * have it and answers `method_not_found`, which is the one failure worth
   * retrying through the command — every other failure is the daemon refusing,
   * and repeating the refusal through a second path would only hide it.
   */
  const chooseModel = useCallback(
    (ref: string) => {
      void client
        .call("session.setModel", { sessionId, model: ref === INHERIT_REF ? null : ref })
        .then((result) => {
          const model = result?.model ? String(result.model) : null;
          showToast(
            result?.pinned === false
              ? `model: inherited${model ? ` (${model})` : ""}`
              : `model: ${model ?? ref}`,
          );
        })
        .catch((error: unknown) => {
          const code = (error as { code?: unknown } | null)?.code;
          if (code === -32601) {
            submit(`/model ${ref}`);
            return;
          }
          dispatch({ type: "error", message: String(error) });
        });
    },
    [client, sessionId, showToast],
  );

  /** Ctrl+V with an image on the clipboard. */
  const takeClipboard = useCallback(() => {
    if (!captureClipboard || !probe) {
      showToast("no clipboard tool available");
      return;
    }
    const path = captureClipboard();
    if (!path) {
      showToast("no image on the clipboard");
      return;
    }
    const { attachments: found } = scanAttachments(path, probe);
    if (found.length === 0) {
      showToast("the clipboard image could not be read");
      return;
    }
    setAttachments((current) => addAttachments(current, found));
  }, [captureClipboard, probe, showToast]);

  const runtime = useMemo<AudioRuntime>(
    () => ({
      audio: audioClient,
      local: localAudio,
      capabilities,
      sessionId,
      localRecordingPath: recordingPath,
      onToast: showToast,
    }),
    [audioClient, localAudio, capabilities, sessionId, recordingPath, showToast],
  );

  /** Ctrl+Space, or `/rec`. */
  const toggleRecording = useCallback(() => {
    if (recordingRef.current) {
      const handle = recordingRef.current;
      recordingRef.current = null;
      setVoice((current) => {
        const outcome = stopRecording(current, Date.now());
        showToast(outcome.message);
        return outcome.state;
      });
      void endRecording(runtime, handle).then((text) => {
        // Transcribed text goes into the draft, never straight to the model:
        // speech recognition is wrong often enough that it has to be read.
        if (text) setInsert(text);
      });
      return;
    }

    const outcome = startRecording(voice, capabilities, Date.now(), {
      localRecorder: Boolean(localAudio) && Boolean(recordingPath),
    });
    if (!outcome.ok) {
      showToast(outcome.message);
      return;
    }
    void beginRecording(runtime).then((handle) => {
      if (!handle) {
        setVoice((current) => ({ ...current, recording: false, startedAt: null }));
        return;
      }
      recordingRef.current = handle;
    });
    setVoice(outcome.state);
    showToast(outcome.message);
  }, [runtime, voice, capabilities, localAudio, recordingPath, showToast]);

  /** Stop a reply that is being read out. */
  const silence = useCallback(() => {
    if (!speechRef.current) return;
    stopSpeaking(runtime, speechRef.current);
    speechRef.current = null;
    setVoice((current) => ({ ...current, speaking: false }));
  }, [runtime]);

  /** Reopen the session this directory was last in. */
  const resumeMemory = useCallback(() => {
    if (!lastSession) return;
    showToast(`resuming ${lastSession.sessionId.slice(0, 8)}`);
    resumeSession(lastSession.sessionId, "main");
  }, [lastSession, resumeSession, showToast]);

  const openResumePicker = useCallback(() => {
    showToast("loading saved sessions");
    void client.call("session.list", { includeClosed: true, workdir }).then((result) => {
      const choices = (Array.isArray(result?.sessions) ? result.sessions : [])
        .filter((row: any) => row.sessionId !== activeSessionRef.current)
        .map((row: any) => ({
          sessionId: String(row.sessionId),
          workdir: String(row.workdir),
          firstPrompt: typeof row.lastPrompt === "string" ? row.lastPrompt : "",
          at: Date.parse(String(row.createdAt)) || 0,
        }));
      if (choices.length === 0) {
        showToast("no saved sessions for this directory");
        return;
      }
      setResumeChoices(choices);
    }).catch((error: unknown) =>
      dispatch({ type: "error", message: `could not list saved sessions: ${String(error)}` }),
    );
  }, [client, workdir, showToast]);

  const submit = useCallback(
    (text: string) => {
      if (resumingRef.current || update.phase === "running" || update.phase === "done") return;
      // `$executor fix the tests` is the compact, explicit delegation form.
      // The daemon validates both the name and membership of the active team.
      const directDelegate = /^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/.exec(text.trim());
      if (directDelegate) {
        void client.call("agent.spawn", {
          sessionId,
          name: directDelegate[1],
          task: directDelegate[2].trim(),
        }).catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
        return;
      }
      // `/update` is a core builtin (headless and IDE run it as a command), but
      // in the TUI it checks freshly before opening the confirmation banner.
      if (/^\/update\s*$/.test(text.trim())) {
        void client.checkUpdate(true).then((check) => {
          setUpdate((current) => fromCheck(current, check));
          setUpdateAvailable(Boolean(check.available) && !check.error);
          if (check.error) {
            setUpdate((current) => updateProgress(current, "failed", check.error ?? "update check failed"));
          } else if (check.available) {
            setUpdate(confirmUpdate);
          } else {
            setUpdate(cancelUpdate);
            showToast(`already up to date${check.current ? ` (${check.current})` : ""}`);
          }
        }).catch((error: unknown) => {
          setUpdate((current) => updateProgress(current, "failed", String(error)));
        });
        return;
      }
      // `/resume` is the TUI's own: the session it reopens is the one this
      // surface remembers for this directory.
      const resume = /^\/resume(?:\s+(\S+))?\s*$/.exec(text.trim());
      if (resume) {
        if (state.turnActive) {
          showToast("interrupt the current turn before resuming another session");
          return;
        }
        if (resume[1]) resumeSession(resume[1], "main");
        else openResumePicker();
        return;
      }
      if (/^\/sessions\s*$/.test(text.trim())) {
        if (state.turnActive) {
          showToast("interrupt the current turn before resuming another session");
          return;
        }
        openResumePicker();
        return;
      }
      const sessionDelete = /^\/session\s+delete\s+(\S+)\s*$/.exec(text.trim());
      const sessionClear = /^\/session\s+clear(?:\s+(--all))?\s*$/.exec(text.trim());
      if (sessionDelete || sessionClear) {
        const params = sessionDelete
          ? { sessionId: sessionDelete[1] }
          : sessionClear?.[1]
            ? { all: true }
            : { workdir };
        void client.call("session.deleteSaved", params).then((result) =>
          showToast(`deleted ${Number(result?.deleted ?? 0)} saved session(s)`),
        ).catch((error: unknown) =>
          dispatch({ type: "error", message: `session cleanup failed: ${String(error)}` }),
        );
        return;
      }
      // `/model` with an argument is the daemon's command; bare `/model` is a
      // list to pick from, which is this surface's job.
      if (/^\/model\s*$/.test(text.trim())) {
        openModelPicker();
        return;
      }
      // So are the ones that only move this surface's own switches.
      const attach = /^\/attach\s+(.+)$/.exec(text.trim());
      if (attach) {
        if (!takePaste(attach[1])) showToast(`no readable file at ${attach[1]}`);
        return;
      }
      if (/^\/(voice|rec|tts)\b/.test(text.trim()) && !audioOffered()) {
        showToast("this daemon has no audio support");
        return;
      }
      if (/^\/voice\s*$/.test(text.trim())) {
        setVoice((current) => {
          const outcome = toggleVoiceInput(current, capabilities, {
            localRecorder: Boolean(localAudio) && Boolean(recordingPath),
          });
          showToast(outcome.message);
          return outcome.state;
        });
        return;
      }
      if (/^\/rec\s*$/.test(text.trim())) {
        toggleRecording();
        return;
      }
      const tts = /^\/tts(?:\s+(on|off))?\s*$/.exec(text.trim());
      if (tts) {
        setVoice((current) => {
          const outcome = setTts(current, capabilities, tts[1] !== "off", {
            localPlayer: Boolean(localAudio),
          });
          showToast(outcome.message);
          return outcome.state;
        });
        return;
      }
      const sent = attachments.map(({ name, mime, size }) => ({ name, mime, size }));
      dispatch({ type: "user/message", text, attachments: sent });
      setAttachments([]);
      // Remember it for the next run: ↑ reaches it, and the launch screen
      // offers the session it belongs to.
      history?.add(text, workdir);
      if (state.messages.every((message) => message.role !== "user")) {
        sessions?.remember({ sessionId, workdir, firstPrompt: text, at: Date.now() });
      }
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
            if (/^team(?:\s|$)/.test(text.slice(1))) {
              setAgentRosterVersion((version) => version + 1);
            }
            if (text.slice(1).startsWith("help")) setShowHelp(true);
            return result;
          })
        : client.prompt(
            sessionId,
            text,
            attachments.map((attachment) => ({
              kind: attachment.mime.startsWith("image/") ? ("image" as const) : ("file" as const),
              name: attachment.name,
              path: attachment.path,
              mimeType: attachment.mime,
              size: attachment.size,
            })),
          );
      void run
        .then((result) => {
          // `turn.queued` carries only a turn id; this is the side that knows
          // which prompt that id belongs to, so the queue can show its text.
          const turnId = (result as { turnId?: string } | null)?.turnId;
          if (turnId) dispatch({ type: "prompt/turn", turnId, text });
          return result;
        })
        .catch((error: unknown) => {
          setRunningCommand(null);
          dispatch({ type: "error", message: String(error) });
        });
    },
    [
      client,
      sessionId,
      history,
      sessions,
      workdir,
      state.messages,
      lastSession,
      resumeMemory,
      resumeSession,
      openResumePicker,
      state.turnActive,
      update.phase,
      attachments,
      capabilities,
      localAudio,
      recordingPath,
      audioOffered,
      openModelPicker,
      showToast,
      takePaste,
      toggleRecording,
    ],
  );

  /** Answer the y/n banner prompt: start the upgrade, or put the banner away. */
  const answerUpdate = useCallback(
    (accepted: boolean) => {
      if (!accepted) {
        setUpdate(cancelUpdate);
        return;
      }
      if (state.turnActive) {
        setUpdate((current) => updateProgress(current, "failed", "finish or interrupt the current turn before updating"));
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
    [client, state.turnActive],
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
    // A prompt on screen owns every other key: mode cycling, Ctrl+O and the
    // rest would otherwise fire underneath the question being asked.
    if (state.pendingApproval || update.phase === "confirm" || modelPicker) {
      return;
    }

    if (showHelp) {
      if (key.escape || key.return || input === "q") {
        setShowHelp(false);
        setFocus(INPUT_FOCUS);
      }
      return;
    }

    if (resumeChoices || modePicker) return;

    // Esc first stops a reply that is being read out; only then does it mean
    // whatever else Esc means here.
    if (key.escape && voice.speaking) {
      silence();
      return;
    }

    // An open agent transcript takes the arrows for scrolling and Esc to leave.
    if (openAgent) {
      if (key.escape) {
        closeAgent();
        return;
      }
      if (key.upArrow) {
        setAgentScroll((offset) => offset + 1);
        return;
      }
      if (key.downArrow) {
        setAgentScroll((offset) => Math.max(0, offset - 1));
        return;
      }
      if (key.pageUp) {
        setAgentScroll((offset) => offset + AGENT_TRANSCRIPT_ROWS);
        return;
      }
      if (key.pageDown) {
        setAgentScroll((offset) => Math.max(0, offset - AGENT_TRANSCRIPT_ROWS));
        return;
      }
    }

    // The cursor has walked out of the input and into the rows under it.
    if (!isInput(focus)) {
      if (key.escape) {
        setFocus(INPUT_FOCUS);
        return;
      }
      if (key.upArrow) {
        setFocus((current) => focusUp(current));
        return;
      }
      if (key.downArrow) {
        setFocus((current) => focusDown(current, agentRows.length));
        return;
      }
      if (key.return) {
        if (focus.zone === "footer") setModePicker(true);
        else if (focus.zone === "agent") openAgentRow(focus.index);
        return;
      }
      if (!openAgent && input.length > 0 && !key.ctrl && !key.meta && !key.tab) {
        setFocus(INPUT_FOCUS);
        setAppend(input);
        return;
      }
      return;
    }

    // Ctrl+A opens the agent panel out; Ctrl+R hands the keyboard to the
    // unattended approval backlog.
    if (key.ctrl && input === "a") {
      setAgentsExpanded((open) => !open);
      return;
    }
    if (key.ctrl && input === "r") {
      setQueueFocused((focused) => !focused && state.approvalQueue.length > 0);
      return;
    }
    // Ctrl+O opens the newest thing worth opening: a tool call's output, or a
    // diff that was cut short.
    if (key.ctrl && input === "o") {
      const newest = [...state.timeline]
        .reverse()
        .find((item) => item.kind === "tool" || item.kind === "diff");
      setExpandedId((current: string | null) => (current ? null : (newest?.id ?? null)));
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
    // Ctrl+P: cheap on/off toggle for plan mode.
    if (key.ctrl && input === "p") {
      changeMode(state.mode === "plan" ? "accept" : "plan");
    }
  });

  /** Open a delegate's conversation in place of the live area. */
  const openAgentRow = useCallback(
    (index: number) => {
      const row = agentRows[index];
      if (!row) return;
      if (row.key === "current") {
        setOpenAgent(null);
        return;
      }
      if (row.key === "overflow" || row.key === "idle-more") {
        setAgentsExpanded(true);
        return;
      }
      const agentId = row.key.startsWith("agent-") ? row.key.slice("agent-".length) : null;
      const entry = state.subagents.find((agent) => agent.agentId === agentId);
      if (!entry?.sessionId) {
        showToast(`${row.name}: no transcript yet`);
        return;
      }
      setOpenAgent({ sessionId: entry.sessionId, name: entry.name || row.name });
      setAgentScroll(0);
      // The child may have run entirely before this surface asked for it.
      if (!state.children[entry.sessionId]) resumeSession(entry.sessionId, "child");
    },
    [agentRows, state.subagents, state.children, resumeSession, showToast],
  );

  const closeAgent = useCallback(() => {
    setOpenAgent(null);
    setFocus(INPUT_FOCUS);
  }, []);

  const approvalActive = state.pendingApproval !== null;

  /** Input block: the working line, the error row, the backlog, and the input. */
  const bottomNode = (
    <>
      <WorkingIndicator line={indicatorText} />

      {delegation ? (
        <Text color={delegation.known ? "magenta" : "yellow"} wrap="truncate-end">
          {`[${delegationLabel(delegation)}]`}
        </Text>
      ) : null}

      <QueuedPrompts queued={state.queued} width={contentWidth} />

      <AttachmentChips attachments={attachments} width={contentWidth} />

      {modeToast && modeToast.length > TOAST_INLINE_MAX ? (
        <Text color="cyan" wrap="truncate-end">
          {modeToast}
        </Text>
      ) : null}

      {modelPicker ? (
        <ModelPicker
          options={modelPicker}
          width={contentWidth}
          isActive={state.pendingApproval === null}
          onCancel={() => setModelPicker(null)}
          onChoose={(option) => {
            setModelPicker(null);
            chooseModel(option.ref);
          }}
        />
      ) : null}

      {update.phase === "confirm" ? (
        <ConfirmMenu<boolean>
          options={UPDATE_OPTIONS}
          initialIndex={1}
          escapeValue={false}
          onChoose={answerUpdate}
          isActive={state.pendingApproval === null}
        />
      ) : null}

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
      ) : modePicker ? (
        <ConfirmMenu<Mode | null>
          options={(["accept", "auto", "plan"] as Mode[]).map((value) => ({ label: `${value} mode`, value }))}
          initialIndex={Math.max(0, (["accept", "auto", "plan"] as Mode[]).indexOf(state.mode))}
          escapeValue={null}
          onChoose={(value) => {
            setModePicker(false);
            setFocus(INPUT_FOCUS);
            if (value) changeMode(value);
          }}
        />
      ) : resumeChoices ? (
        <ConfirmMenu<string | null>
          options={[
            ...resumeChoices.map((entry) => ({
              label: `${entry.sessionId} · ${new Date(entry.at).toLocaleString()} · ${
                entry.firstPrompt
                  ? entry.firstPrompt.length > 48
                    ? `${entry.firstPrompt.slice(0, 47)}…`
                    : entry.firstPrompt
                  : "(no prompt)"
              }`,
              value: entry.sessionId,
            })),
            { label: "Cancel", value: null },
          ]}
          escapeValue={null}
          onChoose={(target) => {
            setResumeChoices(null);
            if (target) resumeSession(target, "main");
          }}
        />
      ) : (
        <Chat
          onSubmit={submit}
          initialHistory={pastPrompts}
          onFocusDown={() => setFocus((current) => focusDown(current, agentRows.length))}
          onQuickUpdate={update.phase === "available" ? () => setUpdate(confirmUpdate) : undefined}
          onQuickResume={
            lastSession && state.messages.length === 0 ? resumeMemory : undefined
          }
          onPaste={takePaste}
          onClipboard={takeClipboard}
          onBackspaceEmpty={() => {
            if (attachments.length === 0) return false;
            setAttachments(removeLast);
            return true;
          }}
          onClearAttachments={() => setAttachments([])}
          onToggleRecording={toggleRecording}
          insert={insert}
          onInserted={() => setInsert(null)}
          append={append}
          onAppended={() => setAppend(null)}
          completions={completions}
          onChange={(next) => {
            setDraft(next);
            if (next.length > 0 && state.errors.length > 0) dispatch({ type: "errors/clear" });
          }}
          onInterrupt={() => void client.interrupt(sessionId).catch(() => undefined)}
          disabled={showHelp || update.phase === "confirm" || update.phase === "running" || update.phase === "done" || approvalActive || queueFocused || !isInput(focus) || openAgent !== null}
        />
      )}
    </>
  );

  // The bottom block, in the order the eye reads it: what the session is
  // (HUD), what is wrong (the context warning), what it is doing (the summary
  // line), and who is doing it (the agent panel).
  const warning = contextWarning(state.context);
  const summary = summaryLine({
    mode: state.mode,
    shells: state.toolCalls.filter(
      (call) => call.state === "running" && toolKind(call.name) === "shell",
    ).length,
    agents: state.subagents.filter((agent) => agent.status === "running").length,
  });
  const statusNode = (
    <>
      <SectionRule width={contentWidth} color="green" />
      <Text
        color={summary.color}
        dimColor={summary.dimColor && focus.zone !== "footer"}
        inverse={focus.zone === "footer"}
        wrap="truncate-end"
      >
        {`${summary.text}${focus.zone === "footer" ? " · Enter to choose mode" : ""}`}
      </Text>
      {shellsOpen ? <ShellList calls={state.toolCalls} now={now} width={contentWidth} /> : null}
      <SectionRule width={contentWidth} />
      <StatusHud rows={hudRows} width={contentWidth} />
      {warning ? (
        <Text color={warning.color} bold={warning.bold} wrap="truncate-end">
          {warning.text}
        </Text>
      ) : null}
      <SectionRule width={contentWidth} label={activeTeam ? `Team: ${activeTeam}` : undefined} />
      <AgentPanel
        rows={agentRows}
        width={contentWidth}
        focusedIndex={focus.zone === "agent" ? focus.index : null}
      />
    </>
  );


  const helpNode = showHelp ? (
    <HelpPanel
      commands={state.commands}
      width={contentWidth}
      height={fullscreen ? layout.transcriptRows : Math.max(4, usableRows(terminal.rows) - layout.bottomRows - layout.statusRows - (bannerText(update) ? 3 : 0))}
      isActive={state.pendingApproval === null && update.phase !== "confirm"}
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
    <Box flexDirection="column">
      <Static items={staticItems}>
        {(entry) => (
          <Box
            key={entry.key}
            flexDirection="column"
            // The launch wordmark runs edge to edge; everything else keeps the
            // one-column gutter the rest of the session is laid out on.
            paddingX={entry.kind === "launch" ? 0 : 1}
          >
            {entry.kind === "launch" ? (
              <LaunchBanner
                width={terminal.columns}
                terminalRows={terminal.rows}
                version={version}
                workdir={workdir}
                mode={mode}
                provider={state.provider}
                model={state.model}
                lastSession={lastSession}
              />
            ) : entry.kind === "tools" ? (
              <ToolSummary calls={entry.calls} />
            ) : entry.kind === "note" ? (
              <Box marginBottom={1}>
                <Text color={entry.ok ? "green" : "red"} dimColor>
                  {entry.text}
                </Text>
              </Box>
            ) : (
              <TimelineEntry
                state={state}
                item={entry.item}
                expandedId={expandedId}
                width={contentWidth}
              />
            )}
          </Box>
        )}
      </Static>

      <Box flexDirection="column" paddingX={1}>
      {showHelp ? null : openAgent ? (
        <AgentTranscript
          name={openAgent.name}
          task={openAgentEntry?.task ?? ""}
          status={openAgentEntry ? agentStatusText(openAgentEntry, now) : ""}
          lines={agentViewport.lines}
          height={AGENT_TRANSCRIPT_ROWS}
          width={contentWidth}
          scrollIndicator={scrollIndicator(agentViewport)}
          empty={agentLines.length === 0}
        />
      ) : (
        live.map((item) => (
          <TimelineEntry
            key={`${item.kind}-${item.id}`}
            state={state}
            item={item}
            expandedId={expandedId}
            width={contentWidth}
          />
        ))
      )}

      <UpdateBanner update={update} />

      {helpNode}

      {bottomNode}

      {statusNode}
      </Box>
    </Box>
  );
}
