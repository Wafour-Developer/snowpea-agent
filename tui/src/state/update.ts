/**
 * Update banner state (CORE-update).
 *
 * The daemon owns the decision — `system.checkUpdate` says whether a newer
 * release exists — so this module only turns that answer, plus the
 * `system.updateProgress` notifications, into what the banner renders.
 */

import type { UpdateCheck } from "../rpc/sdk.js";

/**
 * Where the in-TUI update flow stands.
 *
 *   idle      nothing to show, or the banner was dismissed
 *   available a newer release exists; the banner nudges
 *   confirm   the user pressed U or typed /update; y/n is pending
 *   running   the upgrade subprocess is going
 *   done      it finished; the TUI is about to restart
 *   failed    it did not finish; the message says why
 */
export type UpdatePhase = "idle" | "available" | "confirm" | "running" | "done" | "failed";

export interface UpdateState {
  phase: UpdatePhase;
  /** Version of the running daemon. */
  current: string;
  /** Newest version the daemon found. */
  latest: string;
  /** Latest progress line, shown while running and after it ends. */
  message: string;
  /** True once the user dismissed the banner; it does not come back. */
  dismissed: boolean;
}

export const initialUpdateState: UpdateState = {
  phase: "idle",
  current: "",
  latest: "",
  message: "",
  dismissed: false,
};

/** Fold a `system.checkUpdate` answer into the banner state. */
export function fromCheck(state: UpdateState, check: UpdateCheck): UpdateState {
  const current = check.current ?? state.current;
  const latest = check.latest ?? state.latest;
  // An error or an up-to-date answer leaves the banner off; a check that
  // arrives while an upgrade is already running must not rewind it.
  if (!check.available || check.error || state.phase === "running" || state.phase === "done") {
    return { ...state, current, latest };
  }
  return { ...state, phase: state.dismissed ? "idle" : "available", current, latest };
}

/** The user asked for the update; show the y/n prompt. */
export function confirm(state: UpdateState): UpdateState {
  if (state.phase === "running" || state.phase === "done") return state;
  return { ...state, phase: "confirm", dismissed: false };
}

/** The user said no, or pressed escape; the banner stays gone. */
export function cancel(state: UpdateState): UpdateState {
  if (state.phase === "running" || state.phase === "done") return state;
  return { ...state, phase: "idle", dismissed: true };
}

/** Fold one `system.updateProgress` notification in. */
export function progress(
  state: UpdateState,
  phase: "started" | "done" | "failed",
  message: string,
): UpdateState {
  const next: UpdatePhase = phase === "started" ? "running" : phase;
  return { ...state, phase: next, message };
}

/** What the banner says, or null when nothing should be drawn. */
export function bannerText(state: UpdateState): string | null {
  switch (state.phase) {
    case "available":
      return `⬆ Update available v${state.latest} (current v${state.current}) — press U or type /update`;
    case "confirm":
      return `⬆ Update to v${state.latest} from v${state.current}? [y/N]`;
    case "running":
      return `⬆ ${state.message || `Updating to v${state.latest}…`}`;
    case "done":
      return `⬆ Updated to v${state.latest} — restarting…`;
    case "failed":
      return `⬆ Update failed: ${state.message || "see the update log"}`;
    default:
      return null;
  }
}
