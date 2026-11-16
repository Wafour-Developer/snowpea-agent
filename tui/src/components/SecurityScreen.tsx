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

import React, { useRef, useState } from "react";
import { Box, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";
import { textWidth } from "../layout/text-width.js";
import type { Line, Segment } from "../layout/transcript.js";
import {
  NOTHING_RUN,
  SECURITY_LIMITS,
  addCustom,
  bodyLines,
  cycleLevel,
  editCustom,
  effectiveLines,
  hasCustom,
  listsOf,
  removeCustom,
  sameLists,
  scrollInto,
  tabRows,
  togglePreset,
  verdictLine,
  weakening,
  wrapSegments,
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
  | { ok: false; message: string; readOnly?: string };

export type CheckResult = SecurityVerdict | { error: string };

type Mode =
  | { k: "browse" }
  | { k: "input"; action: "add" | "edit" | "check"; value: string; index?: number }
  | { k: "confirm"; lines: string[]; yes: () => void }
  | { k: "effective" }
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
  /** `security.check`: judges text, never runs it. */
  onCheck: (what: "command" | "path", text: string) => Promise<CheckResult>;
  onClose: () => void;
  isActive?: boolean;
}

const TAB_TITLE: Record<Tab, string> = { commands: "Blocked commands", paths: "Protected folders" };

const INTRO: Record<Tab, string> = {
  commands: "These commands are never run: not asked, not approvable, not handed to Jarvis.",
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
  const scroll = useRef<Record<string, number>>({});

  const dirty = !sameLists(draft, listsOf(base));
  const rows = tabRows(tab, catalog, draft);
  const cursor = Math.min(cursors[tab], Math.max(0, rows.length - 1));
  const row: SecurityRow | undefined = rows[cursor];
  const say = (text: string, color?: string) => setNotes([{ text, color }]);
  const moveTo = (index: number) => setCursors((current) => ({ ...current, [tab]: index }));

  const confirm = (lines: string[], yes: () => void) => {
    setNotes([]);
    setMode({ k: "confirm", lines, yes });
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
          say("Saved. The new lists apply to new commands at once.", "green");
          return;
        }
        if (result.readOnly) {
          setReadOnly(result.readOnly);
          setDraft(listsOf(base));
          say(result.readOnly, "yellow");
          return;
        }
        say(`Not saved: ${result.message}`, "red");
      })
      .catch((error: unknown) => {
        setMode({ k: "browse" });
        say(`Not saved: ${String((error as Error)?.message ?? error)}`, "red");
      });
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
      onCheck(tab === "commands" ? "command" : "path", value)
        .then((result) => {
          if ("error" in result) {
            say(`Could not check: ${result.error}`, "red");
            return;
          }
          const verdict = verdictLine(result, catalog);
          setNotes([
            { text: verdict.text, color: verdict.blocked ? "red" : "green" },
            ...(dirty ? [{ text: "Judged against the saved lists; unsaved changes are not included.", dim: true }] : []),
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
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (mode.k === "saving") return;

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
      if (key.tab || key.leftArrow || key.rightArrow) {
        setTab(tab === "commands" ? "paths" : "commands");
        setNotes([]);
        return;
      }
      if (key.upArrow || input === "k") {
        if (rows.length > 0) moveTo((cursor + rows.length - 1) % rows.length);
        return;
      }
      if (key.downArrow || input === "j") {
        if (rows.length > 0) moveTo((cursor + 1) % rows.length);
        return;
      }
      if (key.pageUp) return moveTo(Math.max(0, cursor - 5));
      if (key.pageDown) return moveTo(Math.min(Math.max(0, rows.length - 1), cursor + 5));
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
        case "v":
          setNotes([]);
          setEffCursor(0);
          setMode({ k: "effective" });
          return;
        case "c":
          setNotes([]);
          setMode({ k: "input", action: "check", value: "" });
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
  const counter = mode.k === "effective" ? "" : rows.length > 0 ? `${cursor + 1}/${rows.length}` : "";
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
  ];
  const intro: Line[] = [
    ...wrapSegments("intro", [{ text: INTRO[tab], dimColor: true }], inner, ""),
    ...wrapSegments("limits", [{ text: SECURITY_LIMITS, dimColor: true }], inner, ""),
  ];
  const lock: Line[] = readOnly ? wrapSegments("lock", [{ text: readOnly, color: "yellow", bold: true }], inner, "") : [];

  const foot: Line[] = [];
  notes.forEach((note, index) => {
    foot.push(...wrapSegments(`note-${index}`, [{ text: note.text, color: note.color, dimColor: note.dim, bold: note.color === "red" }], inner, "  "));
  });
  if (mode.k === "confirm") {
    mode.lines.forEach((text, index) => foot.push(...wrapSegments(`ask-${index}`, [{ text, color: "yellow", bold: index === mode.lines.length - 1 }], inner, "  ")));
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
        : mode.k === "effective"
          ? "↑↓ scroll · Tab other list · Esc back"
          : mode.k === "saving"
            ? "saving…"
            : [
                "↑↓ move",
                readOnly ? null : "Space toggle",
                readOnly ? null : "a add · e edit · d delete",
                readOnly || tab !== "paths" ? null : "l level",
                "v effective",
                "c check",
                readOnly ? null : "s save",
                "Tab other list",
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
  if (mode.k === "effective") {
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
    const built = bodyLines(rows, cursor, inner);
    start = built.start;
    end = built.end;
    const offset = scrollInto(scroll.current[key] ?? 0, start, end, bodyHeight, built.lines.length);
    scroll.current[key] = offset;
    body = built.lines.slice(offset, offset + bodyHeight);
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
