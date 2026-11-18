/**
 * `/feedback` (alias `/개선하기`, capability `improvementFeedback`, 1.59.0)
 * through the real App and a real Ink render against a fake connection. The
 * fake answers `feedback.preview` / `feedback.send` the way the contract says;
 * nothing here reaches a network.
 */

import React from "react";
import { render } from "ink";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env["FORCE_COLOR"] = "1";
});

import { App } from "../src/app.js";
import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import {
  FEEDBACK_CONSENT,
  FEEDBACK_CONSENT_KO,
  feedbackErrorLine,
  normalizePreview,
  parseFeedbackCommand,
  previewMessageLines,
  previewSummary,
} from "../src/state/feedback.js";
import { SlashRegistry, setSurfaceHidden } from "../src/slash/registry.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

afterEach(() => {
  resetUiLanguage();
  setSurfaceHidden([]);
});

const CAPS = ["improvementFeedback"];

function preview() {
  return {
    report: {
      schema: 1,
      kind: "improvement",
      session: {
        id: "s-1",
        messages: [
          { id: "m_1", role: "user", text: "the build fails\nsecond line" },
          { id: "m_2", role: "tool", tool: { name: "shell", status: "error" } },
          { id: "m_3", role: "assistant", text: "Fixed by pinning node 20." },
        ],
      },
    },
    stats: { messages: 3, toolMessages: 1, rawBytes: 2048, redactions: 2, droppedResults: 0 },
    endpoint: "http://127.0.0.1:9/api/feedback",
  };
}

type Failure = { code: string; message: string; details?: Record<string, unknown> };

function fakeClient(opts: { caps?: string[]; sendFail?: Failure; previewFail?: Failure } = {}) {
  const caps = opts.caps ?? CAPS;
  const calls: Array<{ method: string; params: any }> = [];
  const fail = (f: Failure) => Object.assign(new Error(f.message), { data: { code: f.code, details: f.details } });
  const client: any = {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => caps,
    setListeners: () => undefined,
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.91", latest: "0.2.91" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      switch (method) {
        case "feedback.preview":
          if (opts.previewFail) throw fail(opts.previewFail);
          return preview();
        case "feedback.send":
          if (opts.sendFail) throw fail(opts.sendFail);
          return { id: "0123456789abcdef01234567", receivedAt: "2026-11-18T01:00:00Z" };
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
  };
  return client;
}

async function mount(client: any, columns = 80, rows = 40) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/work/p" />, {
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
  await sleep(300);
}

async function press(app: { stdin: any }, key: string, wait = 150) {
  app.stdin.write(key);
  await sleep(wait);
}

const ESC = "\x1b";
const DOWN = "\x1b[B";
const of = (client: any, method: string) => client.calls.filter((call: any) => call.method === method);

describe("pure parts", () => {
  it("parses /feedback and the Korean alias, with an optional first comment", () => {
    expect(parseFeedbackCommand("/feedback")).toEqual({ kind: "enter", comment: "" });
    expect(parseFeedbackCommand("/개선하기 너무 느려요")).toEqual({ kind: "enter", comment: "너무 느려요" });
    expect(parseFeedbackCommand("/feedbacks")).toBeNull();
    expect(parseFeedbackCommand("feedback")).toBeNull();
  });

  it("keeps the consent line word for word in Korean", () => {
    expect(FEEDBACK_CONSENT_KO).toBe("보내기를 누르면 이 대화가 Snowpea 팀에 전달되고, 에이전트를 더 낫게 만드는 데 쓰여요.");
    setUiLanguage("ko");
    expect(FEEDBACK_CONSENT()).toBe(FEEDBACK_CONSENT_KO);
  });

  it("summarises counts, masking and the endpoint, and hides tool rows", () => {
    const view = normalizePreview(preview());
    const summary = previewSummary(view).join("\n");
    expect(summary).toContain("3 messages (1 tool calls");
    expect(summary).toContain("(2 places)");
    expect(summary).toContain("http://127.0.0.1:9/api/feedback");
    const lines = previewMessageLines(view, 40);
    expect(lines).toEqual(["you: the build fails", "agent: Fixed by pinning node 20."]);
    expect(previewMessageLines(view, 12)[1]).toBe("agent: Fixe…");
  });

  it("words each error, with retry-after for busy", () => {
    const err = (code: string, details: Record<string, unknown>) => ({ message: "x", data: { code, details } });
    expect(feedbackErrorLine(err("feedback_busy", { status: 429, retryAfterSec: 120 }))).toContain("Try again in 120 s");
    expect(feedbackErrorLine(err("feedback_rejected", { status: 400, serverMessage: "bad role" }))).toContain("HTTP 400): bad role");
    expect(feedbackErrorLine(err("feedback_unreachable", { status: 0 }))).toContain("Nothing was sent");
    expect(feedbackErrorLine(err("jarvis_person_only", {}))).toContain("person");
  });

  it("leaves /feedback out of the palette when hidden", async () => {
    const registry = new SlashRegistry({ call: async () => ({ commands: [] }) });
    expect((await registry.load()).some((c) => c.name === "feedback")).toBe(true);
    setSurfaceHidden(["feedback"]);
    expect((await registry.load()).some((c) => c.name === "feedback")).toBe(false);
  });
});

describe("an older daemon", () => {
  it("does not offer /feedback in the palette", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await type(app.stdin, "/feedb", 20);
    await sleep(150);
    expect(app.view()).not.toContain("Send this conversation to the Snowpea team");
    app.done();
  });

  it("answers a typed /feedback with one line, calling nothing", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await submit(app, "/feedback");
    expect(app.view()).toContain("requires a newer core");
    expect(client.calls.filter((c: any) => c.method.startsWith("feedback."))).toEqual([]);
    app.done();
  });
});

describe("/feedback", () => {
  it("shows the preview, takes a comment and sends only after Send", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/feedback");
    const shown = app.view();
    expect(of(client, "feedback.preview")).toEqual([{ method: "feedback.preview", params: { sessionId: "s-1" } }]);
    expect(shown).toContain("3 messages");
    expect(shown).toContain("http://127.0.0.1:9/api/feedback");
    expect(shown).toContain("you: the build fails");
    expect(shown).not.toContain("second line");
    expect(shown).toContain("Pressing Send passes this conversation to the Snowpea team");
    await type(app.stdin, "too slow", 15);
    await sleep(100);
    expect(of(client, "feedback.send")).toEqual([]);
    await press(app, "\r", 200);
    expect(app.view()).toContain("Send");
    expect(of(client, "feedback.send")).toEqual([]);
    await press(app, "\r", 300);
    const sends = of(client, "feedback.send");
    expect(sends).toHaveLength(1);
    expect(sends[0].params).toMatchObject({ sessionId: "s-1", surface: "tui", comment: "too slow" });
    expect(typeof sends[0].params.surfaceVersion).toBe("string");
    expect(app.view()).toContain("0123456789abcdef01234567");
    app.done();
  });

  it("closes on Esc or Cancel without sending", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/feedback");
    await press(app, ESC, 250);
    expect(app.view()).not.toContain("Pressing Send passes");
    await submit(app, "/개선하기");
    await press(app, "\r");
    await press(app, DOWN);
    await press(app, DOWN);
    await press(app, "\r", 250);
    expect(app.view()).not.toContain("Pressing Send passes");
    expect(of(client, "feedback.preview")).toHaveLength(2);
    expect(of(client, "feedback.send")).toEqual([]);
    app.done();
  });

  it("shows when to try again when the server is busy", async () => {
    const client = fakeClient({
      sendFail: { code: "feedback_busy", message: "feedback was not sent", details: { status: 503, retryAfterSec: 30 } },
    });
    const app = await mount(client);
    await submit(app, "/feedback");
    await press(app, "\r");
    await press(app, "\r", 300);
    expect(app.view()).toContain("Try again in 30 s");
    app.done();
  });

  it("says why when the preview is refused", async () => {
    const client = fakeClient({ previewFail: { code: "not_found", message: "no such session: s-1" } });
    const app = await mount(client);
    await submit(app, "/feedback");
    expect(app.view()).toContain("not saved yet");
    app.done();
  });
});
