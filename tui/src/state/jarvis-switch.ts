/**
 * The per-session Jarvis switch (core 1.56.0, capability `jarvisSessionSwitch`).
 *
 * Jarvis sees only the sessions a person shares with it; a new session starts
 * not shared (`jarvis.sessions.default`). This surface shows the current
 * session's switch, changes it with `/jarvis share on|off`, and says on the
 * Jarvis screen how many sessions are not shared. An older daemon has no
 * switch: nothing here shows. Pure.
 */

export type SwitchLock = "project_hidden" | "jarvis_session" | "browser_hidden";

export interface SessionSwitch {
  on: boolean;
  locked: SwitchLock | null;
}

/** The capability flag; without it nothing of the switch is shown. */
export function hasSessionSwitch(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("jarvisSessionSwitch");
}

/** `session.list` rows → the switch of every row that carries one. */
export function switchesFromRows(
  rows: ReadonlyArray<{ sessionId: string; jarvisOn?: boolean | null; jarvisLocked?: SwitchLock | null }>,
): Record<string, SessionSwitch> {
  const out: Record<string, SessionSwitch> = {};
  for (const row of rows) {
    if (typeof row.jarvisOn !== "boolean") continue;
    out[row.sessionId] = { on: row.jarvisOn, locked: row.jarvisLocked ?? null };
  }
  return out;
}

const LOCK_TEXT: Record<SwitchLock, string> = {
  project_hidden: "its project is hidden from Jarvis (jarvis.expose off)",
  jarvis_session: "it is Jarvis's own work",
  browser_hidden: "browser sessions are hidden from Jarvis (jarvis.exposeBrowser off)",
};

/** What `/jarvis share` says about a session. */
export function shareLine(state: SessionSwitch | undefined): string {
  if (!state) return "this session's Jarvis switch is not known yet";
  if (state.locked === "jarvis_session") return "Jarvis always sees this session: it is Jarvis's own work";
  const now = state.on ? "shared with Jarvis" : "not shared with Jarvis";
  if (state.locked) return `${now} — cannot be changed: ${LOCK_TEXT[state.locked]}`;
  return state.on ? `${now} — /jarvis share off stops it` : `${now} — /jarvis share on lets Jarvis see and help here`;
}

/** The toast after `jarvis.session.set`. */
export function switchedLine(on: boolean): string {
  return on
    ? "shared with Jarvis — it can see this session and decide within your grants"
    : "not shared with Jarvis any more — what waited for Jarvis here is yours";
}

/** Why a `jarvis.session.set` was refused, from its error details. */
export function lockedLine(reason: unknown): string | null {
  return typeof reason === "string" && reason in LOCK_TEXT
    ? `cannot change it: ${LOCK_TEXT[reason as SwitchLock]}`
    : null;
}

/** The header mark of a shared session (the session's own state). */
export const SHARED_MARK = "Jarvis: shared";

/** The Jarvis screen's line about what Jarvis does not see; null when nothing is hidden. */
export function hiddenLine(count: number | null | undefined): string | null {
  if (!count || count < 1) return null;
  const what = count === 1 ? "1 session is" : `${count} sessions are`;
  return `${what} not shared with Jarvis — in a session: /jarvis share on`;
}
