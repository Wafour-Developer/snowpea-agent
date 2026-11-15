import { describe, expect, it } from "vitest";

import { textWidth } from "../src/layout/text-width.js";
import type { ExternalCliInfo, ExternalSessionRow, ExternalTimelineEvent } from "../src/rpc/sdk.js";
import {
  confirmsUnrestricted,
  describeExternalError,
  extSessionId,
  externalReducer,
  hostLabel,
  isUngated,
  modeLabel,
  modesFor,
  openExternal,
  pickerLabel,
  searchRows,
  toSessionEvent,
  unavailableReason,
  ungatedWarning,
  unrestrictedPrompt,
  usable,
  viewOnlyReason,
  type ExternalView,
} from "../src/state/external.js";

const ref = { cli: "codex" as const, id: "019abcdef0123" };

const session = (patch: Partial<ExternalSessionRow> = {}): ExternalSessionRow => ({
  ref,
  cwd: "/work/snowpea",
  status: "idle",
  host: "none",
  canSend: true,
  approvals: "bridged",
  mode: "default",
  controlled: true,
  title: "fix the build",
  ...patch,
});

const ev = (seq: number, kind: string, payload: Record<string, unknown> = {}): ExternalTimelineEvent => ({
  seq,
  kind,
  payload,
  ts: `2026-11-15T12:00:${String(seq).padStart(2, "0")}Z`,
});

const messages = (view: ExternalView) => view.state.messages.map((m) => `${m.role}:${m.text}`);

describe("the converter", () => {
  it("turns an external event into the session event the reducers already read", () => {
    const event = toSessionEvent(ref, { ...ev(4, "message.delta", { text: "hi" }), turnId: "t-1" });
    expect(event).toEqual({
      sessionId: "ext:codex:019abcdef0123",
      seq: 4,
      kind: "message.delta",
      ts: "2026-11-15T12:00:04Z",
      payload: { text: "hi", turnId: "t-1" },
    });
    expect(extSessionId(ref)).toBe("ext:codex:019abcdef0123");
  });

  it("does not overwrite a turn id the payload already has", () => {
    const event = toSessionEvent(ref, { ...ev(1, "turn.done", { turnId: "own" }), turnId: "other" });
    expect(event.payload?.turnId).toBe("own");
  });
});

describe("opening a session", () => {
  it("folds the newest page, in order, into a transcript", () => {
    const view = openExternal(
      session(),
      // Out of order and with a repeat: the page is sorted and de-duplicated.
      [
        ev(3, "message.done", { role: "assistant", text: "done" }),
        ev(1, "message.user", { text: "fix it" }),
        ev(2, "tool.call", { callId: "c1", name: "shell", args: { command: "make" } }),
        ev(3, "message.done", { role: "assistant", text: "done" }),
      ],
      true,
    );
    expect(messages(view).filter((line) => !line.startsWith("note:"))).toEqual(["user:fix it", "assistant:done"]);
    expect(view.state.toolCalls.map((call) => call.name)).toEqual(["shell"]);
    expect(view.hasMore).toBe(true);
    expect(view.events.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it("loads an older page in front without repeating what it has", () => {
    let view = openExternal(session(), [ev(5, "message.user", { text: "second" })], true);
    view = externalReducer(view, {
      type: "older",
      events: [ev(1, "message.user", { text: "first" }), ev(5, "message.user", { text: "second" })],
      hasMore: false,
    });
    expect(messages(view).filter((line) => line.startsWith("user:"))).toEqual(["user:first", "user:second"]);
    expect(view.hasMore).toBe(false);
  });
});

describe("live events", () => {
  const open = () => openExternal(session(), [ev(1, "message.user", { text: "go" })], false);

  it("streams an answer and drops a repeat of a sequence it has", () => {
    let view = open();
    view = externalReducer(view, { type: "event", event: { ref, seq: 2, kind: "message.delta", ts: "t", payload: { text: "Hel" } } });
    view = externalReducer(view, { type: "event", event: { ref, seq: 3, kind: "message.delta", ts: "t", payload: { text: "lo" } } });
    const replay = externalReducer(view, { type: "event", event: { ref, seq: 3, kind: "message.delta", ts: "t", payload: { text: "lo" } } });
    expect(replay).toBe(view);
    expect(messages(view).pop()).toBe("assistant:Hello");
  });

  it("ignores another session's events", () => {
    const view = open();
    const other = externalReducer(view, {
      type: "event",
      event: { ref: { cli: "claude", id: "x" }, seq: 9, kind: "message.delta", ts: "t", payload: { text: "no" } },
    });
    expect(other).toBe(view);
  });

  it("echoes a sent prompt once, even when the CLI's transcript repeats it", () => {
    let view = open();
    view = externalReducer(view, { type: "sent", text: "run the tests" });
    expect(view.state.turnActive).toBe(true);
    view = externalReducer(view, { type: "event", event: { ref, seq: 2, kind: "message.user", ts: "t", payload: { text: "run the tests" } } });
    expect(messages(view).filter((line) => line === "user:run the tests")).toHaveLength(1);
  });

  it("settles the turn and says why when a send fails", () => {
    let view = open();
    view = externalReducer(view, { type: "sent", text: "x" });
    view = externalReducer(view, { type: "failed", message: "Codex is not available: not installed" });
    expect(view.state.turnActive).toBe(false);
    expect(view.state.errors).toContain("Codex is not available: not installed");
  });

  it("notes a turn that ended badly", () => {
    let view = open();
    view = externalReducer(view, { type: "event", event: { ref, seq: 2, kind: "turn.done", ts: "t", payload: { reason: "error" } } });
    expect(messages(view)).toContain("system:turn ended: error");
    const clean = externalReducer(open(), { type: "event", event: { ref, seq: 2, kind: "turn.done", ts: "t", payload: { reason: "complete" } } });
    expect(messages(clean).some((line) => line.includes("turn ended"))).toBe(false);
  });
});

describe("external.state", () => {
  const state = (patch: Record<string, unknown>) =>
    ({ ref, approvals: "bridged", canSend: true, host: "snowpea", status: "idle", ...patch }) as never;

  it("flips from view-only to usable the moment the other terminal closes, and says so", () => {
    let view = openExternal(session({ host: "terminal", canSend: false }), [], false);
    expect(viewOnlyReason(view.session)).toContain("another terminal");
    view = externalReducer(view, { type: "state", payload: state({ host: "snowpea", canSend: true }) });
    expect(viewOnlyReason(view.session)).toBeNull();
    expect(view.notice).toBe("The other terminal closed — you can send from here now.");
  });

  it("flips the other way when a terminal takes the session", () => {
    let view = openExternal(session({ host: "snowpea" }), [], false);
    view = externalReducer(view, { type: "state", payload: state({ host: "terminal", canSend: false }) });
    expect(view.session.canSend).toBe(false);
    expect(view.notice).toContain("view only until it exits");
  });

  it("marks a turn that died mid-turn and invites a new message", () => {
    let view = openExternal(session({ status: "running" }), [], false);
    view = externalReducer(view, { type: "sent", text: "work" });
    view = externalReducer(view, { type: "state", payload: state({ status: "error", error: "process exited 137" }) });
    expect(view.state.turnActive).toBe(false);
    expect(view.error).toBe("process exited 137");
    expect(messages(view).pop()).toBe("system:Codex stopped mid-turn: process exited 137. Send a message to continue.");
    view = externalReducer(view, { type: "state", payload: state({ status: "idle" }) });
    expect(view.error).toBeNull();
  });

  it("follows the mode, and announces unrestricted", () => {
    let view = openExternal(session(), [], false);
    view = externalReducer(view, { type: "state", payload: state({ mode: "unrestricted", controlled: false }) });
    expect(view.session.mode).toBe("unrestricted");
    expect(view.notice).toBe("Mode is now UNRESTRICTED.");
    expect(ungatedWarning(view.session)).toContain("UNRESTRICTED");
  });
});

describe("view-only and gating words", () => {
  it("explains each way sending is refused", () => {
    expect(viewOnlyReason(session({ host: "terminal", canSend: false }))).toContain("another terminal");
    expect(viewOnlyReason(session({ host: "tmux", canSend: false, tmux: { session: "dev", pane: "%3" } }))).toContain("dev %3");
    expect(viewOnlyReason(session({ host: "none", canSend: false }))).toBe("This session is view only.");
    expect(viewOnlyReason(session())).toBeNull();
    expect(viewOnlyReason(session({ canSend: undefined }))).toBeNull();
  });

  it("warns for unrestricted and for a CLI snowpea cannot gate, not for the rest", () => {
    expect(isUngated(session())).toBe(false);
    expect(ungatedWarning(session())).toBeNull();
    expect(ungatedWarning(session({ mode: "unrestricted" }))).toBe(
      "UNRESTRICTED — snowpea does not control what this Codex session runs",
    );
    expect(ungatedWarning(session({ ref: { cli: "cursor", id: "c" }, controlled: false }))).toBe(
      "not gated — Cursor runs without snowpea's approvals",
    );
    expect(modeLabel("unrestricted")).toBe("UNRESTRICTED");
    expect(modeLabel("readonly")).toBe("read-only");
    expect(modeLabel(undefined)).toBe("default");
  });

  it("asks for the word, and only the word", () => {
    expect(confirmsUnrestricted("unrestricted")).toBe(true);
    expect(confirmsUnrestricted("  Unrestricted ")).toBe(true);
    expect(confirmsUnrestricted("yes")).toBe(false);
    expect(confirmsUnrestricted("unrestricted!")).toBe(false);
    const prompt = unrestrictedPrompt({ cli: "claude", title: "deploy", cwd: "/work/x" }).join("\n");
    expect(prompt).toContain("Claude Code");
    expect(prompt).toContain("Jarvis's guarded-class rules do not apply");
    expect(prompt).toContain('Type "unrestricted"');
  });
});

describe("the picker", () => {
  const rows = [
    session({ title: "fix the build", host: "terminal", canSend: false, status: "running" }),
    session({ ref: { cli: "claude", id: "c1" }, title: "write docs", cwd: "/work/web", host: "snowpea" }),
    session({ ref: { cli: "cursor", id: "u1" }, title: "한글 제목으로 된 아주 긴 세션 이름입니다 그래서 잘립니다", mode: "unrestricted", host: "tmux", tmux: { session: "dev", pane: "%1" } }),
  ];

  it("searches title, CLI, folder and id, all words at once", () => {
    expect(searchRows(rows, "").length).toBe(3);
    expect(searchRows(rows, "docs").map((r) => r.ref.id)).toEqual(["c1"]);
    expect(searchRows(rows, "codex build").map((r) => r.ref.id)).toEqual([ref.id]);
    expect(searchRows(rows, "/work/web").map((r) => r.ref.id)).toEqual(["c1"]);
    expect(searchRows(rows, "019abc").map((r) => r.ref.id)).toEqual([ref.id]);
    expect(searchRows(rows, "nothing like it")).toEqual([]);
  });

  it("labels each row with CLI, status, where it runs and view-only", () => {
    expect(pickerLabel(rows[0], 80, false)).toMatch(/^\[Codex\] fix the build +running · other terminal · view only$/);
    expect(pickerLabel(rows[1], 80, true)).toContain("[Claude Code] write docs (web)");
    expect(pickerLabel(rows[1], 80, false)).toContain("opened by snowpea");
    expect(pickerLabel(rows[2], 80, false)).toContain("UNRESTRICTED");
    expect(hostLabel({ host: "tmux", tmux: { session: "dev", pane: "%1" } })).toBe("tmux dev");
    expect(hostLabel({ host: "none" })).toBe("not open");
  });

  it("never runs past the width, in cells, at 60 columns and narrower", () => {
    for (const width of [60, 52, 40]) {
      for (const row of rows) {
        expect(textWidth(pickerLabel(row, width, true)), `${row.ref.id} at ${width}`).toBeLessThanOrEqual(width);
      }
    }
  });
});

describe("the CLIs", () => {
  const info = (patch: Partial<ExternalCliInfo>): ExternalCliInfo => ({ id: "agy", title: "agy", installed: true, ...patch });

  it("puts the CLI's default mode first, and offers only what it supports", () => {
    expect(modesFor(info({ capabilities: { approvals: "degraded", liveView: false, streaming: true, modes: ["readonly", "unrestricted"] }, defaultMode: "readonly" }))).toEqual(["readonly", "unrestricted"]);
    expect(modesFor(info({ defaultMode: "unrestricted" }))).toEqual(["unrestricted", "default", "readonly"]);
    expect(modesFor(undefined)).toEqual(["default", "readonly", "unrestricted"]);
  });

  it("says why a CLI cannot be started", () => {
    expect(usable(info({}))).toBe(true);
    expect(usable(info({ installed: false }))).toBe(false);
    expect(unavailableReason(info({ installed: false }))).toBe("not installed");
    expect(unavailableReason(info({ enabled: false }))).toBe("turned off in settings");
    expect(usable(info({ enabled: false }))).toBe(false);
  });

  it("turns the daemon's error codes into sentences", () => {
    const coded = (code: string, message = "boom") => ({ message, data: { code } });
    expect(describeExternalError(coded("external_unavailable", "codex is not installed"), "codex")).toBe(
      "Codex is not available: codex is not installed",
    );
    expect(describeExternalError(coded("external_view_only"))).toContain("view only");
    expect(describeExternalError(coded("jarvis_person_only"))).toContain("Only a person");
    expect(describeExternalError(coded("jarvis_disabled"))).toContain("/jarvis on");
    expect(describeExternalError(new Error("plain"))).toBe("plain");
  });
});
