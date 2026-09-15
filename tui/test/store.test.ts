import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

beforeEach(() => {
  __resetIdCounter();
});

describe("delta assembly", () => {
  it("joins message.delta chunks into one streaming assistant message", () => {
    const state = apply(
      initialState,
      event(1, "message.delta", { text: "Hel" }),
      event(2, "message.delta", { text: "lo, " }),
      event(3, "message.delta", { text: "world" }),
    );

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: "assistant",
      text: "Hello, world",
      streaming: true,
    });
    expect(state.lastSeq).toBe(3);
  });

  it("message.done closes the stream and takes the final text", () => {
    const state = apply(
      initialState,
      event(1, "message.delta", { text: "par" }),
      event(2, "message.done", { text: "partial then final", role: "assistant" }),
    );

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe("partial then final");
    expect(state.messages[0].streaming).toBe(false);
  });

  it("starts a new message after a completed one", () => {
    const state = apply(
      initialState,
      event(1, "message.delta", { text: "first" }),
      event(2, "message.done", {}),
      event(3, "message.delta", { text: "second" }),
    );

    expect(state.messages.map((m) => m.text)).toEqual(["first", "second"]);
    expect(state.timeline).toHaveLength(2);
  });

  it("turn.done stops any dangling stream", () => {
    const state = apply(
      initialState,
      event(1, "message.delta", { text: "x" }),
      event(2, "turn.done", { turnId: "t1", reason: "interrupted" }),
    );
    expect(state.messages[0].streaming).toBe(false);
    expect(state.turnActive).toBe(false);
  });
});

describe("tool calls, diffs and usage", () => {
  it("pairs tool.result with its tool.call by callId", () => {
    const state = apply(
      initialState,
      event(1, "tool.call", { callId: "c1", name: "read_file", args: { path: "a.txt" } }),
      event(2, "tool.result", { callId: "c1", name: "read_file", ok: true, output: "hi" }),
    );

    expect(state.toolCalls).toHaveLength(1);
    expect(state.toolCalls[0]).toMatchObject({ state: "ok", output: "hi", name: "read_file" });
  });

  it("marks a failed tool.result as an error", () => {
    const state = apply(
      initialState,
      event(1, "tool.call", { callId: "c1", name: "shell", args: { command: "false" } }),
      event(2, "tool.result", { callId: "c1", name: "shell", ok: false, error: "exit 1" }),
    );
    expect(state.toolCalls[0].state).toBe("error");
    expect(state.toolCalls[0].error).toBe("exit 1");
  });

  it("collects diffs, mode changes and accumulated usage", () => {
    const state = apply(
      initialState,
      event(1, "diff", { path: "src/a.ts", patch: "--- a\n+++ b\n+x" }),
      event(2, "mode.changed", { mode: "plan" }),
      event(3, "usage", { inputTokens: 100, outputTokens: 20 }),
      event(4, "usage", { inputTokens: 5, outputTokens: 2 }),
    );

    expect(state.diffs[0]).toMatchObject({ path: "src/a.ts" });
    expect(state.mode).toBe("plan");
    expect(state.usage).toEqual({ inputTokens: 105, outputTokens: 22 });
  });

  it("ignores unknown event kinds", () => {
    const state = apply(initialState, event(1, "subagent.spawn", { agentId: "a1" }));
    expect(state).toMatchObject({ lastSeq: 1, messages: [] });
  });
});

describe("approvals", () => {
  const request = {
    requestId: "req-1",
    sessionId: "sess-1",
    tool: "shell",
    args: { command: "rm -rf build" },
    risk: "high",
  };

  it("adds an interactive request and resolves it away", () => {
    let state = reducer(initialState, { type: "approval/request", request });
    expect(state.pendingApproval).toMatchObject({ requestId: "req-1", source: "interactive" });

    state = reducer(state, { type: "approval/resolved", requestId: "req-1" });
    expect(state.pendingApproval).toBeNull();
  });

  it("seeds the unattended queue from approval.list and removes resolved entries", () => {
    const queued = { ...request, requestId: "req-2" };
    let state = reducer(initialState, { type: "approval/list", requests: [request, queued] });
    expect(state.approvalQueue.map((r) => r.requestId)).toEqual(["req-1", "req-2"]);
    expect(state.approvalQueue.every((r) => r.source === "queue")).toBe(true);

    state = reducer(state, { type: "approval/resolved", requestId: "req-1" });
    expect(state.approvalQueue.map((r) => r.requestId)).toEqual(["req-2"]);
  });

  it("adds a queued request and drops it again on approval.resolved", () => {
    const queued = { ...request, requestId: "req-9", tool: "shell" };
    let state = reducer(initialState, { type: "approval/list", requests: [queued] });
    expect(state.approvalQueue).toHaveLength(1);
    expect(state.approvalQueue[0]).toMatchObject({ requestId: "req-9", source: "queue" });

    // The daemon broadcasts approval.resolved however the request was answered:
    // from this surface, from another client, or by the timeout.
    state = reducer(state, { type: "approval/resolved", requestId: "req-9" });
    expect(state.approvalQueue).toEqual([]);
    expect(state.pendingApproval).toBeNull();
  });

  it("ignores approval.resolved for a request it never saw", () => {
    const state = reducer(
      reducer(initialState, { type: "approval/list", requests: [request] }),
      { type: "approval/resolved", requestId: "not-mine" },
    );
    expect(state.approvalQueue.map((r) => r.requestId)).toEqual(["req-1"]);
  });

  it("keeps the interactive request out of the unattended queue", () => {
    let state = reducer(initialState, { type: "approval/request", request });
    state = reducer(state, {
      type: "approval/list",
      requests: [request, { ...request, requestId: "req-3" }],
    });
    expect(state.approvalQueue.map((r) => r.requestId)).toEqual(["req-3"]);
    expect(state.pendingApproval?.requestId).toBe("req-1");
  });
});

describe("local actions", () => {
  it("records the user's own message and marks the turn active", () => {
    const state = reducer(initialState, { type: "user/message", text: "hi" });
    expect(state.messages[0]).toMatchObject({ role: "user", text: "hi", streaming: false });
    expect(state.turnActive).toBe(true);
  });

  it("tracks connection status and session metadata", () => {
    let state = reducer(initialState, { type: "status", status: "reconnecting" });
    expect(state.status).toBe("reconnecting");
    state = reducer(state, {
      type: "session/ready",
      sessionId: "sess-1",
      mode: "auto",
      provider: "openai",
      model: "gpt-4o",
    });
    expect(state).toMatchObject({ sessionId: "sess-1", mode: "auto", provider: "openai" });
  });
});

describe("subagent tree", () => {
  it("spawn, update and done build one entry per delegated child", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", {
        agentId: "a-1",
        name: "reader",
        task: "read the config",
        status: "queued",
        sessionId: "sess-child",
      }),
      event(2, "subagent.update", { agentId: "a-1", status: "running", lastText: "calling grep" }),
      event(3, "subagent.done", {
        agentId: "a-1",
        status: "done",
        ok: true,
        summary: "the config sets three ports",
        usage: { inputTokens: 120, outputTokens: 40 },
      }),
    );

    expect(state.subagents).toHaveLength(1);
    expect(state.subagents[0]).toMatchObject({
      agentId: "a-1",
      name: "reader",
      task: "read the config",
      status: "done",
      summary: "the config sets three ports",
      sessionId: "sess-child",
      inputTokens: 120,
      outputTokens: 40,
    });
    expect(state.subagents[0].lastText).toBe("");
  });

  it("keeps several children in spawn order and tracks them separately", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", { agentId: "a-1", task: "first" }),
      event(2, "subagent.spawn", { agentId: "a-2", task: "second" }),
      event(3, "subagent.update", { agentId: "a-2", status: "running", lastText: "working" }),
    );

    expect(state.subagents.map((s) => s.agentId)).toEqual(["a-1", "a-2"]);
    expect(state.subagents[0].status).toBe("queued");
    expect(state.subagents[1]).toMatchObject({ status: "running", lastText: "working" });
  });

  it("tracks a running child's usage as its updates arrive", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", { agentId: "a-1", task: "work", status: "running" }),
      event(2, "subagent.update", {
        agentId: "a-1",
        status: "running",
        usage: { inputTokens: 12_000, outputTokens: 300 },
      }),
    );
    expect(state.subagents[0]).toMatchObject({
      status: "running",
      inputTokens: 12_000,
      outputTokens: 300,
    });

    // An update without usage leaves the counters where they were.
    const later = apply(state, event(3, "subagent.update", { agentId: "a-1", lastText: "still" }));
    expect(later.subagents[0]).toMatchObject({ inputTokens: 12_000, outputTokens: 300 });
  });

  it("ignores a replayed spawn and an update for an unknown child", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", { agentId: "a-1", task: "only once" }),
      event(2, "subagent.spawn", { agentId: "a-1", task: "only once" }),
      event(3, "subagent.update", { agentId: "a-ghost", status: "running" }),
    );

    expect(state.subagents).toHaveLength(1);
  });

  it("reads the older ok/result spelling of subagent.done", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", { agentId: "a-1", task: "legacy" }),
      event(2, "subagent.done", { agentId: "a-1", ok: false, result: "it broke" }),
    );

    expect(state.subagents[0]).toMatchObject({ status: "error", summary: "it broke" });
  });
});
