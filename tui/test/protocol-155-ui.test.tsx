/**
 * Protocol 1.55.0 through the real App: commands beside a running turn,
 * refusals on an open prompt, the waiting tool card, the context line and the
 * `/security` unknown-code switch. The daemon is a fake; the shapes are the
 * ones in `docs/protocol.md` and `sdk/src/protocol.ts`.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env["FORCE_COLOR"] = "1";
});

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

interface Opts {
  caps?: string[];
  /** What `command.run` answers. */
  commandRun?: (params: any) => any;
  security?: Record<string, unknown>;
  settingsSetFail?: { code: string; message: string; details?: Record<string, unknown> };
}

function fakeClient(opts: Opts = {}) {
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  let security: Record<string, unknown> = {
    blockedCommands: { presets: ["essential"], custom: [] },
    protectedPaths: { presets: ["essential"], custom: [] },
    effective: { commands: [], paths: [] },
    writable: true,
    problems: [],
    project: null,
    unknownCode: "ask",
    ...opts.security,
  };
  const client: any = {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => opts.caps ?? ["commandsBesideTurn", "securityLists"],
    setListeners: (next: any) => {
      listeners = next;
    },
    onApprovalRequest: (handler: any) => {
      client.ask = handler;
    },
    onQuestionRequest: (handler: any) => {
      client.askQuestion = handler;
    },
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.88", latest: "0.2.88" }),
    prompt: async (sessionId: string, text: string) => {
      calls.push({ method: "session.prompt", params: { sessionId, text } });
      return { turnId: "t-prompt" };
    },
    interrupt: async () => {
      calls.push({ method: "session.interrupt", params: {} });
    },
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      switch (method) {
        case "command.run":
          return opts.commandRun ? opts.commandRun(params) : { turnId: "t-cmd" };
        case "security.presets.list":
          return { locale: "en", commands: [{ id: "essential", title: "Essential safety", description: "d", examples: ["rm -rf ~"], default: true }], paths: [] };
        case "security.get":
          return security;
        case "settings.set": {
          if (opts.settingsSetFail) {
            throw Object.assign(new Error(opts.settingsSetFail.message), { data: { code: opts.settingsSetFail.code, details: opts.settingsSetFail.details } });
          }
          const patch = params.patch?.security;
          if (patch?.unknownCode) security = { ...security, unknownCode: patch.unknownCode };
          return { settings: {} };
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
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
    fire: (name: string, payload: unknown) => listeners[name]?.(payload),
  };
  return client;
}

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({ sessionId: "s-1", seq, kind, payload });

async function mount(client: any, columns = 100, rows = 30) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/tmp/project" />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(200);
  const view = () => screen(stdout.text(), rows).join("\n");
  return { stdin, stdout, view, done: () => { instance.unmount(); stdin.end(); } };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(250);
}

describe("commands beside a running turn (capability commandsBesideTurn)", () => {
  const busy = async (client: any) => {
    const app = await mount(client);
    client.emit(event(1, "turn.started", { turnId: "t-1", prompt: "write a long answer" }));
    client.emit(event(2, "message.delta", { text: "The answer starts here and " }));
    await sleep(150);
    return app;
  };

  it("an instant command sent mid-turn is not drawn as a prompt and starts no turn; its result is a compact aside", async () => {
    const client = fakeClient({ commandRun: () => ({ turnId: "c-1", besideTurn: true }) });
    const app = await busy(client);
    await submit(app, "/effort high");
    const run = client.calls.find((c: any) => c.method === "command.run");
    expect(run.params).toMatchObject({ name: "effort", args: "high" });
    // Not echoed as a user prompt, not queued.
    expect(app.view()).not.toContain("◷");
    expect(app.view()).not.toMatch(/^\s*›?\s*\/effort high$/m);

    client.emit(event(3, "command.result", { commandId: "c-1", command: "/effort high", ok: true, text: "effort: high", appliesFrom: "next_turn", runningTurnId: "t-1" }));
    client.emit(event(4, "message.delta", { text: "continues without a gap." }));
    await sleep(200);
    const live = app.view();
    // The streaming text is whole and the running turn still has its working line.
    expect(live).toContain("The answer starts here and continues without a gap.");
    expect(live).toMatch(/Working|Thinking|Writing|…/);
    client.emit(event(5, "message.done", { text: "The answer starts here and continues without a gap." }));
    client.emit(event(6, "turn.done", { turnId: "t-1", reason: "complete" }));
    await sleep(250);
    const after = app.view();
    expect(after).toContain("↳ /effort high — effort: high (applies from the next turn)");
    app.done();
  });

  it("Esc still interrupts the running turn after a side command", async () => {
    const client = fakeClient({ commandRun: () => ({ turnId: "c-1", besideTurn: true }) });
    const app = await busy(client);
    await submit(app, "/effort high");
    app.stdin.write("\x1b");
    await sleep(400);
    expect(client.calls.some((c: any) => c.method === "session.interrupt")).toBe(true);
    app.done();
  });

  it("says when a change applies: next tool call, next request, next turn", async () => {
    const client = fakeClient({ commandRun: () => ({ turnId: "c-1", besideTurn: true }) });
    const app = await mount(client);
    client.emit(event(1, "command.result", { commandId: "c-1", command: "/mode auto", ok: true, text: "mode: auto", appliesFrom: "next_tool_call" }));
    client.emit(event(2, "command.result", { commandId: "c-2", command: "/mcp reload", ok: true, text: "reloaded", appliesFrom: "next_request" }));
    client.emit(event(3, "command.result", { commandId: "c-3", command: "/tools", ok: true, text: "3 tools", appliesFrom: null }));
    await sleep(250);
    const view = app.view();
    expect(view).toContain("/mode auto — mode: auto (applies from the next tool call)");
    expect(view).toContain("/mcp reload — reloaded (applies from the next request)");
    expect(view).toContain("/tools — 3 tools");
    expect(view).not.toContain("/tools — 3 tools (");
    app.done();
  });

  it("a failed or unknown command is a red aside, not a failed turn", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit(event(1, "command.result", { commandId: "c-9", command: "/nope", ok: false, text: "unknown command: /nope", appliesFrom: null }));
    await sleep(250);
    expect(app.view()).toContain("✗ /nope — unknown command: /nope");
    app.done();
  });

  it("a turn command sent mid-turn is shown as queued like a prompt", async () => {
    const client = fakeClient({ commandRun: () => ({ turnId: "t-2" }) });
    const app = await busy(client);
    await submit(app, "/compact");
    client.emit(event(3, "turn.queued", { turnId: "t-2", position: 1, queued: 1 }));
    await sleep(200);
    const view = app.view();
    expect(view).toContain("◷ 1 queued");
    expect(view).toContain("/compact");
    app.done();
  });

  it("without the capability a mid-turn command is drawn and sent as before", async () => {
    const client = fakeClient({ caps: [], commandRun: () => ({ turnId: "t-2" }) });
    const app = await busy(client);
    await submit(app, "/effort high");
    expect(client.calls.some((c: any) => c.method === "command.run")).toBe(true);
    app.done();
  });
});

describe("a refusal on an open prompt (refused)", () => {
  const request = {
    requestId: "a-1",
    sessionId: "s-1",
    tool: "shell",
    args: { command: "snowpea settings set" },
    risk: "high",
    personOnly: true,
    reason: "acts on snowpea's own controls",
  };

  it("shows the new ask reason with the always-asks tag, then the refusal on the still-open approval", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.ask(request);
    await sleep(250);
    let view = app.view();
    expect(view).toContain("Approval required");
    expect(view).toContain("[always asks you]");
    expect(view).toContain("acts on snowpea's own controls");
    expect(view).not.toContain("Your answer was not accepted");

    // The re-announcement to the asking connection after its reply was refused.
    client.fire("onApprovalPending", { request, refused: { code: "jarvis_person_only", message: "only a person at this machine may answer", reason: "person_only" } });
    await sleep(250);
    view = app.view();
    expect(view).toContain("Your answer was not accepted: only a person on this machine can decide this.");
    expect(view).toContain("Approval required");
    for (const line of view.split("\n")) expect(textWidth(line)).toBeLessThanOrEqual(100);
    app.done();
  });

  it("shows the second new reason, 'cannot tell what this code changes', when core did not mark it person-only", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.ask({ ...request, requestId: "a-2", personOnly: false, reason: "cannot tell what this code changes" });
    await sleep(250);
    expect(app.view()).toContain("cannot tell what this code changes");
    expect(app.view()).not.toContain("[always asks you]");
    app.done();
  });

  it("shows the refusal on an open question", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.askQuestion({
      requestId: "q-1",
      sessionId: "s-1",
      kind: "mode",
      questions: [{ header: "Mode", question: "Raise the mode to auto?", options: [{ label: "Yes" }, { label: "No" }], allowOther: false }],
    });
    await sleep(250);
    expect(app.view()).toContain("Raise the mode to auto?");
    client.fire("onQuestionPending", { request: { requestId: "q-1", sessionId: "s-1" }, refused: { code: "jarvis_person_only", message: "needs a person", reason: "mode_raise" } });
    await sleep(250);
    expect(app.view()).toContain("Your answer was not accepted: raising the mode needs a person on this machine.");
    expect(app.view()).toContain("Raise the mode to auto?");
    app.done();
  });
});

describe("the waiting tool card, the context line and compaction", () => {
  it("shows waiting with the host's note and clears it on running", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit(event(1, "turn.started", { turnId: "t-1", prompt: "x" }));
    client.emit(event(2, "tool.call", { callId: "c1", name: "browser_snapshot", args: { tab: 1 } }));
    client.emit(event(3, "tool.progress", { callId: "c1", name: "browser_snapshot", state: "waiting", note: "waiting for the page", chunk: "" }));
    await sleep(250);
    expect(app.view()).toContain("waiting: waiting for the page");
    client.emit(event(4, "tool.progress", { callId: "c1", name: "browser_snapshot", state: "running", chunk: "" }));
    await sleep(250);
    expect(app.view()).not.toContain("waiting for the page");
    app.done();
  });

  it("shows where the next turn compacts beside the context figure, and drops it when core says null", async () => {
    const client = fakeClient();
    const app = await mount(client, 120);
    client.emit(event(1, "context", { used: 68000, window: 200000, percent: 34, estimated: false, autoCompactAt: 170000 }));
    await sleep(250);
    expect(app.view()).toContain("ctx 34% (68.0k/200.0k)");
    expect(app.view()).toContain("auto-compact at 85%");
    client.emit(event(2, "context", { used: 68000, window: 200000, percent: 34, estimated: false, autoCompactAt: null }));
    await sleep(250);
    expect(app.view()).not.toContain("auto-compact at");
    app.done();
  });

  it("the HUD fits at 60 columns with the marker on", async () => {
    const client = fakeClient();
    const app = await mount(client, 60, 24);
    client.emit(event(1, "context", { used: 68000, window: 200000, percent: 34, estimated: false, autoCompactAt: 170000 }));
    await sleep(250);
    for (const line of app.view().split("\n")) expect(textWidth(line)).toBeLessThanOrEqual(60);
    app.done();
  });

  it("words an overflow compaction as the request being too long", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit(event(1, "compaction.started", { before: 190000, reason: "overflow" }));
    await sleep(250);
    expect(app.view()).toContain("Compacting (the request was too long)");
    client.emit(event(2, "compaction", { before: 190000, after: 30000, reason: "overflow", auto: true }));
    await sleep(250);
    expect(app.view()).toContain("compacted: request too long (190.0k → 30.0k tokens)");
    app.done();
  });
});

describe("a session error with code project_untrusted", () => {
  it("is one calm notice ending (/trust), not a failed turn", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit(event(1, "error", { code: "project_untrusted", message: "This project's hooks and MCP servers are off until you trust it" }));
    await sleep(250);
    const view = app.view();
    expect(view).toContain("until you trust it  (/trust)");
    expect(view).not.toContain("project_untrusted:");
    app.done();
  });
});

describe("/security: code snowpea can't tell about", () => {
  async function open(client: any) {
    const app = await mount(client, 100, 40);
    await submit(app, "/security");
    await sleep(150);
    return app;
  }

  it("shows the unknownCode row and the line about snowpea's own files", async () => {
    const client = fakeClient();
    const app = await open(client);
    const view = app.view();
    expect(view).toContain("Code snowpea can't tell about: [ask] / allow");
    expect(view).toContain("snowpea's own files stay protected either way");
    app.done();
  });

  it("u asks before allowing, then sends a global settings.set for security.unknownCode", async () => {
    const client = fakeClient();
    const app = await open(client);
    app.stdin.write("u");
    await sleep(120);
    expect(app.view()).toContain("Let code snowpea can't tell about run without asking?");
    expect(client.calls.some((c: any) => c.method === "settings.set")).toBe(false);
    app.stdin.write("y");
    await sleep(300);
    const set = client.calls.find((c: any) => c.method === "settings.set");
    expect(set.params).toEqual({ scope: "global", patch: { security: { unknownCode: "allow" } } });
    expect(app.view()).toContain("ask / [allow]");
    // Back to ask needs no question.
    app.stdin.write("u");
    await sleep(300);
    const sets = client.calls.filter((c: any) => c.method === "settings.set");
    expect(sets[1].params.patch).toEqual({ security: { unknownCode: "ask" } });
    app.done();
  });

  it("reads a global_only refusal in words", async () => {
    const client = fakeClient({ settingsSetFail: { code: "invalid_params", message: "global only", details: { reason: "global_only", key: "security.unknownCode" } } });
    const app = await open(client);
    app.stdin.write("u");
    await sleep(100);
    app.stdin.write("y");
    await sleep(300);
    expect(app.view()).toContain("Not saved:");
    expect(app.view()).toContain("security.unknownCode can only be set for all projects (global)");
    app.done();
  });

  it("a connection that may not change it is told so", async () => {
    const client = fakeClient({ security: { writable: false } });
    const app = await open(client);
    app.stdin.write("u");
    await sleep(150);
    expect(app.view()).toContain("Read-only");
    expect(client.calls.some((c: any) => c.method === "settings.set")).toBe(false);
    app.done();
  });
});
