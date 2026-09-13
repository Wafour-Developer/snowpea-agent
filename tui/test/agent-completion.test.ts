/**
 * Completing an agent's name: where the list opens, what it offers, and what
 * accepting one does to the draft.
 */

import { describe, expect, it } from "vitest";

import {
  agentCandidates,
  agentQuery,
  applyAgentCompletion,
  candidateTag,
  filterAgents,
  type AgentCandidate,
} from "../src/state/agent-completion.js";
import type { KnownAgent } from "../src/layout/agents.js";
import type { TeamTaskEntry } from "../src/state/store.js";

const known: KnownAgent[] = [
  { name: "executor", kind: "definition", description: "Implements one change" },
  { name: "explorer", kind: "definition", description: "Finds things in the codebase" },
  { name: "reviewer", kind: "named", description: "Reviews a diff" },
];

const team: TeamTaskEntry[] = [
  { taskId: "1", teamId: "team-2", status: "claimed", assignee: "worker-1" },
  { taskId: "2", teamId: "team-2", status: "queued", assignee: null },
];

describe("agentCandidates", () => {
  it("offers the definitions and the named agents", () => {
    const candidates = agentCandidates({ known });
    expect(candidates.map((candidate) => candidate.name)).toEqual([
      "executor",
      "explorer",
      "reviewer",
    ]);
    expect(candidates[2].kind).toBe("named");
    expect(candidates[0].description).toBe("Implements one change");
  });

  it("adds whoever is on a team, and skips an unclaimed task", () => {
    const candidates = agentCandidates({ known, teamTasks: team });
    const worker = candidates.find((candidate) => candidate.name === "worker-1");
    expect(worker).toMatchObject({ kind: "team", description: "working on team-2" });
    expect(candidates).toHaveLength(4);
  });

  it("carries the model each agent is assigned", () => {
    const candidates = agentCandidates({ known, models: { executor: "fast" } });
    expect(candidates.find((c) => c.name === "executor")?.model).toBe("fast");
    expect(candidates.find((c) => c.name === "explorer")?.model).toBeNull();
  });

  it("names an agent once, however many places know it", () => {
    const candidates = agentCandidates({
      known: [...known, { name: "executor", kind: "subagent", description: "running now" }],
      teamTasks: [{ taskId: "3", teamId: "t", status: "claimed", assignee: "executor" }],
    });
    expect(candidates.filter((candidate) => candidate.name === "executor")).toHaveLength(1);
  });

  it("has nothing to offer when the daemon knows nothing", () => {
    expect(agentCandidates({})).toEqual([]);
  });
});

describe("agentQuery", () => {
  it("opens on the bare sigil, before a name is typed", () => {
    expect(agentQuery("$")).toEqual({ prefix: "", start: 1, end: 1, form: "dollar" });
  });

  it("follows the name as it is typed", () => {
    expect(agentQuery("$exec")).toMatchObject({ prefix: "exec", start: 1, end: 5 });
  });

  it("closes once the name is finished and the task begins", () => {
    expect(agentQuery("$executor ")).toBeNull();
    expect(agentQuery("$executor do the thing")).toBeNull();
  });

  it("opens on the commands whose first argument is an agent", () => {
    expect(agentQuery("/delegate ")).toMatchObject({ prefix: "", form: "command", start: 10 });
    expect(agentQuery("/delegate rev")).toMatchObject({ prefix: "rev", form: "command" });
    expect(agentQuery("/agent spawn exec")).toMatchObject({ prefix: "exec", form: "command" });
  });

  it("closes once the command's task text begins", () => {
    expect(agentQuery("/delegate executor write the test")).toBeNull();
  });

  it("stays out of ordinary text", () => {
    expect(agentQuery("what does $PATH mean")).toBeNull();
    expect(agentQuery("/help")).toBeNull();
    expect(agentQuery("")).toBeNull();
  });

  it("reads the name the cursor is in, not the whole line", () => {
    // Cursor just after "$ex" in "$executor", which is what typing looks like.
    expect(agentQuery("$executor", 3)).toMatchObject({ prefix: "ex", end: 3 });
  });
});

describe("filterAgents", () => {
  const candidates = agentCandidates({ known });

  it("offers everything for an empty prefix", () => {
    expect(filterAgents(candidates, "")).toHaveLength(3);
  });

  it("filters by what has been typed, ignoring case", () => {
    expect(filterAgents(candidates, "ex").map((c) => c.name)).toEqual(["executor", "explorer"]);
    expect(filterAgents(candidates, "EXE").map((c) => c.name)).toEqual(["executor"]);
  });

  it("offers nothing for a name no agent has", () => {
    expect(filterAgents(candidates, "zz")).toEqual([]);
  });
});

describe("applyAgentCompletion", () => {
  it("completes the sigil form, leaving the cursor after the space", () => {
    const query = agentQuery("$exe")!;
    expect(applyAgentCompletion("$exe", query, "executor")).toEqual({
      text: "$executor ",
      cursor: 10,
    });
  });

  it("completes a command argument without touching the command", () => {
    const query = agentQuery("/delegate rev")!;
    expect(applyAgentCompletion("/delegate rev", query, "reviewer")).toEqual({
      text: "/delegate reviewer ",
      cursor: 19,
    });
  });

  it("keeps whatever followed the name", () => {
    const draft = "$exe do the thing";
    const query = agentQuery(draft, 4)!;
    expect(applyAgentCompletion(draft, query, "executor").text).toBe("$executor  do the thing");
  });
});

describe("candidateTag", () => {
  const candidate = (over: Partial<AgentCandidate>): AgentCandidate => ({
    name: "a",
    description: "",
    model: null,
    kind: "definition",
    ...over,
  });

  it("prefers the assigned model, then says what kind it is", () => {
    expect(candidateTag(candidate({ model: "fast" }))).toBe("fast");
    expect(candidateTag(candidate({ kind: "team" }))).toBe("team");
    expect(candidateTag(candidate({ kind: "definition" }))).toBe("");
  });
});
