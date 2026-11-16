/**
 * The Remote access screen (`/web`) through the real App and a real Ink render
 * against a mock connection. The fake keeps the listener's state in memory and
 * answers `web.remote.*` the way the 1.54.0 contract says, so a start followed
 * by a status reads back what was started.
 *
 * The password and the token appear in this file only as test data; nothing is
 * sent anywhere.
 */

import React from "react";
import { render } from "ink";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CAPS = ["webRemoteControl", "jarvis", "securityLists"];
const TOKEN = "Zx9-secretTOKEN_value";
const link = (host = "192.168.1.5", port = 8787, token = TOKEN) => `http://${host}:${port}/web/?t=${token}`;

interface WebState {
  enabled: boolean;
  listening: boolean;
  host: string;
  port: number;
  mode: "token" | "password";
  passwordSet: boolean;
  username: string;
  sessions: number;
  turnsRunning: number;
  token: string;
  error: string | null;
  needsRestart: boolean;
}

interface Opts {
  caps?: string[];
  web?: Partial<WebState>;
  /** Make a method fail: `{ "web.remote.listen": new Error("port 8787 is already in use") }`. */
  fail?: Record<string, unknown>;
}

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? CAPS;
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  const web: WebState = {
    enabled: false,
    listening: false,
    host: "0.0.0.0",
    port: 8787,
    mode: "token",
    passwordSet: false,
    username: "",
    sessions: 0,
    turnsRunning: 0,
    token: TOKEN,
    error: null,
    needsRestart: false,
    ...opts.web,
  };
  const status = () => ({
    enabled: web.enabled,
    listening: web.listening,
    host: web.host,
    port: web.port,
    mode: web.mode,
    passwordSet: web.passwordSet,
    username: web.username,
    sessions: web.sessions,
    turnsRunning: web.turnsRunning,
    error: web.error,
    needsRestart: web.needsRestart,
    plainHttp: true,
    warning: null,
    listenUrls: web.listening ? [`http://192.168.1.5:${web.port}/web/`] : [],
    accessUrl: web.listening && web.mode === "token" ? link("192.168.1.5", web.port, web.token) : null,
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
      if (opts.fail?.[method]) throw opts.fail[method];
      switch (method) {
        case "web.remote.status":
          return status();
        case "web.remote.listen":
          web.listening = true;
          web.enabled = web.enabled || params.save === true;
          web.host = params.host;
          web.port = params.port === 0 ? 40123 : params.port;
          if (params.username) web.username = params.username;
          web.error = null;
          return status();
        case "web.remote.stop":
          web.listening = false;
          web.sessions = 0;
          if (params.save) web.enabled = false;
          return status();
        case "web.remote.rotateToken":
          web.token = "NEWtoken-8d2f1c";
          web.sessions = 0;
          return { accessUrl: web.listening && web.mode === "token" ? link("192.168.1.5", web.port, web.token) : null };
        case "web.remote.setPassword":
          web.passwordSet = true;
          web.mode = "password";
          if (params.username) web.username = params.username;
          web.sessions = 0;
          return status();
        case "web.remote.clearPassword":
          web.passwordSet = false;
          web.mode = "token";
          web.sessions = 0;
          return status();
        case "session.list":
          return { sessions: [] };
        case "question.list":
          return { requests: [] };
        case "command.list":
          return { commands: [] };
        default:
          return {};
      }
    },
    emit: (name: string, payload: unknown) => listeners[name]?.(payload),
    state: web,
  };
  return client;
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
  return { stdin, stdout, view, done, rows };
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

const ESC = "\x1b";
const TAB = "\t";
const ENTER = "\r";
const DOWN = "\x1b[B";

const methods = (client: any) => client.calls.map((call: any) => call.method);
const webCalls = (client: any) => client.calls.filter((call: any) => String(call.method).startsWith("web.remote."));

async function open(client: any, columns = 100, rows = 40) {
  const app = await mount(client, columns, rows);
  await submit(app, "/web");
  await sleep(150);
  return app;
}

const listeningWeb = { enabled: true, listening: true, sessions: 2, turnsRunning: 1 };

afterEach(() => {
  delete process.env["SNOWPEA_OSC52"];
});

describe("an older daemon", () => {
  it("answers /web with one requires-a-newer-core line and calls nothing", async () => {
    const client = fakeClient({ caps: ["jarvis"] });
    const app = await mount(client);
    await submit(app, "/web");
    expect(app.view()).toContain("requires a newer core");
    expect(webCalls(client)).toEqual([]);
    await submit(app, "/web status");
    expect(webCalls(client)).toEqual([]);
    app.done();
  });

  it("lists /web in the palette as disabled", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await type(app.stdin, "/we", 20);
    await sleep(150);
    expect(app.view()).toContain("requires a newer core");
    app.done();
  });
});

describe("the status", () => {
  it("shows whether it listens, where, who is in, the mode, a masked link and the plain-HTTP warning", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    const view = app.view();
    expect(view).toContain("Remote access");
    expect(view).toContain("Listening on 0.0.0.0:8787");
    expect(view).toContain("http://192.168.1.5:8787/web/");
    expect(view).toContain("2 signed in · 1 turn running");
    expect(view).toContain("Sign-in: access link, no password");
    expect(view).toContain("?t=********");
    expect(view).toContain("Plain HTTP");
    expect(app.stdout.text()).not.toContain("secretTOKEN");
    app.done();
  });

  it("says why it is not listening when it should be", async () => {
    const client = fakeClient({ web: { enabled: true, listening: false, error: "port 8787 is already in use" } });
    const app = await open(client);
    expect(app.view()).toContain("Not listening — port 8787 is already in use");
    expect(app.view()).toContain("Plain HTTP");
    app.done();
  });

  it("warns that settings.json differs from what runs", async () => {
    const client = fakeClient({ web: { ...listeningWeb, needsRestart: true } });
    const app = await open(client);
    expect(app.view()).toContain("next daemon start will differ");
    app.done();
  });

  it("reads the status again when the web settings change", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    const before = methods(client).filter((m: string) => m === "web.remote.status").length;
    client.state.sessions = 5;
    client.emit("onSettingsChanged", { keys: ["web"] });
    await sleep(300);
    expect(methods(client).filter((m: string) => m === "web.remote.status").length).toBeGreaterThan(before);
    expect(app.view()).toContain("5 signed in");
    app.done();
  });

  it("is refused for anyone but the local person: one line, no screen", async () => {
    const refusal = Object.assign(new Error("person only"), { data: { code: "jarvis_person_only", details: { kind: "remote_login" } } });
    const client = fakeClient({ fail: { "web.remote.status": refusal } });
    const app = await mount(client);
    await submit(app, "/web");
    await sleep(150);
    expect(app.view()).toContain("not from a remote login");
    expect(app.view()).not.toContain("Plain HTTP");
    app.done();
  });

  it("prints a one-screen summary for /web status, with the token masked", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await mount(client);
    await submit(app, "/web status");
    await sleep(150);
    const view = app.view();
    expect(view).toContain("Remote web access");
    expect(view).toContain("Listening on 0.0.0.0:8787");
    expect(view).toContain("?t=********");
    expect(view).toContain("Plain HTTP");
    expect(view).not.toContain("secretTOKEN");
    expect(app.stdout.text()).not.toContain("secretTOKEN");
    app.done();
  });
});

describe("the access link", () => {
  it("is revealed with v and hidden again with v", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    expect(app.view()).not.toContain("secretTOKEN");
    await press(app, "v");
    expect(app.view()).toContain(link());
    await press(app, "v");
    expect(app.view()).not.toContain("secretTOKEN");
    expect(app.view()).toContain("?t=********");
    app.done();
  });

  it("is copied with OSC 52 where the terminal takes it, and never printed", async () => {
    process.env["SNOWPEA_OSC52"] = "1";
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "y");
    const sequence = `\u001b]52;c;${Buffer.from(link(), "utf8").toString("base64")}\u0007`;
    expect(app.stdout.text()).toContain(sequence);
    expect(app.view()).toContain("Copied the access link");
    expect(app.view()).not.toContain("secretTOKEN");
    app.done();
  });

  it("is printed once, on request, where the terminal is not known to copy", async () => {
    process.env["SNOWPEA_OSC52"] = "0";
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "y");
    expect(app.stdout.text()).not.toContain("\u001b]52;");
    expect(app.view()).toContain("not known to take OSC 52");
    expect(app.view()).toContain(link());
    // Any key clears it.
    await press(app, "r", 150);
    expect(app.view()).not.toContain("secretTOKEN");
    app.done();
  });

  it("says there is none in password mode or while stopped", async () => {
    const client = fakeClient({ web: { enabled: true, listening: true, mode: "password", passwordSet: true, username: "me" } });
    const app = await open(client);
    await press(app, "y");
    expect(app.view()).toContain("no access link");
    expect(app.view()).toContain("user me");
    app.done();
  });

  it("is rotated after a y/n that says everyone is signed out, and stays masked", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "t");
    const view = app.view();
    expect(view).toContain("The old link stops working and everyone is signed out.");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.rotateToken");
    await press(app, "n");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.rotateToken");
    await press(app, "t");
    await press(app, "y", 250);
    expect(webCalls(client).map((c: any) => c.method)).toContain("web.remote.rotateToken");
    expect(app.view()).toContain("New access link made");
    expect(app.view()).toContain("0 signed in");
    expect(app.view()).not.toContain("NEWtoken");
    await press(app, "v");
    expect(app.view()).toContain("NEWtoken-8d2f1c");
    expect(app.view()).not.toContain("secretTOKEN");
    app.done();
  });
});

describe("start and stop", () => {
  it("starts with a host, a port, a username and the remember toggle", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "l");
    expect(app.view()).toContain("Listen for other machines");
    expect(app.view()).toContain("Address");
    expect(app.view()).toContain("[ ] Remember for next start");
    // Address: replace 0.0.0.0 with 127.0.0.1.
    for (let i = 0; i < 7; i += 1) await press(app, "\x7f", 20);
    await type(app.stdin, "127.0.0.1", 8);
    await press(app, ENTER);
    for (let i = 0; i < 4; i += 1) await press(app, "\x7f", 20);
    await type(app.stdin, "9000", 8);
    await press(app, ENTER);
    await type(app.stdin, "me", 8);
    await press(app, ENTER);
    await press(app, " ");
    expect(app.view()).toContain("[x] Remember for next start");
    await press(app, ENTER);
    await press(app, ENTER, 300);
    const listen = client.calls.find((c: any) => c.method === "web.remote.listen");
    expect(listen.params).toEqual({ host: "127.0.0.1", port: 9000, save: true, username: "me" });
    expect(app.view()).toContain("Listening on 127.0.0.1:9000");
    expect(app.view()).toContain("It will start again with the daemon");
    app.done();
  });

  it("starts from core's defaults with Enter all the way down", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "l");
    for (let i = 0; i < 5; i += 1) await press(app, ENTER, 60);
    await sleep(250);
    expect(client.calls.find((c: any) => c.method === "web.remote.listen").params).toEqual({ host: "0.0.0.0", port: 8787, save: false });
    expect(app.view()).toContain("Listening on 0.0.0.0:8787");
    app.done();
  });

  it("puts core's 'port in use' on the port field and keeps what was typed", async () => {
    const client = fakeClient({ fail: { "web.remote.listen": new Error("[Errno 98] address already in use: port 8787") } });
    const app = await open(client);
    await press(app, "l");
    for (let i = 0; i < 5; i += 1) await press(app, ENTER, 60);
    await sleep(250);
    const view = app.view();
    expect(view).toContain("Listen for other machines");
    expect(view).toMatch(/✗ \[Errno 98\] address already in use/);
    expect(view).toContain("8787");
    // The cursor moved to the field it belongs to.
    expect(view).toMatch(/› Port\s+8787/);
    app.done();
  });

  it("puts a bad address on the address field", async () => {
    const client = fakeClient({ fail: { "web.remote.listen": new Error("cannot assign requested address 10.9.9.9") } });
    const app = await open(client);
    await press(app, "l");
    for (let i = 0; i < 5; i += 1) await press(app, ENTER, 60);
    await sleep(250);
    expect(app.view()).toMatch(/› Address\s+0\.0\.0\.0/);
    expect(app.view()).toContain("✗ cannot assign requested address");
    app.done();
  });

  it("checks the port before asking core", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "l");
    await press(app, ENTER);
    for (let i = 0; i < 4; i += 1) await press(app, "\x7f", 20);
    await type(app.stdin, "99999", 8);
    await press(app, TAB);
    await press(app, TAB);
    await press(app, TAB);
    await press(app, ENTER, 200);
    expect(app.view()).toContain("A port is a number from 0 to 65535");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.listen");
    app.done();
  });

  it("leaves the form with Esc without calling anything", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "l");
    await press(app, ESC);
    expect(app.view()).not.toContain("Listen for other machines");
    expect(app.view()).toContain("Remote access");
    expect(webCalls(client).map((c: any) => c.method)).toEqual(["web.remote.status"]);
    app.done();
  });

  it("stops after a y/n that says everyone is signed out and warns about running turns", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "x");
    const view = app.view();
    expect(view).toContain("Everyone signed in through it is signed out.");
    expect(view).toContain("1 turn is running");
    await press(app, "n");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.stop");
    await press(app, "x");
    await press(app, "y", 250);
    expect(client.calls.find((c: any) => c.method === "web.remote.stop").params).toEqual({});
    expect(app.view()).toContain("Not listening");
    app.done();
  });

  it("can stop and keep it off at the next start", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "x");
    await press(app, "k", 250);
    expect(client.calls.find((c: any) => c.method === "web.remote.stop").params).toEqual({ save: true });
    expect(app.view()).toContain("It stays off at the next start");
    app.done();
  });

  it("says so when there is nothing to stop", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "x");
    expect(app.view()).toContain("It is not listening.");
    app.done();
  });
});

describe("password mode", () => {
  it("asks for a username, then the password twice with stars, and sets it", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "p");
    expect(app.view()).toContain("Username for sign-in:");
    await type(app.stdin, "me", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("New password");
    await type(app.stdin, "hunter2-hunter2", 8);
    // Stars, never the text.
    await sleep(200);
    expect(app.view()).toContain("***************");
    expect(app.view()).not.toContain("hunter2");
    await press(app, ENTER);
    expect(app.view()).toContain("Repeat the password:");
    await type(app.stdin, "hunter2-hunter2", 8);
    await press(app, ENTER, 300);
    const set = client.calls.find((c: any) => c.method === "web.remote.setPassword");
    expect(set.params).toEqual({ password: "hunter2-hunter2", username: "me" });
    const view = app.view();
    expect(view).toContain("Password set");
    expect(view).toContain("Sign-in: username and password (user me)");
    expect(app.stdout.text()).not.toContain("hunter2");
    app.done();
  });

  it("refuses a short password and a mismatch, and starts again", async () => {
    const client = fakeClient({ web: { ...listeningWeb, username: "me" } });
    const app = await open(client);
    await press(app, "p");
    // The username is known, so the password comes first.
    expect(app.view()).toContain("New password");
    await type(app.stdin, "short", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("at least 8 characters");
    expect(app.view()).toContain("New password");
    await type(app.stdin, "longenough1", 8);
    await press(app, ENTER);
    await type(app.stdin, "longenough2", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("The two entries differ");
    expect(app.view()).toContain("New password");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.setPassword");
    await press(app, ESC);
    expect(app.view()).not.toContain("New password");
    expect(app.stdout.text()).not.toContain("longenough");
    app.done();
  });

  it("shows a refusal from core and does not keep the password", async () => {
    const client = fakeClient({ web: { ...listeningWeb, username: "me" }, fail: { "web.remote.setPassword": new Error("password too weak") } });
    const app = await open(client);
    await press(app, "p");
    await type(app.stdin, "longenough1", 8);
    await press(app, ENTER);
    await type(app.stdin, "longenough1", 8);
    await press(app, ENTER, 300);
    expect(app.view()).toContain("The password was not set: password too weak");
    expect(app.view()).toContain("New password");
    expect(app.view()).not.toContain("longenough");
    app.done();
  });

  it("clears it after a y/n", async () => {
    const client = fakeClient({ web: { ...listeningWeb, mode: "password", passwordSet: true, username: "me" } });
    const app = await open(client);
    await press(app, "c");
    expect(app.view()).toContain("Sign-in goes back to the access link and everyone is signed out.");
    await press(app, "n");
    expect(webCalls(client).map((c: any) => c.method)).not.toContain("web.remote.clearPassword");
    await press(app, "c");
    await press(app, "y", 250);
    expect(webCalls(client).map((c: any) => c.method)).toContain("web.remote.clearPassword");
    expect(app.view()).toContain("Sign-in: access link, no password");
    app.done();
  });

  it("says there is no password to clear", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "c");
    expect(app.view()).toContain("No password is set.");
    app.done();
  });
});

describe("narrow terminals", () => {
  it("draws every state inside 60 columns, the warning included", async () => {
    const client = fakeClient({ web: { ...listeningWeb, username: "한글사용자" } });
    const app = await open(client, 60, 30);
    const inside = (label: string) => {
      for (const line of screen(app.stdout.text(), 30)) expect(textWidth(line), `${label}: ${line}`).toBeLessThanOrEqual(60);
    };
    inside("status");
    expect(app.view()).toContain("Plain HTTP");
    await press(app, "v");
    inside("revealed");
    await press(app, "l");
    inside("form");
    expect(app.view()).toContain("Start listening");
    await press(app, ESC);
    await press(app, "t");
    inside("rotate");
    await press(app, "n");
    await press(app, "p");
    inside("password");
    await press(app, ESC);
    app.done();
  });

  it("keeps the warning when the terminal is short", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client, 60, 16);
    expect(app.view()).toContain("Plain HTTP");
    for (const line of screen(app.stdout.text(), 16)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });
});

describe("the keyboard", () => {
  it("is the screen's while it is open: typed text never reaches the chat draft", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, "p");
    await type(app.stdin, "sneaky", 8);
    await press(app, ESC);
    await press(app, ESC);
    expect(app.view()).not.toContain("sneaky");
    expect(app.view()).toContain("> ask anything");
    app.done();
  });

  it("closes with Esc and leaves the daemon as it was", async () => {
    const client = fakeClient({ web: listeningWeb });
    const app = await open(client);
    await press(app, DOWN);
    await press(app, ESC, 150);
    expect(app.view()).not.toContain("Remote access");
    expect(webCalls(client).map((c: any) => c.method)).toEqual(["web.remote.status"]);
    app.done();
  });
});
