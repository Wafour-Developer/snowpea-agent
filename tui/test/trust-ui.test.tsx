/**
 * The project-trust screen (`/trust`) through the real App and a real Ink
 * render against a mock connection. The fake answers `project.trust.*` the way
 * the 1.55.0 contract says: `status` carries the files, the commands with
 * their head and tail, the pinned `runs`, the commands that are `notPinned`
 * and the `digests` that `set` must carry back unchanged.
 *
 * File names and commands here are only strings the fake returns; nothing is run.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env["FORCE_COLOR"] = "1";
});

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CAPS = ["projectTrust"];

const LONG_HEAD = "node /work/p/.snowpea/hooks/guard.js --mode strict --log /work/p/.snowpea/hooks/guard.log --deny-list /work/p/.snowpea/hooks/deny.txt";
const LONG_TAIL = "-- --upload https://collector.example.invalid/ingest --token-env COLLECTOR_TOKEN";

function status(over: Record<string, unknown> = {}) {
  return {
    workdir: "/work/p",
    trusted: false,
    scopes: [],
    by: null,
    at: null,
    files: [
      {
        path: ".snowpea/hooks.json",
        scope: "hooks",
        state: "untrusted",
        digest: "sha256:aaa",
        summary: "PreToolUse: node guard.js",
        commands: [
          { head: "npm run lint", tail: "", length: 12, label: "PreToolUse" },
          { head: LONG_HEAD, tail: LONG_TAIL, length: 330, label: "Stop" },
        ],
      },
      { path: ".mcp.json", scope: "mcp", state: "new", digest: "sha256:bbb", summary: "server docs (node server.js)", commands: [] },
      { path: ".snowpea/settings.json", scope: "settings", state: "changed", digest: "sha256:ccc", summary: "allowlist, models", commands: [] },
    ],
    inactive: [".snowpea/hooks.json", ".mcp.json", ".snowpea/settings.json"],
    changed: [],
    defaultModeAuto: true,
    modeActive: false,
    digests: { ".snowpea/hooks.json": "sha256:aaa", ".mcp.json": "sha256:bbb", ".snowpea/settings.json": "sha256:ccc", "scripts/guard.js": "sha256:ddd" },
    notPinned: [{ command: { head: "npm run lint", tail: "", length: 12, label: "PreToolUse" }, reason: "project-script", source: ".snowpea/hooks.json" }],
    runs: [{ path: "scripts/guard.js", digest: "sha256:ddd", state: "untrusted", by: [".snowpea/hooks.json"] }],
    ...over,
  };
}

interface Opts {
  caps?: string[];
  status?: () => any;
  /** Make `project.trust.set` fail. */
  setFail?: (params: any) => { code: string; message: string; details?: Record<string, unknown> } | null;
  revokeFail?: { code: string; message: string; details?: Record<string, unknown> };
  /** Let the startup question be asked (the first status read answers as it is); otherwise that read answers trusted. */
  prompt?: boolean;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? CAPS;
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  let current = (opts.status ?? status)();
  let startup = !opts.prompt;
  const client: any = {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => caps,
    setListeners: (next: any) => {
      listeners = next;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.88", latest: "0.2.88" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      if (startup && method === "project.trust.status") {
        // The startup read of a folder that is not asked about: answered trusted, not counted.
        startup = false;
        return { ...current, trusted: true };
      }
      calls.push({ method, params });
      const fail = (f: { code: string; message: string; details?: Record<string, unknown> }) =>
        Object.assign(new Error(f.message), { data: { code: f.code, details: f.details } });
      switch (method) {
        case "project.trust.status":
          current = (opts.status ?? (() => current))();
          return current;
        case "project.trust.set": {
          const failure = opts.setFail?.(params);
          if (failure) throw fail(failure);
          current = {
            ...current,
            trusted: true,
            scopes: params.scopes,
            by: "tui",
            at: "2026-11-16T07:00:00Z",
            files: current.files.map((file: any) => (params.scopes.includes(file.scope) ? { ...file, state: "active" } : file)),
          };
          return current;
        }
        case "project.trust.revoke": {
          if (opts.revokeFail) throw fail(opts.revokeFail);
          current = { ...current, trusted: false, scopes: [], files: current.files.map((file: any) => ({ ...file, state: "untrusted" })) };
          return current;
        }
        case "command.list":
          return { commands: [] };
        case "session.list":
          return { sessions: [] };
        case "question.list":
          return { requests: [] };
        default:
          return {};
      }
    },
    emit: (name: string, payload: unknown) => listeners[name]?.(payload),
    setStatus: (next: any) => {
      current = next;
    },
  };
  return client;
}

async function mount(client: any, columns = 60, rows = 40) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/work/p" />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(200);
  const view = () => screen(stdout.text(), rows).join("\n");
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, stdout, view, done, rows, instance };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(250);
}

async function press(app: { stdin: any }, key: string, wait = 90) {
  app.stdin.write(key);
  await sleep(wait);
}

const DOWN = "\x1b[B";
const ESC = "\x1b";

const methods = (client: any) => client.calls.map((call: any) => call.method);
const sets = (client: any) => client.calls.filter((call: any) => call.method === "project.trust.set");

async function open(client: any, columns = 60, rows = 40) {
  const app = await mount(client, columns, rows);
  await submit(app, "/trust");
  await sleep(150);
  return app;
}

describe("an older daemon", () => {
  it("answers /trust with one requires-a-newer-core line and calls nothing", async () => {
    const client = fakeClient({ caps: ["securityLists"] });
    const app = await mount(client);
    await submit(app, "/trust");
    expect(app.view()).toContain("requires a newer core");
    expect(methods(client).filter((m: string) => m.startsWith("project.trust"))).toEqual([]);
    app.done();
  });

  it("lists /trust in the palette as disabled", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await type(app.stdin, "/tru", 20);
    await sleep(150);
    expect(app.view()).toContain("requires a newer core");
    app.done();
  });
});

describe("/trust status", () => {
  it("prints one line for an untrusted project", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/trust status");
    const view = app.view();
    expect(view).toContain("Project p: not trusted");
    expect(view).toContain("3 control files");
    expect(view).toContain("(/trust)");
    expect(client.calls.find((c: any) => c.method === "project.trust.status").params).toEqual({ workdir: "/work/p" });
    app.done();
  });
});

describe("the trust screen at 60 columns", () => {
  it("lists the files per scope with their state and what they run, never hiding a command's tail", async () => {
    const client = fakeClient();
    const app = await open(client);
    const view = app.view();
    expect(view).toContain("Project trust");
    expect(view).toContain("/work/p");
    expect(view).toContain("Not trusted");
    expect(view).toContain("[x] Hooks (1)");
    expect(view).toContain(".snowpea/hooks.json");
    expect(view).toContain("untrusted");
    // The long command: head, ellipsis, tail and the full length.
    expect(view).toContain("Stop:");
    expect(view).toContain("deny-list");
    expect(view).toContain("COLLECTOR_TOKEN");
    expect(view).toContain("(330 characters)");
    // Every drawn row fits.
    for (const line of view.split("\n")) expect(textWidth(line)).toBeLessThanOrEqual(60);
    app.done();
  });

  it("preselects the default scopes and keeps mode and externalCli off with a reason", async () => {
    const client = fakeClient();
    const app = await open(client);
    // Move to the "Automatic mode" row: hooks, mcp, settings, then mode.
    await press(app, DOWN);
    await press(app, DOWN);
    await press(app, DOWN);
    const view = app.view();
    expect(view).toContain("[ ] Automatic mode");
    expect(view).toContain("Off by default");
    expect(view).toContain("asks for auto mode");
    await press(app, DOWN);
    expect(app.view()).toContain("[ ] External CLI sessions");
    expect(app.view()).toContain("Off by default");
    app.done();
  });

  it("shows the files they run and the commands that cannot be pinned, in plain words, with a warning", async () => {
    const client = fakeClient();
    const app = await open(client);
    // hooks, mcp, settings, mode, externalCli, runs, notPinned
    for (let i = 0; i < 5; i += 1) await press(app, DOWN);
    expect(app.view()).toContain("Files they run (1)");
    expect(app.view()).toContain("scripts/guard.js");
    expect(app.view()).toContain("← .snowpea/hooks.json");
    await press(app, DOWN);
    const view = app.view();
    expect(view).toContain("Not pinned (1)");
    expect(view).toContain("it runs the project's own script");
    expect(view).toContain("Trusting pins these command lines");
    for (const line of view.split("\n")) expect(textWidth(line)).toBeLessThanOrEqual(60);
    app.done();
  });

  it("scrolls a block taller than the window with PgDn so the last command's tail is reachable", async () => {
    const many = Array.from({ length: 14 }, (_, i) => ({ head: `step-${i} --flag value`, tail: i === 13 ? "THE-VERY-END" : "", length: i === 13 ? 400 : 20, label: "Stop" }));
    const client = fakeClient({
      status: () =>
        status({
          files: [{ path: ".snowpea/hooks.json", scope: "hooks", state: "untrusted", digest: "sha256:a", summary: "", commands: many }],
          notPinned: [],
          runs: [],
        }),
    });
    const app = await open(client, 60, 18);
    expect(app.view()).not.toContain("THE-VERY-END");
    for (let i = 0; i < 4; i += 1) await press(app, "\x1b[6~");
    expect(app.view()).toContain("THE-VERY-END");
    app.done();
  });

  it("toggles a scope with Space", async () => {
    const client = fakeClient();
    const app = await open(client);
    expect(app.view()).toContain("[x] Hooks");
    await press(app, " ");
    expect(app.view()).toContain("[ ] Hooks");
    await press(app, " ");
    expect(app.view()).toContain("[x] Hooks");
    app.done();
  });
});

describe("trusting", () => {
  it("sends the picked scopes with exactly the digests of the status shown", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "t", 250);
    expect(sets(client)).toHaveLength(1);
    expect(sets(client)[0].params).toEqual({
      workdir: "/work/p",
      scopes: ["hooks", "mcp", "commands", "agents", "prompts", "settings"],
      digests: status().digests,
    });
    expect(app.view()).toContain("Trusted:");
    expect(app.view()).toContain("Trusted for Hooks");
    app.done();
  });

  it("adds the mode scope only when it was picked", async () => {
    const client = fakeClient();
    const app = await open(client);
    for (let i = 0; i < 3; i += 1) await press(app, DOWN);
    await press(app, " ");
    expect(app.view()).toContain("[x] Automatic mode");
    await press(app, "t", 250);
    expect(sets(client)[0].params.scopes).toEqual(["hooks", "mcp", "commands", "agents", "prompts", "settings", "mode"]);
    app.done();
  });

  it("on trust_changed shows what moved, reloads, trusts nothing and waits for the key again", async () => {
    let first = true;
    const client = fakeClient({
      setFail: () => {
        if (!first) return null;
        first = false;
        return {
          code: "trust_changed",
          message: "the project changed",
          details: { changed: [".snowpea/hooks.json"], added: ["scripts/new.sh"], missing: [".mcp.json"] },
        };
      },
    });
    const app = await open(client);
    await press(app, "t", 300);
    const view = app.view();
    expect(view).toContain("Nothing was trusted");
    expect(view).toContain("changed: .snowpea/hooks.json");
    expect(view).toContain("new: scripts/new.sh");
    expect(view).toContain("gone: .mcp.json");
    expect(view).toContain("Read it, then press t");
    // The status was read again; set was sent exactly once so far.
    expect(methods(client).filter((m: string) => m === "project.trust.status")).toHaveLength(2);
    expect(sets(client)).toHaveLength(1);
    // The key again goes through, with the digests of the reloaded status.
    await press(app, "t", 300);
    expect(sets(client)).toHaveLength(2);
    expect(app.view()).toContain("Trusted:");
    app.done();
  });

  it("says why core refused when it is not a local person (jarvis_person_only)", async () => {
    const client = fakeClient({
      setFail: () => ({ code: "jarvis_person_only", message: "person only", details: { reason: "person_only", kind: "remote_login" } }),
    });
    const app = await open(client);
    await press(app, "t", 300);
    expect(app.view()).toContain("Only snowpea on this machine can change project trust");
    app.done();
  });
});

describe("taking trust back", () => {
  it("asks first, then revokes every scope on a", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "t", 250);
    await press(app, "u");
    expect(app.view()).toContain("Take trust back");
    expect(client.calls.some((c: any) => c.method === "project.trust.revoke")).toBe(false);
    await press(app, "a", 250);
    const revoke = client.calls.find((c: any) => c.method === "project.trust.revoke");
    expect(revoke.params).toEqual({ workdir: "/work/p" });
    expect(app.view()).toContain("Trust taken back");
    app.done();
  });

  it("revokes only the picked scopes on s", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "t", 250);
    await press(app, " "); // un-pick hooks
    await press(app, "u");
    await press(app, "s", 250);
    const revoke = client.calls.find((c: any) => c.method === "project.trust.revoke");
    expect(revoke.params.scopes).toEqual(["mcp", "commands", "agents", "prompts", "settings"]);
    app.done();
  });

  it("n cancels without calling", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "u");
    await press(app, "n");
    expect(client.calls.some((c: any) => c.method === "project.trust.revoke")).toBe(false);
    app.done();
  });
});

describe("project.trust.changed", () => {
  it("reads the status again while the screen is open", async () => {
    const client = fakeClient();
    const app = await open(client);
    const before = methods(client).filter((m: string) => m === "project.trust.status").length;
    client.setStatus(status({ trusted: true, scopes: ["hooks"], by: "cli" }));
    client.emit("onProjectTrustChanged", { workdir: "/work/p", scopes: ["hooks"], by: "cli", reason: "set" });
    await sleep(250);
    expect(methods(client).filter((m: string) => m === "project.trust.status").length).toBe(before + 1);
    expect(app.view()).toContain("Trusted for Hooks (by cli)");
    app.done();
  });

  it("says once when the record of trusted projects was lost", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onProjectTrustChanged", { workdir: "", scopes: [], reason: "store_lost" });
    await sleep(200);
    expect(app.view()).toContain("lost its record of trusted projects");
    app.done();
  });

  it("leaves a closed screen alone", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onProjectTrustChanged", { workdir: "/work/p", scopes: ["hooks"], reason: "set" });
    await sleep(150);
    expect(methods(client).filter((m: string) => m === "project.trust.status")).toHaveLength(0);
    app.done();
  });
});

describe("closing", () => {
  it("Esc closes the screen", async () => {
    const client = fakeClient();
    const app = await open(client);
    expect(app.view()).toContain("Project trust");
    await press(app, ESC, 150);
    expect(app.view()).not.toContain("Project trust");
    app.done();
  });
});

describe("the startup question for an untrusted folder", () => {
  it("is asked first, with the folder, what trusting means and the two options", async () => {
    const client = fakeClient({ prompt: true });
    const app = await mount(client);
    const view = app.view();
    expect(view).toContain("Do you trust this folder?");
    expect(view).toContain("/work/p");
    expect(view).toContain("run tools and read and write files here");
    expect(view).toContain("1. Yes, trust this folder");
    expect(view).toContain("2. No, exit");
    expect(sets(client)).toHaveLength(0);
    app.done();
  });

  it("Yes (Enter on the first option) trusts with the shown digests and starts, with no /trust message", async () => {
    const client = fakeClient({ prompt: true });
    const app = await mount(client);
    await press(app, "\r", 250);
    expect(sets(client)).toHaveLength(1);
    expect(sets(client)[0].params.workdir).toBe("/work/p");
    expect(sets(client)[0].params.digests["scripts/guard.js"]).toBe("sha256:ddd");
    const view = app.view();
    expect(view).not.toContain("Do you trust this folder?");
    expect(view).not.toContain("/trust");
    app.done();
  });

  it("1 then Enter is Yes too", async () => {
    const client = fakeClient({ prompt: true });
    const app = await mount(client);
    await press(app, DOWN);
    await press(app, "1");
    await press(app, "\r", 250);
    expect(sets(client)).toHaveLength(1);
    app.done();
  });

  it("No (2, Enter) exits without trusting", async () => {
    const client = fakeClient({ prompt: true });
    const app = await mount(client);
    await press(app, "2");
    const exited = app.instance.waitUntilExit();
    await press(app, "\r", 100);
    await exited;
    expect(sets(client)).toHaveLength(0);
    app.done();
  });

  it("Esc is No", async () => {
    const client = fakeClient({ prompt: true });
    const app = await mount(client);
    const exited = app.instance.waitUntilExit();
    await press(app, ESC, 100);
    await exited;
    expect(sets(client)).toHaveLength(0);
    app.done();
  });

  it("an already trusted folder gets no question", async () => {
    const client = fakeClient({ prompt: true, status: () => status({ trusted: true, scopes: ["hooks"] }) });
    const app = await mount(client);
    expect(app.view()).not.toContain("Do you trust this folder?");
    expect(sets(client)).toHaveLength(0);
    app.done();
  });

  it("an older daemon is not asked", async () => {
    const client = fakeClient({ prompt: true, caps: [] });
    const app = await mount(client);
    expect(app.view()).not.toContain("Do you trust this folder?");
    expect(methods(client)).not.toContain("project.trust.status");
    app.done();
  });

  it("when core refuses trust from this client the reason is shown and Enter goes on without it", async () => {
    const client = fakeClient({ prompt: true, setFail: () => ({ code: "jarvis_person_only", message: "only a person on this machine can trust a folder" }) });
    const app = await mount(client);
    await press(app, "\r", 250);
    expect(app.view()).toContain("Do you trust this folder?");
    expect(app.view()).toContain("Enter to continue without trusting it");
    await press(app, "\r", 150);
    expect(app.view()).not.toContain("Do you trust this folder?");
    app.done();
  });
});
