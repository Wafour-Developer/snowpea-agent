/**
 * Security lists (capability `securityLists`): commands that never run and
 * folders that are protected, chosen from presets plus manual entries.
 *
 * The contract types are declared here, in one place shaped exactly like the
 * wire, to be swapped for the SDK's generated types once core ships them.
 * Everything below is pure: the screen, the slash command and the tests all
 * read the same functions.
 */

import { sanitizeText } from "../util/sanitize.js";
import { fit } from "../layout/fit.js";
import { wrapLine, type Line, type Segment } from "../layout/transcript.js";
import { rpcErrorFacts } from "./external.js";

// --- contract (additive, capability `securityLists`) --------------------------

export type PathLevel = "write" | "all";

export interface Preset {
  id: string;
  title: string;
  description: string;
  examples: string[];
  default: boolean;
}

export type PathPreset = Preset & { level: PathLevel; paths: string[] };

/** `security.presets.list { locale }` result. */
export interface PresetCatalog {
  commands: Preset[];
  paths: PathPreset[];
  locale: string;
}

export interface CustomPath {
  path: string;
  level: PathLevel;
}

/** `security.get {}` result. */
export interface SecurityConfig {
  blockedCommands: { presets: string[]; custom: string[] };
  protectedPaths: { presets: string[]; custom: CustomPath[] };
  effective: {
    commands: Array<{ pattern: string; by: string }>;
    paths: Array<{ path: string; level: PathLevel; by: string }>;
  };
}

/** `security.check { command?, path?, workdir? }` result. */
export interface SecurityVerdict {
  blocked: boolean;
  by: string | null;
  why: string;
  level?: PathLevel;
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

export function normalizeCatalog(raw: any): PresetCatalog {
  return {
    commands: (Array.isArray(raw?.commands) ? raw.commands : []).map(preset).filter((entry: Preset) => entry.id !== ""),
    paths: (Array.isArray(raw?.paths) ? raw.paths : [])
      .map((entry: any) => ({ ...preset(entry), level: level(entry?.level), paths: texts(entry?.paths) }))
      .filter((entry: Preset) => entry.id !== ""),
    locale: text(raw?.locale),
  };
}

export function normalizeConfig(raw: any): SecurityConfig {
  return {
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
        .map((entry: any) => ({ path: text(entry?.path), level: level(entry?.level), by: text(entry?.by) }))
        .filter((entry: { path: string }) => entry.path !== ""),
    },
  };
}

export function normalizeVerdict(raw: any): SecurityVerdict {
  return {
    blocked: raw?.blocked === true,
    by: typeof raw?.by === "string" && raw.by !== "" ? sanitizeText(raw.by) : null,
    why: text(raw?.why),
    ...(raw?.level === "write" || raw?.level === "all" ? { level: raw.level as PathLevel } : {}),
  };
}

export function normalizeBlocked(raw: any): SecurityBlockedEvent {
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

/** The title of the preset `by` names, else `by` itself (a custom pattern or path). */
export function byLabel(by: string | null, catalog: PresetCatalog | null): string {
  if (by === null || by === "") return "your list";
  const found = [...(catalog?.commands ?? []), ...(catalog?.paths ?? [])].find((entry) => entry.id === by);
  return found ? found.title : by;
}

export const NOTHING_RUN = "Nothing was run — the text was only checked against your lists.";

export function verdictLine(verdict: SecurityVerdict, catalog: PresetCatalog | null): { text: string; blocked: boolean } {
  if (!verdict.blocked) return { text: verdict.why ? `not blocked — ${verdict.why}` : "not blocked", blocked: false };
  const level = verdict.level ? ` (${LEVEL_LABEL[verdict.level]})` : "";
  return { text: `BLOCKED by ${byLabel(verdict.by, catalog)}${level}: ${verdict.why}`, blocked: true };
}

/** The summary `/security status` prints. */
export function statusLines(config: SecurityConfig, catalog: PresetCatalog | null): string[] {
  const title = (id: string, entries: Preset[]) => entries.find((entry) => entry.id === id)?.title ?? id;
  const commandOn = config.blockedCommands.presets.map((id) => title(id, catalog?.commands ?? []));
  const pathOn = config.protectedPaths.presets.map((id) => title(id, catalog?.paths ?? []));
  const names = (items: string[]) => (items.length > 0 ? items.join(", ") : "none");
  return [
    "Security lists",
    `Blocked commands: ${config.blockedCommands.presets.length} presets on (${names(commandOn)}), ${config.blockedCommands.custom.length} of your own`,
    `Protected folders: ${config.protectedPaths.presets.length} presets on (${names(pathOn)}), ${config.protectedPaths.custom.length} of your own`,
    `In force now: ${config.effective.commands.length} command patterns, ${config.effective.paths.length} folders`,
    "Change them with /security. They apply to sessions that go through snowpea.",
  ];
}

export function personOnlyReadOnlyLine(error: unknown): string {
  const kind = rpcErrorFacts(error).kind;
  if (kind === "remote_login") return "Read-only: only a local snowpea can change these lists, not a remote login.";
  if (kind === "browser" || kind === "connector") return "Read-only: a browser host or connector cannot change these lists.";
  return "Read-only: only a person on this machine can change these lists.";
}

export const SECURITY_LIMITS =
  "Applies to sessions that go through snowpea. It cannot see inside scripts a command runs.";

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
export function bodyLines(rows: SecurityRow[], cursor: number, width: number): BodyLines {
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
