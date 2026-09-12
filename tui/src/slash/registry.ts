/**
 * Slash command registry — deliberately table-free (plan §3.2).
 *
 * Most commands come from `command.list`. Surface-only commands are merged in
 * here as well so autocomplete describes everything the input can execute.
 */

import type { CommandInfo } from "../rpc/sdk.js";

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
];

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
    const needle = (prefix.startsWith("/") ? prefix.slice(1) : prefix).split(/\s/)[0] ?? "";
    if (needle.length === 0) return this.commands;
    return this.commands.filter((c) => c.name.startsWith(needle));
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
