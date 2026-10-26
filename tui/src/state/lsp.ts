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
export interface LspServer extends Required<Omit<LspServerStatus, "pid" | "reason">> {
  pid: number | null;
  /** Why it is not ready ('exited: code 2', 'not installed', …), null while fine. */
  reason: string | null;
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
        reason: typeof value.reason === "string" && value.reason !== "" ? value.reason : null,
      },
    ];
  });
}

/** `path` with `\` read as `/` and no trailing separator, for prefix tests. */
function normalisePath(path: string): string {
  const slashed = path.replace(/\\/g, "/");
  return slashed.length > 1 ? slashed.replace(/\/+$/, "") : slashed;
}

/** True when `inner` is `outer` or a folder below it. */
function within(inner: string, outer: string): boolean {
  if (outer === "" || inner === "") return false;
  return inner === outer || inner.startsWith(outer.endsWith("/") ? outer : `${outer}/`);
}

/**
 * The servers that belong to `workdir`'s project.
 *
 * `lsp.status` lists every server the daemon runs, for every session's
 * project, so a server that broke in another folder would otherwise show as
 * this session's `lsp 0!`. A server counts when its root is the workdir, a
 * folder below it, or a folder above it (the repo root of a subfolder). With
 * no workdir every server is kept.
 */
export function serversFor(servers: readonly LspServer[], workdir: string | undefined): LspServer[] {
  if (!workdir) return [...servers];
  const dir = normalisePath(workdir);
  return servers.filter((server) => {
    const root = normalisePath(server.root);
    return within(root, dir) || within(dir, root);
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

/**
 * `/lsp`: one line per server of this project, plus a header.
 *
 * Servers of other projects are only counted, so the table answers "what is
 * running here" the way the HUD segment does.
 */
export function lspTable(all: readonly LspServer[], workdir?: string): string {
  const servers = serversFor(all, workdir);
  const others = all.length - servers.length;
  const elsewhere =
    others > 0 ? `\n  (${others} more in other projects' folders; not counted here)` : "";
  if (servers.length === 0) {
    return `no language servers are running (lsp.enabled=false, or nothing matched this project)${elsewhere}`;
  }
  const width = Math.max(...servers.map((server) => server.id.length));
  const rows = servers.map((server) => {
    const pid = server.pid === null ? "" : ` pid ${server.pid}`;
    const language = server.languageId ? ` ${server.languageId}` : "";
    const reason = server.reason && server.state !== "ready" ? ` (${server.reason})` : "";
    return `  ${server.id.padEnd(width)}  ${server.state}${reason}${language}${pid}  ${server.root}`;
  });
  const { ready, broken } = lspSummary(servers);
  const tail = broken > 0 ? `, ${broken} broken` : "";
  return [`language servers (${ready} ready${tail})`, ...rows].join("\n") + elsewhere;
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
