/**
 * The full-screen transcript as wrapped lines, drawn the way the inline layout
 * draws its scrollback.
 *
 * Full-screen owns the alternate buffer and windows the transcript itself, so
 * everything it shows has to be a `Line` it can slice. The inline layout draws
 * components instead (`ToolSummary`, `ToolCall`, `DiffView`, `MessageView`);
 * this module is their line-for-line twin, so switching layouts never changes
 * what a turn looks like:
 *
 *   - a run of successful tool calls folds into one `⏺ Read 3 files` line, a
 *     failure keeps its own card, and the run still being worked on at the end
 *     of an active turn stays as live cards with their progress tail;
 *   - a diff shows the same header, badge and 12-line fold as `DiffView`;
 *   - a prompt you sent is a band in the input colour;
 *   - each finished turn leaves its `✓ Done in 12s` line where it ended.
 *
 * The subagent list is not drawn here: the agent panel under the input already
 * holds it in a fixed place.
 *
 * Pure, so `test/screen-transcript.test.ts` checks it without a terminal.
 */

import { COLLAPSED_LINES, EXPANDED_LINES, diffHeader, diffLineColor } from "../components/DiffView.js";
import { SUMMARY_GLYPH } from "../components/ToolSummary.js";
import { diagnosticsBadge, diagnosticsColor } from "../state/lsp.js";
import type { DiffEntry, Message, State, ToolCallEntry } from "../state/store.js";
import { compactionDivider } from "./bottom.js";
import { currentAccentDim, currentInputBackground } from "./palette.js";
import { groupCalls, hiddenLines, summarizeCalls } from "./summary.js";
import { textWidth } from "./text-width.js";
import { messageLines, toolCallLines, wrapLine, type Line, type Segment } from "./transcript.js";

/** The `✓ Done in 12s` line a finished turn leaves, and where it goes. */
export interface TurnNote {
  /** Timeline length when the turn ended; the note follows that entry. */
  at: number;
  text: string;
  ok: boolean;
}

export interface ScreenTranscriptOptions {
  /** The call or diff Ctrl+O opened. */
  expandedId?: string | null;
  notes?: readonly TurnNote[];
  /** Band colour behind a sent prompt; the terminal's palette decides by default. */
  inputBackground?: string;
}

const blank = (key: string): Line => ({ key, segments: [{ text: "" }] });

/** A prompt the user sent, filled to the full width in the input colour. */
function bandLines(lines: Line[], width: number, background: string | undefined): Line[] {
  if (!background) return lines;
  return lines.map((line) => {
    const used = line.segments.reduce((sum, segment) => sum + textWidth(segment.text), 0);
    const pad = Math.max(0, width - used);
    const segments: Segment[] = line.segments.map((segment) => ({ ...segment, backgroundColor: background }));
    if (pad > 0) segments.push({ text: " ".repeat(pad), backgroundColor: background });
    return { ...line, segments };
  });
}

function messageBlock(
  message: Message,
  width: number,
  colorize: (lines: Line[]) => Line[],
  inputBackground: string | undefined,
): Line[] {
  const base = messageLines(message, width);
  const formatted = message.role === "user" ? colorize(base) : base;
  const wrapped = formatted.flatMap((line) => wrapLine(line, width, "  "));
  const background = message.role === "user" ? inputBackground : undefined;
  const out = bandLines(wrapped, width, background);
  if (message.expansion) {
    const { name, lines } = message.expansion;
    out.push({
      key: `${message.id}-fold`,
      segments: [
        { text: `${SUMMARY_GLYPH} `, color: currentAccentDim() },
        { text: `skill ${name} · ${lines} lines`, dimColor: true },
      ],
    });
  }
  for (const attachment of message.attachments ?? []) {
    out.push(
      ...bandLines(
        [{ key: `${message.id}-att-${attachment.name}`, segments: [{ text: `  📎 ${attachment.name}`, dimColor: true }] }],
        width,
        background,
      ),
    );
  }
  return out;
}

function summaryLine(calls: ToolCallEntry[]): Line {
  const hidden = hiddenLines(calls);
  const segments: Segment[] = [
    { text: `${SUMMARY_GLYPH} `, color: currentAccentDim() },
    { text: summarizeCalls(calls) },
  ];
  if (hidden > 0) segments.push({ text: ` (${hidden} lines)`, dimColor: true });
  return { key: `tools-${calls[0].callId}-${calls.length}`, segments };
}

function diffBlock(diff: DiffEntry, expanded: boolean, state: State): Line[] {
  const diagnostics = state.diagnostics[diff.path];
  const badge = diagnosticsBadge(diagnostics);
  const head: Segment[] = [{ text: diffHeader(diff), bold: true, color: diff.created ? "green" : "yellow" }];
  if (badge) head.push({ text: `  ${badge}`, color: diagnosticsColor(diagnostics) });
  const all = diff.patch.split("\n");
  const shown = all.slice(0, expanded ? EXPANDED_LINES : COLLAPSED_LINES);
  const hidden = all.length - shown.length;
  const out: Line[] = [{ key: `${diff.id}-h`, segments: head }];
  shown.forEach((line, index) => {
    const color = diffLineColor(line);
    out.push({ key: `${diff.id}-d${index}`, segments: [{ text: line, color, dimColor: color === undefined }] });
  });
  if (hidden > 0) {
    out.push({
      key: `${diff.id}-more`,
      segments: [{ text: `… ${hidden} more ${hidden === 1 ? "line" : "lines"} (Ctrl+O)`, dimColor: true }],
    });
  }
  return out;
}

/**
 * A message's wrapped lines, by the message object.
 *
 * The store replaces a message object when it changes and keeps the rest, so
 * during a stream only the message being written is laid out again: each
 * frame used to re-render the markdown of every message in the session, and
 * on a long one that kept the keyboard waiting.
 */
interface CachedBlock {
  width: number;
  background: string | undefined;
  colorize: (lines: Line[]) => Line[];
  lines: Line[];
}
const messageCache = new WeakMap<Message, CachedBlock>();

function cachedMessageLines(
  message: Message,
  width: number,
  colorize: (lines: Line[]) => Line[],
  inputBackground: string | undefined,
): Line[] {
  const hit = messageCache.get(message);
  if (hit && hit.width === width && hit.background === inputBackground && hit.colorize === colorize) {
    return hit.lines;
  }
  const lines = messageBlock(message, width, colorize, inputBackground).flatMap((line) =>
    wrapLine(line, width, "  "),
  );
  messageCache.set(message, { width, background: inputBackground, colorize, lines });
  return lines;
}

/**
 * Every transcript line, oldest first, wrapped to `width`.
 *
 * `colorize` marks file references in a prompt; it is passed in rather than
 * imported so this module stays free of the message component.
 */
export function screenTranscriptLines(
  state: State,
  width: number,
  { expandedId = null, notes = [], inputBackground = currentInputBackground() }: ScreenTranscriptOptions = {},
  colorize: (lines: Line[]) => Line[] = (lines) => lines,
): Line[] {
  // A note can only follow an entry this timeline has; anything else is from a
  // timeline that was replaced, and in order is the order they are placed in.
  notes = notes.filter((note) => note.at <= state.timeline.length).sort((a, b) => a.at - b.at);
  const out: Line[] = [];
  /** The kind of block last pushed, which decides the spacing before the next. */
  let last: "none" | "tools" | "block" = "none";
  let run: ToolCallEntry[] = [];
  let noteIndex = 0;

  // One pass to index, not a scan of every list per timeline entry.
  const messages = new Map(state.messages.map((entry) => [entry.id, entry]));
  const calls = new Map(state.toolCalls.map((entry) => [entry.callId, entry]));
  const diffs = new Map(state.diffs.map((entry) => [entry.id, entry]));

  const push = (
    lines: Line[],
    kind: "tools" | "block",
    gapAfter: boolean,
    wrapped = false,
  ): void => {
    // Tool lines stack tightly; whatever follows a run of them gets one blank
    // line above, as in the inline scrollback.
    if (kind === "block" && last === "tools") out.push(blank(`gap-${out.length}`));
    if (wrapped) {
      for (const line of lines) out.push(line);
    } else {
      out.push(...lines.flatMap((line) => wrapLine(line, width, "  ")));
    }
    if (gapAfter) out.push(blank(`gap-${out.length}`));
    last = gapAfter ? "none" : kind;
  };

  const flush = (live: boolean): void => {
    if (run.length === 0) return;
    if (live) {
      // The calls still being worked on stay as cards: their progress tail is
      // the news, and Ctrl+O expands the newest one.
      for (const call of run) push(toolCallLines(call, expandedId === call.callId), "tools", false);
    } else {
      for (const block of groupCalls(run)) {
        if (block.kind === "single" || block.calls.some((call) => call.callId === expandedId)) {
          const calls = block.kind === "single" ? [block.call] : block.calls;
          for (const call of calls) push(toolCallLines(call, expandedId === call.callId), "tools", false);
        } else {
          push([summaryLine(block.calls)], "tools", false);
        }
      }
    }
    run = [];
  };

  const placeNotes = (position: number): void => {
    while (noteIndex < notes.length && notes[noteIndex].at <= position) {
      const note = notes[noteIndex];
      flush(false);
      push(
        [{
          key: `note-${noteIndex}`,
          segments: [
            { text: note.text.slice(0, 1), color: note.ok ? "green" : "red" },
            { text: note.text.slice(1), dimColor: true },
          ],
        }],
        "block",
        true,
      );
      noteIndex += 1;
    }
  };

  state.timeline.forEach((item, index) => {
    placeNotes(index);
    if (item.kind === "tool") {
      const call = calls.get(item.id);
      if (call) run.push(call);
      return;
    }
    flush(false);
    if (item.kind === "message") {
      const message = messages.get(item.id);
      if (message) {
        push(cachedMessageLines(message, width, colorize, inputBackground), "block", true, true);
      }
      return;
    }
    if (item.kind === "compaction") {
      const entry = state.compactions.find((c) => c.id === item.id);
      if (entry) {
        push([{ key: `compaction-${entry.id}`, segments: [{ text: compactionDivider(entry.before, entry.after, width), dimColor: true }] }], "block", true);
      }
      return;
    }
    const diff = diffs.get(item.id);
    if (diff) push(diffBlock(diff, expandedId === diff.id, state), "block", true);
  });
  // The run at the very end belongs to the turn still going, when one is.
  const tailNotes = notes.slice(noteIndex).some((note) => note.at <= state.timeline.length);
  flush(state.turnActive && !tailNotes);
  placeNotes(state.timeline.length);

  while (out.length > 0 && out[out.length - 1].segments.every((segment) => segment.text === "")) out.pop();
  return out;
}
