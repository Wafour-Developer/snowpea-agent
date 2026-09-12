/**
 * snowpea TUI entry point. Bundled to dist/snowpea-tui.js by esbuild.config.mjs.
 *
 * Invoked by `snowpea` (cli/main.py) as:
 *   node dist/snowpea-tui.js --port <n> --token <t> [--mode plan|accept|auto] [--cwd DIR]
 *
 * Renders inline by default, leaving the transcript in the terminal's
 * scrollback; `--fullscreen` switches to the alternate-buffer layout.
 *
 * Nothing renders at import time: failures during connect are reported on
 * stderr and exit non-zero rather than throwing out of module evaluation.
 */

import React from "react";
import { render } from "ink";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { App } from "./app.js";
import { SessionMemory, TuiHistory, stateDir, type FileStore } from "./state/history.js";
import { createFrameWriter, type FrameWriter } from "./layout/frame.js";
import { installAltScreen } from "./layout/screen.js";
import { TuiClient } from "./rpc/client.js";
import type { Mode } from "./rpc/sdk.js";
import { TUI_VERSION } from "./version.js";

const CLIENT_VERSION = TUI_VERSION;
const MODES: readonly string[] = ["plan", "accept", "auto"];

/** Flags that stand alone: they must not swallow the next argv entry. */
const BOOLEAN_FLAGS = new Set(["fullscreen", "no-fullscreen", "inline"]);

/**
 * Exit code that asks `cli/main.py` to re-exec `snowpea` (CORE-update).
 * Kept in step with `TUI_RESTART_EXIT` there; 75 is EX_TEMPFAIL, which no
 * other snowpea exit path uses.
 */
export const RESTART_EXIT_CODE = 75;

export interface CliArgs {
  port: number;
  token: string;
  mode?: Mode;
  cwd: string;
  /**
   * Draw full screen on the alternate buffer. Off by default: the inline
   * layout keeps the terminal's own scrollback, which is what people expect
   * from a coding agent in a terminal. `--fullscreen` opts in.
   */
  fullscreen: boolean;
}

/** Parse `--key value`, `--key=value` and the standalone flags above. */
export function parseArgs(
  argv: string[],
  cwd = process.cwd(),
  env: NodeJS.ProcessEnv = process.env,
): CliArgs {
  const values = new Map<string, string>();
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) continue;
    const eq = arg.indexOf("=");
    if (eq !== -1) {
      values.set(arg.slice(2, eq), arg.slice(eq + 1));
    } else if (BOOLEAN_FLAGS.has(arg.slice(2))) {
      values.set(arg.slice(2), "true");
    } else {
      values.set(arg.slice(2), argv[i + 1] ?? "");
      i += 1;
    }
  }

  const portRaw = values.get("port");
  const port = Number(portRaw);
  if (!portRaw || !Number.isInteger(port) || port <= 0) {
    throw new Error("--port <number> is required");
  }
  const token = values.get("token");
  if (!token) throw new Error("--token <token> is required");

  const modeRaw = values.get("mode");
  if (modeRaw !== undefined && !MODES.includes(modeRaw)) {
    throw new Error(`--mode must be one of ${MODES.join("|")}`);
  }

  // Inline is the default, so the alternate buffer has to be asked for. The
  // inline flags stay accepted — they are now redundant, not wrong.
  const fullscreenRequested = values.get("fullscreen") === "true";
  const inlineRequested =
    values.get("no-fullscreen") === "true" ||
    values.get("inline") === "true" ||
    values.get("fullscreen") === "false" ||
    env.SNOWPEA_TUI_INLINE === "1";

  return {
    port,
    token,
    mode: modeRaw as Mode | undefined,
    cwd: values.get("cwd") || cwd,
    fullscreen: fullscreenRequested && !inlineRequested,
  };
}

/**
 * A stdout that sends Ink's frames through `writer`.
 *
 * Everything else about the stream — `columns`, `rows`, the `resize` event Ink
 * listens on — has to keep working, so this proxies the real stream and only
 * takes over `write`.
 */
export function frameStdout(stdout: NodeJS.WriteStream, writer: FrameWriter): NodeJS.WriteStream {
  return new Proxy(stdout, {
    get(target, property, receiver) {
      if (property === "write") {
        return (chunk: unknown): boolean => {
          writer.write(String(chunk));
          return true;
        };
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

/**
 * The real file system, behind the tiny interface `state/history.ts` wants.
 *
 * Every operation is best-effort: the history and the last-session record are
 * conveniences, and a home directory that cannot be written must not stop the
 * TUI from running.
 */
export const fileStore: FileStore = {
  read(path) {
    try {
      return readFileSync(path, "utf8");
    } catch {
      return null;
    }
  },
  write(path, contents) {
    try {
      mkdirSync(join(path, ".."), { recursive: true });
      writeFileSync(path, contents, "utf8");
    } catch {
      /* nothing to do about it, and nothing depends on it. */
    }
  },
  join(...parts) {
    return join(...parts);
  },
};

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  let args: CliArgs;
  try {
    args = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`snowpea-tui: ${(error as Error).message}\n`);
    return 2;
  }

  const client = new TuiClient({
    port: args.port,
    token: args.token,
    clientVersion: CLIENT_VERSION,
  });

  let sessionId: string;
  try {
    await client.connect();
    sessionId = await client.createSession({
      workdir: args.cwd,
      mode: args.mode,
      originSurface: "tui",
    });
  } catch (error) {
    process.stderr.write(`snowpea-tui: cannot connect to daemon: ${(error as Error).message}\n`);
    await client.close().catch(() => undefined);
    return 1;
  }

  // Only `--fullscreen` takes the alternate buffer. The alternate buffer goes
  // up before Ink's first frame, so nothing the app draws lands in the
  // scrollback. `installAltScreen` also arms the
  // signal and uncaught-error paths that would otherwise leave the terminal on
  // the alternate buffer with the cursor hidden.
  const screen = args.fullscreen
    ? installAltScreen({ stdout: process.stdout, process })
    : null;

  // Full-screen frames are diffed against the last one, so a keystroke costs a
  // single row update instead of a repaint of the whole screen.
  const stdout = args.fullscreen
    ? frameStdout(process.stdout, createFrameWriter(process.stdout))
    : process.stdout;

  const dir = stateDir(process.env, homedir());
  const history = new TuiHistory(fileStore, dir);
  const sessions = new SessionMemory(fileStore, dir);

  let restart = false;
  const instance = render(
    <App
      client={client}
      sessionId={sessionId}
      mode={args.mode ?? "accept"}
      workdir={args.cwd}
      fullscreen={args.fullscreen}
      history={history}
      sessions={sessions}
      onRestart={() => {
        restart = true;
      }}
    />,
    // Ink's own Ctrl+C handling unmounts before the app can tear the screen
    // down; `App` handles the key itself and calls `exit()`.
    { stdout, exitOnCtrlC: false },
  );

  // A signal or an uncaught error can reach us mid-frame. Unmounting Ink from
  // inside the restore path flushes its last frame onto the alternate buffer,
  // so nothing of the UI is left behind on the main one.
  screen?.setBeforeRestore(() => instance.unmount());

  try {
    await instance.waitUntilExit();
  } finally {
    // Closing the session is best-effort: the daemon reaps orphans anyway.
    await client.closeSession(sessionId).catch(() => undefined);
    await client.close().catch(() => undefined);
    screen?.restore();
  }
  return restart ? RESTART_EXIT_CODE : 0;
}

const isDirectRun =
  typeof process.argv[1] === "string" && !process.env.SNOWPEA_TUI_NO_AUTORUN;

if (isDirectRun) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error: unknown) => {
      process.stderr.write(`snowpea-tui: ${String(error)}\n`);
      process.exitCode = 1;
    },
  );
}
