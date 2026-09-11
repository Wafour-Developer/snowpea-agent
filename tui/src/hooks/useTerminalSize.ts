/**
 * Terminal size, kept current across `SIGWINCH`.
 *
 * Ink's own `useStdout` hands back the stream but never re-renders on a
 * resize, so the full-screen layout listens for `resize` itself. Falls back to
 * 80x24 when stdout is not a TTY (a pipe, or a test).
 */

import { useEffect, useState } from "react";
import { useStdout } from "ink";

export const FALLBACK_COLUMNS = 80;
export const FALLBACK_ROWS = 24;

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

export function useTerminalSize(): TerminalSize {
  const { stdout } = useStdout();
  const [size, setSize] = useState<TerminalSize>(() => readSize(stdout));

  useEffect(() => {
    if (!stdout) return;
    const onResize = (): void => setSize(readSize(stdout));
    onResize();
    stdout.on("resize", onResize);
    return () => {
      stdout.off("resize", onResize);
    };
  }, [stdout]);

  return size;
}
