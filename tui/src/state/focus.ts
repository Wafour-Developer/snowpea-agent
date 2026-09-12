/**
 * Where the keyboard is, in the bottom half of the screen.
 *
 * Claude Code lets you walk down out of the input into the rows under it: the
 * mode footer first, then the agents. That is the whole model — a cursor that
 * moves in one dimension — so it is a handful of pure functions rather than
 * anything stateful, and `test/focus.test.ts` can check every move.
 */

export type Focus =
  /** Typing; the default. */
  | { zone: "input" }
  /** The `⏵⏵ auto mode on · N shells` row. */
  | { zone: "footer" }
  /** One row of the agent panel, by index. */
  | { zone: "agent"; index: number };

export const INPUT_FOCUS: Focus = { zone: "input" };

/** True while the chat line owns the keyboard. */
export function isInput(focus: Focus): boolean {
  return focus.zone === "input";
}

/**
 * One step down: input → footer → the agent rows, stopping at the last one.
 *
 * With no agent rows at all the footer is the bottom, so the cursor cannot
 * walk into an empty panel.
 */
export function focusDown(focus: Focus, agentRows: number): Focus {
  if (focus.zone === "input") return { zone: "footer" };
  if (focus.zone === "footer") return agentRows > 0 ? { zone: "agent", index: 0 } : focus;
  return focus.index + 1 < agentRows ? { zone: "agent", index: focus.index + 1 } : focus;
}

/** One step up, ending back in the input. */
export function focusUp(focus: Focus): Focus {
  if (focus.zone === "input") return focus;
  if (focus.zone === "footer") return INPUT_FOCUS;
  return focus.index === 0 ? { zone: "footer" } : { zone: "agent", index: focus.index - 1 };
}

/** Keep the cursor on a row that still exists after the panel changed. */
export function clampFocus(focus: Focus, agentRows: number): Focus {
  if (focus.zone !== "agent") return focus;
  if (agentRows === 0) return { zone: "footer" };
  return focus.index < agentRows ? focus : { zone: "agent", index: agentRows - 1 };
}
