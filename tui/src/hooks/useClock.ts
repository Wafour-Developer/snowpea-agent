/**
 * A wall clock that only moves once a second.
 *
 * Elapsed columns — the agent panel's `1m 12s`, the working line, the shell
 * list — show whole seconds, so a timestamp read on every render is both
 * needlessly precise and actively harmful: it is a new value each pass, so
 * every `useMemo` that takes it rebuilds, and the rows built from it are new
 * objects that defeat the memo on whatever paints them. One tick a second is
 * as precise as the display, and it holds still between ticks.
 *
 * The timer only runs while something is actually being timed, and the clock
 * is re-read the moment it starts, so a turn that begins between ticks still
 * counts from the right instant.
 */

import { useEffect, useState } from "react";

export const CLOCK_TICK_MS = 1000;

export function useClock(
  active: boolean,
  tickMs: number = CLOCK_TICK_MS,
  now: () => number = Date.now,
): number {
  const [stamp, setStamp] = useState(() => now());

  useEffect(() => {
    if (!active) return;
    // A fresh read on activation: the last tick may be almost a second old.
    setStamp(now());
    const timer = setInterval(() => setStamp(now()), tickMs);
    return () => clearInterval(timer);
  }, [active, tickMs, now]);

  return stamp;
}
