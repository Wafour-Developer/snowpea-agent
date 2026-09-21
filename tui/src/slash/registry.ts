/**
 * Slash command registry — deliberately table-free (plan §3.2).
 *
 * Most commands come from `command.list`. Surface-only commands are merged in
 * here as well so autocomplete describes everything the input can execute.
 */

import type { CommandInfo } from "../rpc/sdk.js";
import { rankCommandMatches } from "../state/slash-completion.js";

const SURFACE_COMMANDS: CommandInfo[] = [
  {
    name: "resume",
    summary: "Resume the last session in this directory, or /resume <sessionId>.",
    source: "tui",
  },
  {
    name: "session",
    summary: "Delete saved sessions: /session delete <id> | clear [--all].",
    source: "tui",
  },
  {
    name: "sessions",
    summary: "List saved sessions and choose one to resume.",
    source: "tui",
  },
  {
    name: "voice",
    summary: "Voice input: /voice toggles, /voice on|off; then Ctrl+Space (or /rec) records.",
    source: "tui",
  },
  { name: "rec", summary: "Start or stop a recording.", source: "tui" },
  {
    name: "tts",
    summary: "Spoken replies: /tts on|off, /tts voices, /tts voice <id> [lang].",
    source: "tui",
  },
  {
    name: "mouse",
    summary: "Mouse on|off: click ◯ rows to open an agent (Shift+drag selects while on).",
    source: "tui",
  },
];

/** The words `/tts` takes, offered as completions after the command. */
export const TTS_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "on", summary: "Read every finished reply aloud." },
  { action: "off", summary: "Stop reading replies." },
  { action: "voices", summary: "List the pinned engine's voices." },
  { action: "voice", summary: "/tts voice <id> [lang] — pick a voice." },
];

export function ttsSubCommands(draft: string): CommandInfo[] {
  return subCommands(draft, "tts", TTS_ACTIONS);
}

/** `/voice on|off` completes too; a bare `/voice` toggles. */
export const VOICE_ACTIONS: ReadonlyArray<{ action: string; summary: string }> = [
  { action: "on", summary: "Arm voice input; Ctrl+Space records." },
  { action: "off", summary: "Disarm voice input." },
];

export function voiceSubCommands(draft: string): CommandInfo[] {
  return subCommands(draft, "voice", VOICE_ACTIONS);
}

function subCommands(
  draft: string,
  command: string,
  actions: ReadonlyArray<{ action: string; summary: string }>,
): CommandInfo[] {
  const match = new RegExp(`^\\/${command}(?:\\s+([^\\s]*))?$`).exec(draft);
  if (!match) return [];
  const typed = match[1] ?? "";
  // `/tts ` with nothing after it is the bare command, ready to run on Enter;
  // the sub-actions appear once a letter of one is typed.
  if (typed === "" && /\s$/.test(draft)) return [];
  return actions
    .filter((entry) => entry.action.startsWith(typed))
    .map((entry) => ({ name: `${command} ${entry.action}`, summary: entry.summary, source: "tui" }));
}

function withSurfaceCommands(commands: CommandInfo[]): CommandInfo[] {
  const names = new Set(commands.map((command) => command.name));
  return [...commands, ...SURFACE_COMMANDS.filter((command) => !names.has(command.name))];
}

/** Structural subset of `TuiClient` the registry needs; keeps tests trivial. */
export interface RegistryClient {
  call(method: string, params?: Record<string, unknown>): Promise<any>;
}

export interface ParsedCommand {
  name: string;
  /** Everything after the command name, untouched. */
  args: string;
}

/**
 * Split only the leading `/name`. The remainder is passed through raw, because
 * argument parsing belongs to the server-side command (`argsSchema`).
 */
export function parse(input: string): ParsedCommand | null {
  if (!input.startsWith("/")) return null;
  const body = input.slice(1);
  const match = /^([^\s]+)\s*([\s\S]*)$/.exec(body);
  if (!match) return null;
  const name = match[1];
  if (name.length === 0) return null;
  return { name, args: match[2] ?? "" };
}

export class SlashRegistry {
  private commands: CommandInfo[] = [];

  constructor(
    private readonly client: RegistryClient,
    private readonly sessionId: string | null = null,
  ) {}

  /** Fetch the command table from the daemon. Also used by `refresh()`. */
  async load(): Promise<CommandInfo[]> {
    const params = this.sessionId ? { sessionId: this.sessionId } : {};
    const result = await this.client.call("command.list", params);
    const commands = Array.isArray(result?.commands) ? (result.commands as CommandInfo[]) : [];
    this.commands = withSurfaceCommands(commands);
    return this.commands;
  }

  /**
   * Re-fetch after anything that can change the server-side table
   * (`skill.reload`, plugin install, or a `commands.changed` event).
   */
  refresh(): Promise<CommandInfo[]> {
    return this.load();
  }

  list(): CommandInfo[] {
    return this.commands;
  }

  /** Autocomplete candidates for a typed prefix, with or without the slash. */
  complete(prefix: string): CommandInfo[] {
    const body = prefix.startsWith("/") ? prefix.slice(1) : prefix;
    // Multi-word commands are completed by the TUI (`/skill`, `/mcp`, …).
    // Once a space appears, the registry must not offer the bare first token again.
    if (body.includes(" ")) return [];
    const needle = body.split(/\s/)[0] ?? "";
    if (needle.length === 0) return this.commands;
    return rankCommandMatches(this.commands, needle);
  }

  /**
   * Run a `/...` line. Returns null when the input is not a slash command, so
   * the caller falls back to `session.prompt`.
   */
  async dispatch(input: string): Promise<{ turnId: string } | null> {
    const parsed = parse(input);
    if (!parsed) return null;
    return (await this.client.call("command.run", {
      sessionId: this.sessionId,
      name: parsed.name,
      args: parsed.args,
    })) as { turnId: string };
  }
}

/** Convenience factory mirroring the `load(client)` shape from the story. */
export async function load(
  client: RegistryClient,
  sessionId: string | null = null,
): Promise<SlashRegistry> {
  const registry = new SlashRegistry(client, sessionId);
  await registry.load();
  return registry;
}
