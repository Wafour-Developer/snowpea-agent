/**
 * `/security`: the blocked-command and protected-folder lists, one full-screen
 * overlay with two tabs.
 *
 * Presets are a multi-select (Space), manual entries are added, edited and
 * deleted beside them, and nothing reaches the daemon until `s`: one
 * `settings.set` carries both tabs. A list can be weakened only after a y/n
 * that names what stops being covered. The screen draws lines from the pure
 * functions in `state/security.ts`, so a long path or a CJK title wraps and is
 * never cut.
 */

import React, { useEffect, useRef, useState } from "react";
import { Box, useInput } from "ink";

import { shortcutKey } from "../input/hangulKeys.js";
import { mouseReports } from "../input/mouse.js";
import { textWidth } from "../layout/text-width.js";
import type { Line, Segment } from "../layout/transcript.js";
import {
  NOTHING_RUN,
  OWN_FILES_LINE,
  PATTERN_HELP,
  READ_ONLY_LINE,
  SECURITY_CLAIM,
  SECURITY_LIMITS,
  UNKNOWN_CODE_ALLOW_CONFIRM,
  OLD_DAEMON_LINE,
  ENABLED_NOW,
  OFF_NOTE,
  enabledSegments,
  offConfirmLines,
  switchHelp,
  nextUnknownCode,
  stepUnknownCode,
  unknownCodeMeaning,
  unknownCodeSegments,
  type UnknownCode,
  addCustom,
  bodyLines,
  cycleLevel,
  editCustom,
  effectiveLines,
  hasCustom,
  listsOf,
  problemNote,
  removeCustom,
  sameLists,
  scrollInto,
  tabRows,
  togglePreset,
  verdictLine,
  weakening,
  wrapSegments,
  type EntryProblem,
  type Lists,
  type PresetCatalog,
  type SecurityConfig,
  type SecurityRow,
  type SecurityVerdict,
  type Tab,
} from "../state/security.js";
import { RenderedLines } from "./RenderedLines.js";

export type SaveResult =
  | { ok: true; config: SecurityConfig }
  | { ok: false; message: string; readOnly?: string; entries?: EntryProblem[]; others?: string[]; autoRefused?: boolean };

export type CheckResult = SecurityVerdict | { error: string };

type Mode =
  | { k: "browse" }
  | { k: "input"; action: "add" | "edit" | "check"; value: string; index?: number }
  | { k: "confirm"; lines: string[]; yes: () => void; neutral?: boolean }
  | { k: "effective" }
  | { k: "help" }
  | { k: "saving" };

interface Note {
  text: string;
  color?: string;
  dim?: boolean;
}

export interface SecurityScreenProps {
  catalog: PresetCatalog;
  config: SecurityConfig;
  /** Why the lists cannot be changed from here, or null. */
  readOnly: string | null;
  width: number;
  height: number;
  /** One `settings.set` with both tabs; resolves with the lists as the daemon now has them. */
  onSave: (lists: Lists) => Promise<SaveResult>;
  /** `security.check`: judges text, never runs it; `draft` judges unsaved edits instead of the saved lists. */
  onCheck: (what: "command" | "path", text: string, draft: Lists | null) => Promise<CheckResult>;
  /** Reads the lists again (`security.get`). */
  onReload?: () => Promise<SecurityConfig>;
  /** Switches `security.unknownCode` (global `settings.set`); resolves with the settings as the daemon now has them. */
  onSetUnknownCode?: (value: UnknownCode) => Promise<SaveResult>;
  /** Switches `security.enabled` (1.58.0); absent for an older core, which hides the switch row. */
  onSetEnabled?: (on: boolean) => Promise<SaveResult>;
  /** 1.66.0: turning the checks off also stops the token/login read block (said in the confirm and help). */
  tokensWhenOff?: boolean;
  /** Counts `settings.changed` events that touched the security settings while the screen is open. */
  changeSeq?: number;
  /** The line saying security settings wait on disk (`/settings disk`), or null. */
  diskNotice?: string | null;
  onClose: () => void;
  isActive?: boolean;
}

type HeadRow = "master" | "unknown";

/** A row drawn dimmed (the checks are off): no colour, the cursor mark kept. */
const dimmed = (segments: Segment[]): Segment[] => segments.map((segment) => (segment.text === "› " ? segment : { ...segment, color: undefined, dimColor: true }));

const TAB_TITLE: Record<Tab, string> = { commands: "Blocked commands", paths: "Protected folders" };

const INTRO: Record<Tab, string> = {
  commands: "snowpea refuses these commands where it sees them: not asked, not approvable, not handed to Jarvis.",
  paths: "\"no changes\" stops writing, deleting, moving and chmod; \"no access\" stops reading too.",
};

export function SecurityScreen({
  catalog,
  config,
  readOnly: initialReadOnly,
  width,
  height,
  onSave,
  onCheck,
  onReload,
  onSetUnknownCode,
  onSetEnabled,
  tokensWhenOff = false,
  changeSeq = 0,
  diskNotice = null,
  onClose,
  isActive = true,
}: SecurityScreenProps): React.ReactElement {
  const [tab, setTab] = useState<Tab>("commands");
  const [cursors, setCursors] = useState<Record<Tab, number>>({ commands: 0, paths: 0 });
  const [base, setBase] = useState<SecurityConfig>(config);
  const [draft, setDraft] = useState<Lists>(() => listsOf(config));
  const [mode, setMode] = useState<Mode>({ k: "browse" });
  const [notes, setNotes] = useState<Note[]>([]);
  const [readOnly, setReadOnly] = useState<string | null>(initialReadOnly);
  const [effCursor, setEffCursor] = useState(0);
  /** The daemon refused "auto" (it knows only ask/allow): the switch stays two-way. */
  const [noAuto, setNoAuto] = useState(false);
  /** Core's reason for an entry it refused, by tab and the entry's text; gone when the entry changes. */
  const [entryProblems, setEntryProblems] = useState<Record<Tab, Map<string, string>>>({ commands: new Map(), paths: new Map() });
  /** The lists changed on the daemon while there were unsaved edits here. */
  const [elsewhere, setElsewhere] = useState(false);
  const [helpTop, setHelpTop] = useState(0);
  const scroll = useRef<Record<string, number>>({});
  /**
   * The cursor is on a row above the lists: the security switch (first, when
   * core has it) or the unknown-code row. ←→ and Space change its value there
   * instead of switching tabs.
   */
  const hasMaster = Boolean(onSetEnabled);
  const hasSwitch = Boolean(onSetUnknownCode);
  const heads: HeadRow[] = [...(hasMaster ? (["master"] as const) : []), ...(hasSwitch ? (["unknown"] as const) : [])];
  const [headRow, setHeadRow] = useState<HeadRow | null>(() => (hasMaster && config.enabled === false ? "master" : null));
  const at: HeadRow | null = headRow !== null && heads.includes(headRow) ? headRow : null;
  const switchHere = at === "unknown";
  const masterHere = at === "master";
  const onHead = at !== null;
  const off = hasMaster && !base.enabled;

  const dirty = !sameLists(draft, listsOf(base));
  const rows = tabRows(tab, catalog, draft);
  const cursor = Math.min(cursors[tab], Math.max(0, rows.length - 1));
  const row: SecurityRow | undefined = onHead ? undefined : rows[cursor];
  const say = (text: string, color?: string) => setNotes([{ text, color }]);
  const moveTo = (index: number) => setCursors((current) => ({ ...current, [tab]: index }));

  const confirm = (lines: string[], yes: () => void, neutral = false) => {
    setNotes([]);
    setMode({ k: "confirm", lines, yes, neutral });
  };
  const apply = (next: Lists, nextCursor?: number) => {
    setDraft(next);
    setNotes([]);
    setMode({ k: "browse" });
    if (nextCursor !== undefined) moveTo(Math.max(0, nextCursor));
  };

  const locked = (): boolean => {
    if (readOnly === null) return false;
    say(readOnly, "yellow");
    return true;
  };

  /** Take the daemon's lists as they are now, dropping unsaved edits. */
  const reload = (quiet = false) => {
    if (!onReload) return;
    onReload()
      .then((next) => {
        setBase(next);
        setDraft(listsOf(next));
        setEntryProblems({ commands: new Map(), paths: new Map() });
        setElsewhere(false);
        setReadOnly(next.writable ? null : READ_ONLY_LINE);
        if (!quiet) say("Reloaded the lists from snowpea.", "green");
      })
      .catch((error: unknown) => say(`Could not reload: ${String((error as Error)?.message ?? error)}`, "red"));
  };

  // The lists changed on the daemon (another surface, the file): follow them
  // while nothing is being edited, otherwise say so and wait for `r`.
  const live = useRef({ dirty, mode });
  live.current = { dirty, mode };
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    if (changeSeq === 0 || live.current.mode.k === "saving") return;
    if (live.current.dirty) setElsewhere(true);
    else reloadRef.current(true);
  }, [changeSeq]);

  const save = () => {
    if (locked()) return;
    if (!dirty) {
      say("Nothing to save.");
      return;
    }
    setMode({ k: "saving" });
    setNotes([{ text: "Saving…", dim: true }]);
    onSave(draft)
      .then((result) => {
        setMode({ k: "browse" });
        if (result.ok) {
          setBase(result.config);
          setDraft(listsOf(result.config));
          setEntryProblems({ commands: new Map(), paths: new Map() });
          setElsewhere(false);
          say("Saved. The new lists apply to new commands at once.", "green");
          return;
        }
        if (result.readOnly) {
          setReadOnly(result.readOnly);
          setDraft(listsOf(base));
          say(result.readOnly, "yellow");
          return;
        }
        // Core refused an entry: its reason goes on that row, the rest as a line.
        if (result.entries && result.entries.length > 0) {
          const next: Record<Tab, Map<string, string>> = { commands: new Map(), paths: new Map() };
          for (const entry of result.entries) next[entry.tab].set(entry.text, entry.reason);
          setEntryProblems(next);
          const first = result.entries.find((entry) => draft && (entry.tab === "commands" ? draft.commandCustom.includes(entry.text) : draft.pathCustom.some((one) => one.path === entry.text)));
          if (first) setTab(first.tab);
        }
        const rest = result.others && result.others.length > 0 ? ` ${result.others.join("; ")}` : "";
        const count = result.entries?.length ?? 0;
        if (count > 0) {
          setNotes([
            { text: `Not saved: ${count === 1 ? "an entry was" : `${count} entries were`} refused (marked ✗).${rest}`, color: "red" },
            { text: "Press ? for the pattern syntax.", dim: true },
          ]);
        } else {
          say(`Not saved: ${result.message}`, "red");
        }
      })
      .catch((error: unknown) => {
        setMode({ k: "browse" });
        say(`Not saved: ${String((error as Error)?.message ?? error)}`, "red");
      });
  };

  /** Set `security.unknownCode` (`u`: the next value round); letting code through asks first. */
  const switchUnknownCode = (target?: UnknownCode) => {
    if (!onSetUnknownCode || locked()) return;
    const next: UnknownCode = target ?? nextUnknownCode(base.unknownCode, !noAuto);
    if (next === base.unknownCode) return;
    const run = () => {
      setMode({ k: "saving" });
      setNotes([{ text: "Saving…", dim: true }]);
      void onSetUnknownCode(next).then((result) => {
        setMode({ k: "browse" });
        if (result.ok) {
          setBase((current) => ({ ...current, unknownCode: result.config.unknownCode }));
          setNotes([{ text: unknownCodeMeaning(result.config.unknownCode), color: "green" }]);
        } else if (result.autoRefused) {
          setNoAuto(true);
          say(OLD_DAEMON_LINE(), "yellow");
        } else if (result.readOnly) {
          setReadOnly(result.readOnly);
          say(result.readOnly, "yellow");
        } else {
          say(`Not saved: ${result.message}`, "red");
        }
      });
    };
    if (next === "allow") confirm([...UNKNOWN_CODE_ALLOW_CONFIRM], run);
    else run();
  };

  /** Flip `security.enabled`: on at once, off after one question naming what stops applying. */
  const switchEnabled = () => {
    if (!onSetEnabled || locked()) return;
    const next = !base.enabled;
    const run = () => {
      setMode({ k: "saving" });
      setNotes([{ text: "Saving…", dim: true }]);
      void onSetEnabled(next).then((result) => {
        setMode({ k: "browse" });
        if (result.ok) {
          // The lists did not change; unsaved edits stay as they are.
          setBase(result.config);
          setNotes([{ text: ENABLED_NOW(result.config.enabled) }]);
        } else if (result.readOnly) {
          setReadOnly(result.readOnly);
          say(result.readOnly, "yellow");
        } else {
          say(`Not saved: ${result.message}`, "red");
        }
      });
    };
    if (next) run();
    else confirm(offConfirmLines(tokensWhenOff), run, true);
  };

  const toggle = () => {
    if (!row || row.kind !== "preset" || locked()) return;
    const next = togglePreset(draft, tab, row.id);
    const loss = row.on ? weakening(tab, catalog, draft, next) : null;
    if (loss) confirm([`Turn off "${row.title}"?`, loss, "Turn it off anyway? (y/n)"], () => apply(next));
    else apply(next);
  };

  const remove = () => {
    if (!row || row.kind !== "custom" || locked()) return;
    const next = removeCustom(draft, tab, row.index);
    const loss = weakening(tab, catalog, draft, next, row.text);
    confirm([`Delete ${row.text}?`, ...(loss ? [loss] : []), "Delete it? (y/n)"], () => apply(next, cursor - 1));
  };

  const submitInput = (current: Extract<Mode, { k: "input" }>) => {
    const value = current.value.trim();
    if (value === "") {
      say("Type something first, or press Esc to cancel.", "red");
      return;
    }
    if (current.action === "check") {
      setNotes([{ text: "Checking… (nothing is run)", dim: true }]);
      setMode({ k: "browse" });
      // Unsaved edits are judged as they stand: that is what "try it" is for.
      onCheck(tab === "commands" ? "command" : "path", value, dirty ? draft : null)
        .then((result) => {
          if ("error" in result) {
            say(`Could not check: ${result.error}`, "red");
            return;
          }
          const verdict = verdictLine(result, catalog);
          setNotes([
            { text: verdict.text, color: verdict.kind === "blocked" ? "red" : verdict.kind === "opaque" ? "yellow" : "green" },
            { text: dirty ? "Judged against your unsaved edits." : "Judged against the saved lists.", dim: true },
            { text: NOTHING_RUN, dim: true },
          ]);
        })
        .catch((error: unknown) => say(`Could not check: ${String((error as Error)?.message ?? error)}`, "red"));
      return;
    }
    const except = current.action === "edit" ? current.index : -1;
    if (hasCustom(draft, tab, value, except)) {
      say("That entry is already in the list.", "red");
      return;
    }
    if (current.action === "add") {
      apply(addCustom(draft, tab, value), rows.length);
    } else if (row && row.kind === "custom") {
      apply(editCustom(draft, tab, row.index, value));
    }
  };

  useInput(
    (typed, key) => {
      if (mouseReports(typed).length > 0) return;
      if (mode.k === "saving") return;
      // A text field keeps the Hangul typed into it; everywhere else a lone
      // jamo (ㅕ under a Korean layout) is the letter key it sits on (u).
      const input = mode.k === "input" ? typed : shortcutKey(typed);

      if (mode.k === "confirm") {
        if (input.toLowerCase() === "y") mode.yes();
        else if (input.toLowerCase() === "n" || key.escape) setMode({ k: "browse" });
        return;
      }

      if (mode.k === "input") {
        if (key.escape) {
          setMode({ k: "browse" });
          setNotes([]);
        } else if (key.return) submitInput(mode);
        else if (key.backspace || key.delete) setMode({ ...mode, value: Array.from(mode.value).slice(0, -1).join("") });
        else if (input && !key.ctrl && !key.meta && !key.tab && !key.upArrow && !key.downArrow) {
          setMode({ ...mode, value: mode.value + input.replace(/[\r\n]+/g, " ") });
        }
        return;
      }

      if (mode.k === "help") {
        if (key.escape || input === "?" || input === "q" || key.return) setMode({ k: "browse" });
        else if (key.upArrow || input === "k") setHelpTop((at) => Math.max(0, at - 1));
        else if (key.downArrow || input === "j") setHelpTop((at) => at + 1);
        return;
      }

      if (mode.k === "effective") {
        const count = Math.max(1, tab === "commands" ? base.effective.commands.length : base.effective.paths.length);
        if (key.escape || input === "v" || input === "q") setMode({ k: "browse" });
        else if (key.upArrow || input === "k") setEffCursor((at) => Math.max(0, at - 1));
        else if (key.downArrow || input === "j") setEffCursor((at) => Math.min(count - 1, at + 1));
        else if (key.tab) {
          setTab(tab === "commands" ? "paths" : "commands");
          setEffCursor(0);
        }
        return;
      }

      // browse
      if (key.escape) {
        if (dirty) confirm(["Discard your unsaved changes?", "Discard them? (y/n)"], onClose);
        else onClose();
        return;
      }
      if (masterHere && (key.leftArrow || key.rightArrow)) return switchEnabled();
      if (switchHere && (key.leftArrow || key.rightArrow)) {
        // On the unknown-code row ←→ picks the value; tabs stay where they are.
        return switchUnknownCode(stepUnknownCode(base.unknownCode, key.leftArrow ? -1 : 1, !noAuto));
      }
      if (key.tab || key.leftArrow || key.rightArrow) {
        setTab(tab === "commands" ? "paths" : "commands");
        setNotes([]);
        return;
      }
      // The switch row, then the unknown-code row, then the list's rows; ↑↓ wrap round.
      if (key.upArrow || input === "k") {
        if (onHead) {
          const index = heads.indexOf(at as HeadRow);
          if (index > 0) setHeadRow(heads[index - 1]);
          else {
            setHeadRow(null);
            if (rows.length > 0) moveTo(rows.length - 1);
          }
        } else if (heads.length > 0 && (cursor === 0 || rows.length === 0)) setHeadRow(heads[heads.length - 1]);
        else if (rows.length > 0) moveTo((cursor + rows.length - 1) % rows.length);
        return;
      }
      if (key.downArrow || input === "j") {
        if (onHead) {
          const index = heads.indexOf(at as HeadRow);
          if (index < heads.length - 1) setHeadRow(heads[index + 1]);
          else {
            setHeadRow(null);
            moveTo(0);
          }
        } else if (heads.length > 0 && cursor >= rows.length - 1) setHeadRow(heads[0]);
        else if (rows.length > 0) moveTo((cursor + 1) % rows.length);
        return;
      }
      if (key.pageUp) {
        if (!onHead && heads.length > 0 && cursor === 0) return setHeadRow(heads[heads.length - 1]);
        return moveTo(Math.max(0, cursor - 5));
      }
      if (key.pageDown) {
        setHeadRow(null);
        return moveTo(Math.min(Math.max(0, rows.length - 1), onHead ? 0 : cursor + 5));
      }
      if (masterHere && (input === " " || key.return)) return switchEnabled();
      if (switchHere && (input === " " || key.return)) return switchUnknownCode();
      if (input === " ") return toggle();
      switch (input.toLowerCase()) {
        case "a":
          if (locked()) return;
          setNotes([]);
          setMode({ k: "input", action: "add", value: "" });
          return;
        case "e":
          if (!row || row.kind !== "custom") {
            say("Only your own entries can be edited; presets are switched on or off with Space.");
            return;
          }
          if (locked()) return;
          setNotes([]);
          setMode({ k: "input", action: "edit", value: row.text, index: row.index });
          return;
        case "d":
          if (!row || row.kind !== "custom") {
            say("Only your own entries can be deleted; turn a preset off with Space.");
            return;
          }
          return remove();
        case "l":
          if (tab !== "paths") return;
          if (!row || row.kind !== "custom") {
            say("A preset has its own level; set one for your own folders.");
            return;
          }
          if (locked()) return;
          return apply(cycleLevel(draft, row.index));
        case "u":
          return switchUnknownCode();
        case "v":
          setNotes([]);
          setEffCursor(0);
          setMode({ k: "effective" });
          return;
        case "c":
          setNotes([]);
          setMode({ k: "input", action: "check", value: "" });
          return;
        case "?":
          setMode({ k: "help" });
          return;
        case "r":
          if (dirty) confirm(["Reload the lists and drop your unsaved changes?", "Reload them? (y/n)"], () => { setMode({ k: "browse" }); reload(); });
          else reload();
          return;
        case "s":
          return save();
        default:
      }
    },
    { isActive },
  );

  // --- drawing -----------------------------------------------------------------

  const inner = Math.max(20, width - 4);
  const rowsLeft = Math.max(4, height - 2);
  const bold = (text: string, color?: string): Segment => ({ text, bold: true, color });

  const titleLeft = "Security lists";
  const titleMid = dirty ? "  ● unsaved changes" : "";
  const counter = mode.k === "effective" || onHead ? "" : rows.length > 0 ? `${cursor + 1}/${rows.length}` : "";
  const gap = Math.max(1, inner - textWidth(titleLeft + titleMid) - textWidth(counter));
  const head: Line[] = [
    {
      key: "title",
      segments: [bold(titleLeft, "cyan"), { text: titleMid, color: "yellow", bold: true }, { text: " ".repeat(gap) + counter, dimColor: true }],
    },
    {
      key: "tabs",
      segments: (["commands", "paths"] as Tab[]).flatMap((one, index): Segment[] => [
        { text: index === 0 ? "" : "  " },
        one === tab ? { text: `[${TAB_TITLE[one]}]`, bold: true, color: "cyan" } : { text: ` ${TAB_TITLE[one]} `, dimColor: true },
      ]),
    },
    ...(hasMaster ? wrapSegments("master", [{ text: masterHere ? "› " : "  ", color: "cyan", bold: true }, ...enabledSegments(base)], inner, "    ") : []),
    ...wrapSegments(
      "unknown",
      [{ text: switchHere ? "› " : "  ", color: "cyan", bold: true }, ...(off ? dimmed : (segments: Segment[]) => segments)(unknownCodeSegments(base.unknownCode, !noAuto))],
      inner,
      "    ",
    ),
    // While the checks are off the rows below are dimmed; one plain line says why.
    ...(off ? wrapSegments("off", [{ text: OFF_NOTE() }], inner, "") : []),
  ];
  const intro: Line[] = [    ...wrapSegments("intro", [{ text: INTRO[tab], dimColor: true }], inner, ""),
    ...wrapSegments("limits", [{ text: SECURITY_LIMITS, dimColor: true }], inner, ""),
    ...wrapSegments("own", [{ text: OWN_FILES_LINE, dimColor: true }], inner, ""),
    ...wrapSegments("claim", [{ text: SECURITY_CLAIM(), ...(off ? { dimColor: true } : { color: "yellow" }) }], inner, ""),
  ];
  const skipped = problemNote(base.problems);
  const lock: Line[] = [
    ...(diskNotice ? wrapSegments("disk", [{ text: `! ${diskNotice}`, color: "yellow", bold: true }], inner, "  ") : []),
    ...(readOnly ? wrapSegments("lock", [{ text: readOnly, color: "yellow", bold: true }], inner, "") : []),
    ...(elsewhere ? wrapSegments("elsewhere", [{ text: "The lists changed elsewhere — r to reload (drops your unsaved changes).", color: "yellow", bold: true }], inner, "") : []),
    ...(skipped ? wrapSegments("skipped", [{ text: `! ${skipped}`, color: "yellow" }], inner, "  ") : []),
    ...base.notices.flatMap((notice, index) => wrapSegments(`notice-${index}`, [{ text: `! ${notice}`, color: "yellow" }], inner, "  ")),
  ];

  const foot: Line[] = [];
  notes.forEach((note, index) => {
    foot.push(...wrapSegments(`note-${index}`, [{ text: note.text, color: note.color, dimColor: note.dim, bold: note.color === "red" }], inner, "  "));
  });
  if (mode.k === "confirm") {
    mode.lines.forEach((text, index) => foot.push(...wrapSegments(`ask-${index}`, [{ text, color: mode.neutral ? undefined : "yellow", bold: index === mode.lines.length - 1 }], inner, "  ")));
  }
  if (mode.k === "input") {
    const prompt =
      mode.action === "check"
        ? tab === "commands"
          ? "Command to check (it is NOT run):"
          : "Folder or file to check (nothing is touched):"
        : mode.action === "add"
          ? tab === "commands"
            ? "Command pattern to block:"
            : "Folder to protect (absolute path):"
          : "Edit entry:";
    foot.push(...wrapSegments("prompt", [{ text: prompt, color: "cyan" }], inner, ""));
    foot.push(...wrapSegments("typed", [{ text: "› ", color: "cyan", bold: true }, { text: mode.value }, { text: "▏", color: "cyan" }], inner, "  "));
  }
  const hints =
    mode.k === "confirm"
      ? "y yes · n / Esc no"
      : mode.k === "input"
        ? "Enter " + (mode.action === "check" ? "check" : "accept") + " · Esc cancel"
        : mode.k === "help"
        ? "↑↓ scroll · Esc back"
        : mode.k === "effective"
          ? "↑↓ scroll · Tab other list · Esc back"
          : mode.k === "saving"
            ? "saving…"
            : onHead
              ? [
                  "↑↓ move",
                  readOnly ? null : masterHere ? "←→ / Space turn on or off" : "←→ / Space change",
                  "Tab other list",
                  "? help",
                  "Esc close",
                ]
                  .filter(Boolean)
                  .join(" · ")
            : [
                "↑↓ move",
                readOnly ? null : "Space toggle",
                readOnly ? null : "a add · e edit · d delete",
                readOnly || tab !== "paths" ? null : "l level",
                readOnly ? null : "u unreadable code",
                "v effective",
                "c check",
                "r reload",
                "? help",
                readOnly ? null : "s save",
                "←→ / Tab other list",
                "Esc close",
              ]
                .filter(Boolean)
                .join(" · ");
  const hint = wrapSegments("hint", [{ text: hints, dimColor: true }], inner, "");

  // The body takes what the head, the messages and the hint leave over; the
  // intro goes first when the terminal is short.
  let top = [...head, ...lock];
  const bodyRoom = (extra: Line[]) => rowsLeft - top.length - extra.length - foot.length - hint.length;
  let shownIntro = intro;
  if (bodyRoom(shownIntro) < 5) shownIntro = shownIntro.slice(0, 1);
  if (bodyRoom(shownIntro) < 4) shownIntro = [];
  top = [...head, ...shownIntro, ...lock];
  const bodyHeight = Math.max(2, rowsLeft - top.length - foot.length - hint.length);

  let body: Line[];
  let start = 0;
  let end = 0;
  let key: string = tab;
  if (mode.k === "help") {
    const lines: Line[] = [...(hasMaster ? switchHelp(tokensWhenOff) : []), ...PATTERN_HELP].flatMap((entry, at) =>
      wrapSegments(
        `help-${at}`,
        [entry.kind === "head" ? { text: entry.text, bold: true, color: "cyan" } : entry.kind === "example" ? { text: entry.text, color: "green" } : { text: entry.text }],
        inner,
        entry.kind === "example" ? "                     " : "  ",
      ),
    );
    const offset = Math.max(0, Math.min(helpTop, Math.max(0, lines.length - bodyHeight)));
    body = lines.slice(offset, offset + bodyHeight);
  } else if (mode.k === "effective") {
    const lines = effectiveLines(tab, base, catalog, inner);
    const heading: Line[] = [
      ...wrapSegments("eff-head", [{ text: `In force now: ${TAB_TITLE[tab]}`, bold: true }, { text: dirty ? "  (saved lists; unsaved changes are not included)" : "  (as saved)", dimColor: true }], inner, "  "),
    ];
    const window = Math.max(1, bodyHeight - heading.length);
    // One entry per line group: put the cursor on the line the entry starts at.
    const starts = lines.map((line, at) => (/-w[1-9]\d*$/.test(line.key) ? -1 : at)).filter((at) => at >= 0);
    const at = Math.min(effCursor, Math.max(0, starts.length - 1));
    start = starts[at] ?? 0;
    end = (starts[at + 1] ?? lines.length);
    key = `eff-${tab}`;
    const offset = scrollInto(scroll.current[key] ?? 0, start, end, window, lines.length);
    scroll.current[key] = offset;
    body = [...heading, ...lines.slice(offset, offset + window)];
  } else {
    const built = bodyLines(rows, onHead ? -1 : cursor, inner, entryProblems[tab]);
    start = built.start;
    end = built.end;
    const offset = scrollInto(scroll.current[key] ?? 0, start, end, bodyHeight, built.lines.length);
    scroll.current[key] = offset;
    body = built.lines.slice(offset, offset + bodyHeight);
    if (off) body = body.map((line) => ({ ...line, segments: dimmed(line.segments) }));
  }

  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor={dirty ? "yellow" : "cyan"} paddingX={1} overflow="hidden">
      <RenderedLines lines={top} />
      <Box flexDirection="column" flexGrow={1} overflow="hidden">
        <RenderedLines lines={body} />
      </Box>
      <RenderedLines lines={foot} />
      <RenderedLines lines={hint} />
    </Box>
  );
}
