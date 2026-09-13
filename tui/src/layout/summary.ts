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

import { uiLanguage, type UiLanguage } from "./language.js";
import { basename, clip } from "../state/working.js";
import type { ToolCallEntry } from "../state/store.js";

export type ToolKind =
  | "shell"
  | "read"
  | "search"
  | "edit"
  | "write"
  | "fetch"
  | "delegate"
  | "other";

/** Which family a tool belongs to, by name. */
export function toolKind(name: string): ToolKind {
  const lower = name.toLowerCase();
  if (/(^|_)delegate/.test(lower)) return "delegate";
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

/**
 * The mechanical verbs, per language.
 *
 * `one` is the call whose subject is unknown, `many` counts a run, `single`
 * names what one call touched, and `tool` falls back to the tool's own name.
 * Nothing here is a translation of a sentence — they are labels, so each
 * language gets the phrasing that language would actually use.
 */
interface Verbs {
  one: Record<ToolKind, string>;
  many: Record<ToolKind, (n: number) => string>;
  single: Record<Exclude<ToolKind, "other">, (what: string) => string>;
  tool: (name: string) => string;
}

const EN: Verbs = {
  one: {
    shell: "Ran shell command",
    read: "Read a file",
    search: "Searched",
    edit: "Edited a file",
    write: "Wrote a file",
    fetch: "Fetched a page",
    delegate: "Delegated a task",
    other: "Ran a tool",
  },
  many: {
    shell: (n) => `Ran ${n} shell commands`,
    read: (n) => `Read ${n} files`,
    search: (n) => `Searched ${n} patterns`,
    edit: (n) => `Edited ${n} files`,
    write: (n) => `Wrote ${n} files`,
    fetch: (n) => `Fetched ${n} pages`,
    delegate: (n) => `Delegated ${n} tasks`,
    other: (n) => `Ran ${n} tools`,
  },
  single: {
    shell: (what) => `Ran shell: ${what}`,
    read: (what) => `Read ${what}`,
    search: (what) => `Searched "${what}"`,
    edit: (what) => `Edited ${what}`,
    write: (what) => `Wrote ${what}`,
    fetch: (what) => `Fetched ${what}`,
    delegate: (what) => `Delegated: ${what}`,
  },
  tool: (name) => `Ran ${name}`,
};

const KO: Verbs = {
  one: {
    shell: "셸 명령 실행",
    read: "파일 읽음",
    search: "검색",
    edit: "파일 수정",
    write: "파일 작성",
    fetch: "페이지 가져옴",
    delegate: "작업 위임",
    other: "도구 실행",
  },
  many: {
    shell: (n) => `셸 명령 ${n}개 실행`,
    read: (n) => `파일 ${n}개 읽음`,
    search: (n) => `${n}개 패턴 검색`,
    edit: (n) => `파일 ${n}개 수정`,
    write: (n) => `파일 ${n}개 작성`,
    fetch: (n) => `페이지 ${n}개 가져옴`,
    delegate: (n) => `작업 ${n}개 위임`,
    other: (n) => `도구 ${n}개 실행`,
  },
  single: {
    shell: (what) => `셸 실행: ${what}`,
    read: (what) => `${what} 읽음`,
    search: (what) => `"${what}" 검색`,
    edit: (what) => `${what} 수정`,
    write: (what) => `${what} 작성`,
    fetch: (what) => `${what} 가져옴`,
    delegate: (what) => `위임: ${what}`,
  },
  tool: (name) => `${name} 실행`,
};

const JA: Verbs = {
  one: {
    shell: "シェルコマンドを実行",
    read: "ファイルを読み込み",
    search: "検索",
    edit: "ファイルを編集",
    write: "ファイルを作成",
    fetch: "ページを取得",
    delegate: "タスクを委任",
    other: "ツールを実行",
  },
  many: {
    shell: (n) => `シェルコマンド ${n} 件を実行`,
    read: (n) => `ファイル ${n} 件を読み込み`,
    search: (n) => `パターン ${n} 件を検索`,
    edit: (n) => `ファイル ${n} 件を編集`,
    write: (n) => `ファイル ${n} 件を作成`,
    fetch: (n) => `ページ ${n} 件を取得`,
    delegate: (n) => `タスク ${n} 件を委任`,
    other: (n) => `ツール ${n} 件を実行`,
  },
  single: {
    shell: (what) => `シェル実行: ${what}`,
    read: (what) => `${what} を読み込み`,
    search: (what) => `"${what}" を検索`,
    edit: (what) => `${what} を編集`,
    write: (what) => `${what} を作成`,
    fetch: (what) => `${what} を取得`,
    delegate: (what) => `委任: ${what}`,
  },
  tool: (name) => `${name} を実行`,
};

const ZH: Verbs = {
  one: {
    shell: "执行了 shell 命令",
    read: "读取了文件",
    search: "进行了搜索",
    edit: "修改了文件",
    write: "写入了文件",
    fetch: "抓取了页面",
    delegate: "委派了任务",
    other: "调用了工具",
  },
  many: {
    shell: (n) => `执行了 ${n} 条 shell 命令`,
    read: (n) => `读取了 ${n} 个文件`,
    search: (n) => `搜索了 ${n} 个模式`,
    edit: (n) => `修改了 ${n} 个文件`,
    write: (n) => `写入了 ${n} 个文件`,
    fetch: (n) => `抓取了 ${n} 个页面`,
    delegate: (n) => `委派了 ${n} 个任务`,
    other: (n) => `调用了 ${n} 个工具`,
  },
  single: {
    shell: (what) => `执行 shell: ${what}`,
    read: (what) => `读取 ${what}`,
    search: (what) => `搜索 "${what}"`,
    edit: (what) => `修改 ${what}`,
    write: (what) => `写入 ${what}`,
    fetch: (what) => `抓取 ${what}`,
    delegate: (what) => `委派: ${what}`,
  },
  tool: (name) => `${name}`,
};

const CATALOG: Record<UiLanguage, Verbs> = { en: EN, ko: KO, ja: JA, zh: ZH };

/** The verbs for a language; unknown languages already resolved to `en`. */
export function verbsFor(language: UiLanguage): Verbs {
  return CATALOG[language] ?? EN;
}

/** The one-line title the model wrote for a delegation, when it wrote one. */
function delegateTitle(call: ToolCallEntry): string | null {
  if (toolKind(call.name) !== "delegate") return null;
  const title = (call.args ?? {}).title;
  return typeof title === "string" && title.trim().length > 0 ? title.trim() : null;
}

/**
 * One line for a run of calls.
 *
 * A single call names what it touched — `Edited README.md` is worth more than
 * `Edited a file` — while a run of several only counts them. A delegation is
 * the one call the model can label itself: its `title` is already in the
 * user's language and says more than any verb here could.
 */
export function summarizeCalls(
  calls: ToolCallEntry[],
  language: UiLanguage = uiLanguage(),
): string {
  if (calls.length === 0) return "";
  const verbs = verbsFor(language);
  const kinds = new Set(calls.map((call) => toolKind(call.name)));
  const kind: ToolKind = kinds.size === 1 ? [...kinds][0] : "other";

  if (calls.length === 1) {
    const call = calls[0];
    const title = delegateTitle(call);
    if (title) return clip(title, 60);
    if (kind === "delegate") {
      // No title: the agent's name is the most the row can say.
      const agent = (call.args ?? {}).agent;
      const named = typeof agent === "string" && agent.trim().length > 0 ? agent.trim() : null;
      return named ? verbs.single.delegate(clip(named, 30)) : verbs.one.delegate;
    }
    const what = subject(call);
    if (!what) return kind === "other" ? verbs.tool(clip(call.name, 30)) : verbs.one[kind];
    switch (kind) {
      case "shell":
        return verbs.single.shell(clip(what, 40));
      case "read":
        return verbs.single.read(basename(what));
      case "edit":
        return verbs.single.edit(basename(what));
      case "write":
        return verbs.single.write(basename(what));
      case "search":
        return verbs.single.search(clip(what, 30));
      case "fetch":
        return verbs.single.fetch(clip(what, 40));
      default:
        return verbs.tool(clip(call.name, 30));
    }
  }

  return verbs.many[kind](calls.length);
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
