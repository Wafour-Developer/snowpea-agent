/**
 * What the TUI remembers between runs: the prompts you typed, and the session
 * you were last in for a given directory.
 *
 * Both live under `SNOWPEA_HOME` beside the daemon's own state, as plain
 * newline-delimited JSON — a format you can read, tail and truncate with the
 * tools already on the machine. Nothing here is load-bearing: every read
 * tolerates a missing, truncated or corrupt file, because a broken history must
 * never stop the TUI from starting.
 *
 * The file system work is behind a tiny interface so `test/history.test.ts` can
 * drive it without touching a disk.
 */

/** Prompts kept; older ones are dropped when the file is written. */
export const MAX_HISTORY = 500;

export const HISTORY_FILE = "tui-history.jsonl";
export const SESSIONS_FILE = "tui-sessions.json";

/** One line of the prompt history. */
export interface HistoryEntry {
  text: string;
  /** Epoch milliseconds. */
  at: number;
  /** Where it was typed, so a directory can be filtered out later. */
  workdir: string;
}

/** What the TUI remembers about the last session in a directory. */
export interface SessionRecord {
  sessionId: string;
  workdir: string;
  /** The first thing asked in it, for the launch screen. */
  firstPrompt: string;
  /** Epoch milliseconds of the last activity. */
  at: number;
}

/** The file operations this module needs; `node:fs` satisfies it. */
export interface FileStore {
  read(path: string): string | null;
  write(path: string, contents: string): void;
  join(...parts: string[]): string;
}

/** Where the TUI keeps its own state: `$SNOWPEA_HOME`, else `~/.snowpea`. */
export function stateDir(env: NodeJS.ProcessEnv, home: string): string {
  return env.SNOWPEA_HOME && env.SNOWPEA_HOME.length > 0
    ? env.SNOWPEA_HOME
    : `${home}/.snowpea`;
}

/** Parse the history file, skipping anything that is not a usable line. */
export function parseHistory(contents: string | null): HistoryEntry[] {
  if (!contents) return [];
  const out: HistoryEntry[] = [];
  for (const line of contents.split("\n")) {
    if (line.trim().length === 0) continue;
    try {
      const entry = JSON.parse(line) as Partial<HistoryEntry>;
      if (typeof entry.text === "string" && entry.text.length > 0) {
        out.push({
          text: entry.text,
          at: typeof entry.at === "number" ? entry.at : 0,
          workdir: typeof entry.workdir === "string" ? entry.workdir : "",
        });
      }
    } catch {
      // A half-written line from a killed process; the rest is still good.
    }
  }
  return out;
}

export class TuiHistory {
  private entries: HistoryEntry[] = [];

  constructor(
    private readonly store: FileStore,
    private readonly dir: string,
  ) {}

  /** Read the history from disk. Safe to call before anything exists. */
  load(): HistoryEntry[] {
    this.entries = parseHistory(this.store.read(this.store.join(this.dir, HISTORY_FILE)));
    return this.entries;
  }

  /** The prompts, oldest first. */
  prompts(): string[] {
    return this.entries.map((entry) => entry.text);
  }

  /**
   * Add one prompt and write the file back.
   *
   * A prompt repeated straight away is not recorded twice: walking back through
   * a history full of the same line is the thing that makes history useless.
   */
  add(text: string, workdir: string, now = Date.now()): void {
    const trimmed = text.trim();
    if (trimmed.length === 0) return;
    if (this.entries[this.entries.length - 1]?.text === trimmed) return;
    this.entries.push({ text: trimmed, at: now, workdir });
    if (this.entries.length > MAX_HISTORY) {
      this.entries = this.entries.slice(this.entries.length - MAX_HISTORY);
    }
    try {
      this.store.write(
        this.store.join(this.dir, HISTORY_FILE),
        `${this.entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`,
      );
    } catch {
      // A read-only or full disk must not break the session.
    }
  }
}

/** Every remembered session, keyed by workdir. */
export function parseSessions(contents: string | null): Record<string, SessionRecord> {
  if (!contents) return {};
  try {
    const parsed = JSON.parse(contents) as Record<string, SessionRecord>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export class SessionMemory {
  private records: Record<string, SessionRecord> = {};

  constructor(
    private readonly store: FileStore,
    private readonly dir: string,
  ) {}

  load(): Record<string, SessionRecord> {
    this.records = parseSessions(this.store.read(this.store.join(this.dir, SESSIONS_FILE)));
    return this.records;
  }

  /** What was last open in this directory, if anything. */
  last(workdir: string): SessionRecord | null {
    return this.records[workdir] ?? null;
  }

  /** Remember this session as the directory's most recent one. */
  remember(record: SessionRecord): void {
    this.records[record.workdir] = record;
    try {
      this.store.write(
        this.store.join(this.dir, SESSIONS_FILE),
        `${JSON.stringify(this.records, null, 2)}\n`,
      );
    } catch {
      // Best effort, like the history.
    }
  }
}

/** A session the daemon still has open, as `session.list` describes it. */
export interface LiveSession {
  sessionId: string;
  workdir: string;
  createdAt?: string;
}

/**
 * The newest session `session.list` reports for this directory, other than the
 * one just created.
 */
export function priorSession(
  sessions: LiveSession[],
  workdir: string,
  currentSessionId: string,
): SessionRecord | null {
  const candidates = sessions
    .filter((entry) => entry.workdir === workdir && entry.sessionId !== currentSessionId)
    .map((entry) => ({
      sessionId: entry.sessionId,
      workdir: entry.workdir,
      firstPrompt: "",
      at: entry.createdAt ? Date.parse(entry.createdAt) || 0 : 0,
    }))
    .sort((a, b) => b.at - a.at);
  return candidates[0] ?? null;
}

/**
 * The session the launch screen may offer, or null.
 *
 * Only a session the daemon still has can be resumed — `session.resume` on a
 * closed one fails — so the live list decides whether there is an offer at all,
 * and this surface's own record only supplies the prompt that makes the line
 * worth reading.
 */
export function offerSession(
  local: SessionRecord | null,
  live: SessionRecord | null,
): SessionRecord | null {
  if (!live) return null;
  if (!local || local.sessionId !== live.sessionId) return live;
  return { ...live, firstPrompt: local.firstPrompt, at: Math.max(live.at, local.at) };
}

/** `4m ago`, `2h ago`, `yesterday` — enough to recognise a session by. */
export function relativeTime(then: number, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}
