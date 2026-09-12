/**
 * Getting an image out of the system clipboard.
 *
 * There is no portable way to do it, so this is a list of the tools that can,
 * tried in order until one works. Two shapes exist: tools that write the image
 * to stdout, which we save ourselves, and tools that are told a path and write
 * it themselves. Both are represented, because the mac and Windows ones are the
 * second kind.
 *
 * The runner and the writer are injected, so `test/clipboard.test.ts` can check
 * the order and the fallbacks without a clipboard, a display server or a disk.
 */

export interface CommandResult {
  /** Exit status; anything but 0 means the tool did not have an image. */
  status: number;
  /** What it wrote to stdout, for the tools of the first kind. */
  stdout: Uint8Array;
}

export interface CommandRunner {
  /** Run a command to completion, or return null when it is not installed. */
  run(command: string, args: string[]): CommandResult | null;
}

export interface ClipboardCommand {
  /** Platforms it applies to, as `process.platform` names. */
  platforms: readonly NodeJS.Platform[];
  command: string;
  /** The tool is told where to write; otherwise it writes to stdout. */
  writesFile: boolean;
  args(path: string): string[];
}

/**
 * The tools, in the order they are tried.
 *
 * Wayland before X11 on Linux, because a Wayland session usually has both and
 * only the first one is telling the truth about the clipboard.
 */
export const CLIPBOARD_COMMANDS: readonly ClipboardCommand[] = [
  {
    platforms: ["linux", "freebsd", "openbsd"],
    command: "wl-paste",
    writesFile: false,
    args: () => ["-t", "image/png"],
  },
  {
    platforms: ["linux", "freebsd", "openbsd"],
    command: "xclip",
    writesFile: false,
    args: () => ["-selection", "clipboard", "-t", "image/png", "-o"],
  },
  {
    // `pbpaste` cannot hand back an image, so the mac path is AppleScript
    // writing the PNG flavour straight to the file.
    platforms: ["darwin"],
    command: "osascript",
    writesFile: true,
    args: (path) => [
      "-e",
      `set theFile to (open for access (POSIX file "${path}") with write permission)`,
      "-e",
      "try",
      "-e",
      "write (the clipboard as «class PNGf») to theFile",
      "-e",
      "end try",
      "-e",
      "close access theFile",
    ],
  },
  {
    platforms: ["win32"],
    command: "powershell",
    writesFile: true,
    args: (path) => [
      "-NoProfile",
      "-Command",
      `$image = Get-Clipboard -Format Image; if ($image -eq $null) { exit 1 }; $image.Save('${path}')`,
    ],
  },
];

/** Where a pasted image is kept: beside the daemon's own state. */
export function pasteFilePath(stateDir: string, now: number, join: (...p: string[]) => string): string {
  return join(stateDir, "tmp", `paste-${now}.png`);
}

export interface ClipboardEnvironment {
  runner: CommandRunner;
  platform: NodeJS.Platform;
  /** Write the bytes a stdout-kind tool produced; returns false on failure. */
  writeFile(path: string, data: Uint8Array): boolean;
  /** Size of a file the tool wrote itself, or null when it wrote nothing. */
  size(path: string): number | null;
  join(...parts: string[]): string;
}

/**
 * Save the clipboard image, and answer with where it went.
 *
 * Null means no tool on this machine could produce one — an empty clipboard, a
 * clipboard holding text, or nothing installed. The caller turns that into one
 * sentence for the user rather than a stack of failures.
 */
export function captureClipboardImage(
  environment: ClipboardEnvironment,
  stateDir: string,
  now: number,
): string | null {
  const path = pasteFilePath(stateDir, now, environment.join);

  for (const entry of CLIPBOARD_COMMANDS) {
    if (!entry.platforms.includes(environment.platform)) continue;
    const result = environment.runner.run(entry.command, entry.args(path));
    // Null is "not installed"; a non-zero status is "installed, no image".
    if (!result || result.status !== 0) continue;

    if (entry.writesFile) {
      const written = environment.size(path);
      if (written !== null && written > 0) return path;
      continue;
    }
    if (result.stdout.length === 0) continue;
    if (!environment.writeFile(path, result.stdout)) continue;
    return path;
  }
  return null;
}
