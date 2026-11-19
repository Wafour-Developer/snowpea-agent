/**
 * Security lists (capability `securityLists`): commands that never run and
 * folders that are protected, chosen from presets plus manual entries.
 *
 * The wire shapes are the SDK's (`SecurityGetResult`, `SecurityPresetsListResult`,
 * `SecurityCheckResult`, `SecurityBlockedPayload`); the functions below turn
 * them into the plain, sanitised values the screen draws. Everything here is
 * pure: the screen, the slash command and the tests all read the same functions.
 */

import type { SecurityBlocked, SecurityCheck, SecurityCheckDraft, SecurityGet, SecurityPresets } from "../rpc/sdk.js";
import { sanitizeText } from "../util/sanitize.js";
import { fit } from "../layout/fit.js";
import { wrapLine, type Line, type Segment } from "../layout/transcript.js";
import { say } from "./errors.js";
import { rpcErrorFacts } from "./external.js";

// --- view models (what the screen keeps of the contract) ----------------------

export type PathLevel = "write" | "all";

export interface Preset {
  id: string;
  title: string;
  description: string;
  examples: string[];
  default: boolean;
}

export type PathPreset = Preset & { level: PathLevel; paths: string[] };

/** `security.presets.list { locale }`, normalised. */
export interface PresetCatalog {
  commands: Preset[];
  paths: PathPreset[];
  locale: string;
}

export interface CustomPath {
  path: string;
  level: PathLevel;
}

/** One protected path in force: `scope` "self" is the folder itself only, `exempt` the folders below it that stay usable. */
export interface EffectivePath {
  path: string;
  level: PathLevel;
  by: string;
  scope: "tree" | "self";
  exempt: string[];
}

/** `ask` always asks; `auto` (default since 0.2.90) skips it in auto mode; `allow` never asks. */
export type UnknownCode = "ask" | "auto" | "allow";

/** One entry core refused or skipped: which list it was in, which entry, and why. */
export interface ProblemItem {
  list: Tab | null;
  entry: string | null;
  reason: string;
}

/** A missing value is an older core's "ask"; a value this TUI does not know is shown as the default, "auto". */
export function readUnknownCode(value: unknown): UnknownCode {
  if (value === "ask" || value === "allow" || value === "auto") return value;
  return value === undefined || value === null ? "ask" : "auto";
}

/** `security.get`, normalised. */
export interface SecurityConfig {
  blockedCommands: { presets: string[]; custom: string[] };
  protectedPaths: { presets: string[]; custom: CustomPath[] };
  effective: {
    commands: Array<{ pattern: string; by: string }>;
    paths: EffectivePath[];
  };
  /** False for a connection that may not change the lists (a remote login, a browser, a connector). */
  writable: boolean;
  /** Entries core skipped as unusable (a hand-edited file), in words. */
  problems: string[];
  /** The same, structured (1.55.0 `problemItems`); empty from an older core. */
  problemItems: ProblemItem[];
  /**
   * Other sentences core puts in `problems` (1.55.0): a lost trust record, a tamper it undid,
   * settings waiting on disk. Told as core says them, never as "skipped entries".
   */
  notices: string[];
  /** What happens to code the check cannot resolve (1.55.0): put to the person, or let through. */
  unknownCode: UnknownCode;
  /** The project's own additions, shown read-only as a count. */
  projectAdds: number;
}

/** `security.check`, normalised: blocked, opaque (cannot be seen, snowpea asks) or neither. */
export interface SecurityVerdict {
  blocked: boolean;
  opaque: boolean;
  by: string | null;
  why: string;
  level?: PathLevel;
  match?: string;
}

/** Notification `security.blocked`. */
export interface SecurityBlockedEvent {
  sessionId: string;
  tool: string;
  summary: string;
  by: string;
}

// --- capability gating ---------------------------------------------------------

export function securitySupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("securityLists");
}

/** The one line an older daemon gets instead of the feature. */
export const SECURITY_REQUIRES_LINE = "Security lists: requires a newer core — this daemon is older. Run /update.";

/** The palette summary of `/security` on an older daemon. */
export const SECURITY_REQUIRES_SUMMARY = "requires a newer core — this daemon is older (/update)";

// --- normalising what the daemon sent -----------------------------------------

const text = (value: unknown): string => (typeof value === "string" ? sanitizeText(value) : "");
const texts = (value: unknown): string[] => (Array.isArray(value) ? value.map(text).filter((item) => item !== "") : []);
const level = (value: unknown): PathLevel => (value === "all" ? "all" : "write");

function preset(raw: any): Preset {
  return {
    id: text(raw?.id),
    title: text(raw?.title) || text(raw?.id),
    description: text(raw?.description),
    examples: texts(raw?.examples),
    default: raw?.default === true,
  };
}

export function normalizeCatalog(raw: SecurityPresets | null | undefined): PresetCatalog {
  return {
    commands: (Array.isArray(raw?.commands) ? raw.commands : []).map(preset).filter((entry: Preset) => entry.id !== ""),
    paths: (Array.isArray(raw?.paths) ? raw.paths : [])
      .map((entry: any) => ({ ...preset(entry), level: level(entry?.level), paths: texts(entry?.paths) }))
      .filter((entry: Preset) => entry.id !== ""),
    locale: text(raw?.locale),
  };
}

/** How many entries a project's additions hold. */
function additions(raw: any): number {
  if (!raw || typeof raw !== "object") return 0;
  return (
    (raw.blockedCommands?.presets?.length ?? 0) +
    (raw.blockedCommands?.custom?.length ?? 0) +
    (raw.protectedPaths?.presets?.length ?? 0) +
    (raw.protectedPaths?.custom?.length ?? 0)
  );
}

/** `problemItems` (1.55.0), normalised; an item with no reason is dropped. */
export function normalizeProblemItems(value: unknown): ProblemItem[] {
  return (Array.isArray(value) ? value : [])
    .map((item: any): ProblemItem => ({
      list: item?.list === "commands" || item?.list === "paths" ? item.list : null,
      entry: typeof item?.entry === "string" && item.entry !== "" ? sanitizeText(item.entry) : null,
      reason: text(item?.reason),
    }))
    .filter((item: ProblemItem) => item.reason !== "");
}

/** A problem item as a sentence: no parsing of core's English, only its fields. */
export function problemItemText(item: ProblemItem): string {
  const what = item.list === "commands" ? "blocked command" : item.list === "paths" ? "protected path" : "entry";
  return `${what}${item.entry ? ` ${JSON.stringify(item.entry)}` : ""}: ${item.reason}`;
}

export function normalizeConfig(raw: SecurityGet | null | undefined): SecurityConfig {
  const problemItems = normalizeProblemItems((raw as any)?.problemItems);
  const sentences = texts((raw as any)?.problems);
  // A core that sends `problemItems` (even an empty list) says in `problems` more than skipped entries:
  // a sentence that none of the items' reasons is part of is a notice of its own.
  const structured = Array.isArray((raw as any)?.problemItems);
  // A sentence about a linked credentials.json is core's text about a file, not a skipped entry: told as it is.
  const fileNotice = (sentence: string): boolean => /credentials\.json/i.test(sentence) && /symbolic link/i.test(sentence);
  const notices = structured
    ? sentences.filter((sentence) => !problemItems.some((item) => sentence.includes(item.reason)))
    : sentences.filter(fileNotice);
  return {
    writable: (raw as any)?.writable !== false,
    // The structured form wins; an older core sent only the sentences.
    problems: problemItems.length > 0 ? problemItems.map(problemItemText) : structured ? [] : sentences.filter((sentence) => !fileNotice(sentence)),
    problemItems,
    notices,
    unknownCode: readUnknownCode((raw as any)?.unknownCode),
    projectAdds: additions((raw as any)?.project),
    blockedCommands: {
      presets: texts(raw?.blockedCommands?.presets),
      custom: texts(raw?.blockedCommands?.custom),
    },
    protectedPaths: {
      presets: texts(raw?.protectedPaths?.presets),
      custom: (Array.isArray(raw?.protectedPaths?.custom) ? raw.protectedPaths.custom : [])
        .map((entry: any) => ({ path: text(entry?.path), level: level(entry?.level) }))
        .filter((entry: CustomPath) => entry.path !== ""),
    },
    effective: {
      commands: (Array.isArray(raw?.effective?.commands) ? raw.effective.commands : [])
        .map((entry: any) => ({ pattern: text(entry?.pattern), by: text(entry?.by) }))
        .filter((entry: { pattern: string }) => entry.pattern !== ""),
      paths: (Array.isArray(raw?.effective?.paths) ? raw.effective.paths : [])
        .map(
          (entry: any): EffectivePath => ({
            path: text(entry?.path),
            level: level(entry?.level),
            by: text(entry?.by),
            scope: entry?.scope === "self" ? "self" : "tree",
            exempt: texts(entry?.exempt),
          }),
        )
        .filter((entry: { path: string }) => entry.path !== ""),
    },
  };
}

export function normalizeVerdict(raw: SecurityCheck | null | undefined): SecurityVerdict {
  const reason = text(raw?.reason);
  const blocked = raw?.blocked === true;
  return {
    blocked,
    // A blocked verdict is never also opaque.
    opaque: !blocked && raw?.opaque === true,
    by: typeof raw?.by === "string" && raw.by !== "" ? sanitizeText(raw.by) : null,
    why: text(raw?.why) || reason,
    ...(raw?.level === "write" || raw?.level === "all" ? { level: raw.level as PathLevel } : {}),
    ...(typeof raw?.match === "string" && raw.match !== "" ? { match: sanitizeText(raw.match) } : {}),
  };
}

export function normalizeBlocked(raw: SecurityBlocked | null | undefined): SecurityBlockedEvent {
  return { sessionId: text(raw?.sessionId), tool: text(raw?.tool), summary: text(raw?.summary), by: text(raw?.by) };
}

/** True when any list is in force (the "lists are set" test of the session warnings). */
export function listsAreSet(config: SecurityConfig | null): boolean {
  return config !== null && (config.effective.commands.length > 0 || config.effective.paths.length > 0);
}

// --- the editable lists ----------------------------------------------------------

export type Tab = "commands" | "paths";

/** What the person edits: both tabs, saved together. */
export interface Lists {
  commandPresets: string[];
  commandCustom: string[];
  pathPresets: string[];
  pathCustom: CustomPath[];
}

export function listsOf(config: SecurityConfig): Lists {
  return {
    commandPresets: [...config.blockedCommands.presets],
    commandCustom: [...config.blockedCommands.custom],
    pathPresets: [...config.protectedPaths.presets],
    pathCustom: config.protectedPaths.custom.map((entry) => ({ ...entry })),
  };
}

export function sameLists(a: Lists, b: Lists): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** The `settings.set` patch: one call for both tabs. */
export function settingsPatch(lists: Lists): Record<string, unknown> {
  return {
    security: {
      blockedCommands: { presets: lists.commandPresets, custom: lists.commandCustom },
      protectedPaths: { presets: lists.pathPresets, custom: lists.pathCustom },
    },
  };
}

export const LEVEL_LABEL: Record<PathLevel, string> = { write: "no changes", all: "no access" };

export function nextLevel(current: PathLevel): PathLevel {
  return current === "write" ? "all" : "write";
}

/** A row of one tab: a preset (toggled with Space) or a manual entry. */
export type SecurityRow =
  | { kind: "preset"; id: string; title: string; description: string; examples: string[]; on: boolean; isDefault: boolean; level?: PathLevel; known: boolean }
  | { kind: "custom"; index: number; text: string; level?: PathLevel };

export function tabRows(tab: Tab, catalog: PresetCatalog, lists: Lists): SecurityRow[] {
  const chosen = tab === "commands" ? lists.commandPresets : lists.pathPresets;
  const entries: Array<Preset & { level?: PathLevel; paths?: string[] }> = tab === "commands" ? catalog.commands : catalog.paths;
  const rows: SecurityRow[] = entries.map((entry) => ({
    kind: "preset",
    id: entry.id,
    title: entry.title,
    description: entry.description,
    examples: tab === "paths" && (entry.paths?.length ?? 0) > 0 ? (entry.paths as string[]) : entry.examples,
    on: chosen.includes(entry.id),
    isDefault: entry.default,
    level: entry.level,
    known: true,
  }));
  // A preset the lists name but this core's catalog does not know stays visible, so it can be switched off.
  for (const id of chosen) {
    if (!entries.some((entry) => entry.id === id)) {
      rows.push({ kind: "preset", id, title: id, description: "not in this core's catalog", examples: [], on: true, isDefault: false, known: false });
    }
  }
  if (tab === "commands") {
    lists.commandCustom.forEach((pattern, index) => rows.push({ kind: "custom", index, text: pattern }));
  } else {
    lists.pathCustom.forEach((entry, index) => rows.push({ kind: "custom", index, text: entry.path, level: entry.level }));
  }
  return rows;
}

export function togglePreset(lists: Lists, tab: Tab, id: string): Lists {
  const key = tab === "commands" ? "commandPresets" : "pathPresets";
  const current = lists[key];
  return { ...lists, [key]: current.includes(id) ? current.filter((entry) => entry !== id) : [...current, id] };
}

export function addCustom(lists: Lists, tab: Tab, value: string): Lists {
  return tab === "commands"
    ? { ...lists, commandCustom: [...lists.commandCustom, value] }
    : { ...lists, pathCustom: [...lists.pathCustom, { path: value, level: "write" }] };
}

export function editCustom(lists: Lists, tab: Tab, index: number, value: string): Lists {
  return tab === "commands"
    ? { ...lists, commandCustom: lists.commandCustom.map((entry, at) => (at === index ? value : entry)) }
    : { ...lists, pathCustom: lists.pathCustom.map((entry, at) => (at === index ? { ...entry, path: value } : entry)) };
}

export function removeCustom(lists: Lists, tab: Tab, index: number): Lists {
  return tab === "commands"
    ? { ...lists, commandCustom: lists.commandCustom.filter((_, at) => at !== index) }
    : { ...lists, pathCustom: lists.pathCustom.filter((_, at) => at !== index) };
}

export function cycleLevel(lists: Lists, index: number): Lists {
  return { ...lists, pathCustom: lists.pathCustom.map((entry, at) => (at === index ? { ...entry, level: nextLevel(entry.level) } : entry)) };
}

/** True when `value` is already a manual entry of the tab (ignoring surrounding blanks). */
export function hasCustom(lists: Lists, tab: Tab, value: string, except = -1): boolean {
  const wanted = value.trim();
  const entries = tab === "commands" ? lists.commandCustom : lists.pathCustom.map((entry) => entry.path);
  return entries.some((entry, at) => at !== except && entry.trim() === wanted);
}

function tabEmpty(tab: Tab, lists: Lists): boolean {
  return tab === "commands"
    ? lists.commandPresets.length === 0 && lists.commandCustom.length === 0
    : lists.pathPresets.length === 0 && lists.pathCustom.length === 0;
}

const NOUN: Record<Tab, string> = { commands: "blocked", paths: "protected" };

/**
 * What stops being covered when `before` becomes `after`, in words, or null
 * when nothing is given up that deserves a question: a preset marked default
 * going off, or a tab ending up with nothing at all.
 */
export function weakening(tab: Tab, catalog: PresetCatalog, before: Lists, after: Lists, what?: string): string | null {
  const key = tab === "commands" ? "commandPresets" : "pathPresets";
  const entries: Array<Preset & { paths?: string[] }> = tab === "commands" ? catalog.commands : catalog.paths;
  const dropped = before[key].filter((id) => !after[key].includes(id));
  const loses = dropped
    .map((id) => entries.find((entry) => entry.id === id))
    .filter((entry): entry is Preset & { paths?: string[] } => entry !== undefined);
  const lines: string[] = [];
  for (const entry of loses.filter((one) => one.default)) {
    const sample = (tab === "paths" && entry.paths?.length ? entry.paths : entry.examples).slice(0, 3).join(", ");
    lines.push(`"${entry.title}" is a default list. ${sample ? `${sample} ` : "These "}will no longer be ${NOUN[tab]}.`);
  }
  if (lines.length === 0 && tabEmpty(tab, after) && !tabEmpty(tab, before)) {
    const gone = what ?? loses.map((entry) => entry.title).join(", ");
    lines.push(`${gone ? `${gone} will no longer be ${NOUN[tab]}. ` : ""}Nothing in this list would be ${NOUN[tab]} any more.`);
  }
  return lines.length > 0 ? lines.join(" ") : null;
}

// --- verdicts and status text ---------------------------------------------------

/**
 * What `by` names, in words: `commands:<id>` / `paths:<id>` give the preset's
 * title, `custom` is one of the person's own entries, `security` is the lists'
 * own protection (an edit of the security lists themselves).
 */
export function byLabel(by: string | null, catalog: PresetCatalog | null): string {
  if (by === null || by === "") return "your list";
  if (by === "custom") return "your own entry";
  if (by === "security") return "the security lists' own protection";
  const at = by.indexOf(":");
  const kind = at > 0 ? by.slice(0, at) : "";
  const id = at > 0 ? by.slice(at + 1) : by;
  const pool = kind === "commands" ? catalog?.commands : kind === "paths" ? catalog?.paths : [...(catalog?.commands ?? []), ...(catalog?.paths ?? [])];
  const found = (pool ?? []).find((entry) => entry.id === id);
  return found ? found.title : kind === "commands" || kind === "paths" ? id : by;
}

export const NOTHING_RUN = "Nothing was run — the text was only checked against your lists.";

/** What an opaque verdict says: not refused, but put to the person. */
export const OPAQUE_LINE = "snowpea cannot tell what this will touch before it runs, so it asks you";

export type VerdictKind = "blocked" | "opaque" | "ok";

/** The verdict as one line and a kind: BLOCKED (red), OPAQUE (yellow) or not blocked. */
export function verdictLine(verdict: SecurityVerdict, catalog: PresetCatalog | null): { text: string; kind: VerdictKind; blocked: boolean } {
  if (verdict.blocked) {
    const level = verdict.level ? ` (${LEVEL_LABEL[verdict.level]})` : "";
    const hit = verdict.match ? ` [${verdict.match}]` : "";
    return { text: `BLOCKED by ${byLabel(verdict.by, catalog)}${level}${hit}: ${verdict.why}`, kind: "blocked", blocked: true };
  }
  if (verdict.opaque) {
    return { text: `OPAQUE — ${OPAQUE_LINE}${verdict.why ? ` (${verdict.why})` : ""}`, kind: "opaque", blocked: false };
  }
  return { text: verdict.why ? `not blocked — ${verdict.why}` : "not blocked", kind: "ok", blocked: false };
}

/** The unsaved edits as the `draft` of a `security.check`. */
export function draftOf(lists: Lists): SecurityCheckDraft {
  return {
    blockedCommands: { presets: lists.commandPresets, custom: lists.commandCustom },
    protectedPaths: { presets: lists.pathPresets, custom: lists.pathCustom.map((entry) => ({ path: entry.path, level: entry.level })) },
  };
}

// --- what core says about an entry it refuses ------------------------------------

/** A custom entry core refused on save, with its reason (`details.problems`). */
export interface EntryProblem {
  tab: Tab;
  text: string;
  reason: string;
}

const PROBLEM = /^(blocked command|protected path) (?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"): ([\s\S]*)$/;
const unescapePython = (value: string): string => value.replace(/\\(.)/g, "$1");

/**
 * The refusal of a `settings.set` that carried a bad entry: the reasons core
 * listed (`details.problems`, else the message), split into the ones that name
 * one entry (shown on its row) and the rest (shown as they are).
 */
export function saveProblems(error: unknown): { entries: EntryProblem[]; others: string[] } {
  const raw = error as { message?: string; data?: { details?: { problems?: unknown; problemItems?: unknown } | null } } | null;
  const entries: EntryProblem[] = [];
  const others: string[] = [];
  // 1.55.0: core says which list and which entry in fields; nothing is read out of its English.
  const items = normalizeProblemItems(raw?.data?.details?.problemItems);
  if (items.length > 0) {
    for (const item of items) {
      if (item.list !== null && item.entry !== null) entries.push({ tab: item.list, text: item.entry, reason: item.reason });
      else others.push(problemItemText(item));
    }
    return { entries, others };
  }
  // An older core sent sentences only (`details.problems`).
  const listed = raw?.data?.details?.problems;
  const lines = Array.isArray(listed) && listed.length > 0 ? texts(listed) : [text(raw?.message ?? String(error))];
  for (const line of lines) {
    const match = PROBLEM.exec(line);
    if (!match) {
      others.push(line);
      continue;
    }
    entries.push({
      tab: match[1] === "blocked command" ? "commands" : "paths",
      text: unescapePython(match[2] ?? match[3] ?? ""),
      reason: match[4],
    });
  }
  return { entries, others };
}

/** The same split for the entries core skipped when it read a hand-edited file (`security.get.problems`). */
export function problemNote(problems: readonly string[]): string | null {
  if (problems.length === 0) return null;
  const first = problems[0];
  return problems.length === 1 ? `core skipped an unusable entry: ${first}` : `core skipped ${problems.length} unusable entries, e.g. ${first}`;
}

/** The pattern syntax in one screen (docs/manual/en/security.md, "Your own blocked commands"). */
export const PATTERN_HELP: ReadonlyArray<{ kind: "head" | "line" | "example"; text: string }> = [
  { kind: "head", text: "Custom command patterns" },
  { kind: "line", text: "One pattern per entry. It is matched against every command a line would run: through sudo, env, xargs, find -exec, inside sh -c '…', on both sides of pipes and &&." },
  { kind: "line", text: "The first word is the program, by its name (/usr/bin/rm is rm). It may use wildcards (mkfs*) but cannot be only a wildcard." },
  { kind: "line", text: "Options (words starting with -) must all be present, in any order. -rf means -r and -f, however written. --force also matches --forc and --force=…" },
  { kind: "line", text: "Other words must appear in the same order, not necessarily next to each other. * is any one word. A word starting with / or ~ is a path, compared after resolving it." },
  { kind: "line", text: "re:<regex> matches the command's text (its words joined by spaces). A regex that matches an empty command is refused." },
  { kind: "head", text: "Examples" },
  { kind: "example", text: "rm -rf               blocks rm -rf x, rm -r -f x, sudo rm --recursive --force x" },
  { kind: "example", text: "git push --force     blocks git push -f; not --force-with-lease" },
  { kind: "example", text: "docker * prune       blocks docker system prune, docker image prune -a" },
  { kind: "example", text: "mkfs*                blocks mkfs.ext4 /dev/sdb1" },
  { kind: "example", text: "re:^ssh .*prod       blocks ssh deploy@prod.example" },
  { kind: "head", text: "Protected folders" },
  { kind: "line", text: "An absolute path (/srv/backups) or one under your home (~/finance), with a level: no changes (reading is fine) or no access (not read, not listed, not changed). Everything below it is protected too." },
  { kind: "head", text: "Keys" },
  { kind: "line", text: "↑↓ move through the rows. The first row is \"Commands and scripts snowpea can't read before they run\": on it, ←→ (or Space) chooses ask / auto / allow, saved at once; u changes it from any row." },
  { kind: "line", text: "On a list row, ←→ or Tab switches between Blocked commands and Protected folders; Space turns a preset on or off." },
];

// --- code snowpea cannot read (1.55.0 `security.unknownCode`) --------------------------

export const UNKNOWN_CODE_LABEL = "Commands and scripts snowpea can't read before they run";
const unknownCodeLabel = (): string => say(UNKNOWN_CODE_LABEL, "실행 전에 알 수 없는 명령·스크립트");

/** The value as the person reads it. */
export function unknownCodeName(value: UnknownCode): string {
  if (value === "ask") return say("ask", "항상 묻기");
  if (value === "auto") return say("auto", "auto 모드에서는 묻지 않음 (기본)");
  return say("allow", "어떤 모드에서도 묻지 않음");
}

/** The next value on `u`: ask, auto, allow, and round; two-way when the daemon refused "auto". */
export function nextUnknownCode(value: UnknownCode, withAuto = true): UnknownCode {
  if (!withAuto) return value === "ask" ? "allow" : "ask";
  return value === "ask" ? "auto" : value === "auto" ? "allow" : "ask";
}

/**
 * ←/→ on the selected unknown-code row: one step along ask / auto / allow (as
 * drawn, left to right), stopping at either end; two-way without "auto".
 */
export function stepUnknownCode(value: UnknownCode, step: -1 | 1, withAuto = true): UnknownCode {
  const choices: UnknownCode[] = withAuto ? ["ask", "auto", "allow"] : ["ask", "allow"];
  const at = Math.max(0, choices.indexOf(value));
  return choices[Math.min(choices.length - 1, Math.max(0, at + step))];
}

/** One line saying snowpea's own files are protected whichever way the switch is set. */
export const OWN_FILES_LINE = "snowpea's own files stay protected either way, and anything on your block lists stays blocked.";

/** `ask` always asks; `auto` skips it in auto mode; `allow` never asks. `withAuto` false: a daemon that only knows ask/allow. */
export function unknownCodeSegments(value: UnknownCode, withAuto = true): Segment[] {
  const pick = (one: UnknownCode): Segment =>
    one === value
      ? { text: `[${unknownCodeName(one)}]`, bold: true, color: one === "allow" ? "yellow" : "green" }
      : { text: unknownCodeName(one), dimColor: true };
  const choices: UnknownCode[] = withAuto ? ["ask", "auto", "allow"] : ["ask", "allow"];
  return [
    { text: `${unknownCodeLabel()}: ` },
    ...choices.flatMap((one, index): Segment[] => (index === 0 ? [pick(one)] : [{ text: " / " }, pick(one)])),
  ];
}

export function unknownCodeMeaning(value: UnknownCode): string {
  if (value === "ask") {
    return say(
      "ask: always asked when snowpea cannot tell what a command or script will touch.",
      "항상 묻기: snowpea가 실행 전에 무엇을 건드릴지 알 수 없는 명령·스크립트는 매번 묻습니다.",
    );
  }
  if (value === "auto") {
    return say(
      "auto (default): not asked in a session in auto mode; plan and accept mode still ask.",
      "auto 모드에서는 묻지 않음 (기본): auto 모드 세션은 그대로 실행하고, plan·accept 모드에서는 계속 묻습니다.",
    );
  }
  return say(
    "allow: not asked in any mode. Only blocked commands, protected folders, snowpea's own files and a few always-asked patterns (a downloaded script run at once) are asked.",
    "어떤 모드에서도 묻지 않음: 차단 명령, 보호 폴더, snowpea 자체 파일, 그리고 항상 묻는 몇 가지 경우(내려받은 스크립트를 바로 실행)만 묻습니다.",
  );
}

/** Said when the daemon refused "auto": it is an older one that knows only ask and allow. */
export const OLD_DAEMON_LINE = (): string =>
  say(
    "This daemon is older and offers only ask and allow; update it for the auto-mode choice.",
    "이 데몬은 오래된 버전이라 '항상 묻기'와 '어떤 모드에서도 묻지 않음'만 지원합니다. 업데이트하면 auto 모드 선택을 쓸 수 있습니다.",
  );

/** The description, as the screen and the status say it. */
export const UNKNOWN_CODE_LEAD = (): string =>
  say(
    "Decides whether snowpea asks about commands whose effect it cannot work out before they run (a path computed while it runs, a program chosen by a variable). If it does not ask, such a command may write to a protected folder without a check. The block list and snowpea's own file protection stay as they are.",
    "snowpea가 실행 전에 무엇을 건드릴지 알아낼 수 없는 명령(실행 중에 계산되는 경로, 변수로 정해지는 프로그램 등)에 대해 묻는지 정합니다. 묻지 않으면, 그런 명령이 보호 폴더에 쓰더라도 확인 없이 지나갈 수 있습니다. 차단 목록과 snowpea 자체 파일 보호는 그대로입니다.",
  );

/** Before letting unreadable code through, say what that gives up. */
export const UNKNOWN_CODE_ALLOW_CONFIRM: readonly string[] = [
  "Let code snowpea can't tell about run without asking?",
  "Code snowpea cannot read (a target computed while it runs, a script it cannot follow, a line it cannot parse) will no longer ask you. Only blocked commands, protected folders, snowpea's own files and a few always-asked patterns (a downloaded script run at once) are asked.",
  "Allow it? (y/n)",
];

/** The `settings.set` patch for the switch; global only, by a local person. */
export function unknownCodePatch(value: UnknownCode): Record<string, unknown> {
  return { security: { unknownCode: value } };
}

/**
 * What these lists promise, in core's own honest terms (the manual's first
 * paragraph): what auto mode means, and what to use for a repository that is
 * not trusted. Two sentences.
 */
export const SECURITY_CLAIM = (): string =>
  say(
    "In auto mode the agent runs programs as you; these lists stop the forms snowpea can recognise and it asks about code it cannot read. For a repository you do not trust, use accept mode, where every command and write is yours to approve.",
    "자동(auto) 모드에서 에이전트는 당신의 권한으로 프로그램을 실행합니다. 이 목록은 snowpea가 알아볼 수 있는 형태를 막고, 읽을 수 없는 코드는 묻습니다. 신뢰하지 않는 저장소에서는 모든 명령과 쓰기를 직접 승인하는 accept 모드를 쓰세요.",
  );

/** The summary `/security status` prints. */
export function statusLines(config: SecurityConfig, catalog: PresetCatalog | null, diskNotice: string | null = null): string[] {
  const title = (id: string, entries: Preset[]) => entries.find((entry) => entry.id === id)?.title ?? id;
  const commandOn = config.blockedCommands.presets.map((id) => title(id, catalog?.commands ?? []));
  const pathOn = config.protectedPaths.presets.map((id) => title(id, catalog?.paths ?? []));
  const names = (items: string[]) => (items.length > 0 ? items.join(", ") : "none");
  return [
    "Security lists",
    ...(diskNotice ? [`! ${diskNotice}`] : []),
    SECURITY_CLAIM(),
    `Blocked commands: ${config.blockedCommands.presets.length} presets on (${names(commandOn)}), ${config.blockedCommands.custom.length} of your own`,
    `Protected folders: ${config.protectedPaths.presets.length} presets on (${names(pathOn)}), ${config.protectedPaths.custom.length} of your own`,
    `In force now: ${config.effective.commands.length} command patterns, ${config.effective.paths.length} folders`,
    `${unknownCodeLabel()}: ${unknownCodeMeaning(config.unknownCode)} ${OWN_FILES_LINE}`,
    UNKNOWN_CODE_LEAD(),
    ...(config.projectAdds > 0 ? [`This project adds ${config.projectAdds} entries of its own (they do not remove yours).`] : []),
    ...(problemNote(config.problems) ? [`! ${problemNote(config.problems)}`] : []),
    ...config.notices.map((notice) => `! ${notice}`),
    config.writable ? "Change them with /security. They apply to sessions that go through snowpea." : READ_ONLY_LINE,
    ...(config.writable ? [] : ["They apply to sessions that go through snowpea."]),
  ];
}

/** Why a connection that reads the lists cannot change them. */
export const READ_ONLY_LINE = "Read-only: only a person on a local connection (this TUI, the IDE, the CLI, the local web page) can change these lists.";

export function personOnlyReadOnlyLine(error: unknown): string {
  const kind = rpcErrorFacts(error).kind;
  if (kind === "remote_login") return "Read-only: only a local snowpea can change these lists, not a remote login.";
  if (kind === "browser" || kind === "connector") return "Read-only: a browser host or connector cannot change these lists.";
  return "Read-only: only a person on this machine can change these lists.";
}

export const SECURITY_LIMITS =
  "Applies to sessions that go through snowpea. It does not read what the programs a command starts load or run in turn.";

// --- drawing -----------------------------------------------------------------------

/** `segments` wrapped to `width` with a hanging indent; long words are broken, never cut. */
export function wrapSegments(key: string, segments: Segment[], width: number, indent = "    "): Line[] {
  return wrapLine({ key, segments }, Math.max(8, width), indent);
}

export interface BodyLines {
  lines: Line[];
  /** First and last (exclusive) line of the row the cursor is on, with its details. */
  start: number;
  end: number;
}

/**
 * The list of one tab as lines. The highlighted row shows its examples (or
 * folders) beneath it, wrapped.
 */
export function bodyLines(rows: SecurityRow[], cursor: number, width: number, problems: ReadonlyMap<string, string> = new Map()): BodyLines {
  const lines: Line[] = [];
  let start = 0;
  let end = 0;
  rows.forEach((row, index) => {
    const here = index === cursor;
    const from = lines.length;
    const pointer: Segment = { text: here ? "› " : "  ", color: "cyan", bold: true };
    if (row.kind === "preset") {
      const mark: Segment = { text: row.on ? "[x] " : "[ ] ", color: row.on ? "green" : undefined, bold: here };
      const segments: Segment[] = [pointer, mark, { text: row.title, bold: true }];
      if (row.level) segments.push({ text: ` (${LEVEL_LABEL[row.level]})`, color: row.level === "all" ? "red" : "yellow" });
      if (row.description) segments.push({ text: ` — ${row.description}`, dimColor: !here });
      lines.push(...wrapSegments(`row-${index}`, segments, width, "      "));
      if (here && row.examples.length > 0) {
        const label = row.level ? "Folders: " : "Examples: ";
        lines.push(...wrapSegments(`detail-${index}`, [{ text: "      " }, { text: label, dimColor: true }, { text: row.examples.join(", "), color: "cyan" }], width, "        "));
      }
    } else {
      const segments: Segment[] = [pointer, { text: "• ", color: "magenta" }, { text: row.text, bold: here }];
      if (row.level) segments.push({ text: `  (${LEVEL_LABEL[row.level]})`, color: row.level === "all" ? "red" : "yellow" });
      lines.push(...wrapSegments(`row-${index}`, segments, width, "      "));
      const problem = problems.get(row.text);
      if (problem) lines.push(...wrapSegments(`problem-${index}`, [{ text: "      ✗ ", color: "red", bold: true }, { text: problem, color: "red" }], width, "        "));
    }
    if (here) {
      start = from;
      end = lines.length;
    }
  });
  if (rows.length === 0) lines.push({ key: "none", segments: [{ text: "Nothing here yet.", dimColor: true }] });
  return { lines, start, end };
}

/** The effective list of a tab: what is in force right now, and which preset or entry put it there. */
export function effectiveLines(tab: Tab, config: SecurityConfig, catalog: PresetCatalog, width: number): Line[] {
  const lines: Line[] = [];
  if (tab === "commands") {
    config.effective.commands.forEach((entry, index) => {
      lines.push(
        ...wrapSegments(`eff-${index}`, [{ text: "  " }, { text: entry.pattern, color: "cyan" }, { text: `  ← ${byLabel(entry.by, catalog)}`, dimColor: true }], width, "      "),
      );
    });
  } else {
    config.effective.paths.forEach((entry, index) => {
      lines.push(
        ...wrapSegments(
          `eff-${index}`,
          [
            { text: "  " },
            { text: entry.path, color: "cyan" },
            { text: ` (${LEVEL_LABEL[entry.level]})`, color: entry.level === "all" ? "red" : "yellow" },
            ...(entry.scope === "self" ? [{ text: " · the folder itself only", dimColor: true }] : []),
            ...(entry.exempt.length > 0 ? [{ text: ` · except ${entry.exempt.join(", ")}`, dimColor: true }] : []),
            { text: `  ← ${byLabel(entry.by, catalog)}`, dimColor: true },
          ],
          width,
          "      ",
        ),
      );
    });
  }
  if (lines.length === 0) lines.push({ key: "eff-none", segments: [{ text: "  Nothing is in force in this list.", dimColor: true }] });
  return lines;
}

/** The scroll offset that keeps `[start, end)` visible in a window of `height` lines, moving as little as it can. */
export function scrollInto(top: number, start: number, end: number, height: number, total: number): number {
  let next = top;
  if (end - start >= height) next = start;
  else if (end > next + height) next = end - height;
  else if (start < next) next = start;
  return Math.max(0, Math.min(next, Math.max(0, total - height)));
}

/** A short single-line fit for the toast. */
export function blockedToast(event: SecurityBlockedEvent, here = false): string {
  return `Security list blocked ${fit(event.summary, 48)}${here ? "" : " in another session"} (/security)`;
}

export function blockedLine(event: SecurityBlockedEvent): string {
  return `Blocked by your security list: ${event.summary} (/security)`;
}

/** Shown on the external screen and the Jarvis panel while lists are set and the session is not gated. */
export const NOT_APPLIED_NOTE = "security lists do not apply to this session";
