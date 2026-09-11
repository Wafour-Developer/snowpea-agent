import { describe, expect, it, vi } from "vitest";

import { SlashRegistry, load, parse } from "../src/slash/registry.js";

const COMMANDS = [
  { name: "help", summary: "list commands", source: "builtin" },
  { name: "plan", summary: "switch to plan mode", source: "builtin" },
  { name: "mode", summary: "show or set the mode", source: "builtin" },
  { name: "ralph", summary: "loop until done", source: "skill" },
];

function mockClient(commands = COMMANDS) {
  const call = vi.fn(async (method: string) => {
    if (method === "command.list") return { commands };
    if (method === "command.run") return { turnId: "turn-1" };
    throw new Error(`unexpected method ${method}`);
  });
  return { call };
}

describe("parse", () => {
  it("returns null for non-slash input", () => {
    expect(parse("hello world")).toBeNull();
  });

  it("splits only the leading /name and keeps args raw", () => {
    expect(parse("/mode plan --save")).toEqual({ name: "mode", args: "plan --save" });
  });

  it("yields empty args for a bare command", () => {
    expect(parse("/help")).toEqual({ name: "help", args: "" });
  });
});

describe("SlashRegistry", () => {
  it("fetches the command table from command.list", async () => {
    const client = mockClient();
    const registry = new SlashRegistry(client, "sess-1");
    const commands = await registry.load();

    expect(client.call).toHaveBeenCalledWith("command.list", { sessionId: "sess-1" });
    expect(commands.map((c) => c.name)).toEqual(["help", "plan", "mode", "ralph"]);
    expect(registry.list()).toHaveLength(4);
  });

  it("has no built-in table before load", () => {
    const registry = new SlashRegistry(mockClient(), null);
    expect(registry.list()).toEqual([]);
    expect(registry.complete("/h")).toEqual([]);
  });

  it("completes from the fetched table", async () => {
    const registry = await load(mockClient(), "sess-1");
    expect(registry.complete("/m").map((c) => c.name)).toEqual(["mode"]);
    expect(registry.complete("p").map((c) => c.name)).toEqual(["plan"]);
    expect(registry.complete("/").map((c) => c.name)).toHaveLength(4);
    expect(registry.complete("/zz")).toEqual([]);
  });

  it("dispatch calls command.run with the parsed name and raw args", async () => {
    const client = mockClient();
    const registry = new SlashRegistry(client, "sess-1");
    const result = await registry.dispatch("/mode plan --save");

    expect(client.call).toHaveBeenCalledWith("command.run", {
      sessionId: "sess-1",
      name: "mode",
      args: "plan --save",
    });
    expect(result).toEqual({ turnId: "turn-1" });
  });

  it("dispatch returns null for plain prompts so the caller uses session.prompt", async () => {
    const client = mockClient();
    const registry = new SlashRegistry(client, "sess-1");
    expect(await registry.dispatch("write a haiku")).toBeNull();
    expect(client.call).not.toHaveBeenCalled();
  });

  it("refresh re-reads command.list after a skill reload", async () => {
    const client = mockClient();
    const registry = new SlashRegistry(client, "sess-1");
    await registry.load();
    client.call.mockResolvedValueOnce({
      commands: [...COMMANDS, { name: "newskill", summary: "added", source: "skill" }],
    });
    const commands = await registry.refresh();

    expect(client.call).toHaveBeenCalledTimes(2);
    expect(commands.map((c) => c.name)).toContain("newskill");
  });
});
