/**
 * "Allow Everything" while planning (1.32.0): the session keeps planning with
 * every approval answered yes, and the HUD says it will end in auto.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/app.js";
import {
  APPROVAL_OPTIONS,
  PLAN_ALLOW_EVERYTHING,
  approvalOptions,
} from "../src/components/ApprovalPrompt.js";
import {
  QUESTION_MAX_LINES,
  clampQuestion,
  questionPromptRows,
} from "../src/components/QuestionPrompt.js";
import { buildHudSegments, type HudInput } from "../src/layout/hud.js";
import type { ApprovalRequestParams, ApprovalResponse, SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer, type State } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

function fakeClient() {
  let onApproval: ((request: ApprovalRequestParams) => Promise<ApprovalResponse>) | undefined;
  return {
    getStatus: () => "connected",
    setListeners: () => undefined,
    onApprovalRequest: (handler: any) => {
      onApproval = handler;
    },
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: vi.fn(async (_sessionId: string, mode: string, extra?: { autoAfterPlan?: boolean }) => ({
      mode,
      autoAfterPlan: extra?.autoAfterPlan === true,
    })),
    respondApproval: async () => undefined,
    ask(): Promise<ApprovalResponse> {
      return onApproval!({
        requestId: "req-1",
        tool: "shell",
        risk: "high",
        args: { command: "npm test" },
      } as ApprovalRequestParams);
    },
  };
}

async function pressAllowEverything(mode: "plan" | "accept") {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode={mode} workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(120);
  const answer = client.ask();
  await sleep(120);
  const shown = stdout.text();
  stdin.write("e");
  const decided = await answer;
  await sleep(80);
  instance.unmount();
  return { client, shown, decided };
}

describe("Allow Everything in plan mode", () => {
  it("names what it does in plan mode and keeps its shortcut", () => {
    const row = approvalOptions("plan").find((option) => option.value.switchToAuto);
    expect(row?.label).toBe(PLAN_ALLOW_EVERYTHING);
    expect(row?.shortcut).toBe("e");
    expect(approvalOptions("accept")).toBe(APPROVAL_OPTIONS);
  });

  it("keeps planning with approvals answered yes instead of switching to auto", async () => {
    const { client, shown, decided } = await pressAllowEverything("plan");
    expect(shown).toContain("Allow Everything (plan, then Auto)");
    expect(decided).toEqual({ decision: "allow", scope: "once" });
    expect(client.setMode).toHaveBeenCalledWith("sess-1", "plan", { autoAfterPlan: true });
  });

  it("still switches to auto outside plan mode", async () => {
    const { client, shown } = await pressAllowEverything("accept");
    expect(shown).toContain("Allow Everything (Auto mode)");
    expect(client.setMode).toHaveBeenCalledWith("sess-1", "auto");
  });
});

describe("the plan-then-auto flag in the store", () => {
  it("follows mode.changed and clears on leaving plan", () => {
    let state = apply(initialState, event(1, "mode.changed", { mode: "plan", autoAfterPlan: true }));
    expect(state).toMatchObject({ mode: "plan", autoAfterPlan: true });
    // An event that does not carry it (a /delegation toggle) keeps it.
    state = apply(state, event(2, "mode.changed", { mode: "plan", delegation: true }));
    expect(state.autoAfterPlan).toBe(true);
    state = apply(state, event(3, "mode.changed", { mode: "auto" }));
    expect(state).toMatchObject({ mode: "auto", autoAfterPlan: false });
  });

  it("is set by the optimistic mode action and cleared by Shift+Tab's", () => {
    let state = reducer(initialState, { type: "mode", mode: "plan", autoAfterPlan: true });
    expect(state.autoAfterPlan).toBe(true);
    state = reducer(state, { type: "mode", mode: "accept" });
    expect(state.autoAfterPlan).toBe(false);
    state = reducer(state, { type: "mode", mode: "plan" });
    expect(state.autoAfterPlan).toBe(false);
  });
});

describe("the HUD mode segment", () => {
  const base: HudInput = {
    status: "connected",
    version: "0.1.2",
    workdir: "/tmp/project",
    sessionId: "s-1",
    latestVersion: null,
    provider: "local",
    model: "m",
    mode: "plan",
    usage: { inputTokens: 0, outputTokens: 0 },
    sessionMs: 0,
    daemonPid: 1,
    daemonSummary: "",
    pendingApprovals: 0,
  };
  const mode = (input: HudInput) =>
    buildHudSegments(input).find((segment) => segment.key === "mode")?.text;

  it("says PLAN→AUTO only for a flagged plan", () => {
    expect(mode(base)).toBe("Mode: PLAN");
    expect(mode({ ...base, autoAfterPlan: true })).toBe("Mode: PLAN→AUTO");
    expect(mode({ ...base, mode: "accept", autoAfterPlan: true })).toBe("Mode: ACCEPT");
  });
});

describe("the plan set_mode puts up for review", () => {
  it("lands before the running set_mode card, so it can leave the live region", () => {
    let state = apply(
      initialState,
      event(1, "tool.call", { callId: "c1", name: "set_mode", args: { mode: "auto" } }),
      event(2, "message.done", { text: "# Plan\n\n1. mesher", role: "assistant", kind: "plan" }),
    );
    expect(state.timeline.map((item) => item.kind)).toEqual(["message", "tool"]);
    expect(state.messages.at(-1)).toMatchObject({ text: "# Plan\n\n1. mesher", streaming: false });
    // A parallel call still running before set_mode: the plan goes before it too.
    const parallel = apply(
      initialState,
      event(1, "tool.call", { callId: "c0", name: "shell", args: {} }),
      event(2, "tool.call", { callId: "c1", name: "set_mode", args: {} }),
      event(3, "tool.result", { callId: "c1", name: "set_mode", ok: true, output: "" }),
      event(4, "message.done", { text: "# Plan", role: "assistant", kind: "plan" }),
    );
    expect(parallel.timeline.map((item) => item.kind)).toEqual(["message", "tool", "tool"]);
    // A plan with nothing running is simply appended.
    state = apply(
      initialState,
      event(1, "message.done", { text: "# Plan", role: "assistant", kind: "plan" }),
    );
    expect(state.timeline.map((item) => item.kind)).toEqual(["message"]);
  });

  it("resets the flag when another session becomes the main one", () => {
    const state = reducer(
      reducer(initialState, { type: "mode", mode: "plan", autoAfterPlan: true }),
      { type: "session/ready", sessionId: "other", mode: "plan" },
    );
    expect(state.autoAfterPlan).toBe(false);
  });
});

describe("a long question", () => {
  it("is clamped and counted with its wrapped lines", () => {
    const long = Array.from({ length: 30 }, (_, n) => `line ${n}`).join("\n");
    const clamped = clampQuestion(long);
    expect(clamped.split("\n")).toHaveLength(QUESTION_MAX_LINES);
    expect(clamped).toContain("+23 lines");
    const entry = {
      requestId: "q",
      questions: [{ header: "h", question: "x".repeat(200), options: [], allowOther: true }],
    } as any;
    const short = { ...entry, questions: [{ ...entry.questions[0], question: "x" }] };
    expect(questionPromptRows(entry, 54) - questionPromptRows(short, 54)).toBe(3);
  });
});
