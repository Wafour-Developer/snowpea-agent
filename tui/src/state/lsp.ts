/**
 * What the language servers are doing, and how to say it in a few characters.
 *
 * The daemon runs them and `lsp.status` reports them; this turns that into the
 * HUD's segment, the `/lsp` table, and the colour a diagnostic line is drawn
 * in. The protocol types are declared here rather than imported because the LSP
 * work is on its own branch: the shapes are the branch's, and this file is what
 * changes when the generated types land.
 *
 * Pure, so `test/lsp.test.ts` can check every state without a language server.
 */

/** Lifecycle of one server, as the daemon reports it (M13 §4). */
export type LspServerState = "starting" | "ready" | "broken" | "stopped";

/** One row of `lsp.status`. */
export interface LspServer {
  id: string;
  root: string;
  state: LspServerState;
  languageId: string;
  pid: number | null;
}

/** Diagnostics counts for one file, from the `lsp.diagnostics` event. */
export interface FileDiagnostics {
  count: number;
  errors: number;
  warnings: number;
}

const STATES: readonly LspServerState[] = ["starting", "ready", "broken", "stopped"];

/**
 * Read an `lsp.status` answer.
 *
 * A daemon without the feature answers with an error or with nothing, and both
 * mean the same thing here: no servers, so no segment.
 */
export function readLspStatus(result: unknown): LspServer[] {
  const rows = (result as { servers?: unknown } | null)?.servers;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const value = (row ?? {}) as Record<string, unknown>;
    const id = typeof value.id === "string" ? value.id : "";
    if (id.length === 0) return [];
    const state = STATES.includes(value.state as LspServerState)
      ? (value.state as LspServerState)
      : "stopped";
    return [
      {
        id,
        root: typeof value.root === "string" ? value.root : "",
        state,
        languageId: typeof value.languageId === "string" ? value.languageId : "",
        pid: typeof value.pid === "number" ? value.pid : null,
      },
    ];
  });
}

export interface LspSummary {
  ready: number;
  starting: number;
  broken: number;
  stopped: number;
}

export function lspSummary(servers: readonly LspServer[]): LspSummary {
  const summary: LspSummary = { ready: 0, starting: 0, broken: 0, stopped: 0 };
  for (const server of servers) summary[server.state] += 1;
  return summary;
}

/**
 * The HUD's text: `lsp 2`, or `lsp 1!` when something is broken.
 *
 * Null when there is nothing to say — no servers at all, or only stopped ones —
 * because a segment that always reads zero is noise on every other project.
 */
export function lspLabel(servers: readonly LspServer[]): string | null {
  const { ready, broken, starting } = lspSummary(servers);
  if (ready === 0 && broken === 0 && starting === 0) return null;
  return `lsp ${ready}${broken > 0 ? "!" : ""}`;
}

/** Red while anything is broken, plain otherwise. */
export function lspColor(servers: readonly LspServer[]): string | undefined {
  return lspSummary(servers).broken > 0 ? "red" : undefined;
}

/** `/lsp`: one line per server, plus a header. */
export function lspTable(servers: readonly LspServer[]): string {
  if (servers.length === 0) {
    return "no language servers are running (lsp.enabled=false, or nothing matched this project)";
  }
  const width = Math.max(...servers.map((server) => server.id.length));
  const rows = servers.map((server) => {
    const pid = server.pid === null ? "" : ` pid ${server.pid}`;
    const language = server.languageId ? ` ${server.languageId}` : "";
    return `  ${server.id.padEnd(width)}  ${server.state}${language}${pid}  ${server.root}`;
  });
  const { ready, broken } = lspSummary(servers);
  const tail = broken > 0 ? `, ${broken} broken` : "";
  return [`language servers (${ready} ready${tail})`, ...rows].join("\n");
}

/**
 * The colour of one line inside a tool result's Diagnostics block.
 *
 * The core writes the block; a surface only has the text, so the severity is
 * read off the front of the line the way a person reads it.
 */
export function diagnosticLineColor(line: string): string | undefined {
  const text = line.trimStart();
  if (text.startsWith("ERROR")) return "red";
  if (text.startsWith("WARNING")) return "yellow";
  return undefined;
}

/** `⚠ 3` for the diff header, or null when the file is clean. */
export function diagnosticsBadge(diagnostics: FileDiagnostics | undefined): string | null {
  if (!diagnostics || diagnostics.count <= 0) return null;
  return `⚠ ${diagnostics.count}`;
}

/** Red when anything is an error, amber when it is only warnings. */
export function diagnosticsColor(diagnostics: FileDiagnostics | undefined): string | undefined {
  if (!diagnostics || diagnostics.count <= 0) return undefined;
  return diagnostics.errors > 0 ? "red" : "yellow";
}
