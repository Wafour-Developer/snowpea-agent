/**
 * What the language servers are doing, and how to say it in a few characters.
 *
 * The daemon runs them and `lsp.status` reports them; this turns that into the
 * HUD's segment, the `/lsp` table, and the colour a diagnostic line is drawn
 * in.
 *
 * The wire shapes come from the generated protocol; the rows here are the same
 * data with the optional fields filled in, so the rest of the TUI never has to
 * ask whether a server reported a pid.
 *
 * Pure, so `test/lsp.test.ts` can check every state without a language server.
 */

import type { LspDiagnostics, LspServerStatus } from "../rpc/sdk.js";

/** Lifecycle of one server, as the daemon reports it (M13 §4). */
export type LspServerState = LspServerStatus["state"];

/** One row of `lsp.status`, with the optional fields settled. */
export interface LspServer extends Required<Omit<LspServerStatus, "pid">> {
  pid: number | null;
}

/** Diagnostics counts for one file, from the `lsp.diagnostics` event. */
export type FileDiagnostics = Required<Pick<LspDiagnostics, "count" | "errors" | "warnings">>;

const STATES: readonly LspServerState[] = ["starting", "ready", "broken", "stopped"];

/**
 * A state the protocol adds stops this compiling until it is listed above,
 * which is the point: an unlisted state would silently read as `stopped`.
 */
const STATES_ARE_EXHAUSTIVE: Record<LspServerState, true> = {
  starting: true,
  ready: true,
  broken: true,
  stopped: true,
};
void STATES_ARE_EXHAUSTIVE;

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
