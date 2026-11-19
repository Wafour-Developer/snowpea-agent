/**
 * `/model` inside an open external session (capability `externalModelSwitch`):
 * the pure helpers, then the real App against a fake connection. The fake never
 * reaches a daemon; it answers `external.*` the way the agreed contract says.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import {
  externalModelLabel,
  externalReducer,
  hasModelSwitch,
  modelSetLine,
  openExternal,
  parseModelCommand,
} from "../src/state/external.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const OLD = ["jarvis", "jarvisDelegation", "externalSessions"];
const NEW = [...OLD, "externalModelSwitch"];

const ext = (patch: Record<string, unknown> = {}) => ({
  ref: { cli: "claude", id: "abcdef012345" },
  cwd: "/tmp/project",
  status: "idle",
  host: "snowpea",
  canSend: true,
  approvals: "bridged",
  mode: "default",
  controlled: true,
  title: "write the docs",
  model: null,
  updatedAt: "2026-11-15T10:00:00Z",
  ...patch,
});

interface Opts {
  caps?: string[];
  session?: any;
  fail?: Record<string, { code: string; message: string }>;
  setModelResult?: (params: any) => any;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? NEW;
  const session = opts.session ?? ext();
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => caps,
    setListeners: (next: any) => {
      listeners = next;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.100", latest: "0.2.100" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      const failure = opts.fail?.[method];
      if (failure) throw Object.assign(new Error(failure.message), { data: { code: failure.code } });
      switch (method) {
        case "jarvis.status":
          return { enabled: false, running: false, folder: "/j", delegationDefault: "staged", sessionId: null };
        case "jarvis.overview":
          return { generatedAt: new Date().toISOString(), rows: [] };
        case "question.list":
          return { requests: [] };
        case "session.list":
          return { sessions: [] };
        case "command.list":
          return { commands: [] };
        case "external.clis":
          return {
            clis: [
              {
                id: "claude",
                title: "Claude Code",
                installed: true,
                enabled: true,
                version: "2.1",
                defaultMode: "default",
                capabilities: { approvals: "bridged", liveView: true, streaming: true, modes: ["default", "readonly", "unrestricted"] },
                models: ["sonnet", "opus"],
                modelsLoaded: true,
              },
            ],
          };
        case "external.list":
          return { sessions: [session], nextBefore: null };
        case "external.open":
          return { session, events: [], hasMore: false };
        case "external.setModel":
          return opts.setModelResult ? opts.setModelResult(params) : { ref: params.ref, model: params.model };
        case "external.send":
          return { turnId: "ext-turn" };
        default:
          return {};
      }
    },
    emit: (name: string, payload: unknown) => listeners[name]?.(payload),
  };
}

async function mount(client: any, columns = 100, rows = 40) {
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
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, view, done };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(150);
}

/** `/external`, Enter on the only row: the session is open. */
async function openSession(app: { stdin: any }) {
  await submit(app, "/external");
  await sleep(250);
  app.stdin.write("\r");
  await sleep(300);
}

const setModelCalls = (client: any) => client.calls.filter((c: any) => c.method === "external.setModel");

describe("external model helpers", () => {
  it("parses /model, /model default and /model <id>, nothing else", () => {
    expect(parseModelCommand("/model")).toEqual({ kind: "pick" });
    expect(parseModelCommand("  /model  ")).toEqual({ kind: "pick" });
    expect(parseModelCommand("/model default")).toEqual({ kind: "set", model: null });
    expect(parseModelCommand("/model claude-opus-4-1-20250805")).toEqual({ kind: "set", model: "claude-opus-4-1-20250805" });
    expect(parseModelCommand("/models")).toBeNull();
    expect(parseModelCommand("/external mode readonly")).toBeNull();
    expect(parseModelCommand("use /model opus")).toBeNull();
  });

  it("gates on the capability, not the version", () => {
    expect(hasModelSwitch(NEW)).toBe(true);
    expect(hasModelSwitch(OLD)).toBe(false);
    expect(hasModelSwitch(undefined)).toBe(false);
  });

  it("labels the model and says when it applies", () => {
    expect(externalModelLabel(null)).toBe("CLI default");
    expect(externalModelLabel("opus")).toBe("opus");
    expect(modelSetLine("opus", false)).toBe("model: opus");
    expect(modelSetLine(null, true)).toContain("applies from the next turn");
  });

  it("takes model from external.state when present and keeps it when an older core omits it", () => {
    const view = openExternal(ext({ model: "sonnet" }) as any, [], false);
    const base = { ref: view.ref, status: "idle", host: "snowpea", canSend: true, approvals: "bridged" };
    expect(externalReducer(view, { type: "state", payload: base as any }).session.model).toBe("sonnet");
    expect(externalReducer(view, { type: "state", payload: { ...base, model: "opus" } as any }).session.model).toBe("opus");
    expect(externalReducer(view, { type: "state", payload: { ...base, model: null } as any }).session.model).toBeNull();
  });
});

describe("/model in an external session", () => {
  it("shows the model in the header and picks one from external.clis", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await openSession(app);
    expect(app.view()).toContain("CLI default");
    await submit(app, "/model");
    await sleep(200);
    expect(app.view()).toContain("Claude Code model");
    expect(app.view()).toContain("opus");
    app.stdin.write("\x1b[B"); // sonnet
    await sleep(50);
    app.stdin.write("\x1b[B"); // opus
    await sleep(50);
    app.stdin.write("\r");
    await sleep(250);
    expect(setModelCalls(client).map((c: any) => c.params)).toEqual([{ ref: { cli: "claude", id: "abcdef012345" }, model: "opus" }]);
    expect(client.calls.some((c: any) => c.method === "external.send")).toBe(false);
    expect(app.view()).toMatch(/default · opus/);
    app.done();
  });

  it("sets a typed id directly, and /model default clears it", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await openSession(app);
    await submit(app, "/model claude-opus-4-1-20250805");
    await sleep(200);
    await submit(app, "/model default");
    await sleep(200);
    expect(setModelCalls(client).map((c: any) => c.params.model)).toEqual(["claude-opus-4-1-20250805", null]);
    app.done();
  });

  it("says it applies from the next turn when the core says so", async () => {
    const client = fakeClient({ setModelResult: (params) => ({ ref: params.ref, model: params.model, appliesNextTurn: true }) });
    const app = await mount(client);
    await openSession(app);
    await submit(app, "/model sonnet");
    await sleep(200);
    expect(app.view()).toContain("applies from the next turn");
    app.done();
  });

  it("shows the refusal inline when the CLI cannot switch", async () => {
    const client = fakeClient({
      fail: { "external.setModel": { code: "invalid_params", message: "change it in the terminal with /model" } },
    });
    const app = await mount(client);
    await openSession(app);
    await submit(app, "/model opus");
    await sleep(250);
    expect(app.view()).toContain("model not changed: change it in the terminal with /model");
    app.done();
  });

  it("follows the model external.state reports", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await openSession(app);
    client.emit("onExternalState", {
      ref: { cli: "claude", id: "abcdef012345" },
      status: "idle",
      host: "snowpea",
      canSend: true,
      approvals: "bridged",
      model: "haiku",
    });
    await sleep(150);
    expect(app.view()).toMatch(/default · haiku/);
    app.done();
  });

  it("never sends external.setModel to an older core, and does not pass /model to the CLI", async () => {
    const client = fakeClient({ caps: OLD });
    const app = await mount(client);
    await openSession(app);
    expect(app.view()).not.toContain("CLI default");
    await submit(app, "/model opus");
    await sleep(200);
    expect(setModelCalls(client)).toHaveLength(0);
    expect(client.calls.some((c: any) => c.method === "external.send")).toBe(false);
    expect(app.view()).toContain("needs a newer core");
    app.done();
  });

  it("leaves snowpea's own /model alone outside an external session", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/model opus");
    await sleep(200);
    expect(setModelCalls(client)).toHaveLength(0);
    expect(app.view()).not.toContain("Claude Code model");
    app.done();
  });
});
