/**
 * Getting an image off the clipboard, and what happens when no tool can.
 */

import { describe, expect, it } from "vitest";

import {
  CLIPBOARD_COMMANDS,
  captureClipboardImage,
  pasteFilePath,
  type ClipboardEnvironment,
  type CommandResult,
} from "../src/util/clipboard.js";

const NOW = 1_700_000_000_000;
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);

interface Scripted {
  /** Command name → what running it does. */
  [command: string]: CommandResult | null;
}

function environmentOf(
  platform: NodeJS.Platform,
  scripted: Scripted,
  options: { writtenByTool?: Record<string, number>; writeFails?: boolean } = {},
): ClipboardEnvironment & { attempted: string[]; written: Record<string, Uint8Array> } {
  const attempted: string[] = [];
  const written: Record<string, Uint8Array> = {};
  return {
    attempted,
    written,
    platform,
    runner: {
      run(command) {
        attempted.push(command);
        return scripted[command] ?? null;
      },
    },
    writeFile(path, data) {
      if (options.writeFails) return false;
      written[path] = data;
      return true;
    },
    size: (path) => options.writtenByTool?.[path] ?? null,
    join: (...parts) => parts.join("/"),
  };
}

const ok = (stdout: Uint8Array = PNG): CommandResult => ({ status: 0, stdout });
const empty = (): CommandResult => ({ status: 0, stdout: new Uint8Array() });
const failed = (): CommandResult => ({ status: 1, stdout: new Uint8Array() });

describe("pasteFilePath", () => {
  it("puts the image beside the daemon's own state", () => {
    expect(pasteFilePath("/state", NOW, (...parts) => parts.join("/"))).toBe(
      `/state/tmp/paste-${NOW}.png`,
    );
  });
});

describe("captureClipboardImage", () => {
  it("saves what the first working tool wrote to stdout", () => {
    const environment = environmentOf("linux", { "wl-paste": ok() });
    const path = captureClipboardImage(environment, "/state", NOW);
    expect(path).toBe(`/state/tmp/paste-${NOW}.png`);
    expect(environment.written[path!]).toEqual(PNG);
    expect(environment.attempted).toEqual(["wl-paste"]);
  });

  it("falls through to the next tool when the first is not installed", () => {
    const environment = environmentOf("linux", { "wl-paste": null, xclip: ok() });
    expect(captureClipboardImage(environment, "/state", NOW)).not.toBeNull();
    expect(environment.attempted).toEqual(["wl-paste", "xclip"]);
  });

  it("falls through when a tool runs but has no image", () => {
    const environment = environmentOf("linux", { "wl-paste": failed(), xclip: ok() });
    expect(captureClipboardImage(environment, "/state", NOW)).not.toBeNull();
    expect(environment.attempted).toEqual(["wl-paste", "xclip"]);
  });

  it("falls through when a tool exits happily with nothing", () => {
    const environment = environmentOf("linux", { "wl-paste": empty(), xclip: ok() });
    expect(captureClipboardImage(environment, "/state", NOW)).not.toBeNull();
  });

  it("gives up when nothing on the machine can do it", () => {
    const environment = environmentOf("linux", { "wl-paste": null, xclip: null });
    expect(captureClipboardImage(environment, "/state", NOW)).toBeNull();
  });

  it("gives up when the file cannot be written", () => {
    const environment = environmentOf("linux", { "wl-paste": ok() }, { writeFails: true });
    expect(captureClipboardImage(environment, "/state", NOW)).toBeNull();
  });

  it("trusts the mac tool to write the file, and checks that it did", () => {
    const path = `/state/tmp/paste-${NOW}.png`;
    const wrote = environmentOf("darwin", { osascript: ok(new Uint8Array()) }, {
      writtenByTool: { [path]: 4096 },
    });
    expect(captureClipboardImage(wrote, "/state", NOW)).toBe(path);
    expect(wrote.written).toEqual({});

    const wroteNothing = environmentOf("darwin", { osascript: ok(new Uint8Array()) });
    expect(captureClipboardImage(wroteNothing, "/state", NOW)).toBeNull();
  });

  it("uses powershell on Windows and the unix tools nowhere near it", () => {
    const path = `/state/tmp/paste-${NOW}.png`;
    const environment = environmentOf("win32", { powershell: ok(new Uint8Array()) }, {
      writtenByTool: { [path]: 1024 },
    });
    expect(captureClipboardImage(environment, "/state", NOW)).toBe(path);
    expect(environment.attempted).toEqual(["powershell"]);
  });

  it("tries nothing at all on a platform with no known tool", () => {
    const environment = environmentOf("aix" as NodeJS.Platform, { "wl-paste": ok() });
    expect(captureClipboardImage(environment, "/state", NOW)).toBeNull();
    expect(environment.attempted).toEqual([]);
  });
});

describe("the command list", () => {
  it("prefers Wayland over X11", () => {
    const linux = CLIPBOARD_COMMANDS.filter((entry) => entry.platforms.includes("linux"));
    expect(linux.map((entry) => entry.command)).toEqual(["wl-paste", "xclip"]);
  });

  it("hands the mac and Windows tools the path to write", () => {
    for (const entry of CLIPBOARD_COMMANDS.filter((command) => command.writesFile)) {
      expect(entry.args("/tmp/x.png").join(" ")).toContain("/tmp/x.png");
    }
  });
});
