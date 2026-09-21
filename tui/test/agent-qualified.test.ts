import { describe, expect, it } from "vitest";

import { filterAgents } from "../src/state/agent-completion.js";
import { delegationHint } from "../src/state/delegation.js";

describe("a plugin's agent is spelled <plugin>:<name>", () => {
  it("is recognised after the dollar sign", () => {
    const hint = delegationHint("$oh-my-claudecode:architect design the cache", [
      "architect",
      "oh-my-claudecode:architect",
    ]);
    expect(hint).not.toBeNull();
    expect(hint?.known).toBe(true);
    expect(JSON.stringify(hint)).toContain("oh-my-claudecode:architect");
  });

  it("is offered when only its own name is typed, after the built-in", () => {
    const rows = [
      { name: "oh-my-claudecode:architect" },
      { name: "architect" },
      { name: "critic" },
    ] as never;
    expect(filterAgents(rows, "arch").map((row: { name: string }) => row.name)).toEqual([
      "architect",
      "oh-my-claudecode:architect",
    ]);
  });
});
