/**
 * Security review 8 in the TUI (protocol 1.55.0), through the real App and a
 * real Ink render at 60 columns against a mock connection: a settings.json core
 * could not read (banner, transcript, /security status, /web), what remote
 * logins left, a long list of settings that wait, and a long tamper notice.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CAPS = ["webRemoteControl", "securityLists", "projectTrust", "jarvis"];

interface Opts {
  degraded?: string[];
  webDegraded?: string[];
  remoteLeft?: { jobs: number; mcpServers: number; skills: number };
  waitingOnReload?: string[];
}

function fakeClient(opts: Opts = {}) {
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  const client: any = {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => CAPS,
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
      calls.push({ method, params });
      switch (method) {
        case "system.info":
          return opts.degraded ? { pid: 1, settingsDegraded: opts.degraded } : { pid: 1 };
        case "web.remote.status":
          return {
            enabled: true,
            listening: true,
            host: "127.0.0.1",
            port: 8787,
            mode: "token",
            passwordSet: false,
            username: "",
            sessions: 0,
            turnsRunning: 0,
            error: null,
            needsRestart: false,
            plainHttp: true,
            warning: "Anyone who has the link can use snowpea as you.",
            listenUrls: ["http://127.0.0.1:8787/web/"],
            accessUrl: "http://127.0.0.1:8787/web/?t=SECRETTOKEN",
            ...(opts.remoteLeft ? { remoteJobs: opts.remoteLeft.jobs, remoteLeft: opts.remoteLeft } : {}),
            ...(opts.webDegraded ? { settingsDegraded: opts.webDegraded } : {}),
          };
        case "system.reloadSettings":
          return opts.waitingOnReload === undefined ? { reloaded: false } : { reloaded: false, waitingKeys: opts.waitingOnReload };
        case "security.get":
          return {
            blockedCommands: { presets: [], custom: [] },
            protectedPaths: { presets: [], custom: [] },
            effective: { commands: [], paths: [] },
            writable: true,
            unknownCode: "ask",
            problems: [],
            projectAdds: 0,
          };
        case "security.presets.list":
          return { commands: [], paths: [] };
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
  await sleep(250);
  const raw = () => screen(stdout.text(), rows).join("\n");
  const view = () => raw().replace(/[│╭╮╰╯─]/g, " ").replace(/\s+/g, " ");
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, stdout, view, raw, done };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(300);
}

const fits = (view: string, columns = 60) => view.split("\n").every((line) => textWidth(line) <= columns);

describe("a settings.json core could not read", () => {
  it("shows a banner and one transcript line, and /security status repeats it", async () => {
    const app = await mount(fakeClient({ degraded: ["security", "web"] }));
    const view = app.view();
    expect(view).toContain("settings.json unreadable: safe defaults in use");
    expect(view).toContain("settings.json: snowpea could not read security, web; safe defaults are in use");
    expect(fits(app.raw())).toBe(true);
    await submit(app, "/security status");
    const status = app.view();
    expect(status).toContain("Security lists");
    expect(status).toContain("! settings.json: snowpea could not read security, web; safe defaults are in use");
    expect(fits(app.raw())).toBe(true);
    app.done();
  });

  it("\"*\" means the whole file", async () => {
    const app = await mount(fakeClient({ degraded: ["*"] }));
    expect(app.view()).toContain("snowpea could not read the whole file; safe defaults are in use");
    app.done();
  });

  it("an older core (no settingsDegraded) shows nothing", async () => {
    const app = await mount(fakeClient());
    expect(app.view()).not.toContain("unreadable");
    expect(app.view()).not.toContain("could not read");
    app.done();
  });

  it("/web status and the /web screen carry the web status's own list", async () => {
    const status = await mount(fakeClient({ webDegraded: ["web"] }));
    await submit(status, "/web status");
    expect(status.view()).toContain("settings.json: snowpea could not read web; safe defaults are in use");
    status.done();
    const screenApp = await mount(fakeClient({ webDegraded: ["web"] }));
    await submit(screenApp, "/web");
    await sleep(200);
    expect(screenApp.view()).toContain("snowpea could not read web; safe defaults are in use");
    expect(fits(screenApp.raw())).toBe(true);
    screenApp.done();
  });
});

describe("/web: what remote logins left", () => {
  it("shows the job line and the MCP servers and skills beside it", async () => {
    const app = await mount(fakeClient({ remoteLeft: { jobs: 2, mcpServers: 1, skills: 3 } }));
    await submit(app, "/web");
    await sleep(200);
    const view = app.view();
    expect(view).toContain("2 scheduled jobs made from a remote login");
    expect(view).toContain("Remote logins also left 1 MCP server and 3 skills");
    expect(view).toContain("a count, not a guarantee");
    expect(fits(app.raw())).toBe(true);
    app.done();
  });
});

describe("settings.changed with a long waitingKeys list", () => {
  it("/settings disk lists the first few and counts the rest, inside 60 columns", async () => {
    const keys = ["security", "audio.player", "audio.stt.command", "mcp.servers.fs.command", "providers.openai.base_url", "updates.channel"];
    const app = await mount(fakeClient({ waitingOnReload: keys }));
    await submit(app, "/settings disk");
    await sleep(200);
    const view = app.view();
    expect(view).toContain("These sections wait for you: security, audio.player, audio.stt.command and 3 more.");
    expect(view).not.toContain("updates.channel");
    expect(fits(app.raw())).toBe(true);
    app.done();
  });
});

describe("a long tamper notice", () => {
  it("is written whole in the transcript at 60 columns", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const summary =
      "Something outside snowpea changed the trusted-projects record (3 times since the last notice); putting the file back failed, so the copy snowpea holds is the one in force.";
    client.emit("onJarvisEscalation", { id: "t1", ts: "2026-11-16T07:00:00Z", reason: "error", summary, importance: "high" });
    await sleep(300);
    expect(app.view()).toContain("(3 times since the last notice)");
    expect(app.view()).toContain("putting the file back failed, so the copy snowpea holds is the one in force.");
    expect(fits(app.raw())).toBe(true);
    app.done();
  });
});
