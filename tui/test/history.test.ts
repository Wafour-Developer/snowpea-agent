/**
 * What the TUI remembers between runs, and how it survives a bad file.
 */

import { describe, expect, it } from "vitest";

import {
  MAX_HISTORY,
  offerSession,
  priorSession,
  SessionMemory,
  TuiHistory,
  parseHistory,
  parseSessions,
  relativeTime,
  sessionKindPrefix,
  stateDir,
  type FileStore,
} from "../src/state/history.js";
import { previewPrompt } from "../src/components/LaunchBanner.js";

function memoryStore(seed: Record<string, string> = {}): FileStore & { files: Record<string, string> } {
  const files = { ...seed };
  return {
    files,
    read: (path) => files[path] ?? null,
    write: (path, contents) => {
      files[path] = contents;
    },
    join: (...parts) => parts.join("/"),
  };
}

describe("stateDir", () => {
  it("prefers SNOWPEA_HOME and falls back to ~/.snowpea", () => {
    expect(stateDir({ SNOWPEA_HOME: "/tmp/sp" } as NodeJS.ProcessEnv, "/home/dev")).toBe("/tmp/sp");
    expect(stateDir({} as NodeJS.ProcessEnv, "/home/dev")).toBe("/home/dev/.snowpea");
  });
});

describe("prompt history", () => {
  it("round-trips through the file", () => {
    const store = memoryStore();
    const history = new TuiHistory(store, "/state");
    history.load();
    history.add("first prompt", "/repo", 1000);
    history.add("second prompt", "/repo", 2000);

    const reopened = new TuiHistory(store, "/state");
    reopened.load();
    expect(reopened.prompts()).toEqual(["first prompt", "second prompt"]);
  });

  it("does not record the same prompt twice in a row", () => {
    const store = memoryStore();
    const history = new TuiHistory(store, "/state");
    history.load();
    history.add("same", "/repo");
    history.add("same", "/repo");
    history.add("different", "/repo");
    history.add("same", "/repo");
    expect(history.prompts()).toEqual(["same", "different", "same"]);
  });

  it("ignores blank prompts", () => {
    const history = new TuiHistory(memoryStore(), "/state");
    history.load();
    history.add("   ", "/repo");
    expect(history.prompts()).toEqual([]);
  });

  it("keeps only the last few hundred", () => {
    const history = new TuiHistory(memoryStore(), "/state");
    history.load();
    for (let index = 0; index < MAX_HISTORY + 20; index += 1) history.add(`p${index}`, "/repo");
    expect(history.prompts()).toHaveLength(MAX_HISTORY);
    expect(history.prompts()[0]).toBe("p20");
  });

  it("reads what it can out of a half-written file", () => {
    const entries = parseHistory('{"text":"good","at":1,"workdir":"/r"}\n{"text":"tru');
    expect(entries.map((entry) => entry.text)).toEqual(["good"]);
    expect(parseHistory(null)).toEqual([]);
  });

  it("starts empty rather than throwing when nothing is there", () => {
    const history = new TuiHistory(memoryStore(), "/state");
    expect(history.load()).toEqual([]);
  });
});

describe("session memory", () => {
  it("remembers the last session per directory", () => {
    const store = memoryStore();
    const memory = new SessionMemory(store, "/state");
    memory.load();
    memory.remember({ sessionId: "s-1", workdir: "/a", firstPrompt: "do a thing", at: 10 });
    memory.remember({ sessionId: "s-2", workdir: "/b", firstPrompt: "do another", at: 20 });
    memory.remember({ sessionId: "s-3", workdir: "/a", firstPrompt: "do a third", at: 30 });

    const reopened = new SessionMemory(store, "/state");
    reopened.load();
    expect(reopened.last("/a")?.sessionId).toBe("s-3");
    expect(reopened.last("/b")?.sessionId).toBe("s-2");
    expect(reopened.last("/never")).toBeNull();
  });

  it("treats a corrupt file as no memory at all", () => {
    expect(parseSessions("{not json")).toEqual({});
    expect(parseSessions(null)).toEqual({});
  });
});

describe("priorSession", () => {
  const listed = [
    { sessionId: "s-old", workdir: "/repo", createdAt: "2026-09-10T10:00:00Z" },
    { sessionId: "s-new", workdir: "/repo", createdAt: "2026-09-12T10:00:00Z" },
    { sessionId: "s-other", workdir: "/elsewhere", createdAt: "2026-09-12T11:00:00Z" },
    { sessionId: "s-current", workdir: "/repo", createdAt: "2026-09-12T12:00:00Z" },
  ];

  it("takes the newest session for this directory, never the current one", () => {
    expect(priorSession(listed, "/repo", "s-current")?.sessionId).toBe("s-new");
  });

  it("has nothing to offer for a directory the daemon has not seen", () => {
    expect(priorSession(listed, "/untouched", "s-current")).toBeNull();
    expect(priorSession([], "/repo", "s-current")).toBeNull();
  });
});

describe("offerSession", () => {
  const local = { sessionId: "s-1", workdir: "/repo", firstPrompt: "do a thing", at: 100 };

  it("offers the persisted local session after a daemon restart", () => {
    expect(offerSession(local, null)).toEqual(local);
    expect(offerSession(null, null)).toBeNull();
  });

  it("dresses the live session with the prompt this surface remembers", () => {
    const merged = offerSession(local, { ...local, firstPrompt: "", at: 200 });
    expect(merged).toEqual({ ...local, at: 200 });
  });

  it("offers a live session it has no memory of, prompt and all", () => {
    const live = { sessionId: "s-2", workdir: "/repo", firstPrompt: "", at: 300 };
    expect(offerSession(local, live)).toEqual(live);
    expect(offerSession(null, live)).toEqual(live);
  });
});

describe("relativeTime", () => {
  it("says how long ago in words", () => {
    const now = 1_000_000_000;
    expect(relativeTime(now - 10_000, now)).toBe("just now");
    expect(relativeTime(now - 4 * 60_000, now)).toBe("4m ago");
    expect(relativeTime(now - 2 * 3_600_000, now)).toBe("2h ago");
    expect(relativeTime(now - 26 * 3_600_000, now)).toBe("yesterday");
    expect(relativeTime(now - 3 * 86_400_000, now)).toBe("3d ago");
  });
});

describe("previewPrompt", () => {
  it("flattens and cuts a long prompt", () => {
    expect(previewPrompt("keep\n  this  short")).toBe("keep this short");
    const long = previewPrompt("x".repeat(200));
    expect(long).toHaveLength(60);
    expect(long.endsWith("…")).toBe(true);
  });
});

describe("session kind prefixes", () => {
  it("leaves a human's own thread unmarked", () => {
    expect(sessionKindPrefix(undefined)).toBe("");
    expect(sessionKindPrefix("chat", "s-850623ab")).toBe("");
  });

  it("marks a scheduled run with its parent thread", () => {
    expect(sessionKindPrefix("scheduled", "s-850623abcdef")).toBe("⏰ s-850623 ");
    expect(sessionKindPrefix("scheduled")).toBe("⏰ ");
  });

  it("marks subagents and named agents", () => {
    expect(sessionKindPrefix("subagent", "s-850623abcdef")).toBe("↳ s-850623 ");
    expect(sessionKindPrefix("agent", "s-850623abcdef")).toBe("◆ ");
  });
});
