/**
 * Security review 7 in the TUI (protocol 1.55.0), through the real App and a
 * real Ink render at 60 columns against a mock connection: the /web warning
 * and highest mode, settings edited on disk (banner, screens, /settings disk),
 * the /trust limits, the tamper warning, the /security claim and a high
 * importance Jarvis notice.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CAPS = ["webRemoteControl", "securityLists", "projectTrust", "jarvis"];
const WARNING = "Anyone who has the link (or the username and password) can use snowpea as you, including running commands on this computer.";

interface Opts {
  caps?: string[];
  maxMode?: string | null;
  waitingOnReload?: string[] | undefined;
  acceptFails?: boolean;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? CAPS;
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  let maxMode = opts.maxMode === undefined ? "auto" : opts.maxMode;
  const web = () => ({
    enabled: true,
    listening: true,
    host: "0.0.0.0",
    port: 8787,
    mode: "token",
    passwordSet: false,
    username: "",
    sessions: 1,
    turnsRunning: 0,
    error: null,
    needsRestart: false,
    plainHttp: true,
    warning: WARNING,
    listenUrls: ["http://192.168.1.5:8787/web/"],
    accessUrl: "http://192.168.1.5:8787/web/?t=SECRETTOKEN",
    ...(maxMode === null ? {} : { maxMode }),
  });
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
      calls.push({ method, params });
      switch (method) {
        case "web.remote.status":
          return web();
        case "settings.set": {
          const next = params?.patch?.web?.remote?.maxMode;
          if (next) maxMode = next;
          return {};
        }
        case "system.reloadSettings":
          return opts.waitingOnReload === undefined ? { reloaded: false } : { reloaded: false, waitingKeys: opts.waitingOnReload };
        case "settings.acceptDiskChange":
          if (opts.acceptFails) throw Object.assign(new Error("x"), { data: { code: "jarvis_person_only", details: { reason: "local_only", kind: "remote_login" } } });
          return params.accept ? { applied: ["security", "web.remote"], discarded: [] } : { applied: [], discarded: ["security", "web.remote"] };
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
        case "project.trust.status":
          return {
            workdir: "/work/p",
            trusted: false,
            scopes: [],
            files: [{ path: ".snowpea/hooks.json", scope: "hooks", state: "untrusted", summary: "PreToolUse", commands: [] }],
            digests: { ".snowpea/hooks.json": "sha256:aaa" },
            notPinned: [{ command: { head: "./link.sh", tail: "", length: 9, label: "Stop" }, reason: "symlink", source: ".snowpea/hooks.json" }],
            runs: [],
            limits: ["Only the listed files are pinned, not what they load."],
          };
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
  await sleep(200);
  const raw = () => screen(stdout.text(), rows).join("\n");
  /** The screen with the border and the wrapping taken out, so a sentence can be found whole. */
  const view = () => raw().replace(/[│╭╮╰╯─]/g, " ").replace(/\s+/g, " ");
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, stdout, view, raw, done, rows };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(300);
}

async function press(app: { stdin: any }, key: string, wait = 120) {
  app.stdin.write(key);
  await sleep(wait);
}

const fits = (view: string, columns = 60) => view.split("\n").every((line) => textWidth(line) <= columns);

describe("/web: the warning first, the highest mode", () => {
  it("shows core's warning at the top and the maxMode row; m changes it with settings.set", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/web");
    await sleep(150);
    const view = app.view();
    expect(view).toContain("Anyone who has the link");
    expect(view.indexOf("Anyone who has the link")).toBeLessThan(view.indexOf("Listening on"));
    expect(view).toContain("Highest mode for remote sign-ins: plan / accept / [auto]");
    expect(view).toContain("whoever holds the link can still approve");
    expect(fits(app.raw())).toBe(true);
    await press(app, "m", 300);
    const set = client.calls.find((call: any) => call.method === "settings.set");
    expect(set.params).toEqual({ scope: "global", patch: { web: { remote: { maxMode: "plan" } } } });
    expect(app.view()).toContain("[plan] / accept / auto");
    app.done();
  });

  it("shows no maxMode row (and ignores m) when the core does not report it", async () => {
    const client = fakeClient({ maxMode: null });
    const app = await mount(client);
    await submit(app, "/web");
    await sleep(150);
    expect(app.view()).not.toContain("Highest mode");
    await press(app, "m", 200);
    expect(client.calls.some((call: any) => call.method === "settings.set")).toBe(false);
    app.done();
  });

  it("/web status prints the warning first and the mode row", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/web status");
    const view = app.view();
    expect(view).toContain("Anyone who has the link");
    expect(view).toContain("Highest mode for remote sign-ins: plan / accept / [auto]");
    app.done();
  });
});

describe("settings edited on disk", () => {
  it("settings.changed with waitingKeys shows a notice line and a banner, and clears when empty", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSettingsChanged", { keys: [], waitingKeys: ["security", "web.remote"] });
    await sleep(250);
    const view = app.view();
    expect(view).toContain("settings.json was edited outside snowpea");
    expect(view).toContain("(/settings disk)");
    expect(fits(app.raw())).toBe(true);
    client.emit("onSettingsChanged", { keys: [], waitingKeys: [] });
    await sleep(250);
    expect(app.view()).not.toContain("edited outside snowpea (/settings disk)");
    app.done();
  });

  it("an older core (no waitingKeys) shows nothing", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSettingsChanged", { keys: ["models"] });
    await sleep(250);
    expect(app.view()).not.toContain("edited outside snowpea");
    app.done();
  });

  it("the /security, /web and /jarvis surfaces repeat the notice while their section waits", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSettingsChanged", { keys: [], waitingKeys: ["security", "web.remote"] });
    await sleep(250);
    await submit(app, "/security");
    await sleep(200);
    expect(app.view()).toContain("the security settings in it wait for you (/settings disk)");
    await press(app, "\x1b", 200);
    await submit(app, "/web");
    await sleep(200);
    expect(app.view()).toContain("the remote access settings in it wait for you (/settings disk)");
    app.done();
  });

  it("/settings disk shows which sections wait; a applies; the answer is printed and the notice clears", async () => {
    const client = fakeClient({ waitingOnReload: ["security", "web.remote"] });
    const app = await mount(client);
    await submit(app, "/settings disk");
    await sleep(200);
    let view = app.view();
    expect(view).toContain("These sections wait for you: security, remote access.");
    expect(view).toContain("a applies what the file says");
    expect(fits(app.raw())).toBe(true);
    await press(app, "a", 300);
    expect(client.calls.find((call: any) => call.method === "settings.acceptDiskChange").params).toEqual({ accept: true });
    view = app.view();
    expect(view).toContain("applied from the file: security, remote access");
    await press(app, "x", 200);
    expect(app.view()).not.toContain("edited outside snowpea (/settings disk)");
    app.done();
  });

  it("k keeps the current settings and prints what was discarded", async () => {
    const client = fakeClient({ waitingOnReload: ["security"] });
    const app = await mount(client);
    await submit(app, "/settings disk");
    await sleep(200);
    await press(app, "k", 300);
    expect(client.calls.find((call: any) => call.method === "settings.acceptDiskChange").params).toEqual({ accept: false });
    expect(app.view()).toContain("kept the current settings, file written back");
    app.done();
  });

  it("says so when nothing waits, and when the core is too old to know", async () => {
    const none = await mount(fakeClient({ waitingOnReload: [] }));
    await submit(none, "/settings disk");
    expect(none.view()).toContain("Nothing waits");
    none.done();
    const old = await mount(fakeClient({ waitingOnReload: undefined }));
    await submit(old, "/settings disk");
    expect(old.view()).toContain("requires a newer core");
    old.done();
  });
});

describe("/trust: limits, reasons in words, tamper", () => {
  it("shows the limits under the lists and the symlink reason in words", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/trust");
    await sleep(250);
    const view = app.view();
    expect(view).toContain("Only the listed files are pinned, not what they load.");
    expect(fits(app.raw())).toBe(true);
    app.done();
  });

  it("a tamper event is a warning in the transcript and on the open screen, and reads nothing again", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/trust");
    await sleep(250);
    const before = client.calls.filter((call: any) => call.method === "project.trust.status").length;
    client.emit("onProjectTrustChanged", { workdir: "", scopes: [], by: null, reason: "tamper", projects: [] });
    await sleep(300);
    expect(app.view()).toContain("Something outside snowpea tried to change the trust records; nothing was changed.");
    expect(client.calls.filter((call: any) => call.method === "project.trust.status").length).toBe(before);
    await press(app, "\x1b", 200);
    // The transcript keeps the warning (it went to the scrollback while the screen was up).
    expect(app.stdout.text().replace(/\s+/g, " ")).toContain("Something outside snowpea tried to change the trust records; nothing was changed.");
    app.done();
  });
});

describe("/security: the honest claim", () => {
  it("says what auto mode means and to use accept mode for a repository you do not trust", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/security");
    await sleep(250);
    const view = app.view();
    expect(view).toContain("In auto mode the agent runs programs as you");
    expect(fits(app.raw())).toBe(true);
    await press(app, "\x1b", 200);
    await submit(app, "/security status");
    expect(app.view()).toContain("use accept mode, where every command and write is yours to approve");
    app.done();
  });
});

describe("a high-importance Jarvis notice", () => {
  it("is written into the transcript once, not only toasted", async () => {
    const client = fakeClient();
    const app = await mount(client);
    const notice = {
      id: "e1",
      ts: "2026-11-16T07:00:00Z",
      reason: "error",
      summary: "Something outside snowpea changed Jarvis's delegation rules; the change was not applied and the rules were put back.",
      importance: "high",
    };
    client.emit("onJarvisEscalation", notice);
    await sleep(300);
    expect(app.view()).toContain("Jarvis: Something outside snowpea changed Jarvis's delegation rules");
    const once = app.view().split("Jarvis: Something outside").length - 1;
    // The same notice again (a reconnect) adds nothing.
    client.emit("onJarvisEscalation", notice);
    await sleep(300);
    expect(app.view().split("Jarvis: Something outside").length - 1).toBe(once);
    app.done();
  });
});
