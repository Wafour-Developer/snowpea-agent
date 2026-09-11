/**
 * Pure mode-cycling logic shared by the Shift+Tab handler in `app.tsx` and its
 * tests. Order matches Claude Code: accept -> auto -> plan -> accept.
 */

import type { Mode } from "../rpc/sdk.js";

export const MODE_CYCLE_ORDER: readonly Mode[] = ["accept", "auto", "plan"];

/** Next mode in the accept -> auto -> plan -> accept cycle. Unknown modes reset to "accept". */
export function cycleMode(mode: Mode): Mode {
  const index = MODE_CYCLE_ORDER.indexOf(mode);
  if (index === -1) return "accept";
  return MODE_CYCLE_ORDER[(index + 1) % MODE_CYCLE_ORDER.length];
}
