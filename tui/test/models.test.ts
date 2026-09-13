/**
 * The list `/model` offers: what goes in it, and in what order.
 */

import { describe, expect, it } from "vitest";

import { modelOptions, modelSource } from "../src/state/models.js";

const profiles = {
  fast: { provider: "anthropic", model: "claude-haiku-4-5" },
  deep: { provider: "anthropic", model: "claude-sonnet-4-5" },
};

describe("modelOptions", () => {
  it("lists profiles first, with their provider and model", () => {
    const options = modelOptions({ profiles });
    expect(options.map((option) => option.ref)).toEqual(["fast", "deep"]);
    expect(options[0].detail).toBe("anthropic/claude-haiku-4-5");
    expect(options[0].origin).toBe("profile");
  });

  it("marks the default profile and who uses one", () => {
    const options = modelOptions({
      profiles,
      defaultProfile: "deep",
      agentModels: { reviewer: "fast", writer: "fast" },
    });
    expect(options[0].detail).toContain("used by reviewer, writer");
    expect(options[1].detail).toContain("default");
  });

  it("marks the profile whose model the session is on", () => {
    const options = modelOptions({ profiles, current: "claude-sonnet-4-5" });
    expect(options.find((option) => option.ref === "deep")?.current).toBe(true);
    expect(options.find((option) => option.ref === "fast")?.current).toBe(false);
  });

  it("adds discovered models a profile does not already cover", () => {
    const options = modelOptions({
      profiles,
      discovered: ["claude-sonnet-4-5", "gpt-4o-mini"],
      vendor: "openai",
    });
    expect(options.map((option) => option.ref)).toEqual(["fast", "deep", "gpt-4o-mini"]);
    expect(options[2]).toMatchObject({ origin: "discovered", detail: "openai" });
  });

  it("offers the model in use even when nothing lists it", () => {
    const options = modelOptions({ profiles, current: "local-model", vendor: "local" });
    expect(options[0]).toMatchObject({ ref: "local-model", origin: "current", current: true });
    expect(options[0].detail).toContain("in use");
  });

  it("copes with a daemon that answered nothing at all", () => {
    expect(modelOptions({})).toEqual([]);
    expect(modelOptions({ profiles: null, discovered: null })).toEqual([]);
  });

  it("never repeats a model id", () => {
    const options = modelOptions({
      profiles,
      discovered: ["claude-haiku-4-5", "claude-haiku-4-5"],
      current: "claude-haiku-4-5",
    });
    expect(options.map((option) => option.ref)).toEqual(["fast", "deep"]);
  });
});

describe("modelSource", () => {
  it("reads a tag the daemon supplies, in either spelling", () => {
    expect(modelSource({ modelSource: "pin" })).toBe("pin");
    expect(modelSource({ model_source: "project" })).toBe("project");
  });

  it("answers null rather than guessing", () => {
    expect(modelSource({})).toBeNull();
    expect(modelSource(undefined)).toBeNull();
    expect(modelSource({ modelSource: "" })).toBeNull();
  });
});
