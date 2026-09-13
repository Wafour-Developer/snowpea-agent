/**
 * Recognising `$agent …` while it is being typed.
 */

import { describe, expect, it } from "vitest";

import { delegationHint, delegationLabel } from "../src/state/delegation.js";

const agents = ["reviewer", "exec-core-login"];

describe("delegationHint", () => {
  it("reads the agent and the rest of the prompt", () => {
    expect(delegationHint("$reviewer look at this diff", agents)).toEqual({
      name: "reviewer",
      known: true,
      rest: "look at this diff",
    });
  });

  it("waits for the space, so the name does not flicker while typed", () => {
    expect(delegationHint("$rev", agents)).toBeNull();
    expect(delegationHint("$reviewer", agents)).toBeNull();
    expect(delegationHint("$reviewer ", agents)).toMatchObject({ name: "reviewer", rest: "" });
  });

  it("marks a name no agent answers to", () => {
    const hint = delegationHint("$nobody do it", agents);
    expect(hint).toMatchObject({ name: "nobody", known: false });
    expect(delegationLabel(hint!)).toBe("delegate to nobody (no such agent)");
  });

  it("takes the hyphens and dots a name can have", () => {
    expect(delegationHint("$exec-core-login fix the flow", agents)?.known).toBe(true);
  });

  it("is not confused by an ordinary prompt", () => {
    expect(delegationHint("what does $PATH do?", agents)).toBeNull();
    expect(delegationHint("", agents)).toBeNull();
    expect(delegationHint("$ ", agents)).toBeNull();
  });

  it("names the agent plainly when it is known", () => {
    expect(delegationLabel({ name: "reviewer", known: true, rest: "x" })).toBe(
      "delegate to reviewer",
    );
  });
});
