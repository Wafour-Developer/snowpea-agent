/**
 * Alternate screen buffer control.
 *
 * A full-screen TUI must not leave its frames in the user's scrollback, so the
 * app switches the terminal to the alternate buffer on start and switches back
 * on every exit path — normal return, Ctrl+C, /exit, SIGTERM, SIGHUP and
 * uncaught errors. Restoring is idempotent: whichever path fires first wins and
 * the rest become no-ops.
 *
 * Kept free of React and of `process` so `test/layout.test.ts` can drive it
 * with a fake stdout and a fake process.
 */

/** Switch to the alternate screen buffer. */
export const ENTER_ALT_SCREEN = "\u001B[?1049h";
/** Switch back to the main screen buffer, restoring the user's scrollback. */
export const EXIT_ALT_SCREEN = "\u001B[?1049l";
/** Erase the whole screen and park the cursor at the home position. */
export const CLEAR_SCREEN = "\u001B[2J\u001B[H";
export const HIDE_CURSOR = "\u001B[?25l";
export const SHOW_CURSOR = "\u001B[?25h";

/** The subset of `NodeJS.WriteStream` this module needs. */
export interface WritableLike {
  write(chunk: string): unknown;
}

/** The subset of `process` this module needs, so tests can pass a stub. */
export interface ProcessLike {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  removeListener?(event: string, listener: (...args: unknown[]) => void): unknown;
  exit?(code?: number): unknown;
}

export interface AltScreenHandle {
  /** False once the main buffer has been restored. */
  readonly active: boolean;
  /** Restore the main buffer and drop the process listeners. Idempotent. */
  restore(): void;
  /**
   * Run this immediately before the buffer is restored.
   *
   * The renderer has to be torn down while the alternate buffer is still up,
   * or its farewell frame is drawn onto the main buffer and lands in the
   * user's scrollback. `index.tsx` hooks Ink's `unmount` here.
   */
  setBeforeRestore(hook: () => void): void;
}

/** Signals that must restore the terminal before the process goes away. */
const SIGNALS: ReadonlyArray<readonly [string, number]> = [
  ["SIGINT", 2],
  ["SIGTERM", 15],
  ["SIGHUP", 1],
];

/** Enter the alternate buffer, clear it and hide the cursor. */
export function enterAltScreen(stdout: WritableLike, { hideCursor = true } = {}): void {
  stdout.write(ENTER_ALT_SCREEN + CLEAR_SCREEN + (hideCursor ? HIDE_CURSOR : ""));
}

/** Show the cursor again and return to the main buffer. */
export function exitAltScreen(stdout: WritableLike, { hideCursor = true } = {}): void {
  stdout.write((hideCursor ? SHOW_CURSOR : "") + EXIT_ALT_SCREEN);
}

/**
 * Enter the alternate buffer and arm every restore path.
 *
 * The returned handle is what the normal exit path calls; the signal and error
 * listeners exist for the paths that never reach it.
 */
export function installAltScreen({
  stdout,
  process: proc,
  hideCursor = true,
  exitOnSignal = true,
}: {
  stdout: WritableLike;
  process: ProcessLike;
  hideCursor?: boolean;
  /** Re-raise the conventional 128+n exit code after restoring. */
  exitOnSignal?: boolean;
}): AltScreenHandle {
  let active = true;
  let beforeRestore: (() => void) | null = null;
  const listeners: Array<[string, (...args: unknown[]) => void]> = [];

  const detach = (): void => {
    if (!proc.removeListener) return;
    for (const [event, listener] of listeners) proc.removeListener(event, listener);
    listeners.length = 0;
  };

  const restore = (): void => {
    if (!active) return;
    active = false;
    try {
      beforeRestore?.();
    } catch {
      // Tearing the renderer down must never stop the terminal being restored.
    }
    exitAltScreen(stdout, { hideCursor });
    detach();
  };

  const on = (event: string, listener: (...args: unknown[]) => void): void => {
    listeners.push([event, listener]);
    proc.on(event, listener);
  };

  on("exit", () => restore());

  for (const [signal, number] of SIGNALS) {
    on(signal, () => {
      restore();
      // 128 + signal number, the shell's convention for "killed by a signal".
      if (exitOnSignal) proc.exit?.(128 + number);
    });
  }

  on("uncaughtException", (error: unknown) => {
    restore();
    stdout.write(`snowpea-tui: ${String((error as Error | undefined)?.stack ?? error)}\n`);
    proc.exit?.(1);
  });

  enterAltScreen(stdout, { hideCursor });

  return {
    get active() {
      return active;
    },
    restore,
    setBeforeRestore(hook: () => void) {
      beforeRestore = hook;
    },
  };
}
