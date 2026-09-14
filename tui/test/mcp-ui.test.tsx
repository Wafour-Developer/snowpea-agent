/**
 * `/mcp` in front of a person: the sub-action palette, the form behind a bare
 * `/mcp add`, the checklist behind `/mcp configure`, and the `mcp 2/3` segment
 * the HUD keeps up to date from `mcp.changed`.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const COMMANDS = [
  { name: "help", summary: "list commands", source: "builtin" },
  { name: "mcp", summary: "Manage MCP servers.", source: "builtin" },
];

const NOTES = {
  name: "notes",
  scope: "project",
  transport: "stdio",
  state: "ready",
  command: "python",
  args: ["-m", "notes"],
  toolCount: 2,
  tools: [
    { name: "search", description: "find a note" },
    { name: "write", description: "add a note" },
  ],
};

const REMOTE = {
  name: "remote",
  scope: "global",
  transport: "http",
  state: "stopped",
  url: "https://example.internal/mcp",
};

function fakeClient(overrides: Record<string, any> = {}, initial: unknown[] = []) {
  let listeners: any;
  let servers = initial;
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    of: (method: string) => calls.filter((call) => call.method === method),
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.9", latest: "0.1.9" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "command.list") return { commands: COMMANDS };
      if (method === "mcp.list") return { servers };
      if (method in overrides) {
        const value = overrides[method];
        return typeof value === "function" ? value(params) : value;
      }
      return {};
    },
    prompt: async () => ({ turnId: "t-1" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    /** Notify, the way the daemon does — and keep the list it would re-read. */
    emitMcp(payload: Record<string, unknown>) {
      servers = servers.map((row: any) =>
        row.name === payload.name ? { ...row, ...payload } : row,
      );
      listeners?.onMcpChanged?.(payload as any);
    },
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

async function open(client: ReturnType<typeof fakeClient>) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(120, 32);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(300);
  return { stdin, stdout, instance, frame: () => stdout.chunks[stdout.chunks.length - 1] ?? "" };
}

describe("the /mcp palette", () => {
  it("offers the sub-actions once /mcp is typed", async () => {
    const { stdin, instance, frame } = await open(fakeClient());
    await type(stdin, "/mcp ", 15);
    await sleep(200);
    const shown = frame();
    instance.unmount();

    expect(shown).toContain("/mcp add");
    expect(shown).toContain("/mcp configure");
    expect(shown).toContain("/mcp catalog");
  });

  it("completes the names of the configured servers", async () => {
    const { stdin, instance, frame } = await open(fakeClient({}, [NOTES, REMOTE]));
    await type(stdin, "/mcp test ", 15);
    await sleep(200);
    const shown = frame();
    instance.unmount();

    expect(shown).toContain("/mcp test notes");
    expect(shown).toContain("/mcp test remote");
  });
});

describe("the /mcp add form", () => {
  it("walks a stdio server to a probe, then writes it", async () => {
    const client = fakeClient({
      "mcp.test": { ok: true, state: "ready", tools: [{ name: "search" }, { name: "write" }] },
      "mcp.add": { ok: true, path: "/work/.mcp.json" },
    });
    const { stdin, instance, frame } = await open(client);

    await type(stdin, "/mcp add ", 12);
    stdin.write("\r");
    await sleep(250);
    expect(frame()).toContain("New MCP server");

    await type(stdin, "notes", 12);
    stdin.write("\r");
    await sleep(200);
    expect(frame()).toContain("Command");

    stdin.write("\r"); // Command, the first transport
    await sleep(200);
    await type(stdin, "python -m notes", 8);
    stdin.write("\r");
    await sleep(200);

    await type(stdin, "TOKEN=secret", 8);
    stdin.write("\r"); // one env line
    await sleep(150);
    const masked = frame();
    stdin.write("\r"); // an empty line ends the list
    await sleep(200);
    expect(frame()).toContain("this project");

    stdin.write("\r"); // project scope, then the probe runs
    await sleep(400);
    const connected = frame();
    expect(connected).toContain("Connected — 2 tools");

    stdin.write("\r"); // Enable all
    await sleep(300);
    const after = frame();
    instance.unmount();

    // The secret is confirmed by its name only.
    expect(masked).toContain("TOKEN=•••");
    expect(masked).not.toContain("TOKEN=secret");

    const [test] = client.of("mcp.test");
    expect(test.params.command).toBe("python");
    expect(test.params.args).toEqual(["-m", "notes"]);
    expect(test.params.env).toEqual({ TOKEN: "secret" });

    const [add] = client.of("mcp.add");
    expect(add.params.name).toBe("notes");
    expect(add.params.scope).toBe("project");
    // The probe already ran; the daemon must not start it a second time.
    expect(add.params.test).toBe(false);
    expect(add.params.toolsInclude).toBeUndefined();
    expect(after).not.toContain("New MCP server");
  });

  it("keeps only the ticked tools when a subset is chosen", async () => {
    const client = fakeClient({
      "mcp.test": { ok: true, state: "ready", tools: [{ name: "search" }, { name: "write" }] },
      "mcp.add": { ok: true, path: "/work/.mcp.json" },
    });
    const { stdin, instance } = await open(client);

    await type(stdin, "/mcp add ", 12);
    stdin.write("\r");
    await sleep(250);
    await type(stdin, "notes", 10);
    stdin.write("\r");
    await sleep(150);
    stdin.write("\r"); // Command
    await sleep(150);
    await type(stdin, "python -m notes", 6);
    stdin.write("\r");
    await sleep(150);
    stdin.write("\r"); // no env
    await sleep(150);
    stdin.write("\r"); // project
    await sleep(400);

    stdin.write("[B"); // ↓ to "Select tools"
    await sleep(120);
    stdin.write("\r");
    await sleep(200);
    stdin.write(" "); // untick "search", the row under the cursor
    await sleep(120);
    stdin.write("\r");
    await sleep(250);
    instance.unmount();

    const [add] = client.of("mcp.add");
    expect(add.params.toolsInclude).toEqual(["write"]);
  });

  it("shows a failed probe and writes nothing when it is cancelled", async () => {
    const client = fakeClient({
      "mcp.test": { ok: false, state: "error", error: "ENOENT: nope" },
    });
    const { stdin, instance, frame } = await open(client);

    await type(stdin, "/mcp add ", 12);
    stdin.write("\r");
    await sleep(250);
    await type(stdin, "broken", 10);
    stdin.write("\r");
    await sleep(150);
    stdin.write("\r"); // Command
    await sleep(150);
    await type(stdin, "nope --serve", 6);
    stdin.write("\r");
    await sleep(150);
    stdin.write("\r"); // no env
    await sleep(150);
    stdin.write("\r"); // project
    await sleep(400);
    const failed = frame();

    expect(failed).toContain("ENOENT: nope");
    expect(failed).toContain("Retry");
    expect(failed).toContain("Save anyway");

    stdin.write(""); // Esc
    await sleep(250);
    const after = frame();
    instance.unmount();

    expect(client.of("mcp.add")).toHaveLength(0);
    expect(after).not.toContain("New MCP server");
  });
});

describe("/mcp configure", () => {
  it("saves the ticked tools as the include list", async () => {
    const client = fakeClient({ "mcp.update": { ok: true } }, [NOTES]);
    const { stdin, instance, frame } = await open(client);

    await type(stdin, "/mcp configure notes", 8);
    stdin.write("\r"); // the first Enter accepts the name completion
    await sleep(200);
    stdin.write("\r");
    await sleep(350);
    const shown = frame();
    expect(shown).toContain("tools to register");
    expect(shown).toContain("search");
    expect(shown).toContain("write");

    stdin.write(" "); // untick the first row
    await sleep(150);
    stdin.write("\r");
    await sleep(300);
    instance.unmount();

    const [update] = client.of("mcp.update");
    expect(update.params.name).toBe("notes");
    expect(update.params.scope).toBe("project");
    expect(update.params.patch).toEqual({ toolsInclude: ["write"] });
  });
});

describe("the mcp HUD segment", () => {
  it("counts the ready servers once the daemon answers", async () => {
    const { stdout, instance } = await open(fakeClient({}, [NOTES, REMOTE]));
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("mcp 1/2");
  });

  it("stays out of the way when nothing is configured", async () => {
    const { stdout, instance } = await open(fakeClient());
    const output = stdout.text();
    instance.unmount();
    expect(output).not.toContain("mcp 0/0");
  });

  it("follows mcp.changed, and says once that a server broke", async () => {
    const client = fakeClient({}, [NOTES, REMOTE]);
    const { stdout, instance, frame } = await open(client);

    client.emitMcp({ name: "remote", scope: "global", state: "ready", toolCount: 3 });
    await sleep(250);
    expect(frame()).toContain("mcp 2/2");

    client.emitMcp({ name: "remote", scope: "global", state: "error", error: "refused" });
    await sleep(200);
    client.emitMcp({ name: "remote", scope: "global", state: "error", error: "refused" });
    await sleep(250);
    const output = stdout.text();
    instance.unmount();

    const said = output.split("mcp: remote is not running").length - 1;
    expect(said).toBeGreaterThan(0);
    expect(output).toContain("refused");
  });
});
