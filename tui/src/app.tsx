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
import { Box, Static, Text, useApp, useInput, useStdin, useStdout } from "ink";

import type { TuiClient } from "./rpc/client.js";
import type {
  ApprovalDecision,
  ApprovalRequestParams,
  ApprovalResponse,
  ApprovalScope,
  QuestionRequestParams,
  QuestionResponse,
  CommandInfo,
  Mode,
  SessionEvent,
} from "./rpc/sdk.js";
import { defaultResolveEndpoint } from "./rpc/sdk.js";
import { SlashRegistry, ttsSubCommands, voiceSubCommands } from "./slash/registry.js";
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
import { derivePhase, queuedLabel, turnSummaryLine, workingLine, estimateTokens } from "./state/working.js";
import { cycleMode } from "./state/mode.js";
import { useTerminalSize } from "./hooks/useTerminalSize.js";
import { useDaemonInfo } from "./hooks/useDaemonInfo.js";
import { useElapsed } from "./hooks/useElapsed.js";
import { useClock } from "./hooks/useClock.js";
import {
  createChildEventBuffer,
  createLiveEventThrottle,
  type ChildEventBuffer,
} from "./state/coalesce.js";
import { useSpinner } from "./hooks/useSpinner.js";
import { useKnownAgents } from "./hooks/useKnownAgents.js";
import { clampFocus, focusDown, focusUp, isInput, INPUT_FOCUS, type Focus } from "./state/focus.js";
import { mouseReports, panelRowAt } from "./input/mouse.js";
import { offerSession, resumeLabel, resumeRows } from "./state/history.js";
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
import {
  EFFORT_REF,
  INHERIT_REF,
  modelOptions,
  modelSource,
  nextEffort,
  type ModelOption,
} from "./state/models.js";
import { lspTable, readLspStatus } from "./state/lsp.js";
import { delegationHint, delegationLabel } from "./state/delegation.js";
import { agentCandidates } from "./state/agent-completion.js";
import { layout as editorLayout } from "./state/editor.js";
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
import { entryRows, settledCount } from "./layout/statics.js";
import {
  commitStreamingPrefixes,
  messageFullyCommitted,
  type StreamChunkEntry,
} from "./layout/stream-scrollback.js";
import { RenderedLines } from "./components/RenderedLines.js";
import { groupCalls, toolKind } from "./layout/summary.js";
import { agentStatusText, buildAgentRows } from "./layout/agents.js";
import { compactionDivider, contextWarning, summaryLine } from "./layout/bottom.js";
import { FullscreenLayout } from "./components/FullscreenLayout.js";
import { Chat } from "./components/Chat.js";
import { MessageView } from "./components/MessageStream.js";
import { ToolCall } from "./components/ToolCall.js";
import { DiffView } from "./components/DiffView.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import {
  QuestionPrompt,
  questionPromptRows,
  type QuestionAnswerItem,
} from "./components/QuestionPrompt.js";
import { ApprovalQueue } from "./components/ApprovalQueue.js";
import { ConfirmMenu, type ConfirmOption } from "./components/ConfirmMenu.js";
import { StatusHud } from "./components/StatusHud.js";
import { AgentPanel } from "./components/AgentPanel.js";
import { AttachmentChips } from "./components/AttachmentChips.js";
import { ModelPicker } from "./components/ModelPicker.js";
import { SkillCreateForm } from "./components/SkillCreateForm.js";
import {
  createdSkillName,
  isBareSkillCreate,
  parseSkillEdit,
  skillCreateCommand,
  skillSubCommands,
} from "./state/skill-completion.js";
import { SKILL_HINT_TEXT, shouldSuggestSkill } from "./state/skill-hint.js";
import { McpAddForm, type McpSubmission } from "./components/McpAddForm.js";
import { McpCatalogPicker } from "./components/McpCatalogPicker.js";
import { ToolChecklist } from "./components/ToolChecklist.js";
import {
  draftFromCatalog,
  emptyDraft,
  isBareMcpAdd,
  isBareMcpCatalog,
  mcpSubCommands,
  parseMcpConfigure,
  readMcpCatalog,
  readMcpList,
  readMcpProbe,
  type McpCatalogEntry,
  type McpDraft,
  type McpProbe,
  type McpServerRow,
  type McpTool,
} from "./state/mcp.js";
import type { EditorRunner } from "./util/editor.js";
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
const ENABLE_MOUSE = "\u001b[?1000h\u001b[?1006h";
const DISABLE_MOUSE = "\u001b[?1000l\u001b[?1006l";

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

/** How often `lsp.status` is re-read; server states change without an event. */
export const LSP_POLL_MS = 30_000;

/** The tallest the open agent transcript is ever drawn; see `agentTranscriptRows`. */
export const AGENT_TRANSCRIPT_ROWS = 12;

/**
 * The transcript's own chrome: the rounded border, the name row, the key hint.
 *
 * Counted so the window can be shrunk to whatever the terminal has left. In
 * the inline layout the live region is drawn *below* the scrollback, and Ink
 * clears the whole screen and rewrites every static block it has ever written
 * as soon as that region is as tall as the terminal (see `RESERVED_FRAME_ROW`)
 * — with a delegate streaming behind it, that clear lands tens of times a
 * second and the terminal does nothing but flicker.
 */
export const AGENT_TRANSCRIPT_CHROME_ROWS = 4;

/** However cramped the terminal is, a window this short is still readable. */
export const MIN_AGENT_TRANSCRIPT_ROWS = 3;

/**
 * Rows a message being streamed keeps even on a terminal with no room.
 *
 * Below this the window says nothing useful; a terminal this small is going to
 * scroll whatever happens, and the whole message reaches the scrollback intact
 * as soon as it is finished.
 */
export const MIN_LIVE_MESSAGE_ROWS = 4;

/**
 * Rows the inline frame holds back beyond the layout's own reservations.
 *
 * `reserveBottomRows` and the status block describe what they *need*; what they
 * actually draw is a little more — the rule lines, the update banner's box, the
 * paddings between blocks. The difference only matters here, because this is
 * the one budget that has to be right in the pessimistic direction: one row
 * over and Ink stops updating the frame in place and starts clearing the screen
 * and re-emitting the entire scrollback instead, which is the flicker the user
 * sees as old text flashing at the top before the view drops back down.
 *
 * This number is empirical, not derived, and that is the honest description of
 * it: every attempt to enumerate the rows those blocks really draw has missed
 * some — a prompt that wrapped to two rows, the blank separators between
 * entries, the queued-prompt preview that exists only while a prompt waits.
 * Four was enough for a 30-row terminal with nothing queued and a short panel,
 * and still put a 120x35 pane exactly on `rows`, where Ink clears. Eight leaves
 * real headroom across every case that has been measured, and what it costs is
 * rows of the streaming window — the cheapest thing here to give up, since that
 * text is one `message.done` away from being in the scrollback in full.
 */
export const INLINE_CHROME_SLACK = 8;

/**
 * The slack the hold budget carries — smaller, because a wrong guess there
 * costs a summary line rather than a screen clear (see `holdRegionRows`).
 */
export const HOLD_CHROME_SLACK = 4;

/** The blank row `MessageView` draws under itself, which the cap must pay for. */
export const MESSAGE_MARGIN_ROWS = 1;

/** Rows to give the open agent transcript, inline layout included. */
export function agentTranscriptRows({
  fullscreen,
  usable,
  statusRows,
  bottomRows,
}: {
  fullscreen: boolean;
  /** Rows the frame may use — `usableRows(terminal.rows)`. */
  usable: number;
  statusRows: number;
  bottomRows: number;
}): number {
  // Full-screen owns the alternate buffer and keeps its own fixed height, so
  // the window there is a constant and the layout absorbs the rest.
  if (fullscreen) return AGENT_TRANSCRIPT_ROWS;
  const room = usable - statusRows - bottomRows - AGENT_TRANSCRIPT_CHROME_ROWS;
  return Math.max(MIN_AGENT_TRANSCRIPT_ROWS, Math.min(AGENT_TRANSCRIPT_ROWS, room));
}

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
  /** Runs `$EDITOR` for `/skill edit`; absent in tests and in a pipe. */
  editor?: EditorRunner;
  /** Starts the daemon when gone; injected for tests or defaults to launcher path. */
  startDaemon?: () => Promise<void> | void;
}

/** One transcript entry — a message, a tool call, a diff or a compaction. */
function TimelineEntry({
  state,
  item,
  expandedId,
  width,
  maxMessageRows,
  messageStartLine = 0,
}: {
  state: State;
  item: TimelineItem;
  /** The entry Ctrl+O opened, if it is this one. */
  expandedId: string | null;
  width: number;
  /**
   * Rows a message may occupy. Passed only by the inline live region, where an
   * entry still being written has to stay inside the terminal; `<Static>` draws
   * the whole thing, so the scrollback is always complete.
   */
  maxMessageRows?: number;
  /** Wrapped rows already frozen into scrollback for this message. */
  messageStartLine?: number;
}): React.ReactElement | null {
  if (item.kind === "message") {
    const message = state.messages.find((m) => m.id === item.id);
    return message ? (
      <MessageView
        message={message}
        width={width}
        maxRows={maxMessageRows}
        startLine={messageStartLine}
      />
    ) : null;
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
  return diff ? (
    <DiffView
      diff={diff}
      expanded={expandedId === item.id}
      diagnostics={state.diagnostics[diff.path]}
    />
  ) : null;
}

/**
 * What `<Static>` has been given, in order.
 *
 * The logo is the first item, so it is printed once at the top of the session
 * and then scrolls away like any other output.
 */
type StaticEntry =
  | { key: "launch"; kind: "launch" }
  | { key: string; kind: "entry"; item: TimelineItem; messageStartLine?: number }
  /** Prefix lines of a message still streaming, frozen into scrollback. */
  | StreamChunkEntry
  /** A run of successful tool calls, folded into one line. */
  | { key: string; kind: "tools"; calls: ToolCallEntry[] }
  /** The `✓ Done in 12s` line a finished turn leaves behind. */
  | { key: string; kind: "note"; text: string; ok: boolean };

/** Tool lines stack tightly; whatever follows a run of them gets one blank line above. */
function followsTools(entries: StaticEntry[], index: number): boolean {
  const previous = entries[index - 1];
  if (!previous) return false;
  if (previous.kind === "tools") return true;
  return previous.kind === "entry" && previous.item.kind === "tool";
}

/**
 * Turn a slice of settled timeline entries into what the scrollback shows.
 *
 * Consecutive successful tool calls fold into one summary line; a failed call
 * breaks the run and keeps its own card, so a failure is never summarised away.
 */
function releaseEntries(
  state: State,
  items: TimelineItem[],
  width: number,
  streamCommitted: ReadonlyMap<string, number>,
): StaticEntry[] {
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
    if (item.kind === "message") {
      const message = state.messages.find((entry) => entry.id === item.id);
      if (message && messageFullyCommitted(message, width, streamCommitted)) {
        continue;
      }
      const startLine = streamCommitted.get(item.id) ?? 0;
      out.push({
        key: `${item.kind}-${item.id}`,
        kind: "entry",
        item,
        messageStartLine: startLine > 0 ? startLine : undefined,
      });
      continue;
    }
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
  editor,
  startDaemon,
}: AppProps): React.ReactElement {
  const { exit } = useApp();
  const [sessionId, setSessionId] = useState(initialSessionId);
  const activeSessionRef = useRef(initialSessionId);
  const resumingRef = useRef(false);
  const [state, dispatch] = useReducer(reducer, initialState);
  const [reconnectAttempt, setReconnectAttempt] = useState<number | null>(null);
  const [daemonGone, setDaemonGone] = useState(false);
  const disconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleStartDaemon = useCallback(async () => {
    if (startDaemon) {
      await startDaemon();
      return;
    }
    try {
      const { spawn } = await import("node:child_process");
      const cmd = process.env.SNOWPEA_DAEMON_CMD;
      if (cmd) {
        const parts = cmd.split(" ");
        const child = spawn(parts[0], parts.slice(1), { detached: true, stdio: "ignore" });
        child.unref();
      } else {
        const child = spawn("snowpea", ["daemon", "start"], { detached: true, stdio: "ignore" });
        child.on("error", () => {
          const fallback = spawn("python3", ["-m", "snowpea_core", "--port", "0"], { detached: true, stdio: "ignore" });
          fallback.unref();
        });
        child.unref();
      }
    } catch {
      // ignore
    }
  }, [startDaemon]);

  const [showHelp, setShowHelp] = useState(false);
  const { stdin, setRawMode } = useStdin();
  const { stdout } = useStdout();
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
  /** `agents.models`: which model profile each agent is assigned. */
  const [agentModels, setAgentModels] = useState<Record<string, string>>({});
  /** Options for the `/model` picker, or null while it is closed. */
  const [modelPicker, setModelPicker] = useState<ModelOption[] | null>(null);
  /** `/skill create` with no arguments: the three questions are up. */
  const [skillForm, setSkillForm] = useState(false);
  /** `/mcp add` with no arguments: the draft the form is collecting, or null. */
  const [mcpForm, setMcpForm] = useState<McpDraft | null>(null);
  /** `/mcp catalog`: the presets, as a list to pick from. */
  const [mcpCatalog, setMcpCatalog] = useState<McpCatalogEntry[] | null>(null);
  /** `/mcp configure <name>`: which of one server's tools to register. */
  const [mcpConfigure, setMcpConfigure] = useState<
    { name: string; scope: "project" | "global"; tools: McpTool[]; selected: string[] } | null
  >(null);
  /** Servers already reported broken, so one failure is said once. */
  const mcpErrorsRef = useRef<Set<string>>(new Set());
  /** The skill the daemon just wrote, offered as "run /<name>". */
  const [skillChip, setSkillChip] = useState<string | null>(null);
  /** The "turn this into a skill" nudge, once per session. */
  const [skillHint, setSkillHint] = useState(false);
  const skillHintSpent = useRef(false);
  /** What the turn in flight had spent before it started, for that nudge. */
  const hintTurnRef = useRef<{ startedAt: number; tools: number; errors: number } | null>(null);
  /** The newest reply already read for a "Run it with /x". */
  const skillReplyRef = useRef<string | null>(null);
  /** True while `$EDITOR` owns the terminal and the TUI draws nothing. */
  const [suspended, setSuspended] = useState(false);
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
  /** Wrapped rows already committed to scrollback per open-stream message id. */
  const streamCommittedRef = useRef<Map<string, number>>(new Map());
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
  /** Frame-rate limiter for the delegates' token streams; see state/coalesce.ts. */
  const childEventsRef = useRef<ChildEventBuffer<SessionEvent> | null>(null);
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
  /** Resolver for the `ask_user` question the daemon is blocked on. */
  const questionResolver = useRef<((response: QuestionResponse) => void) | null>(null);

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


  /**
   * What the language servers are doing.
   *
   * Read on connect, whenever a server publishes diagnostics — that is the
   * moment one has just become useful — and on a slow timer for the states no
   * event announces, like a server going broken on its second crash.
   */
  const refreshLsp = useCallback(() => {
    void client
      .call("lsp.status", {})
      .then((result) => dispatch({ type: "lsp/status", servers: readLspStatus(result) }))
      .catch(() => {
        // A daemon without the feature; the segment simply stays hidden.
        dispatch({ type: "lsp/status", servers: [] });
      });
  }, [client]);

  /**
   * Which MCP servers are configured, and how many of them answered.
   *
   * Read once on connect for the HUD's `mcp 2/3`, and again whenever an
   * `mcp.changed` says something moved — the event carries the state, but only
   * the list carries the tools a picker needs.
   */
  const refreshMcp = useCallback(() => {
    void client
      .call("mcp.list", { sessionId, workdir })
      .then((result) => dispatch({ type: "mcp/list", servers: readMcpList(result) }))
      .catch(() => {
        // A daemon without M14; the segment simply stays hidden.
        dispatch({ type: "mcp/list", servers: [] });
      });
  }, [client, sessionId, workdir]);

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

    // A delegate's tokens arrive as fast as its endpoint produces them, and
    // each one would otherwise be a dispatch and a repaint; they are batched
    // into frames instead. Everything that is not a delta still goes straight
    // through, behind whatever text it followed.
    const childEvents = createChildEventBuffer<SessionEvent>((childSession, event) =>
      dispatch({ type: "child/event", sessionId: childSession, event }),
    );
    childEventsRef.current = childEvents;

    // The same argument for this session's counters: a long thinking block
    // emits one `message.reasoning` per delta and the provider reports usage
    // as it goes, and all either of them moves is a number.
    const liveEvents = createLiveEventThrottle<SessionEvent>((event) =>
      dispatch({ type: "session/event", event }),
    );

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
          childEvents.push(event.sessionId, event);
          return;
        }
        liveEvents.push(event);
      },
      onStatus: (status) => {
        dispatch({ type: "status", status });
        if (status === "reconnecting") {
          if (!defaultResolveEndpoint()) {
            setDaemonGone(true);
          }
          if (!disconnectTimer.current) {
            disconnectTimer.current = setTimeout(() => {
              setDaemonGone(true);
            }, 20_000);
          }
        } else if (status === "connected") {
          setDaemonGone(false);
          setReconnectAttempt(null);
          if (disconnectTimer.current) {
            clearTimeout(disconnectTimer.current);
            disconnectTimer.current = null;
          }
        } else if (status === "closed") {
          setDaemonGone(true);
        }
      },
      onReconnecting: ({ attempt }) => {
        setReconnectAttempt(attempt);
        if (!defaultResolveEndpoint()) {
          setDaemonGone(true);
        }
        if (!disconnectTimer.current) {
          disconnectTimer.current = setTimeout(() => {
            setDaemonGone(true);
          }, 20_000);
        }
      },
      onReconnected: () => {
        setReconnectAttempt(null);
        setDaemonGone(false);
        if (disconnectTimer.current) {
          clearTimeout(disconnectTimer.current);
          disconnectTimer.current = null;
        }
        showToast("daemon restarted — session resumed");
        dispatch({ type: "session/reconnected" });
      },
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
      // An MCP server moved: the HUD follows the payload at once, and the full
      // list is re-read for the tools it carries. A server that broke is said
      // once — a flapping server must not fill the transcript with the same
      // line, so the name is remembered until it recovers.
      onMcpChanged: (params) => {
        dispatch({ type: "mcp/changed", payload: params });
        const name = params?.name ?? "";
        if (params?.state === "error") {
          if (name && !mcpErrorsRef.current.has(name)) {
            mcpErrorsRef.current.add(name);
            dispatch({
              type: "note",
              text: `mcp: ${name} is not running — ${params.error ?? "no reason given"}`,
            });
          }
        } else if (name) {
          mcpErrorsRef.current.delete(name);
        }
        refreshMcp();
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

    refreshLsp();
    refreshMcp();

    // Which model each agent is assigned, for the completion list's tag.
    void client
      .call("settings.get", { scope: "global" })
      .then((result) => {
        const assigned = ((result?.settings as any)?.agents?.models ?? {}) as Record<string, string>;
        setAgentModels(assigned);
      })
      .catch(() => {
        /* advisory: the rows simply carry no model tag. */
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

    client.onApprovalRequest(
      (request: ApprovalRequestParams) =>
        new Promise<ApprovalResponse>((resolve) => {
          approvalResolver.current = resolve;
          dispatch({ type: "approval/request", request });
        }),
    );

    client.onQuestionRequest(
      (request: QuestionRequestParams) =>
        new Promise<QuestionResponse>((resolve) => {
          questionResolver.current = resolve;
          dispatch({ type: "question/request", request });
        }),
    );

    void registryRef.current
      .load()
      .then((commands: CommandInfo[]) => dispatch({ type: "commands", commands }))
      .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));

    refreshApprovals();

    return () => {
      childEvents.dispose();
      liveEvents.dispose();
      if (childEventsRef.current === childEvents) childEventsRef.current = null;
      if (disconnectTimer.current) {
        clearTimeout(disconnectTimer.current);
        disconnectTimer.current = null;
      }
    };
  }, [
    client,
    sessionId,
    mode,
    provider,
    model,
    refreshApprovals,
    refreshCapabilities,
    refreshLsp,
    refreshMcp,
  ]);

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
      if (state.pendingQuestion || state.pendingApproval) return;
      if (update.phase === "confirm" || update.phase === "running") return;
      if (["\u001bOP", "\u001b[11~", "\u001b[[A"].includes(data.toString())) {
        setShowHelp(current => !current);
        setFocus(INPUT_FOCUS);
      }
    };
    stdin.on("data", onData);
    return () => { stdin.off("data", onData); };
  }, [stdin, state.pendingApproval, state.pendingQuestion, update.phase]);

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

  // Servers start, become ready and break without an event of their own.
  useEffect(() => {
    const timer = setInterval(refreshLsp, LSP_POLL_MS);
    return () => clearInterval(timer);
  }, [refreshLsp]);

  // Diagnostics mean a server just did something; its state may have changed
  // with it.
  const diagnosticsVersion = Object.keys(state.diagnostics).length;
  useEffect(() => {
    if (diagnosticsVersion > 0) refreshLsp();
  }, [diagnosticsVersion, refreshLsp]);

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

  const setMouseMode = useCallback(
    (enabled: boolean) => {
      if (!stdout?.isTTY) return;
      stdout.write(enabled ? ENABLE_MOUSE : DISABLE_MOUSE);
    },
    [stdout],
  );

  // Off by default: a terminal cannot scope mouse reporting to one region,
  // so while it is on the terminal's own drag-to-select stops working
  // everywhere. `/mouse` turns it on for people who want to click the ◯ rows.
  const [mouseOn, setMouseOn] = useState(false);
  useEffect(() => {
    setMouseMode(mouseOn);
    return () => setMouseMode(false);
  }, [setMouseMode, mouseOn]);

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
            streamCommittedRef.current = new Map();
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
          // The backlog is applied in one pass, not one event at a time. A
          // resume answers with everything the session ever emitted, so a long
          // one is thousands of events; dispatched individually each is a
          // render, and each finished entry reaches `<Static>` on a frame of
          // its own, which is the history visibly re-typing itself. Folded, the
          // whole reconstructed transcript lands in a single render. The end of
          // the array is the end of the replay — the daemon needs to say
          // nothing extra.
          const events = Array.isArray(result?.events) ? result.events : [];
          if (events.length > 0) {
            if (into === "child") dispatch({ type: "child/replay", sessionId: target, events });
            else dispatch({ type: "session/replay", events });
          }
          // The daemon restored the session's own mode; the HUD must say the
          // one the next turn will actually run under, not the launch flag.
          if (into === "main" && typeof result?.mode === "string") {
            dispatch({ type: "mode", mode: result.mode as Mode });
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

  const completions = useMemo(() => {
    if (!draft.startsWith("/")) return [];
    // `/skill` is one command with five jobs, and only its name reaches the
    // command table; the jobs are offered here so they can be completed too.
    const skills = state.commands.some((command) => command.name === "skill")
      ? skillSubCommands(draft)
      : [];
    // `/mcp` likewise: the daemon knows the command, this surface knows the
    // sub-actions, the server names and the catalog ids they take.
    const mcp = state.commands.some((command) => command.name === "mcp")
      ? mcpSubCommands(draft, state.mcp, mcpCatalog ?? [])
      : [];
    return [
      ...skills,
      ...mcp,
      ...ttsSubCommands(draft),
      ...voiceSubCommands(draft),
      ...registryRef.current.complete(draft),
    ];
  }, [draft, state.commands, state.mcp, mcpCatalog]);

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
        reconnectAttempt,
        daemonGone,
        version,
        latestVersion: updateAvailable ? update.latest : null,
        workdir,
        sessionId: state.sessionId,
        provider: state.provider,
        model: state.model,
        modelSource: state.modelSource ?? sessionModelSource,
        effort: state.effort,
        mode: state.mode,
        usage: state.usage,
        context: state.context,
        toolCount: state.toolCount,
        lsp: state.lsp,
        mcp: state.mcp,
        speaking: voice.tts,
        voiceInput: voice.input,
        recording: voice.recording,
        sttEngine: capabilities?.sttProvider ?? null,
        ttsEngine: capabilities?.ttsProvider ?? null,
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
      reconnectAttempt,
      daemonGone,
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
      state.lsp,
      state.mcp,
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

  // The turn's own clock, read once per render. The scrollback bookkeeping
  // below measures the turn from it; it is deliberately not the 1 Hz `clock`,
  // which exists to keep displays from rebuilding on every frame.
  const now = Date.now();

  // --- who is working for this session ---------------------------------------
  // Everything on screen that counts time shows whole seconds, so it reads one
  // clock that moves once a second rather than `Date.now()` per render; see
  // hooks/useClock.ts for why that matters to the repaint rate.
  // Only while something is being timed: an idle session must not repaint at
  // all, which is the same rule the spinner's timer follows.
  const clock = useClock(state.turnActive || voice.recording);
  const knownAgents = useKnownAgents(client, undefined, agentRosterVersion);
  const teamRow =
    knownAgents.find((agent) => agent.kind === "team" && agent.active) ??
    knownAgents.find((agent) => agent.kind === "team");
  const activeTeam = teamRow?.name;
  const agentRows = useMemo(
    () =>
      buildAgentRows({
        state,
        known: knownAgents.filter((agent) => agent.kind !== "team"),
        roster: teamRow?.agents,
        now: clock,
        expanded: agentsExpanded,
        currentLabel: "main",
      }),
    // `clock`, not `Date.now()`: the elapsed columns move once a second, so the
    // rows are rebuilt once a second. Reading the clock on every render would
    // hand `AgentPanel` a new array on each of the spinner's five frames a
    // second and on every token a delegate streams.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.subagents, state.teamTasks, knownAgents, teamRow, agentsExpanded, clock],
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

  // --- the working indicator -------------------------------------------------
  const phase = derivePhase(state, { runningCommand });
  const spinnerFrame = useSpinner(
    voice.recording || (phase.kind !== "idle" && phase.kind !== "approval"),
  );
  const turn = turnRef.current;
  // The daemon stamps the moment the turn actually began; the local stamp is
  // the fallback for a daemon that does not send `turn.started`.
  const turnStartedAt = state.turnStartedAt ?? turn?.startedAt ?? null;
  const queuedSuffix = state.queued.length > 0 ? ` · ${queuedLabel(state.queued.length)}` : "";
  const workingText = voice.recording
    ? recordingLabel(voice.startedAt, clock)
    : voice.speaking
      ? SPEAKING_LABEL
      : workingLine({
        phase,
        elapsedMs: turnStartedAt === null ? 0 : clock - turnStartedAt,
        inputTokens: turn ? state.usage.inputTokens - turn.inputTokens : 0,
        // The daemon reports usage once per model round; between reports the
        // streamed answer and thinking stand in, so a long round still moves.
        outputTokens:
          (turn ? state.usage.outputTokens - turn.outputTokens : 0) +
          estimateTokens(state.streamedChars + state.reasoningChars),
        frame: spinnerFrame,
        verbOffset: state.messages.length,
        waited: state.turnWaited,
      });
  const indicatorText = workingText === null ? null : `${workingText}${queuedSuffix}`;

  /** Everything `/delegate` and `$` can complete to. */
  const completableAgents = useMemo(
    () => agentCandidates({ known: knownAgents, teamTasks: state.teamTasks, models: agentModels }),
    [knownAgents, state.teamTasks, agentModels],
  );

  // `$agent …` in the draft: say who it is about to go to.
  const delegation = useMemo(
    () => delegationHint(draft, knownAgents.map((agent) => agent.name)),
    [draft, knownAgents],
  );
  const draftRows = useMemo(
    () => editorLayout(draft, Math.max(1, contentWidth - 2), draft.length).lines.length,
    [draft, contentWidth],
  );

  const layout = computeLayout({
    // One row short of the terminal on purpose; see `RESERVED_FRAME_ROW`.
    rows: usableRows(terminal.rows),
    columns: terminal.columns,
    // Logo block, then the workdir row and the rule row.
    headerRows: logoRows(terminal.rows) + HEADER_ROWS,
    // The HUD's rows, the context warning when there is one, the summary line,
    // every row of the agent panel and its key/mouse hint.
    statusRows:
      hudRows.length +
      (contextWarning(state.context) ? 1 : 0) +
      1 +
      agentRows.length +
      3,
    bottomRows: reserveBottomRows({
      inputRows: draftRows,
      paletteCommands: draft.startsWith("/") ? completions.length : 0,
      approvalArgs: state.pendingApproval
        ? Object.keys(state.pendingApproval.args ?? {}).length
        : null,
      queueRequests: state.approvalQueue.length,
      questionRows: state.pendingQuestion ? questionPromptRows(state.pendingQuestion) : 0,
      queueFocused,
      errorVisible: state.errors.length > 0,
      workingVisible: workingText !== null,
      delegationVisible: delegation !== null,
      queuedRows: Math.min(state.queued.length, 4),
      noticeVisible: Boolean(modeToast && modeToast.length > TOAST_INLINE_MAX),
    }),
  });
  // How tall the open agent's window may be. Inline, the live region sits under
  // the scrollback and Ink clears the whole terminal on every frame once that
  // region reaches the terminal's height — so the window takes what the status
  // block and the input leave over, and no more.
  const agentWindowRows = agentTranscriptRows({
    fullscreen,
    usable: usableRows(terminal.rows),
    statusRows: layout.statusRows,
    bottomRows: layout.bottomRows,
  });
  // Rows the inline live region may occupy before Ink starts clearing the
  // screen on every frame. Full-screen draws a fixed window into the alternate
  // buffer and never takes that branch, so nothing is bounded there.
  const liveRegionRows = fullscreen
    ? Number.POSITIVE_INFINITY
    : Math.max(
      1,
      usableRows(terminal.rows) - layout.statusRows - layout.bottomRows - INLINE_CHROME_SLACK,
    );
  // What may be *held* on screen is budgeted separately, and less
  // pessimistically. The two decisions are not the same: the message window is
  // the one unbounded element, so its cap carries the full safety margin, while
  // the hold only decides whether a finished card waits for its neighbours so
  // the run can reach the scrollback as one summary line. Charging the hold the
  // same margin left nothing holdable on a small terminal and split every run
  // into one line per call. The sum stays bounded because the window's cap
  // subtracts the rows the held cards actually take, below.
  const holdRegionRows = fullscreen
    ? Number.POSITIVE_INFINITY
    : Math.max(
      1,
      usableRows(terminal.rows) - layout.statusRows - layout.bottomRows - HOLD_CHROME_SLACK,
    );

  // The key handler reads the height through a ref: PgUp must move by whatever
  // is on screen now, without rebinding `useInput` on every resize.
  const agentWindowRowsRef = useRef(agentWindowRows);
  agentWindowRowsRef.current = agentWindowRows;
  const agentViewport = useMemo(
    () => sliceViewport(agentLines, agentWindowRows, agentScroll),
    [agentLines, agentWindowRows, agentScroll],
  );
  const panelMouseLayout = useMemo(
    () => ({
      totalRows: fullscreen ? layout.rows : terminal.rows,
      bottomRows: layout.bottomRows,
      panelRows: agentRows.length,
    }),
    [fullscreen, layout.rows, layout.bottomRows, terminal.rows, agentRows.length],
  );

  // --- what has reached the scrollback --------------------------------------
  // Appended during render, not from an effect: an effect would draw the
  // just-finished entry once in the live region and move it to `<Static>` on
  // the next pass, writing it to the terminal twice. Every step below is
  // monotonic, so running it again for the same state changes nothing.
  //
  // It runs after the layout because the release is bounded by height: what is
  // still live is repainted on every frame, so a long turn's finished edits
  // have to reach the scrollback as they complete rather than piling up on
  // screen until the turn ends.
  // A message still being written shares the region with the cards held beside
  // it, so the hold has to leave it room: without that reservation the cards
  // can take the whole budget, the message keeps its floor on top, and the two
  // together overflow the terminal again — which is the implementing turn the
  // user saw flicker after the plan-mode one was fixed.
  const holdRows = state.messages.some((message) => message.streaming)
    ? Math.max(1, holdRegionRows - MIN_LIVE_MESSAGE_ROWS)
    : holdRegionRows;
  const released = settledCount(state, staticCursorRef.current, holdRows);
  if (released > staticCursorRef.current) {
    const previous = staticCursorRef.current;
    staticBlocksRef.current = staticBlocksRef.current.concat(
      releaseEntries(
        state,
        state.timeline.slice(previous, released),
        contentWidth,
        streamCommittedRef.current,
      ),
    );
    for (const item of state.timeline.slice(previous, released)) {
      if (item.kind === "message") streamCommittedRef.current.delete(item.id);
    }
    staticCursorRef.current = released;
  }

  // A turn beginning or ending is also scrollback bookkeeping: the indicator
  // measures from the start, and the end leaves one line behind.
  if (state.turnActive && !turnActiveRef.current) {
    turnRef.current = {
      startedAt: state.turnStartedAt ?? now,
      inputTokens: state.usage.inputTokens,
      outputTokens: state.usage.outputTokens,
      errors: state.errors.length,
    };
  }
  // `turn.started` can land after whatever first marked the turn active; when
  // it does, the daemon's stamp wins over the local one.
  if (state.turnActive && turnRef.current && state.turnStartedAt !== null) {
    turnRef.current.startedAt = state.turnStartedAt;
  }
  if (!state.turnActive && turnActiveRef.current && turnRef.current) {
    const turn = turnRef.current;
    const ok = state.lastTurnReason === null || state.lastTurnReason === "complete";
    turnCountRef.current += 1;
    staticBlocksRef.current = staticBlocksRef.current.concat({
      key: `turn-${turnCountRef.current}`,
      kind: "note",
      ok,
      text: turnSummaryLine({
        ok,
        elapsedMs: now - turn.startedAt,
        inputTokens: state.usage.inputTokens - turn.inputTokens,
        outputTokens: state.usage.outputTokens - turn.outputTokens,
      }),
    });
    turnRef.current = null;
  }
  turnActiveRef.current = state.turnActive;

  const staticCursor = staticCursorRef.current;

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
   * `/skill edit <name>`: hand the terminal to `$EDITOR`, then reload.
   *
   * The daemon says where the file is (`skill.read`); the editing happens here,
   * because this is the side with a terminal. While the editor is up the TUI
   * draws nothing and reads nothing — the two of them cannot share a screen.
   */
  const editSkill = useCallback(
    (name: string) => {
      if (!editor) {
        showToast("no editor available in this terminal");
        return;
      }
      void (async () => {
        let path: string | null = null;
        try {
          const result = await client.call("skill.read", { name });
          path = typeof result?.path === "string" ? result.path : null;
        } catch (error: unknown) {
          dispatch({ type: "note", text: `could not open ${name}: ${String(error)}` });
          return;
        }
        if (!path) {
          dispatch({ type: "note", text: `the daemon did not say where ${name} is kept` });
          return;
        }
        setSuspended(true);
        setRawMode?.(false);
        setMouseMode(false);
        let code = 0;
        try {
          code = await editor.run(path);
        } finally {
          setRawMode?.(true);
          setMouseMode(true);
          setSuspended(false);
        }
        if (code !== 0) {
          showToast(`${editor.command()} exited with ${code}`);
          return;
        }
        try {
          await client.call("skill.reload", {});
          const commands = await registryRef.current.refresh();
          dispatch({ type: "commands", commands });
          showToast(`reloaded ${name}`);
        } catch (error: unknown) {
          dispatch({ type: "note", text: `skill.reload failed: ${String(error)}` });
        }
      })();
    },
    [client, editor, setMouseMode, setRawMode, showToast],
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
        discoveredSource: modelsResult?.source ?? null,
        current: state.model ?? modelsResult?.current ?? null,
        vendor: state.provider ?? modelsResult?.vendor ?? null,
        effort: state.effort,
        effortSource: state.effortSource,
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
      if (ref === EFFORT_REF) {
        // The effort row cycles rather than opening a submenu of four; an
        // older daemon has no such method, and falls back to the command.
        const wanted = nextEffort(state.effort);
        void client
          .call("session.setEffort", { sessionId, effort: wanted })
          .then((result) => {
            showToast(`effort: ${result?.effort ?? wanted}`);
          })
          .catch((error: unknown) => {
            if ((error as { code?: unknown } | null)?.code === -32601) {
              submit(`/effort ${wanted}`);
              return;
            }
            dispatch({ type: "error", message: String(error) });
          });
        return;
      }
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
    [client, sessionId, showToast, state.effort],
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
    void client.call("session.list", { includeClosed: true }).then((result) => {
      const choices = (Array.isArray(result?.sessions) ? result.sessions : [])
        .filter((row: any) => row.sessionId !== activeSessionRef.current)
        .map((row: any) => ({
          sessionId: String(row.sessionId),
          workdir: String(row.workdir),
          firstPrompt: typeof row.lastPrompt === "string" ? row.lastPrompt : "",
          at: Date.parse(String(row.createdAt)) || 0,
          kind: typeof row.kind === "string" ? row.kind : "chat",
          parentSessionId:
            typeof row.parentSessionId === "string" ? row.parentSessionId : undefined,
        }));
      const rows = resumeRows(choices, workdir);
      if (rows.length === 0) {
        showToast("no saved sessions for this directory");
        return;
      }
      setResumeChoices(rows);
    }).catch((error: unknown) =>
      dispatch({ type: "error", message: `could not list saved sessions: ${String(error)}` }),
    );
  }, [client, workdir, showToast]);

  /**
   * `mcp.test` on a draft the form is still collecting.
   *
   * Never throws: a broken server is the ordinary case here, and the form draws
   * the failure as a screen with a way forward rather than an error line.
   */
  const testMcpDraft = useCallback(
    (params: Record<string, unknown>): Promise<McpProbe> =>
      client
        .call("mcp.test", { ...params, sessionId, workdir })
        .then((result) => readMcpProbe(result))
        .catch((error: unknown) => ({ ok: false, tools: [], error: String(error) })),
    [client, sessionId, workdir],
  );

  /**
   * Write the entry the form collected.
   *
   * The probe has already run, so `test: false` goes with it: probing twice
   * would start the server a second time and double the wait for no new
   * information.
   */
  const addMcpServer = useCallback(
    (submission: McpSubmission) => {
      setMcpForm(null);
      const params: Record<string, unknown> = {
        ...submission.params,
        sessionId,
        workdir,
        test: false,
      };
      if (submission.toolsInclude) params.toolsInclude = submission.toolsInclude;
      if (submission.force) params.force = true;
      const name = String(params.name ?? "");
      void client
        .call("mcp.add", params)
        .then((result) => {
          const path = typeof result?.path === "string" ? result.path : "";
          dispatch({ type: "note", text: `mcp: added ${name}${path ? ` → ${path}` : ""}` });
          mcpErrorsRef.current.delete(name);
          refreshMcp();
        })
        .catch((error: unknown) =>
          dispatch({ type: "note", text: `mcp: could not add ${name} — ${String(error)}` }),
        );
    },
    [client, refreshMcp, sessionId, workdir],
  );

  /** `/mcp catalog`: the presets, as a list rather than a wall of ids. */
  const openMcpCatalog = useCallback(() => {
    void client
      .call("mcp.catalog", {})
      .then((result) => {
        const entries = readMcpCatalog(result);
        if (entries.length === 0) {
          dispatch({ type: "note", text: "mcp: this daemon ships no presets" });
          return;
        }
        setMcpCatalog(entries);
      })
      .catch((error: unknown) =>
        dispatch({ type: "note", text: `mcp.catalog failed: ${String(error)}` }),
      );
  }, [client]);

  /**
   * `/mcp configure <name>`: the server's tools, as a checklist.
   *
   * The list is re-read rather than taken from the store, because the tools are
   * only known once the server is ready and the store may predate that.
   */
  const openMcpConfigure = useCallback(
    (name: string) => {
      void client
        .call("mcp.list", { sessionId, workdir })
        .then((result) => {
          const servers = readMcpList(result);
          dispatch({ type: "mcp/list", servers });
          const row = servers.find((server: McpServerRow) => server.name === name);
          if (!row) {
            dispatch({ type: "note", text: `mcp: no server named ${name}` });
            return;
          }
          if (row.scope !== "project" && row.scope !== "global") {
            dispatch({
              type: "note",
              text: `mcp: ${name} comes from ${row.plugin ? `plugin ${row.plugin}` : row.scope} and is read-only here`,
            });
            return;
          }
          if (row.tools.length === 0) {
            dispatch({
              type: "note",
              text: `mcp: ${name} has listed no tools yet — /mcp test ${name} starts it`,
            });
            return;
          }
          setMcpConfigure({
            name,
            scope: row.scope,
            tools: row.tools,
            selected: row.toolsInclude,
          });
        })
        .catch((error: unknown) =>
          dispatch({ type: "note", text: `mcp.list failed: ${String(error)}` }),
        );
    },
    [client, sessionId, workdir],
  );

  /** The checklist's answer, as the `tools.include` list the daemon stores. */
  const saveMcpTools = useCallback(
    (name: string, scope: "project" | "global", names: string[]) => {
      setMcpConfigure(null);
      void client
        .call("mcp.update", {
          name,
          scope,
          sessionId,
          workdir,
          patch: { toolsInclude: names },
        })
        .then(() => {
          dispatch({
            type: "note",
            text: `mcp: ${name} now registers ${names.length} tool${names.length === 1 ? "" : "s"}`,
          });
          refreshMcp();
        })
        .catch((error: unknown) =>
          dispatch({ type: "note", text: `mcp: could not update ${name} — ${String(error)}` }),
        );
    },
    [client, refreshMcp, sessionId, workdir],
  );

  const submit = useCallback(
    (text: string) => {
      if (resumingRef.current || update.phase === "running" || update.phase === "done") return;
      // Both nudges are about the turn that just ended; the next prompt buries
      // them.
      setSkillChip(null);
      setSkillHint(false);
      // `/delegate` and `$agent` take the same road: the text goes to the
      // daemon as a prompt and its own parser starts the turn. `command.run`
      // would reach the same `commands.start`, but one road means one thing to
      // check when a delegation does not answer.
      if (/^\/delegate(\s|$)/.test(text.trim())) {
        // The daemon turns this back into the `/delegate` command, so the
        // prompt never joins the history and no `message.user` follows it.
        dispatch({ type: "user/message", text, attachments: [], expectEvent: false });
        history?.add(text, workdir);
        void client
          .prompt(sessionId, text)
          .then((result) => {
            const turnId = (result as { turnId?: string } | null)?.turnId;
            if (turnId) dispatch({ type: "prompt/turn", turnId, text });
            return result;
          })
          .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
        return;
      }
      // `$executor fix the tests` goes through the prompt like anything else.
      // Spawning the agent from here instead would run the delegate and stop:
      // nothing would carry its report back into the conversation, and the main
      // agent would never answer. The daemon rewrites the prefix and runs the
      // whole turn — delegate, result, reply.
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
      const mouse = /^\/mouse(?:\s+(on|off))?\s*$/.exec(text.trim());
      if (mouse) {
        const next = mouse[1] ? mouse[1] === "on" : !mouseOn;
        setMouseOn(next);
        showToast(
          next
            ? "mouse on: click a ◯ row to open it; Shift+drag selects text"
            : "mouse off: the terminal selects text as usual",
        );
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
      // `/lsp` is this surface's own: the daemon answers `lsp.status`, and the
      // table belongs in the transcript rather than in a toast.
      if (/^\/lsp\s*$/.test(text.trim())) {
        void client
          .call("lsp.status", {})
          .then((result) => {
            const servers = readLspStatus(result);
            dispatch({ type: "lsp/status", servers });
            dispatch({ type: "note", text: lspTable(servers) });
          })
          .catch((error: unknown) =>
            dispatch({ type: "note", text: `lsp.status failed: ${String(error)}` }),
          );
        return;
      }
      // `/skill create` on its own opens the form rather than failing on a
      // missing argument; `/skill create <name> "…"` is the daemon's command
      // and goes straight through.
      if (isBareSkillCreate(text)) {
        setSkillForm(true);
        return;
      }
      // `/skill edit` is this surface's: the daemon has no editor, and this one
      // has a terminal to lend.
      const edit = parseSkillEdit(text);
      if (edit) {
        editSkill(edit);
        return;
      }
      // `/mcp add` on its own opens the form rather than failing on a missing
      // argument, the way `/skill create` does; `/mcp add <name> -- cmd` is the
      // daemon's command and goes straight through.
      if (isBareMcpAdd(text)) {
        setMcpForm(emptyDraft());
        return;
      }
      // `/mcp catalog` is a list to pick from here, and picking fills the form.
      if (isBareMcpCatalog(text)) {
        openMcpCatalog();
        return;
      }
      // `/mcp configure <name>` with no tool names is a checklist; naming them
      // on the line is the daemon's own command.
      const configure = parseMcpConfigure(text);
      if (configure) {
        openMcpConfigure(configure);
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
      const voiceCmd = /^\/voice(?:\s+(on|off))?\s*$/.exec(text.trim());
      if (voiceCmd) {
        // `/voice` toggles; `/voice on|off` is the same switch said outright,
        // the way `/tts on|off` is, so the two commands read alike.
        const wanted = voiceCmd[1] === "on" ? true : voiceCmd[1] === "off" ? false : null;
        setVoice((current) => {
          if (wanted !== null && current.input === wanted) {
            showToast(wanted ? "voice input is already on" : "voice input is already off");
            return current;
          }
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
      // `/tts voices` and `/tts voice <id> [lang]`: the engine's catalogue, and
      // the per-language pick `audio.tts.voices` stores.
      const ttsVoices = /^\/tts\s+voices\s*$/.exec(text.trim());
      const ttsVoice = /^\/tts\s+voice\s+(\S+)(?:\s+(\S+))?\s*$/.exec(text.trim());
      if (ttsVoices || ttsVoice) {
        const engine = capabilities?.ttsProvider;
        if (!engine) {
          showToast("no speech engine is set — run /setup");
          return;
        }
        void client
          .call("audio.voices", { engine, languages: [] })
          .then(async (result: any) => {
            const voices: Array<{ id: string; label: string; language: string; installed: boolean }> =
              Array.isArray(result?.voices) ? result.voices : [];
            if (ttsVoices) {
              const lines = voices.map(
                (v) => `${v.id} · ${v.label} · ${v.language}${v.installed ? "" : " · downloads first"}`,
              );
              dispatch({
                type: "note",
                text: lines.length > 0 ? `${engine} voices:\n${lines.join("\n")}` : `${engine}: no voices listed`,
              });
              return;
            }
            const wanted = ttsVoice![1];
            const picked = voices.find((v) => v.id === wanted || v.label === wanted);
            if (!picked) {
              showToast(`${engine}: no voice "${wanted}" — /tts voices lists them`);
              return;
            }
            const lang = (ttsVoice![2] ?? picked.language ?? "").split("-")[0] || "en";
            await client.call("settings.set", {
              scope: "global",
              patch: { audio: { tts: { voices: { [lang]: picked.id } } } },
            });
            showToast(`speech voice for ${lang}: ${picked.label} (${picked.id})`);
          })
          .catch((error: unknown) => showToast(`voices: ${String(error)}`));
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
      dispatch({
        type: "user/message",
        text,
        attachments: sent,
        expectEvent: !text.startsWith("/"),
      });
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
      editSkill,
      openMcpCatalog,
      openMcpConfigure,
      showToast,
      takePaste,
      toggleRecording,
    ],
  );

  /**
   * The form's answer: the command line the user did not have to remember.
   *
   * It goes through `session.prompt`, not `command.run`: creating a skill is a
   * turn the agent takes — it drafts the SKILL.md and writes it — and the
   * transcript should show it as one.
   */
  const createSkill = useCallback(
    (form: { name: string; description: string; scope: "project" | "global" }) => {
      setSkillForm(false);
      const text = skillCreateCommand(form);
      dispatch({ type: "user/message", text, attachments: [], expectEvent: false });
      history?.add(text, workdir);
      setRunningCommand("/skill create");
      void client
        .prompt(sessionId, text)
        .then((result) => {
          const turnId = (result as { turnId?: string } | null)?.turnId;
          if (turnId) dispatch({ type: "prompt/turn", turnId, text });
          return result;
        })
        .catch((error: unknown) => {
          setRunningCommand(null);
          dispatch({ type: "error", message: String(error) });
        });
    },
    [client, history, sessionId, workdir],
  );

  /**
   * A reply that ends with "Run it with /x" means the table has a new command.
   *
   * The chip is the same sentence in three words, kept next to the input where
   * the next thing typed will be.
   */
  useEffect(() => {
    const last = [...state.messages]
      .reverse()
      .find((message) => message.role === "assistant" && !message.streaming);
    if (!last || skillReplyRef.current === last.id) return;
    const name = createdSkillName(last.text);
    if (!name) return;
    skillReplyRef.current = last.id;
    setSkillChip(name);
    void registryRef.current
      .refresh()
      .then((commands) => dispatch({ type: "commands", commands }))
      .catch(() => undefined);
  }, [state.messages]);

  /**
   * A long turn full of tool calls is the kind of work worth keeping.
   *
   * Measured when the turn ends, offered once, and gone for good once waved
   * away — see `state/skill-hint.ts` for the thresholds.
   */
  useEffect(() => {
    if (state.turnActive) {
      hintTurnRef.current = {
        startedAt: Date.now(),
        tools: state.toolCalls.length,
        errors: state.errors.length,
      };
      return;
    }
    const turn = hintTurnRef.current;
    hintTurnRef.current = null;
    if (!turn) return;
    const suggest = shouldSuggestSkill({
      toolCalls: state.toolCalls.length - turn.tools,
      elapsedMs: Date.now() - turn.startedAt,
      ok: state.errors.length === turn.errors,
      shown: skillHintSpent.current,
      dismissed: skillHintSpent.current,
    });
    if (!suggest) return;
    skillHintSpent.current = true;
    setSkillHint(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.turnActive]);

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
    (decision: ApprovalDecision, scope: ApprovalScope, reason?: string) => {
      const resolve = approvalResolver.current;
      const requestId = state.pendingApproval?.requestId;
      approvalResolver.current = null;
      if (requestId) dispatch({ type: "approval/resolved", requestId });
      // `reason` is only ever set by "No, and tell it why"; the daemon quotes
      // it back to the model as the tool's refusal (M15b §1).
      resolve?.(reason ? { decision, scope, reason } : { decision, scope });
    },
    [state.pendingApproval],
  );

  const answerQuestion = useCallback(
    (answers: QuestionAnswerItem[]) => {
      const resolve = questionResolver.current;
      const requestId = state.pendingQuestion?.requestId;
      questionResolver.current = null;
      if (requestId) dispatch({ type: "question/resolved", requestId });
      resolve?.({ answers });
    },
    [state.pendingQuestion],
  );

  const respondQueued = useCallback(
    (requestId: string, decision: ApprovalDecision, scope: ApprovalScope, reason?: string) => {
      dispatch({ type: "approval/resolved", requestId });
      void client
        .respondApproval(requestId, decision, scope, reason)
        .catch((error: unknown) => dispatch({ type: "error", message: String(error) }));
    },
    [client],
  );

  useInput((input, key) => {
    if (key.ctrl && input === "c") {
      exit();
      return;
    }
    if (daemonGone && (input === "R" || input === "r")) {
      void handleStartDaemon();
      return;
    }
    // A prompt on screen owns every other key: mode cycling, Ctrl+O and the
    // rest would otherwise fire underneath the question being asked.
    if (state.pendingQuestion || state.pendingApproval || update.phase === "confirm" || modelPicker) {
      return;
    }
    const reports = mouseReports(input);
    if (reports.length > 0) {
      // Mouse traffic never reaches the editor, handled or not.
      const mouse = reports.find((report) => report.press);
      if (!mouse) return;
      if (openAgent && mouse.button === 64) {
        setAgentScroll((offset) => offset + 1);
        return;
      }
      if (openAgent && mouse.button === 65) {
        setAgentScroll((offset) => Math.max(0, offset - 1));
        return;
      }
      if (openAgent || mouse.button !== 0) return;
      const index = panelRowAt(mouse.row, panelMouseLayout);
      if (index === null) return;
      setFocus({ zone: "agent", index });
      openAgentRow(index);
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
        setAgentScroll((offset) => offset + agentWindowRowsRef.current);
        return;
      }
      if (key.pageDown) {
        setAgentScroll((offset) => Math.max(0, offset - agentWindowRowsRef.current));
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
        showToast(`${row.name}: no run in this session yet`);
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

      {skillChip ? (
        <Text color="green" wrap="truncate-end">{`[run /${skillChip}]`}</Text>
      ) : null}

      {skillHint ? (
        <Text dimColor wrap="truncate-end">{`${SKILL_HINT_TEXT} · Esc dismisses`}</Text>
      ) : null}

      <QueuedPrompts queued={state.queued} width={contentWidth} />

      <AttachmentChips attachments={attachments} width={contentWidth} />

      {modeToast && modeToast.length > TOAST_INLINE_MAX ? (
        <Text color="cyan" wrap="truncate-end">
          {modeToast}
        </Text>
      ) : null}

      {skillForm ? (
        <SkillCreateForm
          width={contentWidth}
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
          onCancel={() => setSkillForm(false)}
          onSubmit={createSkill}
        />
      ) : null}

      {mcpForm ? (
        <McpAddForm
          width={contentWidth}
          initial={mcpForm}
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
          onTest={testMcpDraft}
          onCancel={() => setMcpForm(null)}
          onSubmit={addMcpServer}
        />
      ) : null}

      {mcpCatalog ? (
        <McpCatalogPicker
          width={contentWidth}
          entries={mcpCatalog}
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
          onCancel={() => setMcpCatalog(null)}
          onChoose={(entry) => {
            setMcpCatalog(null);
            setMcpForm(draftFromCatalog(entry));
          }}
        />
      ) : null}

      {mcpConfigure ? (
        <ToolChecklist
          width={contentWidth}
          title={`${mcpConfigure.name} — tools to register`}
          tools={mcpConfigure.tools}
          initial={mcpConfigure.selected}
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
          onCancel={() => setMcpConfigure(null)}
          onSubmit={(names) => saveMcpTools(mcpConfigure.name, mcpConfigure.scope, names)}
        />
      ) : null}

      {modelPicker ? (
        <ModelPicker
          options={modelPicker}
          width={contentWidth}
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
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
          isActive={state.pendingApproval === null && state.pendingQuestion === null}
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
        isActive={queueFocused && state.pendingApproval === null && state.pendingQuestion === null}
        onBlur={() => setQueueFocused(false)}
      />

      {state.pendingQuestion ? (
        <QuestionPrompt request={state.pendingQuestion} onAnswer={answerQuestion} />
      ) : state.pendingApproval ? (
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
              label: resumeLabel(entry, 48, workdir),
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
          onStartDaemon={daemonGone ? () => { void handleStartDaemon(); } : undefined}
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
          placeholder={state.turnActive ? "Esc stops · type what to change" : undefined}
          completions={completions}
          agents={completableAgents}
          draftWidth={Math.max(1, contentWidth - 2)}
          onChange={(next) => {
            setDraft(next);
            if (next.length > 0 && state.errors.length > 0) dispatch({ type: "errors/clear" });
          }}
          onInterrupt={() => {
            // Esc with nothing running is how the nudge is waved away; with a
            // turn in flight it still means "stop".
            if (skillHint && !state.turnActive) {
              setSkillHint(false);
              return;
            }
            setFocus(INPUT_FOCUS);
            void client.interrupt(sessionId).catch(() => undefined);
          }}
          disabled={skillForm || mcpForm !== null || mcpCatalog !== null || mcpConfigure !== null || showHelp || update.phase === "confirm" || update.phase === "running" || update.phase === "done" || approvalActive || queueFocused || !isInput(focus) || openAgent !== null}
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
      <Text
        color={summary.color}
        dimColor={summary.dimColor && focus.zone !== "footer"}
        inverse={focus.zone === "footer"}
        wrap="truncate-end"
      >
        {`${summary.text}${focus.zone === "footer" ? " · Enter to choose mode" : ""}`}
      </Text>
      {shellsOpen ? <ShellList calls={state.toolCalls} now={clock} width={contentWidth} /> : null}
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
      <Text dimColor>{mouseOn ? "↑↓ select · click or Enter opens" : "↑↓ select · Enter opens · /mouse enables clicking"}</Text>
    </>
  );


  const helpNode = showHelp ? (
    <HelpPanel
      commands={state.commands}
      width={contentWidth}
      height={fullscreen ? layout.transcriptRows : Math.max(4, usableRows(terminal.rows) - layout.bottomRows - layout.statusRows - (bannerText(update) ? 3 : 0))}
      isActive={state.pendingApproval === null && state.pendingQuestion === null && update.phase !== "confirm"}
      runningSubagents={state.subagents.filter((agent) => agent.status === "running").length}
    />
  ) : null;

  // While `$EDITOR` has the terminal the TUI draws nothing at all: two
  // full-screen programs cannot share one screen, and Ink would repaint over
  // the editor on its next frame.
  if (suspended) return <Box />;

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

  // Rows a live message may occupy: the region's budget, less whatever the
  // cards held beside it already take. A plan is one message that streams for
  // minutes, so without this the region outgrows the terminal and Ink switches
  // to clearing the screen before every frame — under tmux, a view that shakes
  // between the top and the bottom of the text. What the window hides is one
  // `message.done` away from the scrollback, complete.
  // The budget is shared, because there can be more than one live message: a
  // prompt queued mid-answer puts the user's entry into the timeline between
  // the text that had arrived and the text still coming, which splits the
  // answer into two entries, neither of which ever closes. Capping each of them
  // to the whole budget let the pair add up to twice it, and the frame reached
  // the terminal's height again — the one case that still flickered.
  const liveMessages = Math.max(1, live.filter((item) => item.kind === "message").length);
  const liveMessageRows = Math.max(
    MIN_LIVE_MESSAGE_ROWS,
    Math.floor(
      (liveRegionRows -
        MESSAGE_MARGIN_ROWS * liveMessages -
        live.reduce(
          (rows, item) => (item.kind === "message" ? rows : rows + entryRows(state, item)),
          0,
        )) /
      liveMessages,
    ),
  );

  const streamCommit = commitStreamingPrefixes(
    state,
    staticCursor,
    contentWidth,
    liveMessageRows,
    streamCommittedRef.current,
  );
  if (streamCommit.chunks.length > 0) {
    staticBlocksRef.current = staticBlocksRef.current.concat(streamCommit.chunks);
  }
  streamCommittedRef.current = streamCommit.committed;
  const staticItems = staticBlocksRef.current;

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
            marginTop={followsTools(staticItems, staticItems.indexOf(entry)) ? 1 : 0}
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
            ) : entry.kind === "stream-chunk" ? (
              <RenderedLines lines={entry.lines} />
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
                messageStartLine={entry.messageStartLine}
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
          status={openAgentEntry ? agentStatusText(openAgentEntry, clock) : ""}
          lines={agentViewport.lines}
          height={agentWindowRows}
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
            maxMessageRows={liveMessageRows}
            messageStartLine={
              item.kind === "message" ? streamCommittedRef.current.get(item.id) ?? 0 : 0
            }
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
