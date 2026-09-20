/**
 * The list `/model` offers: what goes in it, and in what order.
 */

import { describe, expect, it } from "vitest";

import {
  EFFORT_REF,
  INHERIT_REF,
  modelOptions,
  modelSource,
  nextEffort,
} from "../src/state/models.js";

/** The picker always ends with the row that clears the pin. */
const withoutInherit = (options: { ref: string }[]) =>
  options.filter((option) => option.ref !== INHERIT_REF && option.ref !== EFFORT_REF);

const profiles = {
  fast: { provider: "anthropic", model: "claude-haiku-4-5" },
  deep: { provider: "anthropic", model: "claude-sonnet-4-5" },
};

describe("modelOptions", () => {
  it("lists profiles first, with their provider and model", () => {
    const options = withoutInherit(modelOptions({ profiles }));
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
    expect(withoutInherit(options).map((option) => option.ref)).toEqual([
      "fast",
      "deep",
      "openai:claude-sonnet-4-5",
      "openai:gpt-4o-mini",
    ]);
    expect(options[2]).toMatchObject({ origin: "discovered", detail: "openai" });
  });

  it("lists models from every configured vendor with routable refs", () => {
    const options = withoutInherit(modelOptions({
      discoveries: [
        { vendor: "openai", models: ["gpt-5"], source: "live" },
        { vendor: "gemini", models: ["gemini-2.5-pro"], source: "curated" },
      ],
      vendor: "openai",
      current: "gpt-5",
    }));
    expect(options.map((option) => option.ref)).toEqual([
      "openai:gpt-5",
      "gemini:gemini-2.5-pro",
    ]);
    expect(options[0].current).toBe(true);
    expect(options[1].detail).toContain("curated list");
  });

  it("offers the model in use even when nothing lists it", () => {
    const options = modelOptions({ profiles, current: "local-model", vendor: "local" });
    expect(options[0]).toMatchObject({ ref: "local-model", origin: "current", current: true });
    expect(options[0].detail).toContain("in use");
  });

  it("always offers the row that clears the pin", () => {
    const options = modelOptions({ profiles });
    const inherit = options[options.length - 1];
    expect(inherit).toMatchObject({ ref: INHERIT_REF, origin: "inherit", current: false });
    expect(inherit.label).toContain("clear pin");
  });

  it("offers an effort row only when the daemon has said what the effort is", () => {
    expect(modelOptions({ profiles }).some((o) => o.ref === EFFORT_REF)).toBe(false);
    const options = modelOptions({ profiles, effort: "high", effortSource: "session" });
    const row = options.find((option) => option.ref === EFFORT_REF);
    expect(row).toMatchObject({ origin: "effort", current: false });
    expect(row?.label).toBe("effort: high");
    // The row says both why it is that tier and what pressing Enter will do.
    expect(row?.detail).toContain("set by session");
    expect(row?.detail).toContain("max");
    // It sits just above the row that clears the model pin.
    expect(options[options.length - 1].ref).toBe(INHERIT_REF);
    expect(options[options.length - 2].ref).toBe(EFFORT_REF);
  });

  it("tags a project profile, and lets it shadow the global one", () => {
    const options = modelOptions({
      profiles: { deep: { provider: "anthropic", model: "claude-sonnet-4-5" } },
      projectProfiles: { deep: { provider: "openai", model: "gpt-5" } },
    });
    const deep = options.find((option) => option.ref === "deep");
    expect(deep?.detail).toContain("[project]");
    expect(deep?.detail).toContain("openai/gpt-5");
    expect(options.filter((option) => option.ref === "deep")).toHaveLength(1);
  });

  it("offers only the clear-pin row when the daemon answered nothing", () => {
    expect(withoutInherit(modelOptions({}))).toEqual([]);
    expect(withoutInherit(modelOptions({ profiles: null, discovered: null }))).toEqual([]);
  });

  it("never repeats a model id", () => {
    const options = modelOptions({
      profiles,
      discovered: ["claude-haiku-4-5", "claude-haiku-4-5"],
      current: "claude-haiku-4-5",
    });
    expect(withoutInherit(options).map((option) => option.ref)).toEqual(["fast", "deep"]);
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

describe("nextEffort", () => {
  it("steps up a tier and wraps at the top", () => {
    expect(nextEffort("low")).toBe("medium");
    expect(nextEffort("medium")).toBe("high");
    expect(nextEffort("high")).toBe("max");
    expect(nextEffort("max")).toBe("low");
  });

  it("starts at low for anything it does not recognise", () => {
    expect(nextEffort(null)).toBe("low");
    expect(nextEffort("hard")).toBe("low");
  });
});
