/**
 * MCP servers, as this surface needs them: completing `/mcp`, filling the
 * `/mcp add` form, and saying `mcp 2/3` in the HUD.
 *
 * The daemon owns every decision — what a server is, whether it starts, which
 * of its tools are registered (M14 §3). This module only turns `mcp.list` rows
 * and `mcp.changed` payloads into the few shapes the TUI draws, and parses the
 * one line the form collects that has a syntax: `command arg "arg with space"`.
 *
 * Pure, so `test/mcp.test.ts` needs neither a daemon nor a terminal.
 */

import type { CommandInfo } from "../rpc/sdk.js";

/** Lifecycle of one server, as the daemon reports it (M14 §3). */
export type McpState = "stopped" | "starting" | "ready" | "error";

/** Where an entry is declared; the last two are read-only here. */
export type McpScope = "project" | "global" | "plugin" | "settings";

/** Scopes `/mcp add` may write to. */
export type McpWritableScope = "project" | "global";

/** One tool a server contributes, as the picker lists it. */
export interface McpTool {
  name: string;
  description: string;
}

/** One row of `mcp.list`, with the optional fields settled. */
export interface McpServerRow {
  name: string;
  scope: McpScope;
  transport: "stdio" | "http" | "sse";
  state: McpState;
  command: string | null;
  url: string | null;
  args: string[];
  envKeys: string[];
  headerKeys: string[];
  disabled: boolean;
  error: string | null;
  toolCount: number;
  tools: McpTool[];
  toolsInclude: string[];
  plugin: string | null;
}

/** One curated preset from `mcp.catalog`. */
export interface McpCatalogEntry {
  id: string;
  label: string;
  description: string;
  transport: "stdio" | "http" | "sse";
  entry: Record<string, unknown>;
  needs: string[];
}

const STATES: readonly McpState[] = ["stopped", "starting", "ready", "error"];
const SCOPES: readonly McpScope[] = ["project", "global", "plugin", "settings"];

/**
 * A state the protocol adds stops this compiling until it is listed above,
 * which is the point: an unlisted state would silently read as `stopped`.
 */
const STATES_ARE_EXHAUSTIVE: Record<McpState, true> = {
  stopped: true,
  starting: true,
  ready: true,
  error: true,
};
void STATES_ARE_EXHAUSTIVE;

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function tools(value: unknown): McpTool[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((row) => {
    const entry = (row ?? {}) as Record<string, unknown>;
    const name = typeof entry.name === "string" ? entry.name : "";
    if (name.length === 0) return [];
    return [{ name, description: typeof entry.description === "string" ? entry.description : "" }];
  });
}

/**
 * Read an `mcp.list` answer.
 *
 * A daemon without the feature answers with an error or with nothing, and both
 * mean the same thing here: no servers, so no segment and no completions.
 */
export function readMcpList(result: unknown): McpServerRow[] {
  const rows = (result as { servers?: unknown } | null)?.servers;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const value = (row ?? {}) as Record<string, unknown>;
    const name = typeof value.name === "string" ? value.name : "";
    if (name.length === 0) return [];
    const found = tools(value.tools);
    return [
      {
        name,
        scope: SCOPES.includes(value.scope as McpScope) ? (value.scope as McpScope) : "project",
        transport:
          value.transport === "http" || value.transport === "sse" ? value.transport : "stdio",
        state: STATES.includes(value.state as McpState) ? (value.state as McpState) : "stopped",
        command: text(value.command),
        url: text(value.url),
        args: strings(value.args),
        envKeys: strings(value.envKeys),
        headerKeys: strings(value.headerKeys),
        disabled: value.disabled === true,
        error: text(value.error),
        toolCount: typeof value.toolCount === "number" ? value.toolCount : found.length,
        tools: found,
        toolsInclude: strings(value.toolsInclude),
        plugin: text(value.plugin),
      },
    ];
  });
}

/** Read the `mcp.catalog` answer the picker lists. */
export function readMcpCatalog(result: unknown): McpCatalogEntry[] {
  const rows = (result as { entries?: unknown } | null)?.entries;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const value = (row ?? {}) as Record<string, unknown>;
    const id = typeof value.id === "string" ? value.id : "";
    if (id.length === 0) return [];
    return [
      {
        id,
        label: typeof value.label === "string" ? value.label : id,
        description: typeof value.description === "string" ? value.description : "",
        transport:
          value.transport === "http" || value.transport === "sse" ? value.transport : "stdio",
        entry: (value.entry ?? {}) as Record<string, unknown>,
        needs: strings(value.needs),
      },
    ];
  });
}

/** Apply one `mcp.changed` payload to the rows the HUD is drawn from. */
export function applyMcpChange(rows: McpServerRow[], payload: unknown): McpServerRow[] {
  const value = (payload ?? {}) as Record<string, unknown>;
  const name = typeof value.name === "string" ? value.name : "";
  if (name.length === 0) return rows;
  if (value.removed === true) return rows.filter((row) => row.name !== name);
  const state = STATES.includes(value.state as McpState) ? (value.state as McpState) : "stopped";
  const toolCount = typeof value.toolCount === "number" ? value.toolCount : undefined;
  const scope = SCOPES.includes(value.scope as McpScope) ? (value.scope as McpScope) : undefined;
  const error = text(value.error);
  const known = rows.find((row) => row.name === name);
  if (!known) {
    // A server added elsewhere: enough of a row to be counted, and the
    // `mcp.list` the caller runs next fills in the rest.
    return [
      ...rows,
      {
        name,
        scope: scope ?? "project",
        transport: "stdio",
        state,
        command: null,
        url: null,
        args: [],
        envKeys: [],
        headerKeys: [],
        disabled: false,
        error,
        toolCount: toolCount ?? 0,
        tools: [],
        toolsInclude: [],
        plugin: null,
      },
    ];
  }
  return rows.map((row) =>
    row.name === name
      ? { ...row, state, error, scope: scope ?? row.scope, toolCount: toolCount ?? row.toolCount }
      : row,
  );
}

/**
 * The HUD's text: `mcp 2/3`, the servers that are ready out of the ones
 * configured.
 *
 * Null when nothing is configured, because a segment that always reads `0/0`
 * is noise on every project that does not use MCP at all.
 */
export function mcpLabel(rows: readonly McpServerRow[]): string | null {
  const counted = rows.filter((row) => !row.disabled);
  if (counted.length === 0) return null;
  const ready = counted.filter((row) => row.state === "ready").length;
  return `mcp ${ready}/${counted.length}`;
}

/** Red while a server is in the error state, plain otherwise. */
export function mcpColor(rows: readonly McpServerRow[]): string | undefined {
  return rows.some((row) => row.state === "error") ? "red" : undefined;
}

/** One sub-action of `/mcp`, as the palette lists it. */
export interface McpAction {
  action: string;
  summary: string;
  /** True when the action is followed by a server name. */
  takesName?: boolean;
}

/** What `/mcp` can do, in the order someone reaches for it (M14 §4). */
export const MCP_ACTIONS: readonly McpAction[] = [
  { action: "list", summary: "Every configured server: scope, transport, state, tools." },
  { action: "add", summary: "Add a server; with no arguments this opens a form." },
  { action: "add-json", summary: "Add a raw .mcp.json entry, verbatim." },
  { action: "catalog", summary: "The curated presets --preset accepts." },
  { action: "get", summary: "One server in full, with its tools.", takesName: true },
  { action: "test", summary: "Start it, list its tools, report the error.", takesName: true },
  { action: "configure", summary: "Choose which of its tools are registered.", takesName: true },
  { action: "enable", summary: "Start this entry again.", takesName: true },
  { action: "disable", summary: "Keep the entry but stop starting it.", takesName: true },
  { action: "remove", summary: "Delete the entry and stop the server.", takesName: true },
  { action: "reload", summary: "Restart one server, or re-read every declaration." },
];

/** The actions whose next word is a server name. */
export const MCP_NAME_ACTIONS: readonly string[] = MCP_ACTIONS.filter(
  (entry) => entry.takesName,
).map((entry) => entry.action);

function scopeTag(row: McpServerRow): string {
  const state = row.disabled ? "disabled" : row.state;
  return `${row.scope} · ${state} · ${row.toolCount} tool${row.toolCount === 1 ? "" : "s"}`;
}

/**
 * The rows `/mcp` offers, or an empty list when the draft is not asking.
 *
 * Three questions wear one prefix: which sub-action, which server, which
 * preset. They are answered here rather than in the app so the palette stays a
 * list of `CommandInfo` and the syntax lives in one place.
 */
export function mcpSubCommands(
  draft: string,
  servers: readonly McpServerRow[] = [],
  catalog: readonly McpCatalogEntry[] = [],
): CommandInfo[] {
  const preset = /^\/mcp\s+add\s+.*--preset(?:=|\s+)([^\s]*)$/.exec(draft);
  if (preset) {
    const typed = preset[1] ?? "";
    const head = draft.slice(1, draft.length - typed.length);
    return catalog
      .filter((entry) => entry.id.startsWith(typed))
      .map((entry) => ({
        name: `${head}${entry.id}`,
        summary: entry.description || entry.label,
        source: "core",
      }));
  }

  const named = /^\/mcp\s+([a-z-]+)\s+([^\s-][^\s]*|)$/.exec(draft);
  if (named && MCP_NAME_ACTIONS.includes(named[1])) {
    const typed = named[2] ?? "";
    return servers
      .filter((row) => row.name.startsWith(typed))
      .map((row) => ({
        name: `mcp ${named[1]} ${row.name}`,
        summary: scopeTag(row),
        source: "core",
      }));
  }

  const match = /^\/mcp(?:\s+([^\s]*))?$/.exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  return MCP_ACTIONS.filter((entry) => entry.action.startsWith(typed)).map((entry) => ({
    name: `mcp ${entry.action}`,
    summary: entry.summary,
    source: "core",
  }));
}

/** True for `/mcp add` with nothing after it: the form's cue. */
export function isBareMcpAdd(text: string): boolean {
  return /^\/mcp\s+add\s*$/.test(text.trim());
}

/** True for a bare `/mcp catalog`: the preset picker's cue. */
export function isBareMcpCatalog(text: string): boolean {
  return /^\/mcp\s+catalog\s*$/.test(text.trim());
}

/** `/mcp configure <name>` with no tool names → the name, or null. */
export function parseMcpConfigure(text: string): string | null {
  const match = /^\/mcp\s+configure\s+([A-Za-z0-9_-]{1,64})\s*$/.exec(text.trim());
  return match ? match[1] : null;
}

/** A server name the daemon will accept (M14 §3). */
export function isValidMcpName(name: string): boolean {
  return /^[a-zA-Z0-9_-]{1,64}$/.test(name.trim());
}

/**
 * `npx -y @modelcontextprotocol/server-github` → `["npx", "-y", "…"]`.
 *
 * Quotes group a word that has spaces in it, and a backslash escapes the next
 * character. Nothing else of a shell is honoured: no globbing, no `$VAR`, no
 * pipes, and the result is argv handed to the daemon, never a string a shell
 * is asked to interpret (M14 §1b).
 */
export function splitArgs(line: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === "\\" && quote !== "'" && i + 1 < line.length) {
      current += line[i + 1];
      started = true;
      i += 1;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      started = true;
      continue;
    }
    if (/\s/.test(char)) {
      if (started) out.push(current);
      current = "";
      started = false;
      continue;
    }
    current += char;
    started = true;
  }
  if (started) out.push(current);
  return out;
}

/** `KEY=value` → the pair, or null when the line has no `=` or no key. */
export function parseAssignment(line: string): { key: string; value: string } | null {
  const index = line.indexOf("=");
  if (index <= 0) return null;
  const key = line.slice(0, index).trim();
  if (key.length === 0) return null;
  return { key, value: line.slice(index + 1) };
}

/** What the form shows back for a value it will never echo. */
export const MASK = "•••";

/** `KEY=•••` — a secret is confirmed by its name, never by its value. */
export function maskAssignment(key: string): string {
  return `${key}=${MASK}`;
}

/** Everything `/mcp add` collects, before it becomes `mcp.add` params. */
export interface McpDraft {
  name: string;
  transport: "stdio" | "url";
  /** The command line as typed; split with `splitArgs` at submit time. */
  commandLine: string;
  url: string;
  env: { key: string; value: string }[];
  headers: { key: string; value: string }[];
  scope: McpWritableScope;
}

/** An empty draft, or one seeded from a catalog preset. */
export function emptyDraft(): McpDraft {
  return { name: "", transport: "stdio", commandLine: "", url: "", env: [], headers: [], scope: "project" };
}

/**
 * A catalog entry as a draft the form can open on.
 *
 * The preset carries the command and the names of the variables it needs; the
 * values are the user's to type, so they start empty and the form asks for them
 * like any other.
 */
export function draftFromCatalog(entry: McpCatalogEntry): McpDraft {
  const raw = entry.entry ?? {};
  const command = typeof raw.command === "string" ? raw.command : "";
  const args = strings(raw.args);
  const url = typeof raw.url === "string" ? raw.url : "";
  return {
    name: entry.id,
    transport: url ? "url" : "stdio",
    commandLine: [command, ...args].filter(Boolean).join(" "),
    url,
    env: entry.needs.map((key) => ({ key, value: "" })),
    headers: [],
    scope: "project",
  };
}

/** The draft as `mcp.test` / `mcp.add` params, minus the flags the caller adds. */
export function draftParams(draft: McpDraft): Record<string, unknown> {
  const pairs = (entries: { key: string; value: string }[]): Record<string, string> =>
    Object.fromEntries(entries.map((entry) => [entry.key, entry.value]));
  if (draft.transport === "url") {
    return {
      name: draft.name.trim(),
      url: draft.url.trim(),
      headers: pairs(draft.headers),
      scope: draft.scope,
    };
  }
  const argv = splitArgs(draft.commandLine);
  return {
    name: draft.name.trim(),
    command: argv[0] ?? "",
    args: argv.slice(1),
    env: pairs(draft.env),
    scope: draft.scope,
  };
}

/** One `mcp.test` answer, as the form reads it. */
export interface McpProbe {
  ok: boolean;
  tools: McpTool[];
  error: string | null;
}

/** Read an `mcp.test` answer; a thrown call becomes `ok: false` too. */
export function readMcpProbe(result: unknown): McpProbe {
  const value = (result ?? {}) as Record<string, unknown>;
  return {
    ok: value.ok === true,
    tools: tools(value.tools),
    error: text(value.error),
  };
}

/**
 * True when the daemon refused the entry on safety grounds (M14 §1b).
 *
 * That is the one failure `force` is allowed to overrule, and only after the
 * user has been shown the finding and said so.
 */
export function isUnsafeError(error: string | null): boolean {
  return error !== null && /mcp_unsafe/i.test(error);
}

/** `Connected — 3 tools: search, fetch, write`. */
export function probeSummary(probe: McpProbe): string {
  const names = probe.tools.map((tool) => tool.name);
  const head = `Connected — ${names.length} tool${names.length === 1 ? "" : "s"}`;
  if (names.length === 0) return head;
  const shown = names.slice(0, 8).join(", ");
  return `${head}: ${shown}${names.length > 8 ? ", …" : ""}`;
}
