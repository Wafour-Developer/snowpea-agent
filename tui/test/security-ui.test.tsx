/**
 * The security-lists screen through the real App and a real Ink render against
 * a mock connection. The daemon side does not exist in this process: the fake
 * answers `security.*` the way the contract says and keeps the saved lists in
 * memory, so a save followed by `security.get` reads back what was saved.
 *
 * Dangerous commands appear here only as string data the fake judges; nothing
 * is ever run.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { textWidth } from "../src/layout/text-width.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CAPS = ["securityLists", "jarvis", "jarvisDelegation", "externalSessions"];

const CATALOG = {
  locale: "en",
  commands: [
    { id: "essential", title: "Essential safety", description: "catastrophic deletes and disk wipes", examples: ["rm -rf ~", "mkfs /dev/sda"], default: true },
    { id: "mass-delete", title: "Mass delete", description: "recursive deletes anywhere", examples: ["find . -delete", "git clean -fdx"], default: false },
    { id: "git-destructive", title: "Destructive git", description: "force pushes and hard resets", examples: ["git push --force", "git reset --hard"], default: false },
  ],
  paths: [
    { id: "essential", title: "System folders", description: "/etc, /usr, /boot and friends", examples: [], default: true, level: "write", paths: ["/etc", "/usr", "/boot"] },
    { id: "credentials", title: "Credentials", description: "keys and tokens", examples: [], default: false, level: "all", paths: ["~/.ssh", "~/.aws", "~/.gnupg"] },
    { id: "snowpea-home", title: "Snowpea home", description: "snowpea's own data", examples: [], default: false, level: "write", paths: ["$SNOWPEA_HOME"] },
  ],
};

interface Saved {
  blockedCommands: { presets: string[]; custom: string[] };
  protectedPaths: { presets: string[]; custom: Array<{ path: string; level: string }> };
}

interface Opts {
  caps?: string[];
  saved?: Saved;
  catalog?: typeof CATALOG;
  /** Make `settings.set` fail. */
  setFail?: { code: string; message: string; details?: Record<string, unknown> };
  /** What `security.check` answers. */
  check?: (params: any) => any;
  externals?: any[];
  rows?: any[];
}

const DEFAULT_SAVED = (): Saved => ({
  blockedCommands: { presets: ["essential"], custom: [] },
  protectedPaths: { presets: ["essential"], custom: [] },
});

function fakeClient(opts: Opts = {}) {
  const caps = opts.caps ?? CAPS;
  const catalog = opts.catalog ?? CATALOG;
  let saved = opts.saved ?? DEFAULT_SAVED();
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  const effective = () => ({
    commands: [
      ...saved.blockedCommands.presets.flatMap((id) =>
        (catalog.commands.find((p) => p.id === id)?.examples ?? [id]).map((pattern) => ({ pattern, by: id })),
      ),
      ...saved.blockedCommands.custom.map((pattern) => ({ pattern, by: pattern })),
    ],
    paths: [
      ...saved.protectedPaths.presets.flatMap((id) => {
        const preset = catalog.paths.find((p) => p.id === id);
        return (preset?.paths ?? [id]).map((path) => ({ path, level: preset?.level ?? "write", by: id }));
      }),
      ...saved.protectedPaths.custom.map((entry) => ({ path: entry.path, level: entry.level, by: entry.path })),
    ],
  });
  const status = { enabled: false, running: false, folder: "/home/me/Snowpea/Jarvis", delegationDefault: "staged", sessionId: null, model: "fast" };
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
        case "security.presets.list":
          return catalog;
        case "security.get":
          return { ...saved, effective: effective() };
        case "settings.set": {
          if (opts.setFail) {
            throw Object.assign(new Error(opts.setFail.message), { data: { code: opts.setFail.code, details: opts.setFail.details } });
          }
          const patch = params.patch.security;
          saved = {
            blockedCommands: { ...patch.blockedCommands },
            protectedPaths: { ...patch.protectedPaths },
          };
          return { settings: {} };
        }
        case "security.check":
          return opts.check ? opts.check(params) : { blocked: false, by: null, why: "" };
        case "jarvis.status":
          return status;
        case "jarvis.overview":
          return { generatedAt: new Date().toISOString(), rows: opts.rows ?? [] };
        case "jarvis.escalations.list":
          return { escalations: [], nextBefore: null };
        case "session.list":
          return { sessions: [] };
        case "question.list":
          return { requests: [] };
        case "command.list":
          return { commands: [] };
        case "external.clis":
          return { clis: [{ id: "claude", title: "Claude Code", installed: true, enabled: true, version: "2.1", defaultMode: "default", capabilities: { approvals: "bridged", liveView: true, streaming: true, modes: ["default", "readonly", "unrestricted"] } }] };
        case "external.list":
          return { sessions: opts.externals ?? [], nextBefore: null };
        case "external.get":
        case "external.open":
          return { session: (opts.externals ?? [])[0], events: [], hasMore: false };
        default:
          return {};
      }
    },
    emit: (name: string, payload: unknown) => listeners[name]?.(payload),
    savedNow: () => saved,
  };
  return client;
}

async function mount(client: any, columns = 100, rows = 40, props: Record<string, unknown> = {}) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/tmp/project" {...props} />, {
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

/** One key press, then a beat for React and Ink to settle. */
async function press(app: { stdin: any }, key: string, wait = 90) {
  app.stdin.write(key);
  await sleep(wait);
}

const DOWN = "\x1b[B";
const UP = "\x1b[A";
const ESC = "\x1b";
const TAB = "\t";
const ENTER = "\r";

const methods = (client: any) => client.calls.map((call: any) => call.method);
const sets = (client: any) => client.calls.filter((call: any) => call.method === "settings.set");

async function open(client: any, columns = 100, rows = 40) {
  const app = await mount(client, columns, rows);
  await submit(app, "/security");
  await sleep(150);
  return app;
}

describe("an older daemon", () => {
  it("answers /security with one requires-a-newer-core line and calls nothing", async () => {
    const client = fakeClient({ caps: ["jarvis"] });
    const app = await mount(client);
    await submit(app, "/security");
    expect(app.view()).toContain("requires a newer core");
    expect(methods(client).filter((m: string) => m.startsWith("security."))).toEqual([]);
    await submit(app, "/security check rm -rf /");
    expect(methods(client).filter((m: string) => m.startsWith("security."))).toEqual([]);
    app.done();
  });

  it("lists /security in the palette as disabled", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await type(app.stdin, "/secu", 20);
    await sleep(150);
    expect(app.view()).toContain("requires a newer core");
    app.done();
  });
});

describe("the security screen", () => {
  it("loads the presets and the lists, asking in the UI language", async () => {
    const client = fakeClient();
    const app = await open(client);
    const view = app.view();
    expect(view).toContain("Security lists");
    expect(view).toContain("[Blocked commands]");
    expect(view).toContain("Protected folders");
    expect(view).toContain("[x] Essential safety — catastrophic deletes and disk wipes");
    expect(view).toContain("[ ] Mass delete");
    expect(client.calls.find((c: any) => c.method === "security.presets.list").params).toEqual({ locale: "en" });
    expect(methods(client)).toContain("security.get");
    app.done();
  });

  it("shows the examples of the highlighted row beneath it and follows the cursor", async () => {
    const client = fakeClient();
    const app = await open(client);
    expect(app.view()).toContain("Examples: rm -rf ~, mkfs /dev/sda");
    expect(app.view()).not.toContain("find . -delete");
    await press(app, DOWN);
    expect(app.view()).toContain("Examples: find . -delete, git clean -fdx");
    expect(app.view()).not.toContain("rm -rf ~");
    app.done();
  });

  it("switches tabs with Tab and shows each folder preset's level and folders", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, TAB);
    const view = app.view();
    expect(view).toContain("[Protected folders]");
    expect(view).toContain("System folders (no changes)");
    expect(view).toContain("Folders: /etc, /usr, /boot");
    await press(app, DOWN);
    expect(app.view()).toContain("Credentials (no access)");
    expect(app.view()).toContain("Folders: ~/.ssh, ~/.aws, ~/.gnupg");
    app.done();
  });

  it("toggles a preset with Space, marks the change unsaved and saves with one settings.set", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, DOWN);
    await press(app, " ");
    expect(app.view()).toContain("[x] Mass delete");
    expect(app.view()).toContain("unsaved changes");
    expect(sets(client)).toHaveLength(0);
    await press(app, TAB);
    await press(app, DOWN);
    await press(app, " ");
    await press(app, "s", 250);
    expect(sets(client)).toHaveLength(1);
    expect(sets(client)[0].params).toEqual({
      scope: "global",
      patch: {
        security: {
          blockedCommands: { presets: ["essential", "mass-delete"], custom: [] },
          protectedPaths: { presets: ["essential", "credentials"], custom: [] },
        },
      },
    });
    expect(app.view()).toContain("Saved");
    expect(app.view()).not.toContain("unsaved changes");
    app.done();
  });

  it("asks before turning off a default preset, naming what stops being blocked", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, " ");
    const view = app.view();
    expect(view).toContain('Turn off "Essential safety"?');
    expect(view).toContain("rm -rf ~");
    expect(view).toContain("will no longer be blocked");
    await press(app, "n");
    expect(app.view()).toContain("[x] Essential safety");
    expect(app.view()).not.toContain("unsaved changes");
    await press(app, " ");
    await press(app, "y");
    expect(app.view()).toContain("[ ] Essential safety");
    expect(app.view()).toContain("unsaved changes");
    app.done();
  });

  it("asks when a change would leave a list with nothing at all", async () => {
    const client = fakeClient({
      saved: { blockedCommands: { presets: ["mass-delete"], custom: [] }, protectedPaths: { presets: [], custom: [] } },
    });
    const app = await open(client);
    await press(app, DOWN);
    await press(app, " ");
    expect(app.view()).toContain("Nothing in this list would be blocked any more");
    expect(app.view()).toContain("Mass delete");
    await press(app, "y");
    expect(app.view()).toContain("[ ] Mass delete");
    app.done();
  });

  it("adds, edits and deletes manual entries, deleting only after y", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, "a");
    expect(app.view()).toContain("Command pattern to block:");
    await type(app.stdin, "terraform apply", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("• terraform apply");
    await press(app, "e");
    await type(app.stdin, " -auto-approve", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("• terraform apply -auto-approve");
    await press(app, "d");
    expect(app.view()).toContain("Delete terraform apply -auto-approve?");
    await press(app, "n");
    expect(app.view()).toContain("• terraform apply -auto-approve");
    await press(app, "d");
    await press(app, "y");
    expect(app.view()).not.toContain("terraform apply");
    expect(app.view()).not.toContain("unsaved changes");
    app.done();
  });

  it("refuses an empty or duplicate manual entry on the spot", async () => {
    const client = fakeClient({
      saved: { blockedCommands: { presets: ["essential"], custom: ["npm publish"] }, protectedPaths: { presets: [], custom: [] } },
    });
    const app = await open(client);
    await press(app, "a");
    await press(app, ENTER);
    expect(app.view()).toContain("Type something first");
    await type(app.stdin, "npm publish", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("already in the list");
    await press(app, ESC);
    expect(app.view()).not.toContain("Command pattern to block:");
    app.done();
  });

  it("shows the daemon's validation error and keeps the edit", async () => {
    const client = fakeClient({ setFail: { code: "invalid_params", message: "protectedPaths.custom[0].path must be an absolute path" } });
    const app = await open(client);
    await press(app, TAB);
    await press(app, "a");
    await type(app.stdin, "relative/dir", 8);
    await press(app, ENTER);
    await press(app, "s", 250);
    expect(sets(client)).toHaveLength(1);
    expect(app.view()).toContain("Not saved: protectedPaths.custom[0].path must be an absolute path");
    expect(app.view()).toContain("relative/dir");
    expect(app.view()).toContain("unsaved changes");
    app.done();
  });

  it("cycles a folder's level between no changes and no access", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, TAB);
    await press(app, "a");
    await type(app.stdin, "/srv/data", 8);
    await press(app, ENTER);
    expect(app.view()).toContain("/srv/data  (no changes)");
    await press(app, "l");
    expect(app.view()).toContain("/srv/data  (no access)");
    await press(app, "l");
    expect(app.view()).toContain("/srv/data  (no changes)");
    await press(app, "l");
    await press(app, "s", 250);
    expect(sets(client)[0].params.patch.security.protectedPaths.custom).toEqual([{ path: "/srv/data", level: "all" }]);
    app.done();
  });

  it("closes at once when clean, asks to discard when dirty", async () => {
    const client = fakeClient();
    const app = await open(client);
    await press(app, ESC, 150);
    expect(app.view()).not.toContain("[Blocked commands]");
    await submit(app, "/security");
    await press(app, DOWN);
    await press(app, " ");
    await press(app, ESC);
    expect(app.view()).toContain("Discard your unsaved changes?");
    await press(app, "n");
    expect(app.view()).toContain("[Blocked commands]");
    expect(app.view()).toContain("unsaved changes");
    await press(app, ESC);
    await press(app, "y", 150);
    expect(app.view()).not.toContain("[Blocked commands]");
    expect(sets(client)).toHaveLength(0);
    app.done();
  });

  it("goes read-only with one line why when only a person may change the lists", async () => {
    const client = fakeClient({ setFail: { code: "jarvis_person_only", message: "person only", details: { kind: "remote_login" } } });
    const app = await open(client);
    await press(app, DOWN);
    await press(app, " ");
    await press(app, "s", 250);
    expect(app.view()).toContain("Read-only: only a local snowpea can change these lists");
    expect(app.view()).not.toContain("unsaved changes");
    expect(app.view()).toContain("[ ] Mass delete");
    await press(app, " ");
    expect(app.view()).toContain("[ ] Mass delete");
    await press(app, "a");
    expect(app.view()).not.toContain("Command pattern to block:");
    expect(sets(client)).toHaveLength(1);
    // Still readable.
    await press(app, "v");
    expect(app.view()).toContain("In force now");
    app.done();
  });

  it("shows what is in force, which list put it there, and what the view leaves out", async () => {
    const client = fakeClient({
      saved: { blockedCommands: { presets: ["essential"], custom: ["npm publish"] }, protectedPaths: { presets: ["credentials"], custom: [{ path: "/srv/data", level: "write" }] } },
    });
    const app = await open(client);
    await press(app, "v");
    let view = app.view();
    expect(view).toContain("In force now: Blocked commands");
    expect(view).toContain("rm -rf ~  ← Essential safety");
    expect(view).toContain("npm publish  ← npm publish");
    expect(view).toContain("cannot see inside scripts");
    await press(app, TAB);
    view = app.view();
    expect(view).toContain("In force now: Protected folders");
    expect(view).toContain("~/.ssh (no access)  ← Credentials");
    expect(view).toContain("/srv/data (no changes)");
    await press(app, ESC);
    expect(app.view()).toContain("[Protected folders]");
    app.done();
  });
});

describe("checking text", () => {
  const check = (params: any) =>
    params.command?.includes("push --force")
      ? { blocked: true, by: "git-destructive", why: "force pushes rewrite shared history" }
      : params.path === "/home/me/.ssh/id_ed25519"
        ? { blocked: true, by: "credentials", why: "keys and tokens", level: "all" }
        : { blocked: false, by: null, why: "" };

  it("judges a command from the screen and says nothing was run", async () => {
    const client = fakeClient({ check });
    const app = await open(client);
    await press(app, "c");
    expect(app.view()).toContain("it is NOT run");
    await type(app.stdin, "git push --force origin main", 8);
    await press(app, ENTER, 200);
    expect(app.view()).toContain("BLOCKED by Destructive git: force pushes rewrite shared history");
    expect(app.view()).toContain("Nothing was run");
    const call = client.calls.find((c: any) => c.method === "security.check");
    expect(call.params).toEqual({ command: "git push --force origin main", workdir: "/tmp/project" });
    await press(app, "c");
    await type(app.stdin, "ls -la", 8);
    await press(app, ENTER, 200);
    expect(app.view()).toContain("not blocked");
    expect(app.view()).not.toContain("BLOCKED by");
    app.done();
  });

  it("judges a path on the folders tab", async () => {
    const client = fakeClient({ check });
    const app = await open(client);
    await press(app, TAB);
    await press(app, "c");
    await type(app.stdin, "/home/me/.ssh/id_ed25519", 8);
    await press(app, ENTER, 200);
    expect(app.view()).toContain("BLOCKED by Credentials (no access): keys and tokens");
    expect(client.calls.find((c: any) => c.method === "security.check").params).toEqual({ path: "/home/me/.ssh/id_ed25519", workdir: "/tmp/project" });
    app.done();
  });

  it("notes that the screen judges the saved lists when changes are pending", async () => {
    const client = fakeClient({ check });
    const app = await open(client);
    await press(app, DOWN);
    await press(app, " ");
    await press(app, "c");
    await type(app.stdin, "ls", 8);
    await press(app, ENTER, 200);
    expect(app.view()).toContain("Judged against the saved lists");
    app.done();
  });

  it("answers /security check and check-path in the transcript", async () => {
    const client = fakeClient({ check });
    const app = await mount(client);
    await submit(app, "/security check git push --force");
    await sleep(150);
    expect(app.view()).toContain("BLOCKED by Destructive git: force pushes rewrite shared history");
    expect(app.view()).toContain("Nothing was run");
    await submit(app, "/security check-path /home/me/.ssh/id_ed25519");
    await sleep(150);
    expect(app.view()).toContain("BLOCKED by Credentials (no access)");
    await submit(app, "/security check echo hello");
    await sleep(150);
    expect(app.view()).toContain("not blocked");
    expect(client.calls.filter((c: any) => c.method === "security.check").map((c: any) => Object.keys(c.params).sort())).toEqual([
      ["command", "workdir"],
      ["path", "workdir"],
      ["command", "workdir"],
    ]);
    app.done();
  });

  it("prints a usage line for a malformed /security command", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/security check");
    expect(app.view()).toContain("Usage: /security");
    expect(methods(client)).not.toContain("security.check");
    app.done();
  });
});

describe("/security status", () => {
  it("prints counts and the presets that are on", async () => {
    const client = fakeClient({
      saved: { blockedCommands: { presets: ["essential", "mass-delete"], custom: ["npm publish"] }, protectedPaths: { presets: ["credentials"], custom: [] } },
    });
    const app = await mount(client);
    await submit(app, "/security status");
    await sleep(200);
    const view = app.view();
    expect(view).toContain("Blocked commands: 2 presets on (Essential safety, Mass delete), 1 of your own");
    expect(view).toContain("Protected folders: 1 presets on (Credentials), 0 of your own");
    app.done();
  });
});

describe("security.blocked", () => {
  it("adds one red line to the session on screen", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSecurityBlocked", { sessionId: "s-1", tool: "shell", summary: "git push --force origin main", by: "git-destructive" });
    await sleep(200);
    expect(app.view()).toContain("Blocked by your security list: git push --force origin main (/security)");
    app.done();
  });

  it("toasts a block in another session instead", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSecurityBlocked", { sessionId: "other", tool: "shell", summary: "rm -rf ~", by: "essential" });
    await sleep(200);
    expect(app.view()).toContain("Security list blocked rm -rf ~ in another session (/security)");
    expect(app.view()).not.toContain("Blocked by your security list");
    app.done();
  });

  it("strips terminal escapes from the summary", async () => {
    const client = fakeClient();
    const app = await mount(client);
    client.emit("onSecurityBlocked", { sessionId: "s-1", tool: "shell", summary: "ls \u001b]52;c;ZXZpbA==\u0007evil", by: "x" });
    await sleep(200);
    expect(app.stdout.text()).not.toContain("]52;c;");
    expect(app.view()).toContain("Blocked by your security list: ls evil (/security)");
    app.done();
  });
});

describe("sessions the lists do not cover", () => {
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

  it("says so on an unrestricted external session while lists are set", async () => {
    const client = fakeClient({ externals: [ext({ mode: "unrestricted", controlled: false })] });
    const app = await mount(client, 100, 40, { startScreen: "external" });
    await sleep(300);
    await press(app, ENTER, 300);
    expect(app.view()).toContain("UNRESTRICTED");
    expect(app.view()).toContain("security lists do not apply to this session");
    app.done();
  });

  it("does not say it for a gated session, nor when no list is set", async () => {
    const gated = fakeClient({ externals: [ext()] });
    const first = await mount(gated, 100, 40, { startScreen: "external" });
    await sleep(300);
    await press(first, ENTER, 300);
    expect(first.view()).not.toContain("security lists do not apply");
    first.done();

    const empty = fakeClient({
      externals: [ext({ mode: "unrestricted", controlled: false })],
      saved: { blockedCommands: { presets: [], custom: [] }, protectedPaths: { presets: [], custom: [] } },
    });
    const second = await mount(empty, 100, 40, { startScreen: "external" });
    await sleep(300);
    await press(second, ENTER, 300);
    expect(second.view()).not.toContain("security lists do not apply");
    second.done();
  });
});

describe("narrow terminals", () => {
  it("draws the screen inside 60 columns with CJK titles and long paths, never cutting the end of a path", async () => {
    const longPath = "/srv/프로젝트/아주-긴-디렉터리-이름/그리고/또-하나의/깊은/폴더/secrets-2026/credentials.json";
    const catalog = {
      ...CATALOG,
      commands: [
        { id: "essential", title: "필수 안전 장치", description: "시스템을 망가뜨리는 치명적인 삭제와 디스크 초기화 명령을 막습니다", examples: ["rm -rf ~", "mkfs /dev/sda"], default: true },
        ...CATALOG.commands.slice(1),
      ],
    };
    const client = fakeClient({
      catalog,
      saved: {
        blockedCommands: { presets: ["essential"], custom: ["kubectl delete namespace production --all --force --grace-period=0 --wait=false"] },
        protectedPaths: { presets: ["essential"], custom: [{ path: longPath, level: "all" }] },
      },
    });
    const app = await open(client, 60, 40);
    let lines = screen(app.stdout.text(), 40);
    for (const line of lines) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    expect(lines.join("\n")).toContain("필수 안전 장치");
    const flat = (text: string) => text.replace(/[\s│╭╮╰╯─]/g, "");
    // The distinguishing end of a long pattern survives, wrapped.
    await press(app, DOWN);
    await press(app, DOWN);
    await press(app, DOWN);
    await press(app, DOWN);
    expect(flat(app.view())).toContain("--grace-period=0--wait=false");
    await press(app, TAB);
    for (let i = 0; i < 4; i += 1) await press(app, DOWN);
    lines = screen(app.stdout.text(), 40);
    for (const line of lines) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    expect(flat(app.view())).toContain("secrets-2026/credentials.json");
    app.done();
  });

  it("keeps the cursor row visible in a short terminal", async () => {
    const client = fakeClient({
      saved: {
        blockedCommands: { presets: ["essential"], custom: Array.from({ length: 12 }, (_, i) => `custom-command-${i + 1}`) },
        protectedPaths: { presets: [], custom: [] },
      },
    });
    const app = await open(client, 60, 18);
    for (let i = 0; i < 14; i += 1) await press(app, DOWN, 40);
    await sleep(100);
    expect(app.view()).toContain("› • custom-command-12");
    expect(app.view()).toContain("15/15");
    for (const line of screen(app.stdout.text(), 18)) expect(textWidth(line), line).toBeLessThanOrEqual(60);
    app.done();
  });
});

describe("Jarvis panel", () => {
  it("says the lists do not apply to an uncontrolled external session", async () => {
    const client = fakeClient({
      rows: [{ sessionId: "ext:claude:abc", project: "/work/p", title: "ext one", status: "running", level: "full", external: { cli: "claude", id: "abc" }, controlled: false }],
    });
    client.call = ((original) => async (method: string, params: any) => {
      if (method === "jarvis.status") return { enabled: true, running: true, folder: "/j", delegationDefault: "staged", sessionId: "s-1", model: "fast" };
      return original(method, params);
    })(client.call);
    const app = await mount(client, 100, 40);
    await sleep(500);
    expect(app.view()).toContain("UNCONTROLLED");
    expect(app.view()).toContain("security lists do not apply to this session");
    app.done();
  });
});
