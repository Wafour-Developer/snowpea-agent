/**
 * snowpea TUI entry point. Bundled to dist/snowpea-tui.js by esbuild.config.mjs.
 *
 * Invoked by `snowpea` (cli/main.py) as:
 *   node dist/snowpea-tui.js --port <n> --token <t> [--mode plan|accept|auto] [--cwd DIR]
 *
 * Nothing renders at import time: failures during connect are reported on
 * stderr and exit non-zero rather than throwing out of module evaluation.
 */

import React from "react";
import { render } from "ink";

import { App } from "./app.js";
import { installAltScreen } from "./layout/screen.js";
import { TuiClient } from "./rpc/client.js";
import type { Mode } from "./rpc/sdk.js";

const CLIENT_VERSION = "0.1.0";
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
   * Draw full screen on the alternate buffer. Off for `--no-fullscreen`,
   * `--inline` or `SNOWPEA_TUI_INLINE=1`, which render inline in the
   * scrollback so the output can be piped or read by a debugger.
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
    fullscreen: !inlineRequested,
  };
}

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

  // The alternate buffer goes up before Ink's first frame, so nothing the app
  // draws ever lands in the user's scrollback. `installAltScreen` also arms the
  // signal and uncaught-error paths that would otherwise leave the terminal on
  // the alternate buffer with the cursor hidden.
  const screen = args.fullscreen
    ? installAltScreen({ stdout: process.stdout, process })
    : null;

  let restart = false;
  const instance = render(
    <App
      client={client}
      sessionId={sessionId}
      mode={args.mode ?? "accept"}
      workdir={args.cwd}
      fullscreen={args.fullscreen}
      onRestart={() => {
        restart = true;
      }}
    />,
    // Ink's own Ctrl+C handling unmounts before the app can tear the screen
    // down; `App` handles the key itself and calls `exit()`.
    { exitOnCtrlC: false },
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
