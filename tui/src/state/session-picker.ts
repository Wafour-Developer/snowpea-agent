/**
 * The rows of the `/resume` / `/sessions` picker: this daemon's saved sessions
 * and, when asked, the current folder's external CLI sessions in the same list.
 *
 * Pure, so `test/session-picker.test.ts` covers the merge, the filter and the
 * row text without a terminal.
 */

import { spread } from "../layout/fit.js";
import {
  agentOfExternal,
  agentOfSession,
  type AgentIdentity,
  type AgentKind,
} from "./agent-tag.js";
import { relativeTime, resumeView, sessionKindPrefix, type SessionRecord } from "./history.js";
import type { ExternalSessionRow } from "../rpc/sdk.js";

export interface PickerItem {
  /** Unique within the list. */
  key: string;
  identity: AgentIdentity;
  title: string;
  /** Epoch milliseconds of the last activity. */
  at: number;
  /** Eight characters of the id, the handle the daemon accepts as a prefix. */
  id: string;
  /** Folder name, set when the row is not from the working directory. */
  elsewhere?: string;
  session?: SessionRecord;
  external?: ExternalSessionRow;
}

/** The record's agent: a browser session when the core said so, else snowpea. */
function recordIdentity(record: SessionRecord): AgentIdentity {
  return agentOfSession(record.agentFields);
}

function lastSegment(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

/** Saved snowpea sessions as picker rows, `workdir` being the folder the picker was opened in. */
export function sessionItems(records: readonly SessionRecord[], workdir: string | undefined): PickerItem[] {
  return records.map((record) => ({
    key: `s:${record.sessionId}`,
    identity: recordIdentity(record),
    title: `${sessionKindPrefix(record.kind, record.parentSessionId)}${record.firstPrompt.trim() || "(no prompt)"}`,
    at: record.at,
    id: record.sessionId.slice(0, 8),
    elsewhere: workdir && record.workdir !== workdir ? lastSegment(record.workdir) : undefined,
    session: record,
  }));
}

/** External CLI sessions as picker rows. */
export function externalItems(rows: readonly ExternalSessionRow[]): PickerItem[] {
  return rows.map((row) => ({
    key: `e:${row.ref.cli}:${row.ref.id}`,
    identity: agentOfExternal(row.ref.cli),
    title: (row.title ?? "").trim() || (row.preview ?? "").trim() || row.ref.id.slice(0, 8),
    at: Date.parse(String(row.updatedAt ?? "")) || 0,
    id: row.ref.id.slice(0, 8),
    external: row,
  }));
}

/**
 * What the picker lists: this directory's rows (snowpea and, when included,
 * external ones) newest first, then — once revealed — the other directories'.
 * `hiddenElsewhere` counts what the reveal row would add.
 */
export function pickerItems(
  records: readonly SessionRecord[],
  externals: readonly ExternalSessionRow[],
  workdir: string | undefined,
  showElsewhere: boolean,
): { items: PickerItem[]; hiddenElsewhere: number } {
  const view = resumeView([...records], workdir, showElsewhere);
  const mine = sessionItems(view.rows, workdir);
  const here = [...mine.filter((item) => !item.elsewhere), ...externalItems(externals)];
  const sorted = here
    .map((item, order) => ({ item, order }))
    .sort((a, b) => b.item.at - a.item.at || a.order - b.order)
    .map((entry) => entry.item);
  return { items: [...sorted, ...mine.filter((item) => item.elsewhere)], hiddenElsewhere: view.hiddenElsewhere };
}

/** The rows a filter keeps. */
export function applyFilter(items: readonly PickerItem[], filter: "all" | AgentKind): PickerItem[] {
  return filter === "all" ? [...items] : items.filter((item) => item.identity.kind === filter);
}

/**
 * One row after the tag: the title, cut with `…` first, then `id · age` on the
 * right (the id only when there is room). `width` is the cells after the tag.
 */
export function itemText(item: PickerItem, width: number, now = Date.now()): string {
  const where = item.elsewhere ? `[${item.elsewhere}] ` : "";
  const age = item.at > 0 ? relativeTime(item.at, now) : "";
  const right = width >= 52 && age ? `${item.id} · ${age}` : age || item.id;
  return spread(`${where}${item.title}`, right, width);
}

/** The picker header line for the include-external toggle. */
export function externalToggleLabel(on: boolean): string {
  return on ? "external sessions: shown (e hides)" : "external sessions: hidden (e shows)";
}
