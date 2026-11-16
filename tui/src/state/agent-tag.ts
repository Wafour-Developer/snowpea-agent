/**
 * Which agent a session belongs to, and the short tag every list shows for it.
 *
 * A plain snowpea session carries NO tag (the column stays blank, so titles
 * line up). An external CLI session gets `[cc]` Claude Code, `[cx]` Codex,
 * `[ag]` agy, `[cu]` Cursor agent, or `[ext]` for a CLI id this build does not
 * know; a session opened from the snowpea browser gets `[web]`. The tag text is
 * the cue; the colour (one for external, one for browser) is secondary and is
 * chosen to read on dark and light terminals and to differ from the status
 * colours (green connected, yellow waiting, red error, cyan plan).
 *
 * Nothing is guessed: a field the core did not send leaves the session a
 * snowpea one. Pure, so `test/agent-tag.test.ts` covers every case.
 */

import { fit } from "../layout/fit.js";
import { textWidth } from "../layout/text-width.js";

export type AgentKind = "snowpea" | "claude" | "codex" | "agy" | "cursor" | "ext" | "web";

export interface AgentIdentity {
  kind: AgentKind;
  /** `[cc]`; empty for a plain snowpea session. */
  tag: string;
  /** `Claude Code`. */
  label: string;
  /** Theme colour for the tag; undefined for snowpea. */
  color?: string;
}

/** One colour for every external CLI, one for the browser. */
export const EXTERNAL_COLOR = "#d2691e";
export const BROWSER_COLOR = "#5b7cfa";

const CLI_LABELS: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  agy: "agy",
  cursor: "Cursor",
};

const CLI_TAGS: Record<string, string> = { claude: "cc", codex: "cx", agy: "ag", cursor: "cu" };

/** The CLI's display name; an unknown id is shown as it came. */
export function cliName(cli: string): string {
  return CLI_LABELS[cli] ?? cli;
}

const SNOWPEA: AgentIdentity = { kind: "snowpea", tag: "", label: "Snowpea" };

/** Legend / filter order. */
const ORDER: AgentKind[] = ["snowpea", "claude", "codex", "agy", "cursor", "ext", "web"];

/** The identity of an external CLI session; an unknown or missing id is `[ext]`. */
export function agentOfExternal(cli: string | null | undefined): AgentIdentity {
  const known = cli ? CLI_TAGS[cli] : undefined;
  if (cli && known) {
    return { kind: cli as AgentKind, tag: `[${known}]`, label: CLI_LABELS[cli], color: EXTERNAL_COLOR };
  }
  return { kind: "ext", tag: "[ext]", label: cli ? cli : "External", color: EXTERNAL_COLOR };
}

/** The fields of `session.list` rows that tell a browser session apart. */
export interface SessionAgentFields {
  hostToolsFrom?: string | null;
  browserProvider?: string | null;
  originSurface?: string | null;
}

/** The identity of a snowpea session row: browser when the core says so, else snowpea. */
export function agentOfSession(row?: SessionAgentFields | null): AgentIdentity {
  if (
    row &&
    ((typeof row.hostToolsFrom === "string" && row.hostToolsFrom.length > 0) ||
      (typeof row.browserProvider === "string" && row.browserProvider.length > 0) ||
      row.originSurface === "browser")
  ) {
    return { kind: "web", tag: "[web]", label: "Browser", color: BROWSER_COLOR };
  }
  return SNOWPEA;
}

/** The identity for a kind (filters, legends). */
export function agentOfKind(kind: AgentKind): AgentIdentity {
  if (kind === "snowpea") return SNOWPEA;
  if (kind === "web") return agentOfSession({ browserProvider: "host" });
  if (kind === "ext") return agentOfExternal(null);
  return agentOfExternal(kind);
}

/** Cells the tag column needs for these sessions; 0 when none carries a tag. */
export function tagColumnWidth(identities: readonly AgentIdentity[]): number {
  return identities.reduce((widest, identity) => Math.max(widest, textWidth(identity.tag)), 0);
}

/** The tag padded to the column, so titles after it line up. */
export function tagCell(identity: AgentIdentity, column: number): string {
  return identity.tag + " ".repeat(Math.max(0, column - textWidth(identity.tag)));
}

/** Kinds present, in the fixed order. */
export function kindsPresent(identities: readonly AgentIdentity[]): AgentKind[] {
  const seen = new Set(identities.map((identity) => identity.kind));
  return ORDER.filter((kind) => seen.has(kind));
}

/** `[cc] Claude Code  [cx] Codex  [web] Browser`: only the tagged kinds present; null when none. */
export function legendLine(identities: readonly AgentIdentity[], width: number): string | null {
  const parts = kindsPresent(identities)
    .filter((kind) => kind !== "snowpea")
    .map((kind) => {
      const identity = agentOfKind(kind);
      return `${identity.tag} ${kind === "ext" ? "Other CLI" : identity.label}`;
    });
  return parts.length === 0 ? null : fit(parts.join("  "), width);
}

/** The filter values for these sessions: all, then snowpea (when present), then each tagged kind. */
export function filterChoices(identities: readonly AgentIdentity[]): Array<"all" | AgentKind> {
  const kinds = kindsPresent(identities);
  return kinds.length <= 1 ? ["all"] : ["all", ...kinds];
}

/** The filter after `current`; wraps; falls back to `all` when `current` is gone. */
export function nextFilter(
  current: "all" | AgentKind,
  identities: readonly AgentIdentity[],
): "all" | AgentKind {
  const choices = filterChoices(identities);
  const at = choices.indexOf(current);
  return choices[(at + 1) % choices.length] ?? "all";
}

/** What the picker header says about a filter. */
export function filterLabel(filter: "all" | AgentKind): string {
  return filter === "all" ? "all" : filter === "ext" ? "other CLI" : agentOfKind(filter).label.toLowerCase();
}
