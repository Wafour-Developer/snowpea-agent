/**
 * Language servers: the segment, the table, and what a diagnostics event does
 * to the store.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import {
  diagnosticLineColor,
  diagnosticsBadge,
  diagnosticsColor,
  lspColor,
  lspLabel,
  lspSummary,
  lspTable,
  readLspStatus,
  serversFor,
  type LspServer,
} from "../src/state/lsp.js";
import { buildHudSegments } from "../src/layout/hud.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

const server = (id: string, state: LspServer["state"], extra: Partial<LspServer> = {}): LspServer => ({
  id,
  root: "/repo",
  state,
  languageId: "python",
  pid: 4242,
  reason: null,
  ...extra,
});

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

function hudBase() {
  return {
    status: "connected" as const,
    version: "0.1.7",
    provider: null,
    model: null,
    mode: "accept" as const,
    usage: { inputTokens: 0, outputTokens: 0 },
    sessionMs: 0,
    pendingApprovals: 0,
  };
}

beforeEach(() => {
  __resetIdCounter();
});

describe("readLspStatus", () => {
  it("reads the rows the daemon sent", () => {
    expect(
      readLspStatus({
        servers: [
          { id: "pyright", root: "/repo", state: "ready", languageId: "python", pid: 7 },
          { id: "ruff", root: "/repo", state: "broken", languageId: "python", reason: "exited: code 2" },
        ],
      }),
    ).toEqual([
      { id: "pyright", root: "/repo", state: "ready", languageId: "python", pid: 7, reason: null },
      { id: "ruff", root: "/repo", state: "broken", languageId: "python", pid: null, reason: "exited: code 2" },
    ]);
  });

  it("treats a daemon without the feature as no servers", () => {
    expect(readLspStatus(undefined)).toEqual([]);
    expect(readLspStatus({})).toEqual([]);
    expect(readLspStatus({ servers: "nonsense" })).toEqual([]);
  });

  it("skips a row with no id and defaults a state it does not know", () => {
    const servers = readLspStatus({
      servers: [{ root: "/repo" }, { id: "gopls", state: "confused" }],
    });
    expect(servers).toHaveLength(1);
    expect(servers[0]).toMatchObject({ id: "gopls", state: "stopped", pid: null });
  });
});

describe("the segment", () => {
  it("counts the ready ones", () => {
    expect(lspLabel([server("pyright", "ready"), server("gopls", "ready")])).toBe("lsp 2");
    expect(lspColor([server("pyright", "ready")])).toBeUndefined();
  });

  it("says nothing when there is nothing running", () => {
    expect(lspLabel([])).toBeNull();
    expect(lspLabel([server("pyright", "stopped")])).toBeNull();
  });

  it("shows a starting server as not ready yet, rather than hiding", () => {
    expect(lspLabel([server("pyright", "starting")])).toBe("lsp 0");
  });

  it("marks a broken server, in red", () => {
    const servers = [server("pyright", "ready"), server("gopls", "broken")];
    expect(lspLabel(servers)).toBe("lsp 1!");
    expect(lspColor(servers)).toBe("red");
  });

  it("counts every state", () => {
    expect(
      lspSummary([
        server("a", "ready"),
        server("b", "ready"),
        server("c", "broken"),
        server("d", "starting"),
        server("e", "stopped"),
      ]),
    ).toEqual({ ready: 2, broken: 1, starting: 1, stopped: 1 });
  });

  it("reaches the HUD, and stays out of it when nothing runs", () => {
    const base = {
      status: "connected" as const,
      version: "0.1.7",
      provider: null,
      model: null,
      mode: "accept" as const,
      usage: { inputTokens: 0, outputTokens: 0 },
      sessionMs: 0,
      pendingApprovals: 0,
    };
    const segment = buildHudSegments({ ...base, lsp: [server("pyright", "ready")] }).find(
      (entry) => entry.key === "lsp",
    );
    expect(segment?.text).toBe("lsp 1");
    expect(buildHudSegments(base).some((entry) => entry.key === "lsp")).toBe(false);
  });
});

describe("the /lsp table", () => {
  it("lists each server with its state, language and root", () => {
    const table = lspTable([server("pyright", "ready"), server("gopls", "broken", { pid: null })]);
    expect(table).toContain("language servers (1 ready, 1 broken)");
    expect(table).toContain("pyright  ready python pid 4242  /repo");
    expect(table).toContain("gopls");
    expect(table).not.toContain("gopls  broken python pid");
  });

  it("says why a server is not ready", () => {
    const table = lspTable([server("ruff", "broken", { reason: "exited: code 2" })]);
    expect(table).toContain("ruff  broken (exited: code 2) python");
  });

  it("says why there is nothing to list", () => {
    expect(lspTable([])).toContain("no language servers are running");
  });

  it("lists this project's servers and only counts the others", () => {
    const servers = [
      server("pyright", "ready", { root: "/work/kokoro" }),
      server("ruff", "broken", { root: "/work/memtest" }),
    ];
    const table = lspTable(servers, "/work/kokoro");
    expect(table).toContain("language servers (1 ready)");
    expect(table).not.toContain("/work/memtest");
    expect(table).toContain("1 more in other projects' folders");
  });
});

describe("servers of this project", () => {
  const servers = [
    server("pyright", "ready", { root: "/work/kokoro" }),
    server("tsserver", "ready", { root: "/work/kokoro/web" }),
    server("ruff", "broken", { root: "/work/memtest" }),
    server("gopls", "ready", { root: "/work/kokoro-old" }),
  ];

  it("keeps the workdir, folders below it and the folder above it", () => {
    expect(serversFor(servers, "/work/kokoro").map((s) => s.id)).toEqual(["pyright", "tsserver"]);
    expect(serversFor(servers, "/work/kokoro/web/src").map((s) => s.id)).toEqual(["pyright", "tsserver"]);
  });

  it("reads Windows separators and a trailing slash", () => {
    const win = [server("pyright", "ready", { root: "C:\\work\\kokoro" })];
    expect(serversFor(win, "C:\\work\\kokoro\\").map((s) => s.id)).toEqual(["pyright"]);
  });

  it("keeps everything without a workdir", () => {
    expect(serversFor(servers, undefined)).toHaveLength(4);
  });

  it("does not show another project's broken server as this session's", () => {
    const hud = buildHudSegments({ ...hudBase(), workdir: "/work/kokoro", lsp: servers });
    expect(hud.find((s) => s.key === "lsp")?.text).toBe("lsp 2");
  });
});

describe("diagnostics", () => {
  it("colours the severities inside a tool result's block", () => {
    expect(diagnosticLineColor("ERROR src/a.py:3 undefined name")).toBe("red");
    expect(diagnosticLineColor("  WARNING src/a.py:9 unused import")).toBe("yellow");
    expect(diagnosticLineColor("Diagnostics (2)")).toBeUndefined();
    expect(diagnosticLineColor("")).toBeUndefined();
  });

  it("badges a file, red for errors and amber for warnings alone", () => {
    expect(diagnosticsBadge({ count: 3, errors: 1, warnings: 2 })).toBe("⚠ 3");
    expect(diagnosticsColor({ count: 3, errors: 1, warnings: 2 })).toBe("red");
    expect(diagnosticsColor({ count: 2, errors: 0, warnings: 2 })).toBe("yellow");
    expect(diagnosticsBadge({ count: 0, errors: 0, warnings: 0 })).toBeNull();
    expect(diagnosticsBadge(undefined)).toBeNull();
  });

  it("keeps the counts per file", () => {
    const state = apply(
      initialState,
      event(1, "lsp.diagnostics", { path: "/repo/a.py", count: 2, errors: 1, warnings: 1 }),
      event(2, "lsp.diagnostics", { path: "/repo/b.py", count: 1, errors: 0, warnings: 1 }),
    );
    expect(state.diagnostics["/repo/a.py"]).toEqual({ count: 2, errors: 1, warnings: 1 });
    expect(state.diagnostics["/repo/b.py"].warnings).toBe(1);
  });

  it("drops a file that came back clean", () => {
    const state = apply(
      initialState,
      event(1, "lsp.diagnostics", { path: "/repo/a.py", count: 2, errors: 2, warnings: 0 }),
      event(2, "lsp.diagnostics", { path: "/repo/a.py", count: 0, errors: 0, warnings: 0 }),
    );
    expect(state.diagnostics).toEqual({});
  });

  it("ignores an event with no path", () => {
    expect(apply(initialState, event(1, "lsp.diagnostics", { count: 3 })).diagnostics).toEqual({});
  });
});

describe("the status action", () => {
  it("replaces what the store had", () => {
    let state = reducer(initialState, { type: "lsp/status", servers: [server("pyright", "ready")] });
    expect(state.lsp).toHaveLength(1);
    state = reducer(state, { type: "lsp/status", servers: [] });
    expect(state.lsp).toEqual([]);
  });
});

describe("a note", () => {
  it("puts one line in the transcript as the surface's own", () => {
    const state = reducer(initialState, { type: "note", text: "language servers (0 ready)" });
    expect(state.messages[0]).toMatchObject({
      role: "system",
      text: "language servers (0 ready)",
    });
    expect(state.timeline).toHaveLength(1);
  });
});
