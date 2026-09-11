/**
 * Compact one-line summaries for runs of tool calls.
 *
 * A turn that reads six files should leave six lines of scrollback behind, not
 * six cards. Consecutive successful calls therefore collapse into `Read 3
 * files` or `Ran 2 shell commands` when they are released to `<Static>`, the
 * way Claude Code does it.
 *
 * Failures are never folded away: a call that errored keeps its own card, so
 * the thing that went wrong is still readable afterwards.
 *
 * Pure, so `test/summary.test.ts` can check the wording.
 */

import { basename, clip } from "../state/working.js";
import type { ToolCallEntry } from "../state/store.js";

export type ToolKind = "shell" | "read" | "search" | "edit" | "write" | "fetch" | "other";

/** Which family a tool belongs to, by name. */
export function toolKind(name: string): ToolKind {
  const lower = name.toLowerCase();
  if (/(^|_)(bash|shell|exec|run|terminal)/.test(lower)) return "shell";
  if (/(^|_)(read|cat|open|view)/.test(lower)) return "read";
  if (/(^|_)(search|grep|glob|find|rg)/.test(lower)) return "search";
  if (/(^|_)(edit|patch|apply|replace)/.test(lower)) return "edit";
  if (/(^|_)(write|create|save)/.test(lower)) return "write";
  if (/(^|_)(fetch|http|curl|web|browse)/.test(lower)) return "fetch";
  return "other";
}

/** The first argument that names what the call worked on. */
function subject(call: ToolCallEntry): string | null {
  const args = call.args ?? {};
  for (const key of ["path", "file", "file_path", "filename", "pattern", "query", "url", "command"]) {
    const value = args[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

const PLURAL: Record<ToolKind, { one: string; many: (n: number) => string }> = {
  shell: { one: "Ran shell command", many: (n) => `Ran ${n} shell commands` },
  read: { one: "Read a file", many: (n) => `Read ${n} files` },
  search: { one: "Searched", many: (n) => `Searched ${n} patterns` },
  edit: { one: "Edited a file", many: (n) => `Edited ${n} files` },
  write: { one: "Wrote a file", many: (n) => `Wrote ${n} files` },
  fetch: { one: "Fetched a page", many: (n) => `Fetched ${n} pages` },
  other: { one: "Ran a tool", many: (n) => `Ran ${n} tools` },
};

/**
 * One line for a run of calls.
 *
 * A single call names what it touched — `Edited README.md` is worth more than
 * `Edited a file` — while a run of several only counts them.
 */
export function summarizeCalls(calls: ToolCallEntry[]): string {
  if (calls.length === 0) return "";
  const kinds = new Set(calls.map((call) => toolKind(call.name)));
  const kind: ToolKind = kinds.size === 1 ? [...kinds][0] : "other";

  if (calls.length === 1) {
    const call = calls[0];
    const what = subject(call);
    if (!what) return kind === "other" ? `Ran ${clip(call.name, 30)}` : PLURAL[kind].one;
    switch (kind) {
      case "shell":
        return `Ran shell: ${clip(what, 40)}`;
      case "read":
        return `Read ${basename(what)}`;
      case "edit":
        return `Edited ${basename(what)}`;
      case "write":
        return `Wrote ${basename(what)}`;
      case "search":
        return `Searched "${clip(what, 30)}"`;
      case "fetch":
        return `Fetched ${clip(what, 40)}`;
      default:
        return `Ran ${clip(call.name, 30)}`;
    }
  }

  return PLURAL[kind].many(calls.length);
}

/** How many lines of output a run hid, for the `(12 lines)` tail. */
export function hiddenLines(calls: ToolCallEntry[]): number {
  let total = 0;
  for (const call of calls) {
    const body = call.output ?? "";
    if (body.length > 0) total += body.split("\n").length;
  }
  return total;
}

export type SummaryBlock =
  /** A run of successful calls, drawn as one line. */
  | { kind: "tools"; calls: ToolCallEntry[] }
  /** Anything that must keep its own card: a failure, or a non-tool entry. */
  | { kind: "single"; call: ToolCallEntry };

/**
 * Split a run of settled calls into the blocks that reach the scrollback.
 *
 * Successful calls fold together; a failure breaks the run so it stands alone,
 * and the calls after it start a new run.
 */
export function groupCalls(calls: ToolCallEntry[]): SummaryBlock[] {
  const blocks: SummaryBlock[] = [];
  let run: ToolCallEntry[] = [];
  const flush = (): void => {
    if (run.length > 0) blocks.push({ kind: "tools", calls: run });
    run = [];
  };
  for (const call of calls) {
    if (call.state === "error") {
      flush();
      blocks.push({ kind: "single", call });
      continue;
    }
    run.push(call);
  }
  flush();
  return blocks;
}
