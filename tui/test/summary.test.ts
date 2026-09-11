/**
 * How a run of tool calls is folded into one line of scrollback.
 */

import { describe, expect, it } from "vitest";

import { groupCalls, hiddenLines, summarizeCalls, toolKind } from "../src/layout/summary.js";
import type { ToolCallEntry } from "../src/state/store.js";

function call(
  callId: string,
  name: string,
  args: Record<string, unknown> = {},
  state: ToolCallEntry["state"] = "ok",
  output?: string,
): ToolCallEntry {
  return { callId, name, args, state, output };
}

describe("toolKind", () => {
  it("sorts tools into families by name", () => {
    expect(toolKind("bash")).toBe("shell");
    expect(toolKind("run_terminal_cmd")).toBe("shell");
    expect(toolKind("read_file")).toBe("read");
    expect(toolKind("Grep")).toBe("search");
    expect(toolKind("edit_file")).toBe("edit");
    expect(toolKind("write")).toBe("write");
    expect(toolKind("web_fetch")).toBe("fetch");
    expect(toolKind("summon_pixies")).toBe("other");
  });
});

describe("summarizeCalls", () => {
  it("names the subject when there is only one call", () => {
    expect(summarizeCalls([call("c1", "read_file", { path: "/src/app.py" })])).toBe("Read app.py");
    expect(summarizeCalls([call("c1", "edit", { file_path: "/repo/README.md" })])).toBe(
      "Edited README.md",
    );
    expect(summarizeCalls([call("c1", "write", { path: "notes.txt" })])).toBe("Wrote notes.txt");
    expect(summarizeCalls([call("c1", "bash", { command: "npm test" })])).toBe(
      "Ran shell: npm test",
    );
    expect(summarizeCalls([call("c1", "grep", { pattern: "TODO" })])).toBe('Searched "TODO"');
  });

  it("falls back to the family when a lone call names nothing", () => {
    expect(summarizeCalls([call("c1", "read_file")])).toBe("Read a file");
    expect(summarizeCalls([call("c1", "summon_pixies")])).toBe("Ran summon_pixies");
  });

  it("counts a run of one family", () => {
    expect(
      summarizeCalls([
        call("c1", "bash", { command: "ls" }),
        call("c2", "bash", { command: "pwd" }),
      ]),
    ).toBe("Ran 2 shell commands");
    expect(
      summarizeCalls([
        call("c1", "read_file", { path: "a.ts" }),
        call("c2", "read_file", { path: "b.ts" }),
        call("c3", "read_file", { path: "c.ts" }),
      ]),
    ).toBe("Read 3 files");
    expect(
      summarizeCalls([
        call("c1", "edit", { path: "a.ts" }),
        call("c2", "edit", { path: "b.ts" }),
      ]),
    ).toBe("Edited 2 files");
  });

  it("counts tools when a run mixes families", () => {
    expect(
      summarizeCalls([
        call("c1", "bash", { command: "ls" }),
        call("c2", "read_file", { path: "a.ts" }),
      ]),
    ).toBe("Ran 2 tools");
  });

  it("says nothing about an empty run", () => {
    expect(summarizeCalls([])).toBe("");
  });
});

describe("hiddenLines", () => {
  it("adds up the output the summary is standing in for", () => {
    expect(
      hiddenLines([call("c1", "bash", {}, "ok", "a\nb\nc"), call("c2", "bash", {}, "ok", "d")]),
    ).toBe(4);
    expect(hiddenLines([call("c1", "bash")])).toBe(0);
  });
});

describe("groupCalls", () => {
  it("folds a run of successes into one block", () => {
    const blocks = groupCalls([call("c1", "bash"), call("c2", "bash")]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ kind: "tools" });
  });

  it("keeps a failure on its own and starts a new run after it", () => {
    const blocks = groupCalls([
      call("c1", "bash"),
      call("c2", "bash", {}, "error"),
      call("c3", "bash"),
      call("c4", "bash"),
    ]);
    expect(blocks.map((block) => block.kind)).toEqual(["tools", "single", "tools"]);
    expect(blocks[0]).toMatchObject({ calls: [{ callId: "c1" }] });
    expect(blocks[1]).toMatchObject({ call: { callId: "c2" } });
    expect(blocks[2]).toMatchObject({ calls: [{ callId: "c3" }, { callId: "c4" }] });
  });

  it("has nothing to group in an empty run", () => {
    expect(groupCalls([])).toEqual([]);
  });
});
