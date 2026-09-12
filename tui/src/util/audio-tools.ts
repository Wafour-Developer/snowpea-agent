/**
 * Recording and playing audio on this machine, when the daemon cannot.
 *
 * The daemon does both itself wherever it can, and that is the path the TUI
 * takes: it is the side that knows which backends are configured. This exists
 * for the case the daemon reports no recorder or no player of its own — a
 * remote daemon, or one on a host with no sound — where the microphone and the
 * speakers are here rather than there.
 *
 * The tools are a table and the spawner is injected, so `test/audio-tools.test.ts`
 * can check the order and the fallbacks without a sound card.
 */

/** A process that was started and can be stopped. */
export interface LocalProcess {
  /** The tool that is running, for the toast. */
  command: string;
  stop(): void;
}

export interface Spawner {
  /**
   * Start a command in the background.
   *
   * Null means it is not installed, which is what makes the next entry in the
   * table worth trying.
   */
  start(command: string, args: string[]): LocalProcess | null;
}

export interface AudioTool {
  command: string;
  args(path: string): string[];
  platforms?: readonly NodeJS.Platform[];
}

/** Recorders, preferred first. Each writes a wav to the path it is given. */
export const RECORDERS: readonly AudioTool[] = [
  { command: "sox", args: (path) => ["-d", "-r", "16000", "-c", "1", path] },
  { command: "rec", args: (path) => ["-r", "16000", "-c", "1", path] },
  {
    command: "arecord",
    args: (path) => ["-q", "-f", "S16_LE", "-r", "16000", "-c", "1", path],
    platforms: ["linux"],
  },
  {
    command: "ffmpeg",
    args: (path) => ["-loglevel", "quiet", "-f", "avfoundation", "-i", ":0", "-y", path],
    platforms: ["darwin"],
  },
];

/** Players, preferred first. */
export const PLAYERS: readonly AudioTool[] = [
  { command: "afplay", args: (path) => [path], platforms: ["darwin"] },
  { command: "paplay", args: (path) => [path], platforms: ["linux"] },
  { command: "aplay", args: (path) => ["-q", path], platforms: ["linux"] },
  { command: "play", args: (path) => ["-q", path] },
  { command: "ffplay", args: (path) => ["-loglevel", "quiet", "-nodisp", "-autoexit", path] },
];

export interface LocalAudio {
  /** Start recording to `path`, or null when nothing here can. */
  record(path: string): LocalProcess | null;
  /** Play a file, or null when nothing here can. */
  play(path: string): LocalProcess | null;
}

function firstWorking(
  tools: readonly AudioTool[],
  spawner: Spawner,
  platform: NodeJS.Platform,
  path: string,
): LocalProcess | null {
  for (const tool of tools) {
    if (tool.platforms && !tool.platforms.includes(platform)) continue;
    const process = spawner.start(tool.command, tool.args(path));
    if (process) return process;
  }
  return null;
}

export function createLocalAudio(spawner: Spawner, platform: NodeJS.Platform): LocalAudio {
  return {
    record: (path) => firstWorking(RECORDERS, spawner, platform, path),
    play: (path) => firstWorking(PLAYERS, spawner, platform, path),
  };
}
