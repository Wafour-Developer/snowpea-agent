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
  /** `jarvis.escalations.list` answer; the test may mutate the array. */
  escalations?: any[];
  /** `session.list` answer (carries each session's `jarvis` state). */
  sessions?: any[];
  /** Methods that fail: `{ "external.send": { code: "session_busy", message: "…" } }`. */
  fail?: Record<string, { code: string; message: string; details?: Record<string, unknown> }>;
  /** Failures that depend on the call (the trust question fails until `trust: true`). */
  failWhen?: (method: string, params: any) => { code: string; message: string; details?: Record<string, unknown> } | null;
  /** `external.get` answer. */
  getResult?: (params: any) => any;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? ALL;
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  const responded: Array<{ requestId: string; decision: string; scope: string; reason?: string }> = [];
  const answered: Array<{ requestId: string; answers: any }> = [];
  let onApproval: ((request: any) => Promise<any>) | undefined;
  let onQuestion: ((request: any) => Promise<any>) | undefined;
  const status = (enabled: boolean) => ({
    enabled,
    running: enabled,
    folder: "/home/me/Snowpea/Jarvis",
    delegationDefault: "staged",
    sessionId: enabled ? JARVIS_SESSION : null,
    model: "fast",
  });
  let enabled = opts.enabled ?? true;
  const listed = { count: 0 };
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
    onQuestionRequest: (handler: any) => {
      onQuestion = handler;
    },
    listApprovals: async () => {
      listed.count += 1;
      return { requests: opts.approvals ?? [] };
    },
    listCount: () => listed.count,
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
      const failure = opts.fail?.[method] ?? opts.failWhen?.(method, params);
      if (failure) throw Object.assign(new Error(failure.message), { data: { code: failure.code, details: failure.details } });
      switch (method) {
        case "jarvis.escalations.list":
          return { escalations: opts.escalations ?? [], nextBefore: null };
        case "jarvis.escalations.dismiss": {
          const list = opts.escalations ?? [];
          const ids = params.all ? list.map((e: any) => e.id) : params.ids;
          for (const id of ids) {
            const at = list.findIndex((e: any) => e.id === id);
            if (at !== -1) list.splice(at, 1);
          }
          return { dismissed: ids };
        }
        case "session.list":
          return { sessions: opts.sessions ?? [] };
        case "external.get":
          return opts.getResult ? opts.getResult(params) : { session: (opts.externals ?? [])[0] };
        case "external.unwatch":
        case "external.close":
        case "external.interrupt":
          return { ok: true };
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
    askQuestion: (request: any) => onQuestion!(request),
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
      personOnly: true,
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
    const answer = client.ask({ requestId: "d-2", sessionId: JARVIS_SESSION, tool: "delegation_set", personOnly: true, args: { scope: "global", level: "full" } });
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
    // Leaving only stops watching; the process keeps running until /external stop.
    expect(methods(client)).toContain("external.unwatch");
    expect(methods(client)).not.toContain("external.close");
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
    expect(app.view()).toContain("Open in another terminal — view only");
    await type(app.stdin, "hello", 10);
    app.stdin.write("\r");
    await sleep(150);
    expect(methods(client)).not.toContain("external.send");
    client.emit("onExternalState", { ref: sessions[0].ref, status: "idle", host: "snowpea", canSend: true, approvals: "bridged", mode: "default", controlled: true });
    await sleep(250);
    expect(app.view()).toContain("The other terminal closed");
    expect(app.view()).not.toContain("Open in another terminal — view only");
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
    expect(methods(client)).toContain("external.unwatch");
    expect(methods(client)).not.toContain("external.close");
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

const inMin = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();

describe("the final Jarvis contract", () => {
  it("keeps notices across a reload, and dismisses them with /jarvis dismiss", async () => {
    const notices = [
      { id: "e1", ts: inMin(-5), reason: "finished", summary: "테스트가 모두 통과했습니다", title: "인증 모듈", project: "/work/웹서비스" },
      { id: "e2", ts: inMin(-9), reason: "error", summary: "build failed", title: "api", project: "/work/api" },
    ];
    const client = fakeClient({ escalations: [...notices] });
    const app = await mount(client);
    await submit(app, "/jarvis");
    await sleep(350);
    expect(client.calls.find((c: any) => c.method === "jarvis.escalations.list")).toBeTruthy();
    expect(app.view()).toContain("Notices (2)");
    expect(app.view()).toContain("테스트가 모두 통과했습니다");
    await submit(app, "/jarvis dismiss 2");
    expect(client.calls.find((c: any) => c.method === "jarvis.escalations.dismiss")?.params).toEqual({ ids: ["e2"] });
    expect(app.view()).toContain("Notices (1)");
    // Another surface dismissed the other one.
    client.emit("onJarvisEscalationDismissed", { ids: ["e1"] });
    await sleep(150);
    expect(app.view()).not.toContain("Notices (");
    app.done();
    // A new connection reads them from the daemon again: nothing lives only in the client.
    const second = await mount(fakeClient({ escalations: [...notices] }));
    await submit(second, "/jarvis");
    await sleep(350);
    expect(second.view()).toContain("Notices (2)");
    second.done();
  });

  it("dismisses every notice at once", async () => {
    const client = fakeClient({ escalations: [{ id: "e1", ts: inMin(-1), reason: "stalled", summary: "no progress" }] });
    const app = await mount(client);
    await submit(app, "/jarvis");
    await sleep(300);
    await submit(app, "/jarvis dismiss");
    expect(client.calls.find((c: any) => c.method === "jarvis.escalations.dismiss")?.params).toEqual({ all: true });
    expect(app.view()).not.toContain("Notices (");
    app.done();
  });

  it("says once, quietly, that Jarvis read or instructed the session on screen", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const at = new Date().toISOString();
    client.emit("onJarvisAccess", { id: "x1", ts: at, sessionId: "s-1", action: "read", summary: "digest" });
    client.emit("onJarvisAccess", { id: "x2", ts: at, sessionId: "s-1", action: "read", summary: "digest again" });
    client.emit("onJarvisAccess", { id: "x3", ts: at, sessionId: "s-9", action: "prompt", summary: "elsewhere" });
    await sleep(200);
    expect(app.view().match(/Jarvis read this session/g)).toHaveLength(1);
    expect(app.view()).not.toContain("elsewhere");
    client.emit("onJarvisAccess", { id: "x4", ts: at, sessionId: "s-1", action: "steer", summary: "use the other branch" });
    await sleep(200);
    expect(app.view()).toContain("Jarvis instructed this session");
    app.done();
  });

  it("keeps the access line and the held-request status inside 60 columns with wide text", async () => {
    const approvals: any[] = [];
    const client = fakeClient({ approvals });
    const app = await mount(client, {}, 60, 40);
    client.emit("onJarvisAccess", { id: "x1", ts: new Date().toISOString(), sessionId: "s-1", action: "prompt", summary: "배포 스크립트 점검 결과를 확인하고 인증 모듈 테스트를 다시 실행하라고 지시함" });
    const request = { requestId: "a-j", sessionId: "s-1", tool: "shell", args: { command: "배포" }, route: "jarvis" };
    approvals.push(request);
    void client.ask(request);
    await sleep(250);
    expect(app.view()).toContain("Jarvis instructed this session");
    expect(app.view()).toContain("Jarvis is deciding");
    for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });

  it("says where an inherited level comes from in the status marker", async () => {
    const client = fakeClient({
      rows: [row({ sessionId: "s-1" })],
      sessions: [{ sessionId: "s-1", jarvis: { exposed: true, level: "full", levelScope: "project", grantId: "g1" } }],
    });
    const app = await mount(client);
    await sleep(300);
    expect(app.view()).toContain("Jarvis deciding · full (from the project)");
    app.done();
  });

  it("shows UNCONTROLLED, read-only and who answers on external rows, inside 60 columns", async () => {
    const client = fakeClient({
      rows: [
        row({ sessionId: null, external: { cli: "cursor", id: "c0ffee00" }, host: "snowpea", mode: "default", controlled: false, approvals: "degraded", title: "한글 제목으로 된 아주 긴 세션 이름", level: null }),
        row({ sessionId: null, external: { cli: "claude", id: "abc12345" }, host: "tmux", mode: "default", controlled: true, approvals: "person", title: "docs", level: null }),
        row({ sessionId: null, external: { cli: "codex", id: "019abc" }, host: "none", mode: "readonly", controlled: true, approvals: "bridged", title: "audit", level: null, status: "idle" }),
      ],
    });
    const app = await mount(client, {}, 60, 40);
    await submit(app, "/jarvis");
    await sleep(350);
    const view = app.view();
    expect(view).toContain("UNCONTROLLED");
    expect(view).toContain("you answer");
    expect(view).toContain("read-only");
    for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });

  it("does not open the prompt for a request routed to Jarvis; it appears if the daemon routes it to the person", async () => {
    const approvals: any[] = [];
    const client = fakeClient({ approvals });
    const app = await mount(client);
    const request = { requestId: "a-j", sessionId: "s-1", tool: "shell", args: { command: "pytest -q" }, route: "jarvis", risk: "low" };
    approvals.push(request);
    const answer = client.ask(request);
    let settled = false;
    void answer.then(() => {
      settled = true;
    });
    await sleep(250);
    expect(app.view()).not.toContain("Approval required");
    expect(app.view()).toContain("Jarvis is deciding");
    // The daemon gives up on Jarvis and asks the person.
    const listsBefore = client.listCount();
    client.emit("onApprovalRouteChanged", { requestId: "a-j", sessionId: "s-1", route: "person", reason: "paused" });
    await sleep(200);
    // The event moved it; nothing was polled.
    expect(client.listCount()).toBe(listsBefore);
    expect(app.view()).toContain("Approval required");
    expect(app.view()).not.toContain("Jarvis is deciding");
    expect(settled).toBe(false);
    app.stdin.write("\r");
    expect(await answer).toEqual({ decision: "allow", scope: "once" });
    app.done();
  });

  it("drops a held request when Jarvis answers it, and labels who answered", async () => {
    const approvals: any[] = [];
    const client = fakeClient({ approvals });
    const app = await mount(client);
    const request = { requestId: "a-j", sessionId: "s-1", tool: "shell", args: { command: "pytest -q" }, route: "jarvis", risk: "low" };
    approvals.push(request);
    void client.ask(request);
    await sleep(200);
    expect(app.view()).toContain("Jarvis is deciding");
    approvals.length = 0;
    client.emit("onApprovalResolved", { requestId: "a-j", decision: "allow", by: "jarvis" });
    await sleep(200);
    expect(app.view()).not.toContain("Jarvis is deciding");
    expect(app.view()).toContain("Jarvis allowed shell: pytest -q");
    const auto = { ...request, requestId: "a-k", args: { command: "ls" } };
    void client.ask(auto);
    await sleep(100);
    client.emit("onApprovalResolved", { requestId: "a-k", decision: "deny", by: "jarvis:auto" });
    await sleep(200);
    expect(app.view()).toContain("Jarvis (auto) denied shell: ls");
    // A person's answer from another surface is not labelled as Jarvis's.
    void client.ask({ ...request, requestId: "a-l", route: "person" });
    await sleep(100);
    client.emit("onApprovalResolved", { requestId: "a-l", decision: "allow", by: "tui" });
    await sleep(150);
    expect(app.view().match(/Jarvis allowed/g)).toHaveLength(1);
    app.done();
  });

  it("holds a question routed to Jarvis the same way, and asks it when it falls to the person", async () => {
    const questions: any[] = [];
    const client = fakeClient({ questions });
    const app = await mount(client);
    const request = { requestId: "q-j", sessionId: "s-1", route: "jarvis", questions: [{ question: "Which database?", options: [{ label: "pg" }, { label: "sqlite" }] }] };
    questions.push(request);
    void client.askQuestion(request);
    await sleep(250);
    expect(app.view()).not.toContain("Which database?");
    expect(app.view()).toContain("Jarvis is deciding");
    const listsBefore = client.calls.filter((c: any) => c.method === "question.list").length;
    client.emit("onQuestionRouteChanged", { requestId: "q-j", sessionId: "s-1", route: "person", reason: "declined" });
    await sleep(200);
    expect(client.calls.filter((c: any) => c.method === "question.list").length).toBe(listsBefore);
    expect(app.view()).toContain("Which database?");
    app.done();
  });

  it("withdraws a prompt that is on screen when Jarvis mode takes the request", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const request = { requestId: "a-w", sessionId: "s-1", tool: "shell", args: { command: "make test" }, route: "person", risk: "low" };
    void client.ask(request);
    await sleep(250);
    expect(app.view()).toContain("Approval required");
    client.emit("onApprovalRouteChanged", { requestId: "a-w", sessionId: "s-1", route: "jarvis", reason: "jarvis_enabled" });
    await sleep(200);
    expect(app.view()).not.toContain("Approval required");
    expect(app.view()).toContain("Jarvis is deciding");
    client.emit("onApprovalRouteChanged", { requestId: "a-w", sessionId: "s-1", route: null, reason: "jarvis_disabled" });
    await sleep(200);
    expect(app.view()).toContain("Approval required");
    app.done();
  });

  it("does not poll approval.list or question.list while Jarvis holds a request", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.ask({ requestId: "a-h", sessionId: "s-1", tool: "shell", args: { command: "ls" }, route: "jarvis", risk: "low" });
    await sleep(300);
    const lists = client.listCount();
    const questionLists = client.calls.filter((c: any) => c.method === "question.list").length;
    await sleep(5500);
    expect(client.listCount()).toBe(lists);
    expect(client.calls.filter((c: any) => c.method === "question.list").length).toBe(questionLists);
    app.done();
  }, 15_000);

  it("asks a person-only request once, with no session, project or always rows", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const answer = client.ask({ requestId: "p-1", sessionId: "s-1", tool: "tmux.other", args: { x: 1 }, route: "person", personOnly: true, risk: "high" });
    await sleep(250);
    const view = app.view();
    expect(view).toContain("Approval required");
    expect(view).not.toContain("this session only");
    expect(view).not.toContain("project allowlist");
    expect(view).not.toContain("Allow Everything");
    app.stdin.write("\r");
    expect(await answer).toEqual({ decision: "allow", scope: "once" });
    app.done();
  });

  it("takes a session over while Jarvis is off", async () => {
    const client = fakeClient({ enabled: false });
    const app = await mount(client);
    await submit(app, "/jarvis takeover 20");
    expect(client.calls.find((c: any) => c.method === "jarvis.takeover")?.params).toEqual({ sessionId: "s-1", minutes: 20 });
    app.done();
  });

  it("explains jarvis_person_only when a hand-back is refused", async () => {
    const client = fakeClient({ fail: { "jarvis.handback": { code: "jarvis_person_only", message: "refused" } } });
    const app = await mount(client);
    await submit(app, "/jarvis handback");
    await sleep(150);
    expect(app.view()).toContain("Only a person can do that");
    app.done();
  });

  it("shows a briefing Jarvis wrote like any other Jarvis message, and its wake message as a quiet line", async () => {
    const base = fakeClient();
    const original = base.call;
    base.call = async (method: string, params: any = {}) => {
      if (method === "session.resume") {
        return {
          sessionId: params.sessionId,
          mode: "accept",
          events: [
            { sessionId: params.sessionId, seq: 1, kind: "message.user", ts: "2026-11-16T08:00:00Z", payload: { text: "<untrusted source=briefing>\nsessions: 3\n</untrusted>", origin: { kind: "system" } } },
            { sessionId: params.sessionId, seq: 2, kind: "message.done", ts: "2026-11-16T08:00:05Z", payload: { role: "assistant", text: "Good morning. Three sessions ran overnight; one waits for you." } },
          ],
        };
      }
      return original(method, params);
    };
    const app = await mount(base);
    await submit(app, "/jarvis");
    await sleep(400);
    const view = app.view();
    expect(view).toContain("Good morning. Three sessions ran overnight; one waits for you.");
    expect(view).not.toContain("❯ <untrusted");
    app.done();
  });
});

describe("external sessions, final contract", () => {
  const ref = { cli: "claude", id: "abcdef012345" };
  const history = [{ seq: 1, kind: "message.user", ts: "2026-11-16T10:00:01Z", payload: { text: "document the api" } }];
  /** A turn starts and streams: the session is mid-answer. */
  const running = async (client: any, who = ref) => {
    client.emit("onExternalEvent", { ref: who, seq: 2, kind: "turn.started", ts: "t", payload: { turnId: "t1" } });
    client.emit("onExternalEvent", { ref: who, seq: 3, kind: "message.delta", ts: "t", payload: { text: "Working on it" } });
    await sleep(200);
  };
  const cli = (id: string, patch: Record<string, unknown> = {}) => ({
    id,
    title: id,
    installed: true,
    enabled: true,
    defaultMode: "default",
    capabilities: { approvals: "bridged", liveView: true, streaming: true },
    ...patch,
  });

  async function openFirst(client: any, app: any) {
    await submit(app, "/external");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(300);
  }

  it("leaving only unwatches; /external stop closes the process", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await openFirst(client, app);
    await submit(app, "/external close");
    expect(client.calls.filter((c: any) => c.method === "external.unwatch")).toHaveLength(1);
    expect(client.calls.find((c: any) => c.method === "external.unwatch")?.params).toEqual({ ref });
    expect(methods(client)).not.toContain("external.close");
    await openFirst(client, app);
    await submit(app, "/external stop");
    expect(client.calls.find((c: any) => c.method === "external.close")?.params).toEqual({ ref });
    expect(app.view()).not.toContain("Claude Code · write the docs");
    app.done();
  });

  it("opens a session by its handle through external.get, and says why a stale one cannot open", async () => {
    const sessions = [ext()];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await submit(app, "/external open claude:abcdef012345");
    await sleep(250);
    expect(client.calls.find((c: any) => c.method === "external.get")?.params).toEqual({ ref });
    expect(methods(client).indexOf("external.get")).toBeLessThan(methods(client).indexOf("external.open"));
    expect(app.view()).toContain("Claude Code · write the docs");
    app.done();
    const stale = fakeClient({ fail: { "external.get": { code: "not_found", message: "no such session" } } });
    const second = await mount(stale);
    await submit(second, "/external open codex:gone");
    await sleep(200);
    expect(second.view()).toContain("That session does not exist");
    expect(methods(stale)).not.toContain("external.open");
    second.done();
    const outside = fakeClient({ fail: { "external.get": { code: "outside_root", message: "x" } } });
    const third = await mount(outside);
    await submit(third, "/external open codex:elsewhere");
    await sleep(200);
    expect(third.view()).toContain("not in a registered project");
    third.done();
  });

  it("picks the model from the CLI's list, with a typed name as the fallback", async () => {
    const clis = [cli("claude", { models: ["opus", "sonnet"], modes: ["default", "readonly", "unrestricted"] })];
    const client = fakeClient({ externals: [], clis });
    const app = await mount(client);
    await submit(app, "/external new");
    await sleep(250);
    app.stdin.write("\r"); // Claude Code
    await sleep(100);
    app.stdin.write("\r"); // default mode
    await sleep(150);
    expect(app.view()).toContain("opus");
    expect(app.view()).toContain("sonnet");
    expect(app.view()).toContain("Type a name");
    app.stdin.write("\x1b[B"); // opus
    await sleep(50);
    app.stdin.write("\r");
    await sleep(300);
    expect(client.calls.find((c: any) => c.method === "external.create")?.params).toMatchObject({ cli: "claude", mode: "default", model: "opus" });
    app.done();
  });

  it("falls back to a text field while the list is empty, and fills it in when external.clis.changed brings models", async () => {
    const clis = [cli("claude", { models: [] })];
    const client = fakeClient({ externals: [], clis });
    const app = await mount(client);
    await submit(app, "/external new");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(100);
    app.stdin.write("\r");
    await sleep(150);
    expect(app.view()).toContain("model: ");
    client.emit("onExternalClisChanged", { clis: [cli("claude", { models: ["haiku"] })] });
    await sleep(200);
    expect(app.view()).toContain("haiku");
    app.done();
  });

  it("offers agy no read-only mode, and Cursor read-only first", async () => {
    const clis = [
      cli("agy", { modes: ["default", "unrestricted"] }),
      cli("cursor", { modes: ["readonly", "default", "unrestricted"], defaultMode: "readonly" }),
    ];
    const client = fakeClient({ externals: [], clis });
    const app = await mount(client);
    await submit(app, "/external new");
    await sleep(250);
    app.stdin.write("\r"); // agy
    await sleep(150);
    expect(app.view()).not.toContain("read-only");
    expect(app.view()).toContain("UNRESTRICTED");
    app.stdin.write("\x1b"); // back to the CLI list
    await sleep(100);
    app.stdin.write("\x1b[B"); // cursor
    await sleep(50);
    app.stdin.write("\r");
    await sleep(150);
    const view = app.view();
    expect(view.indexOf("read-only")).toBeGreaterThan(-1);
    expect(view.indexOf("read-only")).toBeLessThan(view.indexOf("UNRESTRICTED"));
    app.done();
  });

  it("refuses /external mode for a mode the CLI lacks", async () => {
    const sessions = [ext({ ref: { cli: "agy", id: "a1" }, mode: "default" })];
    const client = fakeClient({ externals: sessions, clis: [cli("agy", { modes: ["default", "unrestricted"] })], openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await openFirst(client, app);
    await submit(app, "/external mode readonly");
    await sleep(150);
    expect(app.view()).toContain("agy has no read-only mode");
    expect(methods(client)).not.toContain("external.setMode");
    app.done();
  });

  it("sends during a running turn only where the CLI can steer it", async () => {
    // Claude cannot steer: nothing is sent, and the person is told.
    const sessions = [ext({ status: "running" })];
    const client = fakeClient({ externals: sessions, clis: [cli("claude", { capabilities: { approvals: "bridged", liveView: true, streaming: true, steer: false } })], openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await openFirst(client, app);
    await running(client);
    await submit(app, "and also the cli");
    expect(methods(client)).not.toContain("external.send");
    expect(app.view()).toContain("still answering");
    app.done();
    // Codex can: the message goes out, to be added to the turn.
    const codex = [ext({ ref: { cli: "codex", id: "c1" }, status: "running" })];
    const steer = fakeClient({ externals: codex, clis: [cli("codex", { capabilities: { approvals: "bridged", liveView: true, streaming: true, steer: true } })], openResult: () => ({ session: codex[0], events: history, hasMore: false }) });
    const second = await mount(steer);
    await openFirst(steer, second);
    await running(steer, { cli: "codex", id: "c1" });
    expect(second.view()).toContain("type to steer it");
    await submit(second, "use the other branch");
    expect(steer.calls.find((c: any) => c.method === "external.send")?.params).toEqual({ ref: { cli: "codex", id: "c1" }, text: "use the other branch" });
    second.done();
  });

  it("sends to a session with no process without opening it first", async () => {
    const sessions = [ext({ host: "none", canSend: true })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await openFirst(client, app);
    const opens = methods(client).filter((m: string) => m === "external.open").length;
    await submit(app, "continue please");
    expect(methods(client).filter((m: string) => m === "external.send")).toHaveLength(1);
    expect(methods(client).filter((m: string) => m === "external.open")).toHaveLength(opens);
    app.done();
  });

  it("shows the daemon's reason when typing into a tmux pane is refused", async () => {
    const sessions = [ext({ host: "tmux", canSend: true, approvals: "person", tmux: { session: "work", pane: "%3" } })];
    const client = fakeClient({
      externals: sessions,
      openResult: () => ({ session: sessions[0], events: history, hasMore: false }),
      fail: { "external.send": { code: "session_busy", message: "someone is typing in the terminal; try again in a moment" } },
    });
    const app = await mount(client);
    await openFirst(client, app);
    // Prompts of a tmux pane are answered in the terminal, and the screen says so.
    expect(app.view()).toContain("answer permission prompts in the terminal");
    await submit(app, "hello");
    await sleep(200);
    expect(app.view()).toContain("someone is typing in the terminal; try again in a moment");
    app.done();
  });

  it("says why Esc cannot stop a turn in another terminal, and leaves on the second Esc", async () => {
    const sessions = [ext({ host: "terminal", canSend: false, status: "running" })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false, readOnly: true }) });
    const app = await mount(client);
    await openFirst(client, app);
    await running(client);
    app.stdin.write("\x1b");
    await sleep(200);
    expect(methods(client)).not.toContain("external.interrupt");
    expect(app.view()).toContain("runs in another terminal");
    expect(app.view()).toContain("Claude Code · write the docs");
    app.stdin.write("\x1b");
    await sleep(200);
    expect(methods(client)).toContain("external.unwatch");
    expect(app.view()).not.toContain("Claude Code · write the docs");
    app.done();
  });

  it("interrupts a tmux pane with Esc", async () => {
    const sessions = [ext({ host: "tmux", canSend: true, approvals: "person", status: "running", tmux: { session: "work", pane: "%3" } })];
    const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false }) });
    const app = await mount(client);
    await openFirst(client, app);
    await running(client);
    app.stdin.write("\x1b");
    await sleep(200);
    expect(client.calls.find((c: any) => c.method === "external.interrupt")?.params).toEqual({ ref });
    app.done();
  });

  it("explains an interrupt the daemon refuses for a terminal session", async () => {
    const sessions = [ext({ host: "snowpea", canSend: true, status: "running" })];
    const client = fakeClient({
      externals: sessions,
      openResult: () => ({ session: sessions[0], events: history, hasMore: false }),
      fail: { "external.interrupt": { code: "external_view_only", message: "this session runs in another terminal; stop it there" } },
    });
    const app = await mount(client);
    await openFirst(client, app);
    await running(client);
    app.stdin.write("\x1b");
    await sleep(200);
    expect(app.view()).toContain("stop it there");
    app.done();
  });

  describe("adopting a tmux pane", () => {
    const viewOnly = () => ext({ host: "terminal", canSend: false, status: "idle" });
    const request = { requestId: "ad-1", sessionId: "ext:claude:abcdef012345", external: ref, tool: "tmux.adopt", personOnly: true, route: "person", risk: "high", args: { session: "work", pane: "%3" }, note: "snowpea will type into this tmux pane" };

    it("asks the person, shows the pane, and types into it after a yes", async () => {
      let allow!: () => void;
      const gate = new Promise<void>((resolve) => {
        allow = resolve;
      });
      const sessions = [viewOnly()];
      const approvals: any[] = [];
      const client = fakeClient({
        externals: sessions,
        approvals,
        openResult: (params: any) =>
          params.transport === "tmux"
            ? gate.then(() => ({ session: { ...sessions[0], host: "tmux", canSend: true, approvals: "person", tmux: { session: "work", pane: "%3" } }, events: history, hasMore: false }))
            : { session: sessions[0], events: history, hasMore: false, readOnly: true },
      });
      const app = await mount(client);
      await openFirst(client, app);
      expect(app.view()).toContain("t types into its tmux pane");
      app.stdin.write("t");
      await sleep(50);
      client.emit("onApprovalPending", { request });
      await sleep(300);
      expect(client.calls.find((c: any) => c.method === "external.open" && c.params.transport === "tmux")?.params).toEqual({ ref, limit: 200, transport: "tmux" });
      const view = app.view();
      expect(view).toContain("Let snowpea type into your tmux pane?");
      expect(view).toContain("work %3");
      expect(view).toContain("y confirm");
      app.stdin.write("\r");
      await sleep(100);
      expect(client.responded).toEqual([]);
      app.stdin.write("y");
      await sleep(150);
      expect(client.responded).toEqual([{ requestId: "ad-1", decision: "allow", scope: "once", reason: undefined }]);
      allow();
      await sleep(300);
      expect(app.view()).toContain("tmux work");
      expect(app.view()).toContain("answer permission prompts in the terminal");
      app.done();
    });

    it("stays view only when the person declines", async () => {
      const sessions = [viewOnly()];
      const approvals: any[] = [request];
      let reject!: () => void;
      const gate = new Promise<void>((_, fail) => {
        reject = () => fail(Object.assign(new Error("typing into that tmux pane was not allowed"), { data: { code: "external_view_only" } }));
      });
      gate.catch(() => undefined);
      const client = fakeClient({
        externals: sessions,
        approvals,
        openResult: (params: any) =>
          params.transport === "tmux" ? gate : { session: sessions[0], events: history, hasMore: false, readOnly: true },
      });
      const app = await mount(client);
      await openFirst(client, app);
      app.stdin.write("t");
      await sleep(50);
      client.emit("onApprovalPending", { request });
      await sleep(300);
      app.stdin.write("n");
      await sleep(150);
      expect(client.responded[0]).toMatchObject({ requestId: "ad-1", decision: "deny", scope: "once" });
      reject();
      await sleep(250);
      expect(app.view()).toContain("stays view only");
      expect(app.view()).toContain("Claude Code · write the docs");
      app.done();
    });

    it("answers a tmux.adopt that reaches the prompt directly with the same one-key confirmation", async () => {
      const client = fakeClient();
      const app = await mount(client);
      const answer = client.ask({ ...request, sessionId: "s-1" });
      await sleep(250);
      expect(app.view()).toContain("Let snowpea type into your tmux pane?");
      expect(app.view()).not.toContain("this session only");
      app.stdin.write("y");
      expect(await answer).toEqual({ decision: "allow", scope: "once" });
      app.done();
    });

    it("only a Claude Code session in another terminal can be adopted", async () => {
      const sessions = [ext({ ref: { cli: "codex", id: "c1" }, host: "terminal", canSend: false })];
      const client = fakeClient({ externals: sessions, openResult: () => ({ session: sessions[0], events: history, hasMore: false, readOnly: true }) });
      const app = await mount(client);
      await openFirst(client, app);
      app.stdin.write("t");
      await sleep(150);
      expect(app.view()).toContain("needs a Claude Code session");
      expect(client.calls.some((c: any) => c.method === "external.open" && c.params.transport === "tmux")).toBe(false);
      app.done();
    });
  });
});

describe("the security-driven contract", () => {
  const ref = { cli: "claude", id: "abcdef012345" };
  const history = [{ seq: 1, kind: "message.user", ts: "2026-11-16T10:00:01Z", payload: { text: "document the api" } }];
  const claude = (patch: Record<string, unknown> = {}) => ({
    id: "claude",
    title: "Claude Code",
    installed: true,
    enabled: true,
    version: "2.1",
    defaultMode: "default",
    capabilities: { approvals: "bridged", liveView: true, streaming: true, modes: ["default", "readonly", "unrestricted"] },
    ...patch,
  });
  /** `/external new` with Claude Code and the default mode, down to the model step. */
  async function toModelStep(app: any) {
    await submit(app, "/external new");
    await sleep(250);
    app.stdin.write("\r");
    await sleep(100);
    app.stdin.write("\r");
    await sleep(100);
  }
  const untrusted = (_method: string, params: any) =>
    _method === "external.create" && !params.trust
      ? { code: "external_unavailable", message: "first session in this project", details: { reason: "untrusted_project", project: "/tmp/project" } }
      : null;

  describe("project trust", () => {
    it("asks once, then retries external.create with trust: true after a yes", async () => {
      const client = fakeClient({ clis: [claude()], failWhen: untrusted });
      const app = await mount(client);
      await toModelStep(app);
      app.stdin.write("\r");
      await sleep(300);
      const view = app.view();
      expect(view).toContain("This CLI will load this project's own settings and hooks.");
      expect(view).toContain("Open external sessions in /tmp/project?");
      expect(client.calls.filter((c: any) => c.method === "external.create")).toHaveLength(1);
      app.stdin.write("\r");
      await sleep(100);
      expect(client.calls.filter((c: any) => c.method === "external.create")).toHaveLength(1);
      app.stdin.write("y");
      await sleep(300);
      const creates = client.calls.filter((c: any) => c.method === "external.create");
      expect(creates).toHaveLength(2);
      expect(creates[0].params.trust).toBeUndefined();
      expect(creates[1].params).toMatchObject({ cli: "claude", project: "/tmp/project", trust: true });
      app.done();
    });

    it("does not retry after a no", async () => {
      const client = fakeClient({ clis: [claude()], failWhen: untrusted });
      const app = await mount(client);
      await toModelStep(app);
      app.stdin.write("\r");
      await sleep(300);
      app.stdin.write("n");
      await sleep(200);
      expect(client.calls.filter((c: any) => c.method === "external.create")).toHaveLength(1);
      expect(app.view()).not.toContain("This CLI will load");
      app.done();
    });

    it("asks for trust when external.open needs it, too", async () => {
      const sessions = [ext()];
      const client = fakeClient({
        externals: sessions,
        openResult: (params: any) =>
          params.trust ? { session: sessions[0], events: history, hasMore: false } : (() => { throw Object.assign(new Error("untrusted"), { data: { code: "external_unavailable", details: { reason: "untrusted_project", project: "/work/web" } } }); })(),
      });
      const app = await mount(client);
      await submit(app, "/external");
      await sleep(250);
      app.stdin.write("\r");
      await sleep(300);
      expect(app.view()).toContain("Open external sessions in /work/web?");
      app.stdin.write("y");
      await sleep(300);
      expect(client.calls.filter((c: any) => c.method === "external.open").pop()?.params.trust).toBe(true);
      expect(app.view()).toContain("write the docs");
      app.done();
    });

    it("only says so when just a person can trust the project", async () => {
      const client = fakeClient({
        clis: [claude()],
        fail: { "external.create": { code: "external_unavailable", message: "x", details: { reason: "untrusted_project_person_only", project: "/tmp/project" } } },
      });
      const app = await mount(client);
      await toModelStep(app);
      app.stdin.write("\r");
      await sleep(300);
      expect(app.view()).toContain("only you can allow it");
      expect(app.view()).not.toContain("This CLI will load");
      expect(client.calls.filter((c: any) => c.method === "external.create")).toHaveLength(1);
      app.done();
    });
  });

  describe("a busy tmux pane", () => {
    const busy = async (reason: string) => {
      const sessions = [ext({ host: "tmux", canSend: true, approvals: "person", tmux: { session: "work", pane: "%3" } })];
      const client = fakeClient({
        externals: sessions,
        openResult: () => ({ session: sessions[0], events: history, hasMore: false }),
        fail: { "external.send": { code: "session_busy", message: "busy", details: { reason } } },
      });
      const app = await mount(client);
      await submit(app, "/external");
      await sleep(250);
      app.stdin.write("\r");
      await sleep(300);
      await submit(app, "hello");
      await sleep(200);
      const view = app.view();
      app.done();
      return view;
    };

    it("says a permission prompt is open", async () => {
      expect(await busy("terminal_prompt")).toContain("A permission prompt is open in the terminal");
    });
    it("says Claude Code is busy", async () => {
      expect(await busy("terminal_busy")).toContain("Claude Code is busy in that terminal");
    });
    it("says someone is typing", async () => {
      expect(await busy("terminal_typing")).toContain("Someone is typing in that tmux pane");
    });
  });

  describe("a CLI path the person has not confirmed", () => {
    const pending = (patch: Record<string, unknown> = {}) =>
      claude({ installed: false, pendingPath: "/opt/tools/bin/claude", ...patch });

    it("is said in the picker, inside 60 columns", async () => {
      const client = fakeClient({ clis: [pending()], externals: [] });
      const app = await mount(client, {}, 60, 40);
      await submit(app, "/external");
      await sleep(300);
      const text = app.view();
      expect(text).toContain("path not confirmed");
      expect(text).toContain("/external confirm-path claude");
      expect(text).not.toContain("not installed");
      for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
      app.done();
    });

    it("is said in the new-session form, and does not start the CLI", async () => {
      const client = fakeClient({ clis: [pending()], externals: [] });
      const app = await mount(client, {}, 60, 40);
      await submit(app, "/external new");
      await sleep(250);
      expect(app.view()).toContain("path not confirmed");
      app.stdin.write("\r");
      await sleep(100);
      expect(app.view()).toContain("path not confirmed");
      // Still on the CLI step: the form did not move on to the mode.
      expect(app.view()).not.toContain("snowpea asks before it runs things");
      for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
      expect(methods(client)).not.toContain("external.create");
      app.done();
    });

    it("/external confirm-path shows the exact path and confirms it after a yes", async () => {
      const client = fakeClient({ clis: [pending()], externals: [] });
      const app = await mount(client);
      await submit(app, "/external confirm-path claude");
      await sleep(300);
      expect(app.view()).toContain("/opt/tools/bin/claude");
      app.stdin.write("\r");
      await sleep(100);
      expect(methods(client)).not.toContain("external.clis.confirmPath");
      app.stdin.write("y");
      await sleep(300);
      expect(client.calls.find((c: any) => c.method === "external.clis.confirmPath")?.params).toEqual({
        cli: "claude",
        path: "/opt/tools/bin/claude",
      });
      app.done();
    });

    it("/external confirm-path does nothing after a no, and says when nothing is pending", async () => {
      const client = fakeClient({ clis: [pending(), { id: "codex", title: "Codex", installed: true, enabled: true }], externals: [] });
      const app = await mount(client);
      await submit(app, "/external confirm-path claude");
      await sleep(300);
      app.stdin.write("n");
      await sleep(200);
      expect(methods(client)).not.toContain("external.clis.confirmPath");
      await submit(app, "/external confirm-path codex");
      await sleep(300);
      expect(app.view()).toContain("no path is waiting for confirmation");
      app.done();
    });

    it("explains a refused confirmation: a remote login, or a path that changed", async () => {
      const remote = fakeClient({
        clis: [pending()],
        externals: [],
        fail: { "external.clis.confirmPath": { code: "jarvis_person_only", message: "x", details: { reason: "person_only", kind: "remote_login" } } },
      });
      const first = await mount(remote);
      await submit(first, "/external confirm-path claude");
      await sleep(300);
      first.stdin.write("y");
      await sleep(300);
      expect(first.view()).toContain("a remote login cannot");
      first.done();
      const changed = fakeClient({
        clis: [pending()],
        externals: [],
        fail: { "external.clis.confirmPath": { code: "invalid_params", message: "path changed" } },
      });
      const second = await mount(changed);
      await submit(second, "/external confirm-path claude");
      await sleep(300);
      second.stdin.write("y");
      await sleep(300);
      expect(second.view()).toContain("the configured path changed");
      second.done();
    });
  });

  describe("the model list", () => {
    it("says the models are loading, then that there is no list", async () => {
      const client = fakeClient({ clis: [claude({ models: [], modelsLoaded: false })], externals: [] });
      const app = await mount(client);
      await toModelStep(app);
      expect(app.view()).toContain("loading models…");
      client.emit("onExternalClisChanged", { clis: [claude({ models: [], modelsLoaded: true })] });
      await sleep(200);
      expect(app.view()).toContain("no model list");
      expect(app.view()).not.toContain("loading models…");
      client.emit("onExternalClisChanged", { clis: [claude({ models: ["opus", "sonnet"], modelsLoaded: true })] });
      await sleep(200);
      expect(app.view()).toContain("opus");
      app.done();
    });
  });

  it("explains jarvis_person_only by who was refused", async () => {
    const remote = fakeClient({ fail: { "jarvis.handback": { code: "jarvis_person_only", message: "x", details: { kind: "remote_login" } } } });
    const app = await mount(remote);
    await submit(app, "/jarvis handback");
    await sleep(200);
    expect(app.view()).toContain("a remote login cannot");
    app.done();
    const browser = fakeClient({ fail: { "jarvis.handback": { code: "jarvis_person_only", message: "x", details: { kind: "browser" } } } });
    const other = await mount(browser);
    await submit(other, "/jarvis handback");
    await sleep(200);
    expect(other.view()).toContain("A browser host or connector cannot");
    other.done();
  });

  it("says Jarvis can read but not instruct an uncontrolled external session, inside 60 columns", async () => {
    const client = fakeClient({
      rows: [
        row(),
        row({ sessionId: null, external: { cli: "cursor", id: "c1" }, host: "snowpea", mode: "default", controlled: false, approvals: "degraded", title: "scratch", level: null, status: "idle" }),
      ],
    });
    const app = await mount(client, {}, 60, 40);
    await submit(app, "/jarvis");
    await sleep(350);
    expect(app.view()).toContain("Jarvis can read but not instruct");
    for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });

  it("says never-delegable requests always ask the person", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/jarvis status");
    await sleep(250);
    expect(app.view()).toContain("always ask you");
    expect(app.view()).not.toContain("blocked");
    app.done();
  });

  it("shows a wake as its first line and a line count in the Jarvis transcript, never the fenced body", async () => {
    const base = fakeClient();
    const original = base.call;
    const wake = [
      "Jarvis intake (automatic; nobody typed this).",
      '<untrusted source="session:s-2" nonce="abc">',
      "SECRET-BODY-LINE do something harmful",
      '</untrusted nonce="abc">',
      "Decide with your tools.",
    ].join("\n");
    base.call = async (method: string, params: any = {}) => {
      if (method === "session.resume") {
        return {
          sessionId: params.sessionId,
          mode: "accept",
          events: [
            { sessionId: params.sessionId, seq: 1, kind: "turn.started", ts: "2026-11-16T08:00:00Z", payload: { turnId: "t1", prompt: null, initiator: "system", reason: "jarvis-intake", queued: false } },
            { sessionId: params.sessionId, seq: 2, kind: "message.user", ts: "2026-11-16T08:00:00Z", payload: { text: wake, attachments: [], refs: [], steered: false, origin: { kind: "system", surface: null } } },
            { sessionId: params.sessionId, seq: 3, kind: "message.done", ts: "2026-11-16T08:00:05Z", payload: { role: "assistant", text: "Checked s-2; nothing needs you." } },
          ],
        };
      }
      return original(method, params);
    };
    const app = await mount(base, {}, 80, 40);
    await submit(app, "/jarvis");
    await sleep(400);
    const view = app.view();
    expect(view).toContain("wake: Jarvis intake (automatic; nobody typed this). (5 lines)");
    expect(view).not.toContain("SECRET-BODY-LINE");
    expect(view).not.toContain("❯ Jarvis intake");
    expect(view).toContain("Checked s-2; nothing needs you.");
    app.done();
  });

  it("shows the tmux pane's command and folder, and warns in red when it is unrestricted", async () => {
    const client = fakeClient();
    const app = await mount(client, {}, 60, 40);
    const answer = client.ask({
      requestId: "ad-9",
      sessionId: "s-1",
      tool: "tmux.adopt",
      personOnly: true,
      route: "person",
      risk: "high",
      args: { cli: "claude", sessionId: "abc", tmux: { session: "work", pane: "%3" }, cwd: "/home/me/proj", paneCommand: "claude --dangerously-skip-permissions", paneCwd: "/home/me/proj", unrestricted: true },
    });
    await sleep(300);
    const view = app.view();
    expect(view).toContain("work %3");
    expect(view).toContain("Running  claude --dangerously");
    expect(view).toContain("Folder   /home/me/proj");
    expect(view).toContain("UNRESTRICTED");
    for (const line of screen(app.stdout.text(), 40)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.stdin.write("n");
    expect(await answer).toMatchObject({ decision: "deny" });
    app.done();
  });
});
