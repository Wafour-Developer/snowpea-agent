/**
 * `/jarvis` and `/external`: this surface's own commands for Jarvis mode and
 * the external CLI sessions (1.53.0, beta).
 *
 * The daemon has no command of these names; they drive `jarvis.*` and
 * `external.*` calls and move the screen, so they are parsed here. Pure.
 */

import type { CommandInfo } from "../rpc/sdk.js";

export type JarvisCommand =
  /** Go to the Jarvis screen (`/jarvis`, `snowpea jarvis`). */
  | { kind: "enter" }
  /** Back to the session the person came from. */
  | { kind: "back" }
  | { kind: "status" }
  | { kind: "on" }
  | { kind: "off" }
  | { kind: "handback"; target: string | null }
  | { kind: "takeover"; target: string | null; minutes: number | null }
  | { kind: "sessions" }
  | { kind: "open"; target: string }
  | { kind: "waiting" }
  /** Dismiss notices: the nth, or all of them. */
  | { kind: "dismiss"; target: number | "all" }
  /**
   * The session's own Jarvis switch (1.56.0, `jarvis.session.set`): show it
   * (`enabled: null`), or turn it on/off for this session or the one named.
   */
  | { kind: "share"; enabled: boolean | null; target: string | null }
  | { kind: "invalid"; usage: string };

export const JARVIS_USAGE =
  "/jarvis [on|off|status|back|sessions|waiting|open <n>|dismiss [n|all]|handback [n]|takeover [n] [minutes]|share [on|off] [session]]";

export const JARVIS_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "on", summary: "Turn Jarvis mode on (beta): one session supervises the others." },
  { action: "off", summary: "Turn Jarvis mode off; every request goes to you again." },
  { action: "status", summary: "Show whether Jarvis is running, its model and budget." },
  { action: "back", summary: "Return to the session you came from." },
  { action: "sessions", summary: "Pick a supervised session to open." },
  { action: "waiting", summary: "Answer what is waiting for you, from any session." },
  { action: "open", summary: "/jarvis open <n> — open the nth supervised session." },
  { action: "dismiss", summary: "Dismiss Jarvis's notices: /jarvis dismiss [n|all]." },
  { action: "handback", summary: "Give this session back to Jarvis now." },
  { action: "takeover", summary: "Take this session back from Jarvis: /jarvis takeover [minutes]." },
  { action: "share", summary: "Share this session with Jarvis or not: /jarvis share [on|off] (off by default)." },
];

/** `null` when the line is not a `/jarvis` command. */
export function parseJarvisCommand(line: string): JarvisCommand | null {
  const match = /^\/jarvis(?:\s+(.*))?$/.exec(line.trim());
  if (!match) return null;
  const words = (match[1] ?? "").trim().split(/\s+/).filter(Boolean);
  const [action, ...rest] = words;
  switch (action) {
    case undefined:
      return { kind: "enter" };
    case "on":
    case "off":
    case "status":
    case "back":
    case "sessions":
    case "waiting":
      return rest.length === 0 ? { kind: action } : { kind: "invalid", usage: JARVIS_USAGE };
    case "list":
      return { kind: "sessions" };
    case "open":
      return rest.length === 1 ? { kind: "open", target: rest[0] } : { kind: "invalid", usage: JARVIS_USAGE };
    case "dismiss": {
      if (rest.length === 0 || (rest.length === 1 && rest[0] === "all")) return { kind: "dismiss", target: "all" };
      return rest.length === 1 && /^\d+$/.test(rest[0]) && Number(rest[0]) > 0
        ? { kind: "dismiss", target: Number(rest[0]) }
        : { kind: "invalid", usage: JARVIS_USAGE };
    }
    case "share": {
      // `/jarvis share` shows it; `/jarvis share on|off [session]` switches it.
      if (rest.length === 0) return { kind: "share", enabled: null, target: null };
      const [state, target, ...extra] = rest;
      if ((state !== "on" && state !== "off") || extra.length > 0) return { kind: "invalid", usage: JARVIS_USAGE };
      return { kind: "share", enabled: state === "on", target: target ?? null };
    }
    case "handback":
      return rest.length <= 1 ? { kind: "handback", target: rest[0] ?? null } : { kind: "invalid", usage: JARVIS_USAGE };
    case "takeover": {
      // `/jarvis takeover 30` is minutes for this session; `/jarvis takeover 2 30` names row 2.
      const numbers = rest.filter((word) => /^\d+$/.test(word));
      if (rest.length > 2 || numbers.length !== rest.length) {
        return { kind: "invalid", usage: JARVIS_USAGE };
      }
      if (rest.length === 0) return { kind: "takeover", target: null, minutes: null };
      if (rest.length === 1) return { kind: "takeover", target: null, minutes: Number(rest[0]) };
      return { kind: "takeover", target: rest[0], minutes: Number(rest[1]) };
    }
    default:
      return { kind: "invalid", usage: JARVIS_USAGE };
  }
}

export type ExternalCommand =
  /** The picker of this folder's sessions. */
  | { kind: "picker" }
  | { kind: "new" }
  /** Open a session by its handle, `<cli>:<id>` (a deep link). */
  | { kind: "open"; ref: { cli: string; id: string } }
  /** Leave the open session's screen; it keeps running (`external.unwatch`). */
  | { kind: "close" }
  /** Stop the process snowpea runs for the open session (`external.close`). */
  | { kind: "stop" }
  /** Type into the person's own tmux pane (`external.open` with `transport: "tmux"`). */
  | { kind: "adopt" }
  /** Switch the open session's mode. */
  | { kind: "mode"; mode: string }
  /** Load an older page of the open session. */
  | { kind: "more" }
  /** Show the open session's tmux pane as text (`external.terminalSnapshot`, 1.56.0). */
  | { kind: "snapshot" }
  /** Confirm a CLI's configured executable path (`external.clis.confirmPath`). */
  | { kind: "confirmPath"; cli: string }
  | { kind: "invalid"; usage: string };

export const EXTERNAL_USAGE =
  "/external [new|open <cli>:<id>|close|stop|adopt|mode <mode>|snapshot|more|confirm-path <cli>]";

export const EXTERNAL_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "new", summary: "Start a new Claude Code, Codex, agy or Cursor session." },
  { action: "open", summary: "/external open <cli>:<id> — open a session by its handle." },
  { action: "close", summary: "Leave the open external session; it keeps running." },
  { action: "stop", summary: "Stop the process snowpea runs for the open session (the transcript stays)." },
  { action: "adopt", summary: "Let snowpea type into the tmux pane this Claude Code session runs in." },
  { action: "mode", summary: "/external mode <mode> — switch the open session to one of its CLI's modes (plan, auto, …)." },
  { action: "more", summary: "Load earlier messages of the open session." },
  { action: "snapshot", summary: "Show the tmux pane the open session runs in, as text (a prompt snowpea does not recognise)." },
  { action: "confirm-path", summary: "/external confirm-path <cli> — allow snowpea to run a CLI path you configured." },
];

export function parseExternalCommand(line: string): ExternalCommand | null {
  const match = /^\/external(?:\s+(.*))?$/.exec(line.trim());
  if (!match) return null;
  const [action, ...rest] = (match[1] ?? "").trim().split(/\s+/).filter(Boolean);
  switch (action) {
    case undefined:
      return { kind: "picker" };
    case "new":
    case "close":
    case "stop":
    case "adopt":
    case "more":
    case "snapshot":
      return rest.length === 0 ? { kind: action } : { kind: "invalid", usage: EXTERNAL_USAGE };
    case "open": {
      const ref = rest.length === 1 ? /^(claude|codex|agy|cursor):(\S+)$/.exec(rest[0]) : null;
      return ref ? { kind: "open", ref: { cli: ref[1], id: ref[2] } } : { kind: "invalid", usage: EXTERNAL_USAGE };
    }
    case "confirm-path":
      return rest.length === 1 && /^(claude|codex|agy|cursor)$/.test(rest[0])
        ? { kind: "confirmPath", cli: rest[0] }
        : { kind: "invalid", usage: EXTERNAL_USAGE };
    case "mode":
      // A CLI's own mode id (1.56.0: plan, acceptEdits, read-only, …) or a bucket;
      // the open session's CLI decides which exist.
      return rest.length === 1 && /^[A-Za-z][A-Za-z0-9_-]{0,40}$/.test(rest[0])
        ? { kind: "mode", mode: rest[0] }
        : { kind: "invalid", usage: EXTERNAL_USAGE };
    default:
      return { kind: "invalid", usage: EXTERNAL_USAGE };
  }
}

function subCommands(
  draft: string,
  command: string,
  actions: ReadonlyArray<{ action: string; summary: string }>,
): CommandInfo[] {
  const match = new RegExp(`^\\/${command}(?:\\s+([^\\s]*))?$`).exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  if (typed === "" && /\s$/.test(draft)) return [];
  return actions
    .filter((entry) => entry.action.startsWith(typed))
    .map((entry) => ({ name: `${command} ${entry.action}`, summary: entry.summary, source: "tui" }));
}

export function jarvisSubCommands(draft: string): CommandInfo[] {
  return subCommands(draft, "jarvis", JARVIS_ACTIONS);
}

export function externalSubCommands(draft: string): CommandInfo[] {
  return subCommands(draft, "external", EXTERNAL_ACTIONS);
}
