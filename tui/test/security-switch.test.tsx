/**
 * The security switch on `/security` (1.58.0 `securityMasterSwitch`, 1.66.0
 * `securityDefaultOff`): its row above the unknown-code row, the keys, the one
 * neutral question before turning the checks off, the dimmed rows while off,
 * and older daemons. The daemon is a fake; the shapes are `sdk/src/protocol.ts`.
 */

import React from "react";
import { render } from "ink";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env["FORCE_COLOR"] = "1";
});

import { App } from "../src/app.js";
import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import { enabledLabel, normalizeConfig, offConfirmLines, statusLines } from "../src/state/security.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const DISABLED_NOTE =
  "security checks are off (security.enabled: false): blocked commands and protected paths are not applied and code snowpea cannot read ahead is not asked about; snowpea's own controls and the mode rules still apply";

const ALL_CAPS = ["securityLists", "securityMasterSwitch", "securityDefaultOff"];

/** `security.get` as core sends it with the checks off. */
function offState(byDefault: boolean | undefined): Record<string, unknown> {
  return {
    enabled: false,
    ...(byDefault === undefined ? {} : { byDefault }),
    disabledBy: byDefault ? null : "tui",
    disabledAt: byDefault ? null : "2026-11-19T00:00:00Z",
    effective: { commands: [], paths: [] },
    problems: ["Security checks are off.", "core skipped one"],
    problemItems: [
      { list: null, entry: null, reason: DISABLED_NOTE, code: "security-disabled" },
      { list: "commands", entry: "bad[", reason: "core skipped one" },
    ],
  };
}

function fakeClient(caps: string[], security: Record<string, unknown> = {}) {
  let listeners: any = {};
  const calls: Array<{ method: string; params: any }> = [];
  let state: Record<string, unknown> = {
    blockedCommands: { presets: ["essential"], custom: [] },
    protectedPaths: { presets: [], custom: [] },
    effective: { commands: [{ pattern: "rm -rf ~", by: "commands:essential" }], paths: [] },
    writable: true,
    problems: [],
    project: null,
    unknownCode: "auto",
    ...security,
  };
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
    checkUpdate: async () => ({ available: false, current: "0.2.99", latest: "0.2.99" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      switch (method) {
        case "security.presets.list":
          return { locale: "en", commands: [{ id: "essential", title: "Essential safety", description: "d", examples: ["rm -rf ~"], default: true }], paths: [] };
        case "security.get":
          return state;
        case "settings.set": {
          const enabled = params.patch?.security?.enabled;
          if (enabled === true) {
            state = { ...state, enabled: true, byDefault: false, disabledBy: null, disabledAt: null, problems: [], problemItems: [], effective: { commands: [{ pattern: "rm -rf ~", by: "commands:essential" }], paths: [] } };
          } else if (enabled === false) {
            state = { ...state, ...offState(false), problems: ["Security checks are off."], problemItems: [{ list: null, entry: null, reason: DISABLED_NOTE, code: "security-disabled" }] };
          }
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
    fire: (name: string, payload: unknown) => listeners[name]?.(payload),
  };
  return client;
}

async function open(client: any, columns = 110, rows = 44) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/tmp/project" />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(200);
  await type(stdin, "/security ", 8);
  stdin.write("\r");
  await sleep(400);
  const view = () => screen(stdout.text(), rows).join("\n");
  return { stdin, stdout, view, done: () => { instance.unmount(); stdin.end(); } };
}

const UP = "\x1b[A";
const DOWN = "\x1b[B";
const RIGHT = "\x1b[C";
const LEFT = "\x1b[D";
const sets = (client: any) => client.calls.filter((c: any) => c.method === "settings.set");
// eslint-disable-next-line no-control-regex
const plain = (text: string) => text.replace(/\x1b\[[0-9;]*m/g, "");

/** The last drawn line of the raw output holding `needle`, escapes and all. */
function rawLine(out: string, needle: string): string {
  const at = out.lastIndexOf(needle);
  if (at < 0) return "";
  const to = out.indexOf("\n", at);
  return out.slice(out.lastIndexOf("\n", at) + 1, to < 0 ? undefined : to);
}

afterEach(() => resetUiLanguage());

describe("security.get with the checks off: normalised", () => {
  it("reads enabled and byDefault, and drops the security-disabled item and its line", () => {
    const config = normalizeConfig(offState(true) as never);
    expect(config.enabled).toBe(false);
    expect(config.offByDefault).toBe(true);
    expect(config.problemItems.map((item) => item.reason)).toEqual(["core skipped one"]);
    expect(config.notices).toEqual([]);
    expect(config.problems.join(" ")).not.toContain("security checks are off");
  });

  it("an older core: no enabled means on, no byDefault means not by default", () => {
    expect(normalizeConfig({} as never)).toMatchObject({ enabled: true, offByDefault: false });
    const config = normalizeConfig(offState(undefined) as never);
    expect(config).toMatchObject({ enabled: false, offByDefault: false });
  });

  it("labels in both languages", () => {
    expect(enabledLabel({ enabled: true, offByDefault: false })).toBe("Security checks: on");
    expect(enabledLabel({ enabled: false, offByDefault: true })).toBe("Security checks: off (default)");
    expect(enabledLabel({ enabled: false, offByDefault: false })).toBe("Security checks: off");
    setUiLanguage("ko");
    expect(enabledLabel({ enabled: true, offByDefault: false })).toBe("보안 검사: 켜짐");
    expect(enabledLabel({ enabled: false, offByDefault: true })).toBe("보안 검사: 꺼짐 (기본값)");
    expect(enabledLabel({ enabled: false, offByDefault: false })).toBe("보안 검사: 꺼짐");
  });

  it("the off question names what stops applying; the token block only for a 1.66 core", () => {
    expect(offConfirmLines(true).join(" ")).toContain("the block on reading stored tokens and logins");
    expect(offConfirmLines(false).join(" ")).not.toContain("tokens");
    for (const line of offConfirmLines(true)) expect(line).not.toMatch(/danger|warning|unsafe|risk/i);
  });

  it("/security status says the switch state with the switch, nothing without", () => {
    const config = normalizeConfig(offState(true) as never);
    const lines = statusLines(config, null, null, true);
    expect(lines).toContain("Security checks: off (default)");
    expect(lines.join("\n")).not.toMatch(/^! .*security checks are off/im);
    expect(statusLines(normalizeConfig({} as never), null, null, false).join("\n")).not.toContain("Security checks:");
  });
});

describe("/security: the switch row", () => {
  it("off by default: the switch is the first row and selected, the rest dimmed with a neutral note, no warning", async () => {
    const client = fakeClient(ALL_CAPS, offState(true));
    const app = await open(client);
    const view = app.view();
    const text = plain(view);
    expect(text).toContain("› Security checks: off (default)");
    expect(text.indexOf("Security checks: off")).toBeLessThan(text.indexOf("Commands and scripts snowpea can't read"));
    expect(text).toContain("The lists and the choice above apply once security checks are on.");
    expect(text).not.toMatch(/security checks are off \(security\.enabled/);
    expect(text).not.toMatch(/! .*(security checks|disabled)/i);
    // A real skipped entry is still said.
    expect(text).toContain("core skipped an unusable entry");
    expect(text).toContain("←→ / Space turn on or off");
    // Dimmed: the preset row and the unknown-code value carry no colour (no green [x], no green [auto]).
    const presetLine = rawLine(app.stdout.text(), "Essential safety");
    expect(presetLine).toContain("\x1b[2m");
    expect(presetLine).not.toContain("\x1b[32m");
    const unknownLine = rawLine(app.stdout.text(), "Commands and scripts");
    expect(unknownLine).toContain("\x1b[2m");
    expect(unknownLine).not.toContain("\x1b[32m");
    app.done();
  });

  it("turning on asks nothing and sends one global settings.set {enabled: true}", async () => {
    const client = fakeClient(ALL_CAPS, offState(true));
    const app = await open(client);
    app.stdin.write(" ");
    await sleep(400);
    expect(sets(client)).toHaveLength(1);
    expect(sets(client)[0].params).toEqual({ scope: "global", patch: { security: { enabled: true } } });
    const text = plain(app.view());
    expect(text).toContain("Security checks: on");
    expect(text).not.toContain("apply once security checks are on");
    expect(text).toContain("Security checks are on.");
    // No longer dimmed: the preset's [x] is green again.
    expect(rawLine(app.stdout.text(), "Essential safety")).toContain("\x1b[32m");
    app.done();
  });

  it("turning off asks once, neutrally, listing what stops applying; n keeps it on, y sends enabled: false", async () => {
    const client = fakeClient(ALL_CAPS, { enabled: true, byDefault: false });
    const app = await open(client);
    // On: the cursor starts on the list; ↑ reaches the unknown-code row, ↑ again the switch.
    expect(plain(app.view())).toContain("› [x] Essential safety");
    app.stdin.write(UP);
    await sleep(100);
    expect(plain(app.view())).toContain("› Commands and scripts");
    app.stdin.write(UP);
    await sleep(100);
    expect(plain(app.view())).toContain("› Security checks: on");
    app.stdin.write(RIGHT);
    await sleep(150);
    let view = app.view();
    expect(plain(view)).toContain("Turn security checks off?");
    expect(plain(view)).toContain("these are not applied: blocked commands, protected folders, asking about commands");
    expect(plain(view)).toContain("stored tokens and logins");
    // Neutral: the question is not drawn in yellow.
    const ask = rawLine(app.stdout.text(), "Turn security checks off?");
    expect(ask).not.toBe("");
    expect(ask).not.toContain("\x1b[33m");
    expect(sets(client)).toHaveLength(0);
    app.stdin.write("n");
    await sleep(150);
    expect(sets(client)).toHaveLength(0);
    expect(plain(app.view())).toContain("Security checks: on");
    app.stdin.write("\r");
    await sleep(150);
    expect(plain(app.view())).toContain("Turn security checks off?");
    app.stdin.write("y");
    await sleep(400);
    expect(sets(client)[0].params).toEqual({ scope: "global", patch: { security: { enabled: false } } });
    view = plain(app.view());
    // Chosen off: just "off", not "(default)".
    expect(view).toContain("› Security checks: off");
    expect(view).not.toContain("off (default)");
    expect(view).toContain("apply once security checks are on");
    app.done();
  });

  it("↓ from the switch goes to the unknown-code row, then the list; ↑ from the switch wraps to the last list row", async () => {
    const client = fakeClient(ALL_CAPS, offState(true));
    const app = await open(client);
    app.stdin.write(DOWN);
    await sleep(100);
    expect(plain(app.view())).toContain("› Commands and scripts");
    app.stdin.write(LEFT);
    await sleep(300);
    // ←→ on the unknown-code row still change its value (stored for when checks are on).
    expect(sets(client)[0].params.patch).toEqual({ security: { unknownCode: "ask" } });
    app.stdin.write(DOWN);
    await sleep(100);
    expect(plain(app.view())).toContain("› [x] Essential safety");
    app.stdin.write(DOWN);
    await sleep(100);
    expect(plain(app.view())).toContain("› Security checks");
    app.stdin.write(UP);
    await sleep(100);
    expect(plain(app.view())).toContain("› [x] Essential safety");
    app.done();
  });

  it("? help explains the checks in detail", async () => {
    const app = await open(fakeClient(ALL_CAPS, offState(true)));
    app.stdin.write("?");
    await sleep(150);
    const text = plain(app.view());
    expect(text).toContain("Security checks");
    expect(text).toContain("They are off unless you turn them on");
    expect(text).toContain("Stored tokens and logins");
    app.done();
  });

  it("a read-only connection sees the switch but cannot flip it", async () => {
    const client = fakeClient(ALL_CAPS, { ...offState(true), writable: false });
    const app = await open(client);
    app.stdin.write(" ");
    await sleep(200);
    expect(sets(client)).toHaveLength(0);
    expect(plain(app.view())).toContain("Read-only");
    app.done();
  });
});

describe("/security against older daemons", () => {
  it("1.58–1.65 (no securityDefaultOff): the switch shows, off reads just \"off\", the question leaves out tokens", async () => {
    const client = fakeClient(["securityLists", "securityMasterSwitch"], offState(undefined));
    const app = await open(client);
    let text = plain(app.view());
    expect(text).toContain("› Security checks: off");
    expect(text).not.toContain("(default)");
    app.stdin.write(" ");
    await sleep(400);
    expect(sets(client)[0].params.patch).toEqual({ security: { enabled: true } });
    app.stdin.write(" ");
    await sleep(150);
    text = plain(app.view());
    expect(text).toContain("Turn security checks off?");
    expect(text).not.toContain("tokens");
    app.done();
  });

  it("before 1.58 (no securityMasterSwitch): no switch row, nothing dimmed, enabled never sent", async () => {
    const client = fakeClient(["securityLists"]);
    const app = await open(client);
    const text = plain(app.view());
    expect(text).not.toContain("Security checks:");
    expect(text).not.toContain("apply once security checks are on");
    // The cursor starts on the list; ↑ reaches the unknown-code row as before.
    expect(text).toContain("› [x] Essential safety");
    app.stdin.write(UP);
    await sleep(100);
    app.stdin.write(UP);
    await sleep(100);
    expect(plain(app.view())).toContain("› [x] Essential safety");
    app.stdin.write(" ");
    await sleep(100);
    expect(client.calls.some((c: any) => c.method === "settings.set" && c.params.patch?.security?.enabled !== undefined)).toBe(false);
    app.done();
  });
});
