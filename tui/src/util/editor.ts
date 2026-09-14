/**
 * Handing the terminal to `$EDITOR` and taking it back.
 *
 * `/skill edit` opens a file the way `git commit` does: the editor gets the
 * real terminal, and the TUI stops drawing until it exits. The spawn is behind
 * an interface so the tests can watch what would have been run without opening
 * vi in the test runner.
 */

import { spawn } from "node:child_process";

export interface EditorRunner {
  /** Runs the editor on `path` and resolves with its exit code. */
  run(path: string): Promise<number>;
  /** What would be run, for the message shown while it is open. */
  command(): string;
}

/**
 * `$VISUAL`, then `$EDITOR`, then `vi`.
 *
 * `VISUAL` wins because that is the one meant for a full-screen terminal, which
 * is exactly the situation here.
 */
export function editorCommand(env: NodeJS.ProcessEnv = process.env): string {
  const chosen = (env.VISUAL ?? env.EDITOR ?? "").trim();
  return chosen.length > 0 ? chosen : "vi";
}

/**
 * The real thing: the editor inherits this process's stdio, so it owns the
 * screen and the keyboard until it exits.
 *
 * A command with arguments in it (`code -w`, `emacsclient -nw`) is split on
 * spaces the way a shell would for the simple case; nothing is passed to a
 * shell, so a name with spaces in it cannot become an injection.
 */
export function createEditorRunner(env: NodeJS.ProcessEnv = process.env): EditorRunner {
  const spelled = editorCommand(env);
  const [command, ...args] = spelled.split(/\s+/).filter((part) => part.length > 0);
  return {
    command: () => spelled,
    run: (path: string) =>
      new Promise<number>((resolve) => {
        const child = spawn(command, [...args, path], { stdio: "inherit" });
        child.on("error", () => resolve(-1));
        child.on("exit", (code) => resolve(code ?? 0));
      }),
  };
}
