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
import { rpcErrorFacts } from "./external.js";

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
  /** Why it is not listening though it should. */
  error: string | null;
  /** settings.json says otherwise than what runs. */
  needsRestart: boolean;
  /** The warning core sent (the plain-HTTP one), if any. */
  warning: string | null;
  plainHttp: boolean;
}

const text = (value: unknown): string => (typeof value === "string" ? sanitizeText(value) : "");

export function normalizeStatus(raw: Partial<WebRemoteStatus> | null | undefined): WebStatus {
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
    error: text(raw?.error) || null,
    needsRestart: raw?.needsRestart === true,
    warning: text(raw?.warning) || null,
    // Plain HTTP unless core says otherwise: the listener has no TLS of its own.
    plainHttp: raw?.plainHttp !== false,
  };
}

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

export const RESTART_LINE = "settings.json says otherwise than what runs: the next daemon start will differ. Start with “remember” to keep what runs.";

/** What `/web status` prints (the token stays masked). */
export function summaryLines(status: WebStatus): string[] {
  const lines = ["Remote web access", listenLine(status)];
  if (status.listening) {
    for (const url of status.listenUrls) lines.push(`Open: ${url}`);
    lines.push(sessionsLine(status));
  }
  lines.push(modeLine(status));
  if (status.listening && status.mode === "token" && status.accessUrl) lines.push(`Access link: ${maskLink(status.accessUrl)}  (reveal it in /web)`);
  if (status.needsRestart) lines.push(RESTART_LINE);
  lines.push(`! ${status.warning ?? PLAIN_HTTP_WARNING}`);
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
