/**
 * The local recorder and player: which tool is tried, and in what order.
 */

import { describe, expect, it } from "vitest";

import {
  PLAYERS,
  RECORDERS,
  createLocalAudio,
  type LocalProcess,
  type Spawner,
} from "../src/util/audio-tools.js";

function spawnerOf(installed: string[]): Spawner & { attempted: string[]; args: string[][] } {
  const attempted: string[] = [];
  const args: string[][] = [];
  return {
    attempted,
    args,
    start(command, commandArgs) {
      attempted.push(command);
      args.push(commandArgs);
      if (!installed.includes(command)) return null;
      const process: LocalProcess = { command, stop: () => undefined };
      return process;
    },
  };
}

describe("recording here", () => {
  it("takes the first recorder that is installed", () => {
    const spawner = spawnerOf(["arecord"]);
    const audio = createLocalAudio(spawner, "linux");
    expect(audio.record("/tmp/a.wav")?.command).toBe("arecord");
    expect(spawner.attempted).toEqual(["sox", "rec", "arecord"]);
  });

  it("writes to the path it was given", () => {
    const spawner = spawnerOf(["sox"]);
    createLocalAudio(spawner, "linux").record("/tmp/a.wav");
    expect(spawner.args[0]).toContain("/tmp/a.wav");
  });

  it("answers null when nothing on this machine records", () => {
    const spawner = spawnerOf([]);
    expect(createLocalAudio(spawner, "linux").record("/tmp/a.wav")).toBeNull();
  });

  it("keeps platform-only tools off other platforms", () => {
    const spawner = spawnerOf([]);
    createLocalAudio(spawner, "darwin").record("/tmp/a.wav");
    expect(spawner.attempted).not.toContain("arecord");
    expect(spawner.attempted).toContain("ffmpeg");
  });
});

describe("playing here", () => {
  it("prefers the platform's own player", () => {
    const mac = spawnerOf(["afplay", "ffplay"]);
    expect(createLocalAudio(mac, "darwin").play("/tmp/a.wav")?.command).toBe("afplay");

    const linux = spawnerOf(["ffplay", "paplay"]);
    expect(createLocalAudio(linux, "linux").play("/tmp/a.wav")?.command).toBe("paplay");
  });

  it("falls through to a portable one", () => {
    const spawner = spawnerOf(["ffplay"]);
    expect(createLocalAudio(spawner, "linux").play("/tmp/a.wav")?.command).toBe("ffplay");
  });

  it("answers null when nothing can play", () => {
    expect(createLocalAudio(spawnerOf([]), "linux").play("/tmp/a.wav")).toBeNull();
  });
});

describe("the tables", () => {
  it("name a command and take a path in every entry", () => {
    for (const tool of [...RECORDERS, ...PLAYERS]) {
      expect(tool.command).toMatch(/^[a-z]+$/);
      expect(tool.args("/tmp/x.wav").join(" ")).toContain("/tmp/x.wav");
    }
  });
});
