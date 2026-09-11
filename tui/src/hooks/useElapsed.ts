/**
 * Milliseconds since a fixed start, ticking slowly.
 *
 * The HUD shows whole seconds under a minute and whole minutes above one, so a
 * ten-second tick is as precise as the display can be. Anything faster would
 * repaint the frame for nothing.
 */

import { useEffect, useRef, useState } from "react";

export const ELAPSED_TICK_MS = 10_000;

export function useElapsed(tickMs: number = ELAPSED_TICK_MS, now: () => number = Date.now): number {
  const start = useRef<number>(now());
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setElapsed(now() - start.current), tickMs);
    return () => clearInterval(timer);
  }, [tickMs, now]);

  return elapsed;
}
