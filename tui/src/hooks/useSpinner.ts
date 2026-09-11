/**
 * The spinner's frame counter.
 *
 * The timer only exists while something is actually spinning: an idle session
 * must not repaint the screen at all, and a turn blocked on an approval has
 * nothing to animate either. Five frames a second reads as motion without
 * costing much — every frame rewrites the live region, so the rate is a real
 * bandwidth decision, not a cosmetic one.
 */

import { useEffect, useState } from "react";

import { SPINNER_INTERVAL_MS } from "../state/working.js";

export function useSpinner(active: boolean, intervalMs: number = SPINNER_INTERVAL_MS): number {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    if (!active) {
      setFrame(0);
      return;
    }
    const timer = setInterval(() => setFrame((value) => value + 1), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);

  return frame;
}
