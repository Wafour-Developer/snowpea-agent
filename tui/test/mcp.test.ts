/**
 * The MCP module: the `/mcp` completion table, the command line the form
 * splits, and the `mcp 2/3` the HUD draws.
 */

import { describe, expect, it } from "vitest";

import {
  applyMcpChange,
  draftFromCatalog,
  draftParams,
  isBareMcpAdd,
  isBareMcpCatalog,
  isUnsafeError,
  isValidMcpName,
  mcpColor,
  mcpLabel,
  mcpSubCommands,
  parseAssignment,
  parseMcpConfigure,
  probeSummary,
  readMcpCatalog,
  readMcpList,
  readMcpProbe,
  splitArgs,
  type McpServerRow,
} from "../src/state/mcp.js";

const LIST = {
  servers: [
    {
      name: "notes",
      scope: "project",
      transport: "stdio",
      state: "ready",
      command: "python",
      args: ["-m", "notes"],
      envKeys: ["TOKEN"],
      toolCount: 2,
      tools: [
        { name: "search", description: "find a note" },
        { name: "write", description: "" },
      ],
    },
    {
      name: "remote",
      scope: "global",
      transport: "http",
      state: "error",
      url: "https://example.internal/mcp",
      headerKeys: ["Authorization"],
      error: "connection refused",
    },
  ],
};

const rows = (): McpServerRow[] => readMcpList(LIST);

describe("readMcpList", () => {
  it("fills in what the daemon left out", () => {
    const [notes, remote] = rows();
    expect(notes.args).toEqual(["-m", "notes"]);
    expect(notes.tools.map((tool) => tool.name)).toEqual(["search", "write"]);
    expect(notes.headerKeys).toEqual([]);
    expect(remote.transport).toBe("http");
    expect(remote.error).toBe("connection refused");
    // The count is taken from the tools when the daemon did not send one.
    expect(remote.toolCount).toBe(0);
  });

  it("answers empty for a daemon without the feature", () => {
    expect(readMcpList(null)).toEqual([]);
    expect(readMcpList({ error: "unknown method" })).toEqual([]);
  });

  it("never invents a state", () => {
    const [only] = readMcpList({ servers: [{ name: "x", state: "exploded" }] });
    expect(only.state).toBe("stopped");
  });
});

describe("the HUD segment", () => {
  it("counts the ready servers out of the configured ones", () => {
    expect(mcpLabel(rows())).toBe("mcp 1/2");
    expect(mcpColor(rows())).toBe("red");
  });

  it("stays out of the way when nothing is configured", () => {
    expect(mcpLabel([])).toBeNull();
    expect(mcpColor([])).toBeUndefined();
  });

  it("leaves a disabled entry out of both halves", () => {
    const disabled = rows().map((row) => (row.name === "remote" ? { ...row, disabled: true } : row));
    expect(mcpLabel(disabled)).toBe("mcp 1/1");
  });
});

describe("applyMcpChange", () => {
  it("moves a known server without waiting for a new list", () => {
    const next = applyMcpChange(rows(), { name: "remote", state: "ready", toolCount: 4 });
    expect(mcpLabel(next)).toBe("mcp 2/2");
    expect(next.find((row) => row.name === "remote")?.toolCount).toBe(4);
  });

  it("drops a removed one and adopts an unknown one", () => {
    expect(applyMcpChange(rows(), { name: "notes", state: "stopped", removed: true })).toHaveLength(1);
    expect(applyMcpChange(rows(), { name: "fresh", state: "starting" })).toHaveLength(3);
  });

  it("ignores a payload with no name", () => {
    expect(applyMcpChange(rows(), {})).toHaveLength(2);
  });
});

describe("the /mcp palette", () => {
  it("offers the sub-actions, narrowing as they are typed", () => {
    const all = mcpSubCommands("/mcp ").map((entry) => entry.name);
    expect(all).toContain("mcp list");
    expect(all).toContain("mcp add");
    expect(all).toContain("mcp configure");
    expect(mcpSubCommands("/mcp re").map((entry) => entry.name)).toEqual([
      "mcp remove",
      "mcp reload",
    ]);
  });

  it("completes server names for the actions that take one", () => {
    const offered = mcpSubCommands("/mcp test ", rows());
    expect(offered.map((entry) => entry.name)).toEqual(["mcp test notes", "mcp test remote"]);
    expect(offered[0].summary).toContain("project");
    expect(offered[0].summary).toContain("2 tools");
    expect(mcpSubCommands("/mcp remove re", rows()).map((entry) => entry.name)).toEqual([
      "mcp remove remote",
    ]);
  });

  it("does not offer names to an action that takes none", () => {
    expect(mcpSubCommands("/mcp list ", rows())).toEqual([]);
  });

  it("completes catalog ids after --preset", () => {
    const catalog = readMcpCatalog({
      entries: [
        { id: "github", label: "GitHub", description: "repos and issues", needs: ["TOKEN"] },
        { id: "fetch", label: "Fetch", description: "http get" },
      ],
    });
    const offered = mcpSubCommands("/mcp add gh --preset gi", rows(), catalog);
    expect(offered.map((entry) => entry.name)).toEqual(["mcp add gh --preset github"]);
  });

  it("says nothing for another command", () => {
    expect(mcpSubCommands("/skill ")).toEqual([]);
  });
});

describe("the cues the app watches for", () => {
  it("knows a bare add, a bare catalog and a bare configure", () => {
    expect(isBareMcpAdd("/mcp add")).toBe(true);
    expect(isBareMcpAdd("/mcp add notes -- python")).toBe(false);
    expect(isBareMcpCatalog(" /mcp catalog ")).toBe(true);
    expect(parseMcpConfigure("/mcp configure notes")).toBe("notes");
    expect(parseMcpConfigure("/mcp configure notes search")).toBeNull();
  });

  it("holds the daemon's name rule", () => {
    expect(isValidMcpName("my-server_1")).toBe(true);
    expect(isValidMcpName("my server")).toBe(false);
    expect(isValidMcpName("")).toBe(false);
  });
});

describe("splitArgs", () => {
  it("splits a command line into argv", () => {
    expect(splitArgs("npx -y @scope/server")).toEqual(["npx", "-y", "@scope/server"]);
  });

  it("keeps a quoted argument together", () => {
    expect(splitArgs('python -c "import x; x.run()"')).toEqual([
      "python",
      "-c",
      "import x; x.run()",
    ]);
    expect(splitArgs("say 'hello there'")).toEqual(["say", "hello there"]);
  });

  it("keeps an empty quoted argument", () => {
    expect(splitArgs('cmd ""')).toEqual(["cmd", ""]);
  });

  it("escapes with a backslash and ignores surrounding space", () => {
    expect(splitArgs("  cmd a\\ b  ")).toEqual(["cmd", "a b"]);
    expect(splitArgs("")).toEqual([]);
  });
});

describe("the draft", () => {
  it("becomes stdio params", () => {
    expect(
      draftParams({
        name: "notes",
        transport: "stdio",
        commandLine: 'python -m "my notes"',
        url: "",
        env: [{ key: "TOKEN", value: "abc" }],
        headers: [],
        scope: "project",
      }),
    ).toEqual({
      name: "notes",
      command: "python",
      args: ["-m", "my notes"],
      env: { TOKEN: "abc" },
      scope: "project",
    });
  });

  it("becomes url params, with headers and no argv", () => {
    const params = draftParams({
      name: "remote",
      transport: "url",
      commandLine: "",
      url: "https://example.internal/mcp",
      env: [],
      headers: [{ key: "Authorization", value: "Bearer x" }],
      scope: "global",
    });
    expect(params).toEqual({
      name: "remote",
      url: "https://example.internal/mcp",
      headers: { Authorization: "Bearer x" },
      scope: "global",
    });
    expect(params.command).toBeUndefined();
  });

  it("opens on a catalog preset, with its variables waiting to be filled", () => {
    const [entry] = readMcpCatalog({
      entries: [
        {
          id: "github",
          label: "GitHub",
          description: "repos",
          entry: { command: "npx", args: ["-y", "@mcp/github"] },
          needs: ["GITHUB_TOKEN"],
        },
      ],
    });
    const draft = draftFromCatalog(entry);
    expect(draft.name).toBe("github");
    expect(draft.commandLine).toBe("npx -y @mcp/github");
    expect(draft.env).toEqual([{ key: "GITHUB_TOKEN", value: "" }]);
  });
});

describe("K=V lines", () => {
  it("splits on the first = and keeps the rest verbatim", () => {
    expect(parseAssignment("TOKEN=a=b")).toEqual({ key: "TOKEN", value: "a=b" });
    expect(parseAssignment("  TOKEN =x")).toEqual({ key: "TOKEN", value: "x" });
  });

  it("refuses a line that is not one", () => {
    expect(parseAssignment("TOKEN")).toBeNull();
    expect(parseAssignment("=x")).toBeNull();
  });
});

describe("the probe", () => {
  it("summarises what the server answered", () => {
    const probe = readMcpProbe({ ok: true, tools: [{ name: "a" }, { name: "b" }] });
    expect(probeSummary(probe)).toBe("Connected — 2 tools: a, b");
  });

  it("reads a failure without throwing", () => {
    const probe = readMcpProbe({ ok: false, error: "mcp_unsafe: writes to ~/.bashrc" });
    expect(probe.ok).toBe(false);
    expect(isUnsafeError(probe.error)).toBe(true);
    expect(isUnsafeError("ENOENT: no such file")).toBe(false);
  });
});
