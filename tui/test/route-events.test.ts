import { describe, expect, it } from "vitest";

import { initialJarvis, jarvisReducer, personOnlyView } from "../src/state/jarvis.js";
import { initialState, reducer, type State } from "../src/state/store.js";

const approval = (patch: Record<string, unknown> = {}): any => ({
  requestId: "a-1",
  sessionId: "s-1",
  tool: "shell",
  args: { command: "pytest -q" },
  risk: "low",
  route: "person",
  ...patch,
});
const question = (patch: Record<string, unknown> = {}): any => ({
  requestId: "q-1",
  sessionId: "s-1",
  route: "person",
  questions: [{ question: "Which database?" }],
  ...patch,
});

describe("approval.route.changed in the store", () => {
  it("withdraws the prompt on screen when Jarvis takes the request, and brings it back", () => {
    let state: State = reducer(initialState, { type: "approval/request", request: approval() });
    expect(state.pendingApproval?.requestId).toBe("a-1");
    state = reducer(state, { type: "approval/route", requestId: "a-1", route: "jarvis" });
    expect(state.pendingApproval).toBeNull();
    expect(state.heldApprovals.map((r) => r.requestId)).toEqual(["a-1"]);
    state = reducer(state, { type: "approval/route", requestId: "a-1", route: "person" });
    expect(state.heldApprovals).toEqual([]);
    expect(state.pendingApproval).toMatchObject({ requestId: "a-1", route: "person" });
  });

  it("queues a request that comes back while another prompt is open", () => {
    let state: State = reducer(initialState, { type: "approval/request", request: approval({ route: "jarvis" }) });
    state = reducer(state, { type: "approval/request", request: approval({ requestId: "a-2" }) });
    state = reducer(state, { type: "approval/route", requestId: "a-1", route: null });
    expect(state.pendingApproval?.requestId).toBe("a-2");
    expect(state.approvalQueue.map((r) => r.requestId)).toEqual(["a-1"]);
    expect(state.heldApprovals).toEqual([]);
  });

  it("never hands a person-only request to Jarvis", () => {
    let state: State = reducer(initialState, { type: "approval/request", request: approval({ personOnly: true }) });
    state = reducer(state, { type: "approval/route", requestId: "a-1", route: "jarvis" });
    expect(state.pendingApproval?.requestId).toBe("a-1");
    expect(state.heldApprovals).toEqual([]);
  });

  it("retags a queued request and ignores an unknown one", () => {
    const listed = reducer(initialState, { type: "approval/list", requests: [approval({ requestId: "a-9" })] });
    const routed = reducer(listed, { type: "approval/route", requestId: "a-9", route: "jarvis" });
    expect(routed.approvalQueue[0].route).toBe("jarvis");
    expect(reducer(routed, { type: "approval/route", requestId: "nope", route: "person" })).toEqual(routed);
  });
});

describe("question.route.changed in the store", () => {
  it("holds a question Jarvis took and asks it again when it falls to the person", () => {
    let state: State = reducer(initialState, { type: "question/request", request: question() });
    state = reducer(state, { type: "question/route", requestId: "q-1", route: "jarvis" });
    expect(state.pendingQuestion).toBeNull();
    expect(state.heldQuestions).toHaveLength(1);
    state = reducer(state, { type: "question/route", requestId: "q-1", route: "person" });
    expect(state.heldQuestions).toEqual([]);
    expect(state.pendingQuestion?.requestId).toBe("q-1");
  });

  it("keeps a second question waiting until the first is answered", () => {
    let state: State = reducer(initialState, { type: "question/request", request: question({ route: "jarvis" }) });
    state = reducer(state, { type: "question/request", request: question({ requestId: "q-2" }) });
    state = reducer(state, { type: "question/route", requestId: "q-1", route: "person" });
    expect(state.pendingQuestion?.requestId).toBe("q-2");
    expect(state.heldQuestions.map((r) => r.route)).toEqual(["person"]);
    state = reducer(state, { type: "question/resolved", requestId: "q-2" });
    expect(state.pendingQuestion?.requestId).toBe("q-1");
    expect(state.heldQuestions).toEqual([]);
  });
});

describe("route events in the Jarvis panel state", () => {
  it("moves a request between waiting and deciding without a list call", () => {
    let state = jarvisReducer(initialJarvis, { type: "pending", kind: "approval", request: approval({ route: "jarvis" }) });
    expect(state.deciding).toBe(1);
    expect(state.waiting).toEqual([]);
    state = jarvisReducer(state, { type: "route", requestId: "a-1", route: "person" });
    expect(state.deciding).toBe(0);
    expect(state.waiting.map((item) => item.requestId)).toEqual(["a-1"]);
    state = jarvisReducer(state, { type: "route", requestId: "a-1", route: "jarvis" });
    expect(state.deciding).toBe(1);
    expect(state.waiting).toEqual([]);
    state = jarvisReducer(state, { type: "resolved", requestId: "a-1" });
    expect(state.deciding).toBe(0);
    expect(state.requests).toEqual([]);
  });

  it("treats a route Jarvis mode turned off (null) as the person's", () => {
    let state = jarvisReducer(initialJarvis, { type: "pending", kind: "question", request: question({ route: "jarvis" }) });
    state = jarvisReducer(state, { type: "route", requestId: "q-1", route: null });
    expect(state.waiting.map((item) => item.requestId)).toEqual(["q-1"]);
  });
});

describe("the tmux.adopt confirmation", () => {
  const adopt = (args: Record<string, unknown>): any =>
    approval({ tool: "tmux.adopt", personOnly: true, args: { cli: "claude", sessionId: "x", ...args } });

  it("shows what tmux says runs in the pane and where, from the contract's tmux {session, pane}", () => {
    const view = personOnlyView(
      adopt({ tmux: { session: "work", pane: "%3" }, cwd: "/p", paneCommand: "claude", paneCwd: "/home/me/proj", unrestricted: false }),
      0,
    )!;
    const text = view.lines.join("\n");
    expect(text).toContain("work %3");
    expect(text).toContain("Running  claude");
    expect(text).toContain("Folder   /home/me/proj");
    expect(view.warnPrefix).toBeUndefined();
    expect(text).not.toContain("UNRESTRICTED");
  });

  it("warns in red when the pane was started with a permission bypass", () => {
    const view = personOnlyView(adopt({ tmux: { session: "w", pane: "%1" }, paneCommand: "claude --x", paneCwd: "/p", unrestricted: true }), 0)!;
    expect(view.warnPrefix).toBe("UNRESTRICTED");
    expect(view.lines.filter((line) => line.startsWith("UNRESTRICTED")).length).toBeGreaterThan(0);
  });
});
