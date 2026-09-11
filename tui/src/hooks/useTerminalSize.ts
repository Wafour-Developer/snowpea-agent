/**
 * Terminal size, kept current across `SIGWINCH`.
 *
 * Ink's own `useStdout` hands back the stream but never re-renders on a
 * resize, so the full-screen layout listens for `resize` itself. Falls back to
 * 80x24 when stdout is not a TTY (a pipe, or a test).
 *
 * Dragging a window emits a burst of `resize` events; each one that reached
 * React would reflow the whole transcript, so they are debounced and a size
 * that did not actually change keeps its object identity.
 */

import { useEffect, useRef, useState } from "react";
import { useStdout } from "ink";

export const FALLBACK_COLUMNS = 80;
export const FALLBACK_ROWS = 24;

/** Resize events inside this window collapse into one state update. */
export const RESIZE_DEBOUNCE_MS = 60;

export interface TerminalSize {
  columns: number;
  rows: number;
}

export function readSize(stream: NodeJS.WriteStream | undefined): TerminalSize {
  // A pty with no window size reports 0, not undefined, so `??` is not enough:
  // a zero width would truncate every row to nothing.
  return {
    columns: stream?.columns || FALLBACK_COLUMNS,
    rows: stream?.rows || FALLBACK_ROWS,
  };
}

export function useTerminalSize(debounceMs: number = RESIZE_DEBOUNCE_MS): TerminalSize {
  const { stdout } = useStdout();
  const [size, setSize] = useState<TerminalSize>(() => readSize(stdout));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!stdout) return;
    const apply = (): void =>
      setSize((current) => {
        const next = readSize(stdout);
        // Same size, same object: nothing downstream re-renders.
        return current.columns === next.columns && current.rows === next.rows ? current : next;
      });
    const onResize = (): void => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(apply, debounceMs);
    };
    apply();
    stdout.on("resize", onResize);
    return () => {
      if (timer.current) clearTimeout(timer.current);
      stdout.off("resize", onResize);
    };
  }, [stdout, debounceMs]);

  return size;
}
