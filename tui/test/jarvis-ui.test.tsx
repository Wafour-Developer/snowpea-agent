/**
 * The Jarvis screen and the external-session screens, through the real App and
 * a real Ink render against a mock connection. The daemon side does not exist
 * in this process: the fake answers `jarvis.*` / `external.*` the way the 1.53.0
 * contract says and lets the test push the notifications.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const ALL = ["jarvis", "jarvisDelegation", "externalSessions"];
const JARVIS_SESSION = "jarvis-session";

interface Opts {
  caps?: string[];
  enabled?: boolean;
  rows?: any[];
  approvals?: any[];
  questions?: any[];
  externals?: any[];
  clis?: any[];
  openResult?: (params: any) => any;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? ALL;
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  const responded: Array<{ requestId: string; decision: string; scope: string; reason?: string }> = [];
  const answered: Array<{ requestId: string; answers: any }> = [];
  let onApproval: ((request: any) => Promise<any>) | undefined;
  const status = (enabled: boolean) => ({
    enabled,
    running: enabled,
    folder: "/home/me/Snowpea/Jarvis",
    delegationDefault: "staged",
    sessionId: enabled ? JARVIS_SESSION : null,
    model: "fast",
  });
  let enabled = opts.enabled ?? true;
  const client: any = {
    calls,
    responded,
    answered,
    getStatus: () => "connected",
    serverCapabilities: () => caps,
    setListeners: (next: any) => {
      listeners = next;
    },
    onApprovalRequest: (handler: any) => {
      onApproval = handler;
    },
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: opts.approvals ?? [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.88", latest: "0.2.88" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async (requestId: string, decision: string, scope: string, reason?: string) => {
      responded.push({ requestId, decision, scope, reason });
    },
    respondQuestion: async (requestId: string, answers: any) => {
      answered.push({ requestId, answers });
    },
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      switch (method) {
        case "jarvis.status":
          return status(enabled);
        case "jarvis.enable":
          enabled = true;
          return status(true);
        case "jarvis.disable":
          enabled = false;
          return status(false);
        case "jarvis.overview":
          return { generatedAt: new Date().toISOString(), rows: opts.rows ?? [] };
        case "jarvis.ledger":
          return { entries: [] };
        case "jarvis.handback":
        case "jarvis.takeover":
          return { state: { exposed: true } };
        case "question.list":
          return { requests: opts.questions ?? [] };
        case "session.resume":
          return { sessionId: params.sessionId, mode: "accept", events: [] };
        case "command.list":
          return { commands: [] };
        case "external.clis":
          return {
            clis: opts.clis ?? [
              { id: "claude", title: "Claude Code", installed: true, enabled: true, version: "2.1", defaultMode: "default", capabilities: { approvals: "bridged", liveView: true, streaming: true, modes: ["default", "readonly", "unrestricted"] } },
              { id: "codex", title: "Codex", installed: false },
            ],
          };
        case "external.list":
          return { sessions: opts.externals ?? [], nextBefore: null };
        case "external.open":
          return opts.openResult
            ? opts.openResult(params)
            : { session: (opts.externals ?? [])[0], events: [], hasMore: false };
        case "external.create":
          return { session: { ref: { cli: params.cli, id: "new-1" }, cwd: params.project, status: "idle", host: "snowpea", canSend: true, mode: params.mode, controlled: params.mode !== "unrestricted", title: null } };
        case "external.setMode":
          return { session: { ...(opts.externals ?? [])[0], mode: params.mode, controlled: params.mode !== "unrestricted" } };
        case "external.send":
          return { turnId: "ext-turn" };
        default:
          return {};
      }
    },
    emit: (name: string, payload: unknown) => listeners[name]?.(payload),
    ask: (request: any) => onApproval!(request),
  };
  return client;
}

const row = (patch: Record<string, unknown> = {}) => ({
  sessionId: "s-2",
  project: "/work/snowpea",
  title: "fix auth",
  status: "running",
  level: "full",
  ...patch,
});

const ext = (patch: Record<string, unknown> = {}) => ({
  ref: { cli: "claude", id: "abcdef012345" },
  cwd: "/tmp/project",
  status: "idle",
  host: "none",
  canSend: true,
  approvals: "bridged",
  mode: "default",
  controlled: true,
  title: "write the docs",
  updatedAt: "2026-11-15T10:00:00Z",
  ...patch,
});

async function mount(client: any, props: Record<string, unknown> = {}, columns = 100, rows = 40) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(
    <App client={client} sessionId="s-1" mode="accept" workdir="/tmp/project" {...props} />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(200);
  const view = () => screen(stdout.text(), rows).join("\n");
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, stdout, view, done };
}

/**
 * Type a line and press Enter. The trailing space accepts a slash command the
 * palette would otherwise only complete on the first Enter (`/jarvis ` runs).
 */
async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(150);
}

const methods = (client: any) => client.calls.map((call: any) => call.method);

describe("an older daemon", () => {
  it("answers /jarvis and /external with the one requires-core-1.53 line and calls nothing", async () => {
    const client = fakeClient({ caps: ["audio"] });
    const app = await mount(client);
    await submit(app, "/jarvis");
    expect(app.view()).toContain("requires core 1.53");
    await submit(app, "/external");
    expect(app.view().match(/requires core 1\.53/g)!.length).toBeGreaterThanOrEqual(2);
    expect(methods(client).filter((m: string) => /^(jarvis|external)\./.test(m))).toEqual([]);
    app.done();
  });

  it("lists the two commands in the palette as disabled", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await type(app.stdin, "/jar", 20);
    await sleep(150);
    expect(app.view()).toContain("requires core 1.53");
    app.done();
  });

  it("shows no Jarvis marker, panel or HUD segment", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    expect(app.view()).not.toContain("Jarvis");
    expect(methods(client).filter((m: string) => m.startsWith("jarvis."))).toEqual([]);
    app.done();
  });
});

describe("Jarvis mode", () => {
  it("is off until asked: /jarvis says so, /jarvis on enables it", async () => {
    const client = fakeClient({ enabled: false });
    const app = await mount(client);
    await submit(app, "/jarvis");
    expect(app.view()).toContain("Jarvis is off");
    await submit(app, "/jarvis on");
    expect(methods(client)).toContain("jarvis.enable");
    expect(app.view()).toContain("Jarvis is on (beta)");
    await submit(app, "/jarvis off");
    expect(methods(client)).toContain("jarvis.disable");
    app.done();
  });

  it("opens the Jarvis session with the overview, what waits for you and its session names", async () => {
    const client = fakeClient({
      rows: [
        row({ waitingOnPerson: 1 }),
        row({ sessionId: "s-3", title: "api migration", pausedUntil: new Date(Date.now() + 7 * 60_000 - 5_000).toISOString() }),
        row({ sessionId: null, external: { cli: "codex", id: "019abc" }, host: "terminal", title: "docs", status: "idle", level: null }),
      ],
      approvals: [{ requestId: "a-1", sessionId: "s-2", tool: "shell", args: { command: "git push origin main" }, route: "person", risk: "high" }],
    });
    const app = await mount(client);
    await submit(app, "/jarvis");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "session.resume")?.params.sessionId).toBe(JARVIS_SESSION);
    const view = app.view();
    expect(view).toContain("◆ Jarvis");
    expect(view).toContain("Sessions (3)");
    expect(view).toContain("snowpea · fix auth");
    expect(view).toContain("paused 7 min");
    expect(view).toContain("other terminal");
    expect(view).toContain("Waiting for you (1)");
    expect(view).toContain("shell: git push origin main");
    app.done();
  });

  it("answers what waits for you in place, with the existing approval prompt and the session name", async () => {
    const client = fakeClient({
      rows: [row({ waitingOnPerson: 1 })],
      approvals: [{ requestId: "a-1", sessionId: "s-2", tool: "shell", args: { command: "git push" }, route: "person", risk: "high" }],
    });
    const app = await mount(client);
    await submit(app, "/jarvis");
    await sleep(300);
    app.stdin.write("\x17"); // Ctrl+W
    await sleep(200);
    expect(app.view()).toContain("Approval required · snowpea · fix auth");
    expect(app.view()).not.toContain("Allow Everything");
    app.stdin.write("\r"); // Yes
    await sleep(200);
    expect(client.responded).toEqual([{ requestId: "a-1", decision: "allow", scope: "once", reason: undefined }]);
    expect(app.view()).not.toContain("Approval required");
    app.done();
  });

  it("answers a waiting question from another session", async () => {
    const client = fakeClient({
      rows: [row({ sessionId: "s-4", title: "db" })],
      questions: [{ requestId: "q-1", sessionId: "s-4", route: "person", questions: [{ header: "DB", question: "Which database?", options: [{ label: "postgres" }, { label: "sqlite" }], allowOther: false }] }],
    });
    const app = await mount(client);
    await submit(app, "/jarvis waiting");
    await sleep(200);
    // Not on the Jarvis screen yet, so the list is read only after a refresh: enter first.
    await submit(app, "/jarvis");
    await sleep(300);
    await submit(app, "/jarvis waiting");
    await sleep(200);
    expect(app.view()).toContain("from snowpea · db");
    expect(app.view()).toContain("Which database?");
    app.stdin.write("\r"); // pick postgres
    await sleep(100);
    app.stdin.write("\x1b[B"); // to Confirm
    await sleep(100);
    app.stdin.write("\r");
    await sleep(200);
    expect(client.answered).toEqual([{ requestId: "q-1", answers: [{ selected: ["postgres"], text: null }] }]);
    app.done();
  });

  it("shows the one-press confirmation for a delegation Jarvis proposes, and Enter does not confirm it", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const answer = client.ask({
      requestId: "d-1",
      sessionId: JARVIS_SESSION,
      tool: "delegation_set",
      risk: "high",
      args: { scope: "project", target: "/work/snowpea", level: "full", allow: ["deploy"], expiresAt: new Date(Date.now() + 90 * 60_000).toISOString(), note: "ship it" },
    });
    await sleep(200);
    const view = app.view();
    expect(view).toContain("Jarvis asks to change what it may decide");
    expect(view).toContain("project /work/snowpea");
    expect(view).toContain("full");
    expect(view).toContain("deploy, release, push");
    expect(view).toContain("Expires");
    expect(view).toContain("y confirm");
    let settled = false;
    void answer.then(() => {
      settled = true;
    });
    app.stdin.write("\r");
    await sleep(150);
    expect(settled).toBe(false);
    app.stdin.write("y");
    expect(await answer).toEqual({ decision: "allow", scope: "once" });
    app.done();
  });

  it("declines a proposal with n, telling Jarvis why", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const answer = client.ask({ requestId: "d-2", sessionId: JARVIS_SESSION, tool: "delegation_set", args: { scope: "global", level: "full" } });
    await sleep(200);
    app.stdin.write("n");
    expect(await answer).toEqual({ decision: "deny", scope: "once", reason: "declined by the person" });
    app.done();
  });

  it("marks an ordinary session Jarvis supervises, and the pause after the person typed", async () => {
    const until = new Date(Date.now() + 7 * 60_000 - 5_000).toISOString();
    const client = fakeClient({ rows: [row({ sessionId: "s-1", pausedUntil: until })] });
    const app = await mount(client);
    await sleep(200);
    expect(app.view()).toContain("Jarvis paused, returns in 7 min");
    app.done();
    const live = fakeClient({ rows: [row({ sessionId: "s-1" })] });
    const second = await mount(live);
    await sleep(200);
    expect(second.view()).toContain("Jarvis deciding · full");
    second.done();
  });

  it("hands a session back and takes it over, for this session or a numbered one", async () => {
    const client = fakeClient({ rows: [row({ sessionId: "s-1" }), row({ sessionId: "s-2" })] });
    const app = await mount(client);
    await submit(app, "/jarvis handback");
    expect(client.calls.find((c: any) => c.method === "jarvis.handback")?.params).toEqual({ sessionId: "s-1" });
    await submit(app, "/jarvis takeover 30");
    expect(client.calls.find((c: any) => c.method === "jarvis.takeover")?.params).toEqual({ sessionId: "s-1", minutes: 30 });
    await submit(app, "/jarvis takeover 2 15");
    expect(client.calls.filter((c: any) => c.method === "jarvis.takeover")[1].params).toEqual({ sessionId: "s-2", minutes: 15 });
    await submit(app, "/jarvis handback nonsense");
    expect(app.view()).toContain('no supervised session "nonsense"');
    app.done();
  });

  it("opens a listed session and comes back", async () => {
    const client = fakeClient({ rows: [row({ sessionId: "s-2" })] });
    const app = await mount(client);
    await submit(app, "/jarvis open 1");
    await sleep(200);
    expect(client.calls.filter((c: any) => c.method === "session.resume").map((c: any) => c.params.sessionId)).toEqual(["s-2"]);
    await submit(app, "/jarvis back");
    await sleep(200);
    expect(client.calls.filter((c: any) => c.method === "session.resume").map((c: any) => c.params.sessionId)).toEqual(["s-2", "s-1"]);
    app.done();
  });

  it("picks a session from the list with /jarvis sessions", async () => {
    const client = fakeClient({ rows: [row({ sessionId: "s-2" }), row({ sessionId: "s-3", title: "other" })] });
    const app = await mount(client);
    await submit(app, "/jarvis sessions");
    await sleep(150);
    expect(app.view()).toContain("1. snowpea · fix auth");
    app.stdin.write("\x1b[B");
    await sleep(60);
    app.stdin.write("\r");
    await sleep(200);
    expect(client.calls.find((c: any) => c.method === "session.resume")?.params.sessionId).toBe("s-3");
    app.done();
  });

  it("starts on the Jarvis screen for `snowpea jarvis`", async () => {
    const client = fakeClient({ rows: [row()] });
    const app = await mount(client, { startScreen: "jarvis" });
    await sleep(400);
    expect(client.calls.find((c: any) => c.method === "session.resume")?.params.sessionId).toBe(JARVIS_SESSION);
    expect(app.view()).toContain("◆ Jarvis");
    app.done();
  });

  it("start on an older daemon prints the requires line instead", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client, { startScreen: "jarvis" });
    await sleep(200);
    expect(app.view()).toContain("requires core 1.53");
    app.done();
  });

  it("toasts an escalation pushed by the daemon", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onJarvisEscalation", {
      id: "e1", ts: new Date().toISOString(), reason: "guarded", summary: "git push needs you", importance: "high", sessionId: "s-2",
    });
    await sleep(200);
    expect(app.view()).toContain("Jarvis: git push needs you");
    app.done();
  });

  it("draws the panel inside 60 columns with wide characters", async () => {
    const client = fakeClient({
      rows: [
        row({ project: "/work/웹서비스", title: "배포 스크립트 점검과 인증 모듈 리팩터링", waitingOnPerson: 1 }),
        row({ sessionId: "s-3", title: "한글 제목", pausedUntil: new Date(Date.now() + 5 * 60_000 - 5_000).toISOString() }),
      ],
      approvals: [{ requestId: "a-1", sessionId: "s-2", tool: "shell", args: { command: "배포 스크립트 실행 --force" }, route: "person" }],
    });
    const app = await mount(client, {}, 60, 40);
    await submit(app, "/jarvis");
    await sleep(300);
    const lines = screen(app.stdout.text(), 40);
    expect(lines.join("\n")).toContain("Waiting for you");
    for (const line of lines) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });
});

describe("external sessions", () => {
  const history = [
    { seq: 1, kind: "message.user", ts: "2026-11-15T10:00:01Z", payload: { text: "document the api" } },
    { seq: 2, kind: "message.done", ts: "2026-11-15T10:00:02Z", payload: { role: "assistant", text: "The API has three endpoints." } },
  ];

  it("lists this folder's sessions with CLI, title, status and where they run, then opens one", async () => {
    const sessions = [
      ext(),
      ext({ ref: { cli: "codex", id: "019aaaaaaa" }, title: "port the parser", host: "terminal", canSend: false, status: "running" }),
    ];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    expect(client.calls.find((c: any) => c.method === "external.list")?.params).toMatchObject({ cwd: "/tmp/project", allProjects: false });
    const view = app.view();
    expect(view).toContain("External sessions");
    expect(view).toContain("[Claude Code] write the docs");
    expect(view).toContain("[Codex] port the parser");
    expect(view).toContain("other terminal · view only");
    expect(view).toContain("unavailable: Codex (not installed)");
    app.stdin.write("\r");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "external.open")?.params.ref).toEqual({ cli: "claude", id: "abcdef012345" });
    const opened = app.view();
    expect(opened).toContain("Claude Code · write the docs");
    expect(opened).toContain("document the api");
    expect(opened).toContain("The API has three endpoints.");
    app.done();
  });

  it("searches the list by what is typed", async () => {
    const sessions = [ext(), ext({ ref: { cli: "codex", id: "019aaaaaaa" }, title: "port the parser" })];
    const client = fakeClient({ externals: sessions });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    await type(app.stdin, "parser", 10);
    await sleep(100);
    const view = app.view();
    expect(view).toContain("search: parser");
    expect(view).toContain("[Codex] port the parser");
    expect(view).not.toContain("write the docs");
    app.done();
  });

  it("toggles to all projects with Tab", async () => {
    const client = fakeClient({ externals: [ext()] });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\t");
    await sleep(250);
    expect(client.calls.filter((c: any) => c.method === "external.list").map((c: any) => c.params.allProjects)).toEqual([false, true]);
    expect(app.view()).toContain("all projects");
    app.done();
  });

  it("streams a reply, sends a message and stops the turn with Esc", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    await submit(app, "now the cli docs");
    expect(client.calls.find((c: any) => c.method === "external.send")?.params).toEqual({
      ref: { cli: "claude", id: "abcdef012345" },
      text: "now the cli docs",
    });
    client.emit("onExternalEvent", { ref: { cli: "claude", id: "abcdef012345" }, seq: 3, kind: "message.delta", ts: "t", payload: { text: "Writing the CLI page" } });
    await sleep(200);
    expect(app.view()).toContain("Writing the CLI page");
    app.stdin.write("\x1b");
    await sleep(150);
    expect(methods(client)).toContain("external.interrupt");
    expect(methods(client)).not.toContain("external.close");
    client.emit("onExternalEvent", { ref: { cli: "claude", id: "abcdef012345" }, seq: 4, kind: "turn.done", ts: "t", payload: { reason: "stopped" } });
    await sleep(150);
    app.stdin.write("\x1b");
    await sleep(200);
    expect(methods(client)).toContain("external.close");
    expect(app.view()).not.toContain("Claude Code · write the docs");
    app.done();
  });

  it("is view-only for a session open in another terminal, says why, and flips when that closes", async () => {
    const sessions = [ext({ host: "terminal", canSend: false, status: "running" })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false, readOnly: true }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    expect(app.view()).toContain("Open in another terminal — view only.");
    await type(app.stdin, "hello", 10);
    app.stdin.write("\r");
    await sleep(150);
    expect(methods(client)).not.toContain("external.send");
    client.emit("onExternalState", { ref: sessions[0].ref, status: "idle", host: "snowpea", canSend: true, approvals: "bridged", mode: "default", controlled: true });
    await sleep(250);
    expect(app.view()).toContain("The other terminal closed");
    expect(app.view()).not.toContain("Open in another terminal — view only.");
    await submit(app, "hello again");
    expect(client.calls.find((c: any) => c.method === "external.send")?.params.text).toBe("hello again");
    app.done();
  });

  it("leaves a view-only session with Esc, since there is no input to take it", async () => {
    const sessions = [ext({ host: "terminal", canSend: false })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false, readOnly: true }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    expect(app.view()).toContain("Claude Code · write the docs");
    app.stdin.write("\x1b");
    await sleep(250);
    expect(methods(client)).toContain("external.close");
    expect(methods(client)).not.toContain("external.interrupt");
    expect(app.view()).not.toContain("Claude Code · write the docs");
    app.done();
  });

  it("starts on the picker for `snowpea external`, or says core 1.53 is needed", async () => {
    const client = fakeClient({ externals: [ext()] });
    const app = await mount(client, { startScreen: "external" });
    await sleep(300);
    expect(app.view()).toContain("External sessions");
    app.done();
    const old = fakeClient({ caps: [] });
    const second = await mount(old, { startScreen: "external" });
    await sleep(200);
    expect(second.view()).toContain("requires core 1.53");
    expect(methods(old)).not.toContain("external.list");
    second.done();
  });

  it("shows a CLI that died mid-turn and lets the person continue", async () => {
    const sessions = [ext({ status: "running" })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    await submit(app, "keep going");
    client.emit("onExternalState", { ref: sessions[0].ref, status: "error", error: "process exited 137", host: "snowpea", canSend: true, approvals: "bridged", mode: "default", controlled: true });
    await sleep(250);
    expect(app.view()).toContain("Claude Code stopped mid-turn: process exited 137");
    await submit(app, "try again");
    expect(client.calls.filter((c: any) => c.method === "external.send")).toHaveLength(2);
    app.done();
  });

  it("shows a CLI that is not installed when opening fails with external_unavailable", async () => {
    const sessions = [ext()];
    const client = fakeClient({
      externals: sessions,
      openResult: () => {
        throw Object.assign(new Error("claude is not installed"), { data: { code: "external_unavailable" } });
      },
    });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    expect(app.view()).toContain("Claude Code is not available: claude is not installed");
    app.done();
  });

  it("shows CLI approvals as ordinary approval prompts with the session name", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    const answer = client.ask({
      requestId: "x-1",
      sessionId: "ext:claude:abcdef012345",
      external: { cli: "claude", id: "abcdef012345" },
      tool: "Bash",
      args: { command: "npm test" },
      risk: "medium",
    });
    await sleep(200);
    expect(app.view()).toContain("Approval required · Claude Code abcdef01");
    expect(app.view()).not.toContain("Allow Everything");
    app.stdin.write("\r");
    expect(await answer).toEqual({ decision: "allow", scope: "once" });
    app.done();
  });

  it("starts a new session: CLI, mode, model, create", async () => {
    const client = fakeClient({ externals: [] });
    const app = await mount(client);
    await submit(app, "/external new");
    await sleep(250);
    expect(app.view()).toContain("New external session");
    expect(app.view()).toContain("Claude Code 2.1");
    expect(app.view()).toContain("unavailable");
    app.stdin.write("\x1b[B"); // Codex, not installed
    await sleep(60);
    app.stdin.write("\r");
    await sleep(100);
    expect(app.view()).toContain("Codex is not installed");
    app.stdin.write("\x1b[A");
    await sleep(60);
    app.stdin.write("\r"); // Claude Code
    await sleep(100);
    app.stdin.write("\x1b[B"); // read-only
    await sleep(60);
    app.stdin.write("\r");
    await sleep(100);
    await type(app.stdin, "opus", 10);
    app.stdin.write("\r");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "external.create")?.params).toEqual({
      cli: "claude",
      project: "/tmp/project",
      mode: "readonly",
      transport: "structured",
      model: "opus",
    });
    expect(app.view()).toContain("Claude Code");
    app.done();
  });

  it("will not create an unrestricted session without the typed word", async () => {
    const client = fakeClient({ externals: [] });
    const app = await mount(client);
    await submit(app, "/external new");
    await sleep(250);
    app.stdin.write("\r"); // Claude Code
    await sleep(100);
    app.stdin.write("\x1b[B");
    await sleep(40);
    app.stdin.write("\x1b[B"); // unrestricted
    await sleep(60);
    app.stdin.write("\r");
    await sleep(100);
    app.stdin.write("\r"); // no model
    await sleep(150);
    expect(app.view()).toContain("UNRESTRICTED mode?");
    expect(app.view()).toContain("Jarvis's guarded-class rules do not apply");
    await type(app.stdin, "yes", 10);
    app.stdin.write("\r");
    await sleep(120);
    expect(methods(client)).not.toContain("external.create");
    expect(app.view()).toContain('Type exactly "unrestricted"');
    for (let i = 0; i < 3; i += 1) {
      app.stdin.write("\x7f");
      await sleep(30);
    }
    await type(app.stdin, "unrestricted", 5);
    app.stdin.write("\r");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "external.create")?.params.mode).toBe("unrestricted");
    app.done();
  });

  it("switches an open session to unrestricted only after the typed word, then warns persistently", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    await submit(app, "/external mode unrestricted");
    await sleep(100);
    expect(app.view()).toContain('Type "unrestricted"');
    app.stdin.write("\x1b");
    await sleep(100);
    expect(methods(client)).not.toContain("external.setMode");
    await submit(app, "/external mode unrestricted");
    await type(app.stdin, "unrestricted", 5);
    app.stdin.write("\r");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "external.setMode")?.params).toEqual({
      ref: { cli: "claude", id: "abcdef012345" },
      mode: "unrestricted",
    });
    const view = app.view();
    expect(view).toContain("UNRESTRICTED — snowpea does not control what this Claude Code session runs");
    // The HUD carries it too, so it stays when the transcript scrolls.
    expect(view.match(/UNRESTRICTED/g)!.length).toBeGreaterThanOrEqual(2);
    app.done();
  });

  it("switches to a safer mode at once, without a confirmation", async () => {
    const sessions = [ext({ mode: "unrestricted", controlled: false })];
    const client = fakeClient({ externals: sessions });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    await submit(app, "/external mode readonly");
    await sleep(200);
    expect(client.calls.find((c: any) => c.method === "external.setMode")?.params.mode).toBe("readonly");
    app.done();
  });

  it("reopens the session after the daemon restarts", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    client.emit("onReconnected", { attempts: 1, sessions: [] });
    await sleep(300);
    expect(client.calls.filter((c: any) => c.method === "external.open")).toHaveLength(2);
    app.done();
  });

  it("loads earlier messages with /external more", async () => {
    const sessions = [ext()];
    const client = fakeClient({
      externals: sessions,
      openResult: () => ({ session: sessions[0], events: history.slice(1), hasMore: true }),
    });
    const app = await mount(client);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    const read = client.call;
    client.call = async (method: string, params: any) => {
      if (method === "external.read") {
        client.calls.push({ method, params });
        return { events: [history[0]], hasMore: false };
      }
      return read(method, params);
    };
    await submit(app, "/external more");
    await sleep(250);
    expect(client.calls.find((c: any) => c.method === "external.read")?.params).toMatchObject({ beforeSeq: 2 });
    expect(app.view()).toContain("document the api");
    app.done();
  });

  it("draws the picker and the screen inside 60 columns with wide characters", async () => {
    const sessions = [ext({ title: "한글로 된 아주 긴 세션 제목입니다 잘려야 합니다", host: "terminal", canSend: false })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client, {}, 60, 40);
    await submit(app, "/external");
    await sleep(250);
    for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.stdin.write("\r");
    await sleep(300);
    const lines = screen(app.stdout.text(), 40);
    expect(lines.join("\n")).toContain("view only");
    for (const line of lines) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });
});

describe("full screen", () => {
  const fullscreen = true;
  it("draws the external session in the transcript region, with the warning and the input below", async () => {
    const sessions = [ext({ mode: "unrestricted", controlled: false })];
    const client = fakeClient({
      externals: sessions,
      openResult: () => ({
        session: sessions[0],
        events: [{ seq: 1, kind: "message.done", ts: "2026-11-15T10:00:02Z", payload: { role: "assistant", text: "FULLSCREEN-ANSWER" } }],
        hasMore: false,
      }),
    });
    const app = await mount(client, { fullscreen }, 80, 30);
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
    const view = app.view();
    expect(view).toContain("Claude Code · write the docs");
    expect(view).toContain("FULLSCREEN-ANSWER");
    expect(view).toContain("UNRESTRICTED");
    expect(view).toContain("message Claude Code");
    app.done();
  });

  it("draws the Jarvis panel above the input without pushing the status block off the screen", async () => {
    const client = fakeClient({
      rows: Array.from({ length: 6 }, (_, i) => row({ sessionId: `s-${i + 2}`, title: `task ${i}` })),
      approvals: [{ requestId: "a-1", sessionId: "s-2", tool: "shell", args: { command: "git push" }, route: "person" }],
    });
    const app = await mount(client, { fullscreen }, 80, 30);
    await submit(app, "/jarvis");
    await sleep(350);
    const view = app.view();
    expect(view).toContain("◆ Jarvis");
    expect(view).toContain("Waiting for you (1)");
    // The HUD and the agent panel are still the last rows.
    expect(view).toContain("● connected");
    app.done();
  });
});
