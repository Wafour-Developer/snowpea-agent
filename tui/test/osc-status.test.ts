import { describe, expect, it } from "vitest";

import { OscEmitter, deriveStatus, oscEnabled, oscSequence } from "../src/util/osc-status.js";

async function index() {
  process.env.SNOWPEA_TUI_NO_AUTORUN = "1";
  return import("../src/index.js");
}

const idle = { turnActive: false, permissionPending: false, questionPending: false };

describe("OSC 7501 status", () => {
  it("maps turn and approval state", () => {
    expect(deriveStatus(idle, null)).toEqual({ state: "idle" });
    expect(deriveStatus({ ...idle, turnActive: true }, null)).toEqual({ state: "working" });
    expect(deriveStatus({ ...idle, turnActive: true, permissionPending: true }, null)).toEqual({
      state: "blocked",
      kind: "permission",
    });
    expect(deriveStatus({ ...idle, questionPending: true }, null)).toEqual({
      state: "blocked",
      kind: "question",
    });
    expect(deriveStatus(idle, { state: "working" })).toEqual({ state: "done" });
  });

  it("formats the grammar as the root record (no id)", () => {
    expect(oscSequence({ state: "blocked", kind: "permission" })).toBe(
      "\u001b]7501;state=blocked:kind=permission:app=snowpea\u0007",
    );
  });

  it("emits only on change and clears on exit", () => {
    const out: string[] = [];
    const osc = new OscEmitter((c) => out.push(c), true);
    osc.update(idle);
    osc.update(idle);
    osc.update({ ...idle, turnActive: true });
    osc.update({ ...idle, turnActive: true, permissionPending: true });
    osc.update({ ...idle, turnActive: true });
    osc.update(idle);
    osc.clear();
    osc.clear();
    const states = out.map((c) => /state=(\w+)/.exec(c)![1]);
    expect(states).toEqual(["idle", "working", "blocked", "working", "done", "clear"]);
  });

  it("is silent when disabled", () => {
    const out: string[] = [];
    const osc = new OscEmitter((c) => out.push(c), false);
    osc.update({ ...idle, turnActive: true });
    osc.clear();
    expect(out).toEqual([]);
  });

  it("needs a TTY and an opt-in", () => {
    expect(oscEnabled({ isTTY: true }, {})).toBe(false);
    expect(oscEnabled({ isTTY: true }, { SNOWPEA_TERMINAL_ID: "t1" })).toBe(true);
    expect(oscEnabled({ isTTY: true }, { SNOWPEA_OSC_STATUS: "1" })).toBe(true);
    expect(oscEnabled({ isTTY: false }, { SNOWPEA_TERMINAL_ID: "t1" })).toBe(false);
  });
});

describe("--session attach", () => {
  it("parses the flag", async () => {
    const { parseArgs } = await index();
    expect(parseArgs(["--port", "1", "--token", "t", "--session", "s-9"], "/w", {}).session).toBe("s-9");
    expect(parseArgs(["--port", "1", "--token", "t"], "/w", {}).session).toBeUndefined();
  });

  it("resumes from the start and returns the replay", async () => {
    const { attachSession } = await index();
    const calls: unknown[] = [];
    const client = {
      call: async (m: string, p: unknown) => {
        calls.push([m, p]);
        return { events: [{ seq: 1 }], mode: "plan", delegation: true };
      },
    };
    const out = await attachSession(client as never, "abc");
    expect(calls).toEqual([["session.resume", { sessionId: "abc", afterSeq: 0 }]]);
    expect(out.sessionId).toBe("abc");
    expect(out.attached).toMatchObject({ mode: "plan", delegation: true, events: [{ seq: 1 }] });
  });

  it("reports a missing session clearly", async () => {
    const { AttachError, attachSession } = await index();
    const client = {
      call: async () => {
        throw Object.assign(new Error("unknown session"), { code: -32004, data: { code: "session_not_found" } });
      },
    };
    await expect(attachSession(client as never, "nope")).rejects.toBeInstanceOf(AttachError);
    await expect(attachSession(client as never, "nope")).rejects.toThrow(/session nope not found/);
  });

  it("passes a lost connection through", async () => {
    const { AttachError, attachSession } = await index();
    const client = { call: async () => Promise.reject(new Error("socket closed")) };
    await expect(attachSession(client as never, "x")).rejects.not.toBeInstanceOf(AttachError);
  });
});

describe("token from the environment", () => {
  it("reads SNOWPEA_TUI_TOKEN and removes it; --token still works", async () => {
    const { parseArgs } = await index();
    const env: NodeJS.ProcessEnv = { SNOWPEA_TUI_TOKEN: "secret" };
    expect(parseArgs(["--port", "1"], "/w", env).token).toBe("secret");
    expect(env.SNOWPEA_TUI_TOKEN).toBeUndefined();
    expect(parseArgs(["--port", "1", "--token", "old"], "/w", {}).token).toBe("old");
    expect(() => parseArgs(["--port", "1"], "/w", {})).toThrow(/token/);
  });
});
