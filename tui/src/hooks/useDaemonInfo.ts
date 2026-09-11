/**
 * The daemon's pid and idle-shutdown summary, for the status HUD.
 *
 * `system.info` is cheap but not free, and nothing about it changes between
 * keystrokes, so it is polled on a slow timer rather than read per render. A
 * failed call leaves the previous answer in place: the HUD losing a segment is
 * worse than showing one that is a few seconds stale.
 */

import { useEffect, useState } from "react";

/** Structural subset of `TuiClient`, so tests can pass a stub. */
export interface DaemonInfoClient {
  call(method: string, params?: Record<string, unknown>): Promise<any>;
}

export interface DaemonInfo {
  pid: number | null;
  /** `will not exit: 1 job`, straight from the lifecycle status. */
  summary: string | null;
}

export const DAEMON_INFO_POLL_MS = 30_000;

export const emptyDaemonInfo: DaemonInfo = { pid: null, summary: null };

/** Fold a `system.info` result into what the HUD draws. */
export function fromSystemInfo(result: any): DaemonInfo {
  const pid = typeof result?.pid === "number" ? result.pid : null;
  const lifecycle = result?.lifecycle ?? null;
  const summary =
    typeof lifecycle?.summary === "string" && lifecycle.summary.length > 0
      ? lifecycle.summary
      : null;
  return { pid, summary };
}

export function useDaemonInfo(
  client: DaemonInfoClient,
  pollMs: number = DAEMON_INFO_POLL_MS,
): DaemonInfo {
  const [info, setInfo] = useState<DaemonInfo>(emptyDaemonInfo);

  useEffect(() => {
    let cancelled = false;
    const read = (): void => {
      void client
        .call("system.info", {})
        .then((result) => {
          if (cancelled) return;
          const next = fromSystemInfo(result);
          // Keep the object identity when nothing moved, so the HUD's memo
          // holds and a poll does not cost a repaint.
          setInfo((current) =>
            current.pid === next.pid && current.summary === next.summary ? current : next,
          );
        })
        .catch(() => {
          /* advisory: the HUD keeps the last answer. */
        });
    };
    read();
    const timer = setInterval(read, pollMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, pollMs]);

  return info;
}
