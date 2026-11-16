/**
 * Project trust (capability `projectTrust`, protocol 1.55.0): which of a
 * project's own control files (hooks, MCP servers, commands with allowed-tools,
 * agents with a permission, prompt overrides, widening settings, a project
 * `defaultMode: auto`) may make snowpea do things, and the person's decision to
 * pin them.
 *
 * Everything here is pure: the `/trust` screen, the `/trust status` line, the
 * hook and the tests read the same functions. The status is the daemon's
 * (`project.trust.status`); trusting sends back exactly the `digests` of the
 * status that was shown, so what is pinned is what the person saw.
 */

import type { ProjectTrustStatus } from "../rpc/sdk.js";
import { say } from "./errors.js";
import { rpcErrorFacts } from "./external.js";
import { wrapSegments } from "./security.js";
import type { Line, Segment } from "../layout/transcript.js";
import { sanitizeText } from "../util/sanitize.js";

export function trustSupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("projectTrust");
}

/** The one line an older daemon gets instead of the feature. */
export const TRUST_REQUIRES_LINE = "Project trust: requires a newer core — this daemon is older. Run /update.";

/** The palette summary of `/trust` on an older daemon. */
export const TRUST_REQUIRES_SUMMARY = "requires a newer core — this daemon is older (/update)";

// --- scopes ----------------------------------------------------------------------

export type TrustScope = "hooks" | "mcp" | "commands" | "agents" | "prompts" | "settings" | "mode" | "externalCli";

/** In the order the screen lists them. */
export const TRUST_SCOPES: readonly TrustScope[] = ["hooks", "mcp", "commands", "agents", "prompts", "settings", "mode", "externalCli"];

/** What trusting grants when nothing else is said; `mode` and `externalCli` must be chosen on purpose. */
export const DEFAULT_TRUST_SCOPES: readonly TrustScope[] = ["hooks", "mcp", "commands", "agents", "prompts", "settings"];

const SCOPE_TITLE: Record<TrustScope, [string, string]> = {
  hooks: ["Hooks", "훅"],
  mcp: ["MCP servers", "MCP 서버"],
  commands: ["Commands and skills", "명령과 스킬"],
  agents: ["Agents", "에이전트"],
  prompts: ["Prompt overrides", "프롬프트 덮어쓰기"],
  settings: ["Project settings", "프로젝트 설정"],
  mode: ["Automatic mode", "자동 모드"],
  externalCli: ["External CLI sessions", "외부 CLI 세션"],
};

const SCOPE_WHAT: Record<TrustScope, [string, string]> = {
  hooks: ["run the commands the project's hooks name", "프로젝트 훅이 정한 명령을 실행"],
  mcp: ["start the project's MCP servers", "프로젝트의 MCP 서버를 시작"],
  commands: ["let project commands and skills use the tools they ask for", "프로젝트 명령과 스킬이 요청한 도구를 쓰게 함"],
  agents: ["let project agents carry a permission", "프로젝트 에이전트가 권한을 갖게 함"],
  prompts: ["use the project's prompt overrides", "프로젝트의 프롬프트 덮어쓰기를 사용"],
  settings: ["apply the project's widening settings (allowlist, models, providers)", "프로젝트의 확장 설정(허용 목록, 모델, 제공자)을 적용"],
  mode: ["let the project start sessions in auto mode", "프로젝트가 세션을 자동 모드로 시작하게 함"],
  externalCli: ["let external CLI sessions open here without asking", "외부 CLI 세션을 묻지 않고 여기서 열게 함"],
};

/** Why a scope is off by default, shown beside it. */
const SCOPE_OFF_NOTE: Partial<Record<TrustScope, [string, string]>> = {
  mode: [
    "Off by default: it lets the project file start sessions with no approvals at all. Turn it on only if you mean that.",
    "기본값은 꺼짐: 프로젝트 파일이 승인 없이 세션을 시작할 수 있게 됩니다. 의도한 경우에만 켜세요.",
  ],
  externalCli: [
    "Off by default: external CLIs (Claude Code, Codex, …) then open here without the first-time question.",
    "기본값은 꺼짐: 외부 CLI(Claude Code, Codex 등)가 처음 묻는 질문 없이 열립니다.",
  ],
};

export const scopeTitle = (scope: TrustScope): string => say(...SCOPE_TITLE[scope]);
export const scopeWhat = (scope: TrustScope): string => say(...SCOPE_WHAT[scope]);

const isScope = (value: unknown): value is TrustScope => typeof value === "string" && (TRUST_SCOPES as readonly string[]).includes(value);

// --- the status, normalised -------------------------------------------------------

export type FileState = "active" | "untrusted" | "new" | "changed";

/** A command a hook or MCP server runs: the end of it is never hidden. */
export interface CommandView {
  head: string;
  tail: string;
  length: number;
  label: string;
}

export interface TrustFileView {
  path: string;
  scope: TrustScope;
  state: FileState;
  summary: string;
  commands: CommandView[];
}

export type NotPinnedReason = "computed" | "project-script" | "package-runner" | "module" | "missing" | "too-large" | "too-many" | "unreadable";

export interface NotPinnedView {
  command: CommandView;
  reason: string;
  source: string;
}

export interface RunView {
  path: string;
  state: FileState;
  by: string[];
}

export interface TrustView {
  workdir: string;
  trusted: boolean;
  scopes: TrustScope[];
  by: string | null;
  at: string | null;
  files: TrustFileView[];
  inactive: string[];
  changed: string[];
  defaultModeAuto: boolean;
  modeActive: boolean;
  /** Exactly what `project.trust.set` must carry back. */
  digests: Record<string, string>;
  notPinned: NotPinnedView[];
  runs: RunView[];
}

const text = (value: unknown): string => (typeof value === "string" ? sanitizeText(value) : "");
const texts = (value: unknown): string[] => (Array.isArray(value) ? value.map(text).filter((item) => item !== "") : []);
const state = (value: unknown): FileState => (value === "active" || value === "new" || value === "changed" ? value : "untrusted");

function command(raw: any): CommandView | null {
  const head = text(raw?.head);
  if (head === "" && text(raw?.tail) === "") return null;
  const length = typeof raw?.length === "number" && Number.isFinite(raw.length) ? raw.length : Array.from(head + text(raw?.tail)).length;
  return { head, tail: text(raw?.tail), length, label: text(raw?.label) };
}

export function normalizeTrust(raw: Partial<ProjectTrustStatus> | null | undefined): TrustView {
  const digests: Record<string, string> = {};
  const given = (raw as any)?.digests;
  if (given && typeof given === "object" && !Array.isArray(given)) {
    for (const [path, digest] of Object.entries(given)) if (typeof digest === "string") digests[path] = digest;
  }
  return {
    workdir: text(raw?.workdir),
    trusted: raw?.trusted === true,
    scopes: (Array.isArray(raw?.scopes) ? raw.scopes : []).filter(isScope),
    by: text(raw?.by) || null,
    at: text(raw?.at) || null,
    files: (Array.isArray(raw?.files) ? raw.files : [])
      .map(
        (file: any): TrustFileView => ({
          path: text(file?.path),
          scope: isScope(file?.scope) ? file.scope : "settings",
          state: state(file?.state),
          summary: text(file?.summary),
          commands: (Array.isArray(file?.commands) ? file.commands : []).map(command).filter((one: CommandView | null): one is CommandView => one !== null),
        }),
      )
      .filter((file: TrustFileView) => file.path !== ""),
    inactive: texts(raw?.inactive),
    changed: texts(raw?.changed),
    defaultModeAuto: raw?.defaultModeAuto === true,
    modeActive: raw?.modeActive === true,
    digests,
    notPinned: (Array.isArray(raw?.notPinned) ? raw.notPinned : [])
      .map((entry: any) => ({ command: command(entry?.command), reason: text(entry?.reason), source: text(entry?.source) }))
      .filter((entry: { command: CommandView | null }): entry is NotPinnedView => entry.command !== null),
    runs: (Array.isArray(raw?.runs) ? raw.runs : [])
      .map((run: any): RunView => ({ path: text(run?.path), state: state(run?.state), by: texts(run?.by) }))
      .filter((run: RunView) => run.path !== ""),
  };
}

/** The scopes selected when the screen opens: the defaults, plus whatever is trusted already. */
export function initialSelection(view: TrustView): Set<TrustScope> {
  return new Set<TrustScope>([...DEFAULT_TRUST_SCOPES, ...view.scopes]);
}

// --- words ------------------------------------------------------------------------

const REASON_WORDS: Record<NotPinnedReason, [string, string]> = {
  computed: ["the command builds what it runs while it runs", "명령이 실행 중에 실행할 내용을 만듭니다"],
  "project-script": ["it runs the project's own script (npm run, make, …), which can change", "프로젝트 자체 스크립트(npm run, make 등)를 실행하며 바뀔 수 있습니다"],
  "package-runner": ["it fetches or resolves a package when it runs (npx, uvx, …)", "실행할 때 패키지를 받아 옵니다(npx, uvx 등)"],
  module: ["it runs a Python module (python -m), and the module can change", "Python 모듈(python -m)을 실행하며 모듈은 바뀔 수 있습니다"],
  missing: ["it names a project file that is not there", "프로젝트에 없는 파일을 가리킵니다"],
  "too-large": ["the file is too large to pin", "파일이 너무 커서 고정할 수 없습니다"],
  "too-many": ["there are too many files to pin", "고정하기엔 파일이 너무 많습니다"],
  unreadable: ["the control file cannot be read", "제어 파일을 읽을 수 없습니다"],
};

/** The reason in plain words; a value this build does not know is shown as core sent it. */
export function reasonWords(reason: string): string {
  const known = (REASON_WORDS as Record<string, [string, string]>)[reason];
  return known ? say(...known) : reason;
}

export const NOT_PINNED_WARNING = (): string =>
  say(
    "Trusting pins these command lines, not what they run: it can change later without asking you again.",
    "신뢰하면 이 명령줄은 고정되지만 그것이 실행하는 내용은 고정되지 않습니다. 나중에 다시 묻지 않고 바뀔 수 있습니다.",
  );

export const STATE_WORD: Record<FileState, [string, string]> = {
  active: ["active", "사용 중"],
  untrusted: ["untrusted", "신뢰 안 함"],
  new: ["new", "새 파일"],
  changed: ["changed", "바뀜"],
};

export const stateWord = (value: FileState): string => say(...STATE_WORD[value]);

const STATE_COLOR: Record<FileState, string | undefined> = { active: "green", untrusted: "yellow", new: "yellow", changed: "red" };

/** A command as one string: head, an ellipsis and tail when long, and the full length. */
export function commandText(one: CommandView): string {
  const body = one.tail === "" ? one.head : `${one.head} … ${one.tail}`;
  const chars = one.tail === "" ? "" : ` (${one.length} ${say("characters", "자")})`;
  return `${body}${chars}`;
}

/** `project.trust.status` as the single line of `/trust status`. */
export function statusLine(view: TrustView): string {
  const name = view.workdir.split(/[\\/]/).filter(Boolean).pop() ?? view.workdir;
  const pending = view.files.filter((file) => file.state !== "active").length;
  if (!view.trusted) {
    return say(
      `Project ${name}: not trusted — ${view.files.length} control file${view.files.length === 1 ? " waits" : "s wait"} for you (/trust)`,
      `프로젝트 ${name}: 신뢰하지 않음 — 제어 파일 ${view.files.length}개가 확인을 기다립니다 (/trust)`,
    );
  }
  const scopes = view.scopes.map(scopeTitle).join(", ");
  const tail = pending > 0 ? say(` · ${pending} to review (/trust)`, ` · 확인할 파일 ${pending}개 (/trust)`) : "";
  return say(`Project ${name}: trusted for ${scopes}${tail}`, `프로젝트 ${name}: 신뢰함 — ${scopes}${tail}`);
}

// --- the screen's rows and lines ------------------------------------------------------

export type RowKind = { kind: "scope"; scope: TrustScope } | { kind: "runs" } | { kind: "notPinned" };

/** The rows the cursor visits: a scope with files (and `mode`, `externalCli` always), then the run files and the unpinned commands. */
export function trustRows(view: TrustView): RowKind[] {
  const rows: RowKind[] = TRUST_SCOPES.filter(
    (scope) => scope === "mode" || scope === "externalCli" || view.files.some((file) => file.scope === scope),
  ).map((scope): RowKind => ({ kind: "scope", scope }));
  if (view.runs.length > 0) rows.push({ kind: "runs" });
  if (view.notPinned.length > 0) rows.push({ kind: "notPinned" });
  return rows;
}

/** Default scopes that have nothing in this project: trusted along with the rest, never shown as rows. */
export function quietScopes(view: TrustView): TrustScope[] {
  return DEFAULT_TRUST_SCOPES.filter((scope) => !view.files.some((file) => file.scope === scope));
}

export interface TrustBody {
  lines: Line[];
  /** First and last (exclusive) line of the block the cursor is on. */
  start: number;
  end: number;
}

const wrap = (key: string, segments: Segment[], width: number, indent = "    "): Line[] => wrapSegments(key, segments, width, indent);

function commandLines(key: string, one: CommandView, width: number, lead: string): Line[] {
  const label = one.label ? `${one.label}: ` : "";
  return wrap(key, [{ text: lead }, { text: label, dimColor: true }, { text: commandText(one), color: "cyan" }], width, `${lead}  `);
}

/** Everything under the cursor row's checkbox: its files, their summaries and the commands they run. */
function scopeBlock(view: TrustView, scope: TrustScope, key: string, width: number): Line[] {
  const files = view.files.filter((file) => file.scope === scope);
  const lines: Line[] = [];
  const off = SCOPE_OFF_NOTE[scope];
  if (off && !view.scopes.includes(scope)) lines.push(...wrap(`${key}-off`, [{ text: "    " }, { text: say(...off), color: "yellow" }], width, "      "));
  if (scope === "mode" && view.defaultModeAuto) {
    lines.push(
      ...wrap(
        `${key}-auto`,
        [{ text: "    " }, { text: view.modeActive ? say("The project file asks for auto mode, and it applies.", "프로젝트 파일이 자동 모드를 요청했고 적용됩니다.") : say("The project file asks for auto mode; it is held at accept until you trust this.", "프로젝트 파일이 자동 모드를 요청했습니다. 신뢰하기 전까지는 accept로 제한됩니다."), color: "yellow" }],
        width,
        "      ",
      ),
    );
  }
  if (files.length === 0 && scope !== "mode" && scope !== "externalCli") {
    lines.push({ key: `${key}-none`, segments: [{ text: "    " }, { text: say("no files", "파일 없음"), dimColor: true }] });
  }
  files.forEach((file, index) => {
    const fkey = `${key}-f${index}`;
    lines.push(
      ...wrap(fkey, [{ text: "    " }, { text: file.path, bold: true }, { text: `  ${stateWord(file.state)}`, color: STATE_COLOR[file.state], bold: file.state !== "active" }], width, "      "),
    );
    if (file.commands.length > 0) {
      file.commands.forEach((one, at) => lines.push(...commandLines(`${fkey}-c${at}`, one, width, "      ")));
    } else if (file.summary !== "") {
      lines.push(...wrap(`${fkey}-s`, [{ text: "      " }, { text: file.summary, dimColor: true }], width, "        "));
    }
  });
  return lines;
}

/**
 * The whole body as lines, with the block of the cursor row marked. A row is
 * `› [x] Hooks (2 files)` with its files beneath it.
 */
export function trustBody(view: TrustView, selected: ReadonlySet<TrustScope>, cursor: number, width: number): TrustBody {
  const rows = trustRows(view);
  const lines: Line[] = [];
  let start = 0;
  let end = 0;
  rows.forEach((row, index) => {
    const here = index === cursor;
    const from = lines.length;
    const pointer: Segment = { text: here ? "› " : "  ", color: "cyan", bold: true };
    const key = `row-${index}`;
    if (row.kind === "scope") {
      const on = selected.has(row.scope);
      const count = view.files.filter((file) => file.scope === row.scope).length;
      const trusted = view.scopes.includes(row.scope);
      lines.push(
        ...wrap(
          key,
          [
            pointer,
            { text: on ? "[x] " : "[ ] ", color: on ? "green" : undefined, bold: here },
            { text: scopeTitle(row.scope), bold: true },
            ...(count > 0 ? [{ text: ` (${count})`, dimColor: true }] : []),
            ...(trusted ? [{ text: `  ${say("trusted", "신뢰함")}`, color: "green" }] : []),
            { text: ` — ${scopeWhat(row.scope)}`, dimColor: !here },
          ],
          width,
          "      ",
        ),
      );
      lines.push(...scopeBlock(view, row.scope, key, width));
    } else if (row.kind === "runs") {
      lines.push(
        ...wrap(key, [pointer, { text: say("Files they run", "실행되는 파일"), bold: true }, { text: ` (${view.runs.length}) — ${say("pinned with the project", "프로젝트와 함께 고정")}`, dimColor: !here }], width, "      "),
      );
      view.runs.forEach((run, at) => {
        lines.push(
          ...wrap(
            `${key}-r${at}`,
            [{ text: "    " }, { text: run.path }, { text: `  ${stateWord(run.state)}`, color: STATE_COLOR[run.state] }, ...(run.by.length > 0 ? [{ text: `  ← ${run.by.join(", ")}`, dimColor: true }] : [])],
            width,
            "      ",
          ),
        );
      });
    } else {
      lines.push(
        ...wrap(key, [pointer, { text: say("Not pinned", "고정되지 않음"), bold: true, color: "yellow" }, { text: ` (${view.notPinned.length})`, color: "yellow" }], width, "      "),
      );
      lines.push(...wrap(`${key}-warn`, [{ text: "    ! ", color: "yellow", bold: true }, { text: NOT_PINNED_WARNING(), color: "yellow" }], width, "      "));
      view.notPinned.forEach((entry, at) => {
        lines.push(...commandLines(`${key}-n${at}`, entry.command, width, "    "));
        lines.push(
          ...wrap(`${key}-n${at}-why`, [{ text: "      " }, { text: reasonWords(entry.reason), color: "yellow" }, { text: entry.source ? `  (${entry.source})` : "", dimColor: true }], width, "        "),
        );
      });
    }
    if (here) {
      start = from;
      end = lines.length;
    }
  });
  if (rows.length === 0) lines.push({ key: "none", segments: [{ text: say("This project brings no control files.", "이 프로젝트에는 제어 파일이 없습니다."), dimColor: true }] });
  return { lines, start, end };
}

/** The lines under the title: where, whether it is trusted, and what is waiting. */
export function trustHeader(view: TrustView, width: number): Line[] {
  const pending = view.files.filter((file) => file.state !== "active").length;
  const lines: Line[] = [
    ...wrap("where", [{ text: view.workdir, color: "cyan" }], width, ""),
    ...wrap(
      "state",
      [
        view.trusted
          ? { text: say(`Trusted for ${view.scopes.map(scopeTitle).join(", ")}${view.by ? ` (by ${view.by})` : ""}.`, `신뢰함: ${view.scopes.map(scopeTitle).join(", ")}${view.by ? ` (${view.by})` : ""}.`), color: "green" }
          : { text: say("Not trusted: its own control files are off.", "신뢰하지 않음: 제어 파일은 꺼져 있습니다."), color: "yellow" },
        ...(pending > 0 ? [{ text: say(`  ${pending} file${pending === 1 ? "" : "s"} to review.`, `  확인할 파일 ${pending}개.`), color: "yellow" }] : []),
      ],
      width,
      "",
    ),
  ];
  if (view.notPinned.length > 0) {
    lines.push(
      ...wrap(
        "np",
        [{ text: say(`! ${view.notPinned.length} command${view.notPinned.length === 1 ? "" : "s"} cannot be pinned (see "Not pinned").`, `! 고정할 수 없는 명령 ${view.notPinned.length}개 ("고정되지 않음" 참고).`), color: "yellow" }],
        width,
        "  ",
      ),
    );
  }
  return lines;
}

// --- sending and failing ----------------------------------------------------------------

/** The params of `project.trust.set` for the screen as shown: exactly the digests of that status. */
export function setParams(view: TrustView, selected: ReadonlySet<TrustScope>): { workdir: string; scopes: TrustScope[]; digests: Record<string, string> } {
  return { workdir: view.workdir, scopes: TRUST_SCOPES.filter((scope) => selected.has(scope)), digests: { ...view.digests } };
}

/** The files that moved between the status shown and the disk (`trust_changed`, `details`). */
export interface ChangedFiles {
  changed: string[];
  added: string[];
  missing: string[];
}

export function changedFiles(error: unknown): ChangedFiles {
  const details = (error as { data?: { details?: Record<string, unknown> | null } } | null)?.data?.details ?? {};
  return { changed: texts(details["changed"]), added: texts(details["added"]), missing: texts(details["missing"]) };
}

/** `trust_changed` in words: nothing was trusted, what moved, and that the key is needed again. */
export function changedLines(files: ChangedFiles): string[] {
  const parts: string[] = [];
  if (files.changed.length > 0) parts.push(say(`changed: ${files.changed.join(", ")}`, `바뀐 파일: ${files.changed.join(", ")}`));
  if (files.added.length > 0) parts.push(say(`new: ${files.added.join(", ")}`, `새 파일: ${files.added.join(", ")}`));
  if (files.missing.length > 0) parts.push(say(`gone: ${files.missing.join(", ")}`, `사라진 파일: ${files.missing.join(", ")}`));
  return [
    say("The project's files changed after you were shown them. Nothing was trusted.", "보여 드린 뒤 프로젝트 파일이 바뀌었습니다. 아무것도 신뢰하지 않았습니다."),
    ...(parts.length > 0 ? [parts.join(" · ")] : []),
    say("The list below is reloaded. Read it, then press t again.", "아래 목록을 다시 불러왔습니다. 읽어 본 뒤 t를 다시 누르세요."),
  ];
}

/** Why core refused `set` / `revoke`: a person on this machine only. */
export function trustRefusalLine(error: unknown): string {
  const facts = rpcErrorFacts(error);
  const kind = facts.kind;
  if (kind === "remote_login") return say("Only snowpea on this machine can change project trust, not a remote login.", "프로젝트 신뢰는 이 컴퓨터의 snowpea만 바꿀 수 있습니다. 원격 로그인은 안 됩니다.");
  if (kind === "browser" || kind === "connector") return say("A browser host or connector cannot change project trust.", "브라우저 호스트나 커넥터는 프로젝트 신뢰를 바꿀 수 없습니다.");
  return say("Only a person on this machine can change project trust — not Jarvis or a script.", "프로젝트 신뢰는 이 컴퓨터의 사람만 바꿀 수 있습니다. Jarvis나 스크립트는 안 됩니다.");
}

/** `project.trust.changed` payload, as the hook and the tests read it. */
export interface TrustChanged {
  workdir: string;
  scopes: TrustScope[];
  by: string | null;
  reason: "set" | "revoke" | "migration" | "store_lost";
  projects: string[];
}

export function normalizeChanged(raw: unknown): TrustChanged {
  const payload = (raw ?? {}) as Record<string, unknown>;
  const reason = payload["reason"];
  return {
    workdir: text(payload["workdir"]),
    scopes: (Array.isArray(payload["scopes"]) ? payload["scopes"] : []).filter(isScope),
    by: text(payload["by"]) || null,
    reason: reason === "revoke" || reason === "migration" || reason === "store_lost" ? reason : "set",
    projects: texts(payload["projects"]),
  };
}

/** Whether a change concerns this project (a migration and a lost store concern all of them). */
export function concerns(change: TrustChanged, workdir: string): boolean {
  return change.reason === "migration" || change.reason === "store_lost" || change.workdir === "" || change.workdir === workdir;
}

export const STORE_LOST_LINE = (): string =>
  say(
    "snowpea lost its record of trusted projects: no project is trusted until you trust it again (/trust).",
    "신뢰한 프로젝트 기록이 사라졌습니다. 다시 신뢰하기 전까지 어떤 프로젝트도 신뢰되지 않습니다 (/trust).",
  );
