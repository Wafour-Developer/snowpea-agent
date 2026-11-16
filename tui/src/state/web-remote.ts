/**
 * Remote web access (capability `webRemoteControl`, 1.54.0): the second
 * listener that lets other machines open snowpea web.
 *
 * Everything here is pure. The screen, the `/web` command and the tests read
 * the same functions: the normalised status, the access link with its token
 * masked, the one-screen summary, input validation and the choice of the
 * field a core error belongs to. Only a local person may see or change any of
 * it (`web.remote.*` answers `jarvis_person_only` to anyone else).
 */

import type { WebRemoteStatus } from "../rpc/sdk.js";
import { sanitizeText } from "../util/sanitize.js";
import { say } from "./errors.js";
import { rpcErrorFacts } from "./external.js";
import { degradedLine, normalizeDegraded } from "./settings-degraded.js";

export function webRemoteSupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("webRemoteControl");
}

/** The one line an older daemon gets instead of the feature. */
export const WEB_REQUIRES_LINE = "Remote web access: requires a newer core — this daemon is older. Run /update.";

/** The palette summary of `/web` on an older daemon. */
export const WEB_REQUIRES_SUMMARY = "requires a newer core — this daemon is older (/update)";

/** The warning that never goes away: the listener speaks plain HTTP. */
export const PLAIN_HTTP_WARNING =
  "Plain HTTP: the link, the password and everything you do travel unencrypted. Off a network you trust, use TLS (a reverse proxy), a VPN or an ssh tunnel.";

export const MIN_PASSWORD = 8;

// --- the status ----------------------------------------------------------------

export interface WebStatus {
  enabled: boolean;
  listening: boolean;
  host: string;
  port: number;
  mode: "token" | "password";
  passwordSet: boolean;
  username: string;
  listenUrls: string[];
  /** The access link with its token; treat it as a secret. */
  accessUrl: string | null;
  sessions: number;
  turnsRunning: number;
  /** Scheduled jobs a remote login created that still exist (1.55.0); they keep running after the login ends. */
  remoteJobs: number;
  /** What remote logins left that keeps acting after the login ends (1.55.0): MCP servers and skills marked as theirs. */
  remoteLeft: { jobs: number; mcpServers: number; skills: number };
  /** The settings.json sections core could not read (1.55.0); empty when none, "*" is the whole file. */
  settingsDegraded: string[];
  /** Why it is not listening though it should. */
  error: string | null;
  /** settings.json says otherwise than what runs. */
  needsRestart: boolean;
  /** The warning core sent (it begins "Anyone who has the link…"), if any. */
  warning: string | null;
  plainHttp: boolean;
  /** The widest mode a remote sign-in may use (1.55.0); null when this core does not report it. */
  maxMode: MaxMode | null;
}

export type MaxMode = "plan" | "accept" | "auto";

export const MAX_MODES: readonly MaxMode[] = ["plan", "accept", "auto"];

const count = (value: unknown): number => (typeof value === "number" && value > 0 ? Math.floor(value) : 0);

const text = (value: unknown): string => (typeof value === "string" ? sanitizeText(value) : "");

export function normalizeStatus(given: Partial<WebRemoteStatus> | null | undefined): WebStatus {
  // `remoteLeft` and `settingsDegraded` are 1.55.0 fields; read them loosely so an older SDK build still compiles.
  const raw = given as (Partial<WebRemoteStatus> & { remoteLeft?: { jobs?: unknown; mcpServers?: unknown; skills?: unknown } | null; settingsDegraded?: unknown }) | null | undefined;
  const port = typeof raw?.port === "number" && Number.isFinite(raw.port) ? raw.port : 0;
  return {
    enabled: raw?.enabled === true,
    listening: raw?.listening === true,
    host: text(raw?.host),
    port,
    mode: raw?.mode === "password" ? "password" : "token",
    passwordSet: raw?.passwordSet === true,
    username: text(raw?.username),
    listenUrls: (Array.isArray(raw?.listenUrls) ? raw.listenUrls : []).map(text).filter((url) => url !== ""),
    accessUrl: typeof raw?.accessUrl === "string" && raw.accessUrl !== "" ? sanitizeText(raw.accessUrl) : null,
    sessions: typeof raw?.sessions === "number" ? raw.sessions : 0,
    turnsRunning: typeof raw?.turnsRunning === "number" ? raw.turnsRunning : 0,
    remoteJobs: Math.max(count(raw?.remoteJobs), count(raw?.remoteLeft?.jobs)),
    remoteLeft: { jobs: count(raw?.remoteLeft?.jobs), mcpServers: count(raw?.remoteLeft?.mcpServers), skills: count(raw?.remoteLeft?.skills) },
    settingsDegraded: normalizeDegraded(raw?.settingsDegraded),
    error: text(raw?.error) || null,
    needsRestart: raw?.needsRestart === true,
    warning: text(raw?.warning) || null,
    // Plain HTTP unless core says otherwise: the listener has no TLS of its own.
    plainHttp: raw?.plainHttp !== false,
    maxMode: raw?.maxMode === "plan" || raw?.maxMode === "accept" || raw?.maxMode === "auto" ? raw.maxMode : null,
  };
}

// --- the highest mode for remote sign-ins (1.55.0) ---------------------------------------

/** "Highest mode for remote sign-ins: plan / accept / [auto]"; the chosen one in brackets. */
export function maxModeRow(value: MaxMode): string {
  const label = say("Highest mode for remote sign-ins", "원격 로그인의 최고 모드");
  return `${label}: ${MAX_MODES.map((mode) => (mode === value ? `[${mode}]` : mode)).join(" / ")}`;
}

/** What the limit does and does not do, in one honest sentence. */
export const MAX_MODE_EXPLAIN = (): string =>
  say(
    "It stops unattended runs above that mode; whoever holds the link can still approve each action.",
    "그 모드를 넘는 무인 실행을 막습니다. 링크를 가진 사람은 여전히 각 동작을 승인할 수 있습니다.",
  );

/** The next value when the key is pressed: plan, accept, auto, plan… */
export function nextMaxMode(value: MaxMode): MaxMode {
  return MAX_MODES[(MAX_MODES.indexOf(value) + 1) % MAX_MODES.length];
}

/** The `settings.set` patch for the limit; global, by a local person. */
export const maxModePatch = (value: MaxMode): Record<string, unknown> => ({ web: { remote: { maxMode: value } } });

// --- the access link -------------------------------------------------------------

const TOKEN = /([?&]t=)[^&#\s]+/;
export const MASK = "********";

/** The link with its token replaced by asterisks. */
export function maskLink(link: string): string {
  return link.replace(TOKEN, `$1${MASK}`);
}

/** The link as shown: masked unless the person asked to see it. */
export function shownLink(link: string, revealed: boolean): string {
  return revealed ? link : maskLink(link);
}

// --- what is said about it ---------------------------------------------------------

/** "listening on 0.0.0.0:8787", "not listening", "not listening — <why>". */
export function listenLine(status: WebStatus): string {
  if (status.listening) return `Listening on ${status.host}:${status.port}`;
  const why = status.error ? ` — ${status.error}` : status.enabled ? " — it should be on, but it is not" : "";
  return `Not listening${why}`;
}

export function modeLine(status: WebStatus): string {
  if (status.mode === "password") {
    return `Sign-in: username and password${status.username ? ` (user ${status.username})` : " (no username set — it listens on this machine only)"}`;
  }
  return `Sign-in: access link${status.passwordSet ? " (a password is stored but not in use)" : ", no password"}`;
}

export function sessionsLine(status: WebStatus): string {
  const people = `${status.sessions} signed in`;
  return status.turnsRunning > 0 ? `${people} · ${status.turnsRunning} turn${status.turnsRunning === 1 ? "" : "s"} running` : people;
}

/**
 * "2 scheduled jobs made from a remote login" (1.55.0), or null when there are
 * none. The jobs keep running after that login ends, so they are worth a line.
 */
export function remoteJobsLine(status: WebStatus): string | null {
  if (status.remoteJobs <= 0) return null;
  const n = status.remoteJobs;
  return `${n} scheduled job${n === 1 ? "" : "s"} made from a remote login ${n === 1 ? "keeps" : "keep"} running after that login ends (/schedule).`;
}

/**
 * "Remote logins also left 2 MCP servers and 1 skill (counts, not a guarantee)"
 * (1.55.0), or null when there are none. The marks live in files, so it is a
 * count and not a promise.
 */
export function remoteLeftLine(status: WebStatus): string | null {
  const { mcpServers, skills } = status.remoteLeft;
  const parts = [
    ...(mcpServers > 0 ? [`${mcpServers} MCP server${mcpServers === 1 ? "" : "s"}`] : []),
    ...(skills > 0 ? [`${skills} skill${skills === 1 ? "" : "s"}`] : []),
  ];
  if (parts.length === 0) return null;
  return `Remote logins also left ${parts.join(" and ")} that keep working after the login ends (a count, not a guarantee).`;
}

/** The tag a job list shows for a job a remote login created (`createdBy: "remote_login"`); empty otherwise. */
export const REMOTE_JOB_TAG = "[remote]";

export function jobTag(createdBy: unknown): string {
  return createdBy === "remote_login" ? REMOTE_JOB_TAG : "";
}

export const RESTART_LINE = "settings.json says otherwise than what runs: the next daemon start will differ. Start with “remember” to keep what runs.";

/** What `/web status` prints (the token stays masked). */
export function summaryLines(status: WebStatus, diskNotice: string | null = null): string[] {
  // Core's warning comes first and is always printed (the transcript marks the first line of a note with "!").
  const lines = [status.warning ?? PLAIN_HTTP_WARNING, ...(diskNotice ? [`! ${diskNotice}`] : []), "Remote web access", listenLine(status)];
  if (status.listening) {
    for (const url of status.listenUrls) lines.push(`Open: ${url}`);
    lines.push(sessionsLine(status));
  }
  const jobs = remoteJobsLine(status);
  if (jobs) lines.push(jobs);
  const left = remoteLeftLine(status);
  if (left) lines.push(left);
  const degraded = degradedLine(status.settingsDegraded);
  if (degraded) lines.push(`! ${degraded}`);
  lines.push(modeLine(status));
  if (status.listening && status.mode === "token" && status.accessUrl) lines.push(`Access link: ${maskLink(status.accessUrl)}  (reveal it in /web)`);
  if (status.maxMode !== null) lines.push(`${maxModeRow(status.maxMode)}. ${MAX_MODE_EXPLAIN()}`);
  if (status.needsRestart) lines.push(RESTART_LINE);
  lines.push("Manage it with /web.");
  return lines;
}

// --- input ------------------------------------------------------------------------

export interface ListenForm {
  host: string;
  port: string;
  username: string;
  remember: boolean;
}

export type ListenField = "host" | "port" | "username";

/** The form a start begins with: what runs now, else core's defaults. */
export function initialForm(status: WebStatus | null): ListenForm {
  return {
    host: status?.host || "0.0.0.0",
    port: String(status && status.port > 0 ? status.port : 8787),
    username: status?.username ?? "",
    remember: status?.enabled === true,
  };
}

export type FormErrors = Partial<Record<ListenField, string>>;

/** What can be said before asking core. */
export function validateForm(form: ListenForm): FormErrors {
  const errors: FormErrors = {};
  if (form.host.trim() === "") errors.host = "Type an address to listen on, e.g. 0.0.0.0 (every interface).";
  else if (/\s/.test(form.host.trim())) errors.host = "An address has no spaces.";
  const port = form.port.trim();
  if (!/^\d+$/.test(port) || Number(port) > 65535) errors.port = "A port is a number from 0 to 65535 (0 picks a free one).";
  return errors;
}

/** `web.remote.listen` params for a valid form. */
export function listenParams(form: ListenForm): { host: string; port: number; save: boolean; username?: string } {
  const username = form.username.trim();
  return { host: form.host.trim(), port: Number(form.port.trim()), save: form.remember, ...(username !== "" ? { username } : {}) };
}

/**
 * `web.remote.listen` failures carry `details {field, reason}` (1.55.0): the
 * reason alone names the field when `field` is missing.
 */
const REASON_FIELD: Record<string, ListenField> = {
  in_use: "port",
  permission: "port",
  out_of_range: "port",
  bad_address: "host",
  required: "username",
};

/**
 * The field a refusal from core belongs to, with its words: `details.field`
 * when core names one, else the message (a port in use is the port's, a bad
 * address the host's, a name the username's). Anything else belongs to the form.
 */
export function fieldError(error: unknown): { field: ListenField | null; message: string } {
  const raw = error as { message?: unknown; data?: { details?: unknown } } | null;
  const message = sanitizeText(typeof raw?.message === "string" ? raw.message : String(error));
  const details = raw?.data?.details && typeof raw.data.details === "object" ? (raw.data.details as Record<string, unknown>) : {};
  const named = [details["field"], details["param"]].find((value) => typeof value === "string") as string | undefined;
  if (named === "host" || named === "port" || named === "username") return { field: named, message };
  // 1.55.0: core says why in `details.reason` (typed), so the field is known without reading its English.
  const byReason = REASON_FIELD[String(details["reason"] ?? "")];
  if (byReason) return { field: byReason, message };
  if (/in use|EADDRINUSE|already|\bport\b/i.test(message)) return { field: "port", message };
  if (/\buser(name)?\b/i.test(message)) return { field: "username", message };
  if (/address|host|resolve|bind|assign|invalid|unreachable/i.test(message)) return { field: "host", message };
  return { field: null, message };
}

/** Why a password cannot be set, or null. */
export function passwordProblem(first: string, second: string): string | null {
  if (Array.from(first).length < MIN_PASSWORD) return `A password needs at least ${MIN_PASSWORD} characters.`;
  if (first !== second) return "The two entries differ — start again.";
  return null;
}

/** One `*` per typed character, never the password. */
export function hidden(value: string): string {
  return "*".repeat(Array.from(value).length);
}

/** The line for a refused `web.remote.*` call from someone who is not the local person. */
export function localPersonOnlyLine(error: unknown): string {
  const kind = rpcErrorFacts(error).kind;
  if (kind === "remote_login") return "Remote access can only be seen and changed from a local snowpea, not from a remote login.";
  return "Only a person on this machine can see or change remote access (the TUI, the IDE or the CLI there).";
}

// --- copying the link ----------------------------------------------------------------

/**
 * True when this terminal is known to obey OSC 52 (set the clipboard). Nothing
 * is guessed: `SNOWPEA_OSC52=1` forces it on, `=0` off, otherwise only
 * terminals that document support count.
 */
export function osc52Supported(env: Record<string, string | undefined>): boolean {
  if (env["SNOWPEA_OSC52"] === "0") return false;
  if (env["SNOWPEA_OSC52"] === "1") return true;
  // Inside tmux or screen the sequence is swallowed unless they are configured for it.
  if (env["TMUX"] || env["STY"]) return false;
  const program = env["TERM_PROGRAM"] ?? "";
  const term = env["TERM"] ?? "";
  return (
    ["iTerm.app", "WezTerm", "ghostty"].includes(program) ||
    /kitty|alacritty|foot|wezterm|ghostty/i.test(term) ||
    Boolean(env["KITTY_WINDOW_ID"]) ||
    Boolean(env["WT_SESSION"])
  );
}

/** The escape sequence that puts `value` on the system clipboard. */
export function osc52Sequence(value: string): string {
  return `\u001b]52;c;${Buffer.from(value, "utf8").toString("base64")}\u0007`;
}
