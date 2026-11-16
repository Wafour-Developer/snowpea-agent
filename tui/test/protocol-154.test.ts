import stringWidth from "string-width";
import { describe, expect, it } from "vitest";

import { fit } from "../src/layout/fit.js";
import { textWidth } from "../src/layout/text-width.js";
import { SUMMARY_GLYPH } from "../src/components/ToolSummary.js";
import { agentOfSession } from "../src/state/agent-tag.js";
import {
  contextWindowLine,
  contextWindowShort,
  hookFailedLine,
  MODE_RAISE_LINE,
  rpcCode,
  rpcErrorText,
  sessionErrorView,
} from "../src/state/errors.js";
import {
  approvalResolvedNote,
  initialJarvis,
  jarvisReducer,
  needsPerson,
  panelLines,
  rememberChosenScope,
  rowAgent,
  rowChips,
  scopeNarrowed,
  sessionLabel,
  takeChosenScope,
} from "../src/state/jarvis.js";
import { ALWAYS_ASKS, alwaysAsksReason, alwaysAsksYou } from "../src/state/person-only.js";
import { initialState, reducer } from "../src/state/store.js";
import { diagnosticsBadge } from "../src/state/lsp.js";
import { bannerText } from "../src/state/update.js";
import { queuedLabel } from "../src/state/working.js";

const failure = (code: string, message = "m", details?: unknown): unknown => Object.assign(new Error(message), { data: { code, details } });

describe("rpc errors in plain words", () => {
  it("reads the code from data.code, else code, defensively", () => {
    expect(rpcCode(failure("jarvis_person_only"))).toBe("jarvis_person_only");
    expect(rpcCode({ code: -32601 })).toBeUndefined();
    expect(rpcCode(null)).toBeUndefined();
    expect(rpcCode("boom")).toBeUndefined();
  });

  it("says plainly that the mode was not raised (jarvis_person_only, details.reason mode_raise)", () => {
    expect(rpcErrorText(failure("jarvis_person_only", "x", { reason: "mode_raise" }))).toBe(MODE_RAISE_LINE);
    expect(MODE_RAISE_LINE).toContain("was not raised");
    expect(rpcErrorText(failure("jarvis_person_only", "x", { reason: "other" }))).toContain("Only a person");
    expect(rpcErrorText(failure("jarvis_person_only", "x"))).toContain("Only a person");
    // details may be anything.
    expect(rpcErrorText(failure("jarvis_person_only", "x", "mode_raise"))).toContain("Only a person");
    expect(rpcErrorText(failure("jarvis_person_only", "x", null))).toContain("Only a person");
    expect(rpcErrorText(failure("jarvis_person_only", "x", ["mode_raise"]))).toContain("Only a person");
  });

  it("words steer_slash_command, a catch-all allowlist rule and a security block", () => {
    expect(rpcErrorText(failure("steer_slash_command"))).toContain("slash command cannot be sent as a steer");
    expect(rpcErrorText(failure("invalid_params", "bad allowlist pattern: a rule must start with the program it allows, not a wildcard", { reason: "catch_all" }))).toBe(
      "A rule must name a program — a rule must start with the program it allows, not a wildcard",
    );
    expect(rpcErrorText(failure("invalid_params", "other", { reason: "else" }))).toContain("other");
    expect(rpcErrorText(failure("security_blocked", "commands:essential — nope"))).toBe("Blocked by your security list: commands:essential — nope");
  });

  it("keeps the text it always showed for anything else", () => {
    const error = new Error("session_busy");
    expect(rpcErrorText(error)).toBe(String(error));
    expect(rpcErrorText("plain")).toBe("plain");
    expect(rpcErrorText(failure("not_found", "gone"))).toContain("gone");
  });
});

describe("session error events", () => {
  it("turns hook_failed into one notice with where to look", () => {
    const view = sessionErrorView("hook_failed", "a plugin guard hook is not running: lintguard — exit 2: boom");
    expect(view).toEqual({ kind: "notice", text: "a plugin guard hook is not running: lintguard — exit 2: boom  (/skill hooks)" });
    expect(hookFailedLine("")).toBe("a plugin guard hook is not running  (/skill hooks)");
    expect(hookFailedLine("a\u001b]52;c;QQ==\u0007b")).toBe("ab  (/skill hooks)");
  });

  it("gives context_window_too_small both numbers from details, else from the message, else none", () => {
    const message = "the model's context window is 4096 tokens, but snowpea's system prompt and tool definitions alone need about 9800; use a model or server setting with a larger context window";
    expect(contextWindowLine(message)).toContain("4,096 tokens");
    expect(contextWindowLine(message)).toContain("about 9,800");
    expect(contextWindowLine("x", { windowTokens: 2048, neededTokens: 7000 })).toContain("2,048 tokens");
    expect(contextWindowLine("x", { windowTokens: 2048, neededTokens: 7000 })).toContain("about 7,000");
    expect(contextWindowLine("x", { windowTokens: 2048 })).toContain("only 2,048 tokens");
    expect(contextWindowLine("unreadable")).toContain("too small");
    // Bad details are ignored, not trusted.
    expect(contextWindowLine("x", { windowTokens: "big", neededTokens: -1 })).toContain("too small");
    expect(contextWindowLine("x", "details")).toContain("too small");
    expect(contextWindowLine("x", null)).toContain("too small");
    expect(contextWindowShort(message)).toBe("context_window_too_small: window 4,096 tokens, needs about 9,800");
    expect(contextWindowShort("x")).toBe("context_window_too_small");
  });

  it("offers no retry hint for a window that cannot fit", () => {
    const line = contextWindowLine("the model's context window is 4096 tokens, but snowpea's system prompt and tool definitions alone need about 9800");
    expect(line).not.toMatch(/retr|summaris|compact/i);
    expect(line).toContain("will not help");
  });

  it("keeps the old shape for other codes", () => {
    expect(sessionErrorView("approval_denied", "shell was not approved")).toEqual({ kind: "error", text: "approval_denied: shell was not approved" });
    expect(sessionErrorView(undefined, "x")).toEqual({ kind: "error", text: "error: x" });
    expect(sessionErrorView("security_blocked", "x").kind).toBe("blocked");
  });

  const run = (kind: string, payload: Record<string, unknown>) =>
    reducer(initialState, { type: "session/event", event: { sessionId: "s", seq: 1, kind, payload } as never });

  it("puts hook_failed in the transcript, not in the error line or the turn's reason", () => {
    const state = run("error", { code: "hook_failed", message: "a plugin guard hook is not running: p — r" });
    expect(state.messages.map((message) => message.text)).toEqual(["a plugin guard hook is not running: p — r  (/skill hooks)"]);
    expect(state.errors).toEqual([]);
    expect(state.lastTurnError).toBeNull();
  });

  it("puts context_window_too_small in the error line short, and in the transcript whole", () => {
    const state = run("error", { code: "context_window_too_small", message: "the model's context window is 4096 tokens, but snowpea's system prompt and tool definitions alone need about 9800" });
    expect(state.errors).toEqual(["context_window_too_small: window 4,096 tokens, needs about 9,800"]);
    expect(state.messages[0]).toMatchObject({ blocked: true });
    expect(state.messages[0].text).toContain("4,096 tokens");
    expect(state.messages[0].text).toContain("9,800");
  });

  it("puts security_blocked in the transcript as a red line", () => {
    const state = run("error", { code: "security_blocked", message: "git push --force" });
    expect(state.messages[0]).toMatchObject({ blocked: true, text: "Blocked by your security list: git push --force" });
    expect(state.errors).toEqual([]);
  });
});

describe("requests only a person may decide", () => {
  it("is personOnly, neverDelegable or a mode picker; anything else is not", () => {
    expect(alwaysAsksYou({ personOnly: true })).toBe(true);
    expect(alwaysAsksYou({ neverDelegable: true })).toBe(true);
    expect(alwaysAsksYou({ kind: "mode" })).toBe(true);
    expect(alwaysAsksYou({ personOnly: false, neverDelegable: null, kind: null })).toBe(false);
    expect(alwaysAsksYou({})).toBe(false);
    expect(alwaysAsksYou(null)).toBe(false);
    expect(ALWAYS_ASKS).toBe("always asks you");
  });

  it("reads core's reason, trimmed, when there is one", () => {
    expect(alwaysAsksReason({ reason: "  cannot be checked against your block list " })).toBe("cannot be checked against your block list");
    expect(alwaysAsksReason({ reason: "" })).toBeNull();
    expect(alwaysAsksReason({ reason: null })).toBeNull();
    expect(alwaysAsksReason(undefined)).toBeNull();
  });

  it("waits on the person whatever the route says, and is never counted as 'deciding'", () => {
    const request = { requestId: "r", sessionId: "s", tool: "shell", args: {}, route: "jarvis", neverDelegable: true } as never;
    expect(needsPerson(request)).toBe(true);
    expect(needsPerson({ ...(request as object), neverDelegable: false } as never)).toBe(false);
    const state = jarvisReducer(initialJarvis, { type: "waiting", approvals: [request], questions: [] } as never);
    expect(state.deciding).toBe(0);
    expect(state.waiting[0].alwaysAsks).toBe(true);
  });

  it("holds an ordinary question for Jarvis but never a mode picker or personOnly one", () => {
    const question = (patch: Record<string, unknown>) => ({ requestId: "q", sessionId: "s", route: "jarvis", questions: [], ...patch }) as never;
    const held = reducer(initialState, { type: "question/request", request: question({}) });
    expect(held.heldQuestions).toHaveLength(1);
    expect(held.pendingQuestion).toBeNull();
    for (const patch of [{ kind: "mode" }, { personOnly: true }, { kind: "mode", personOnly: true }]) {
      const state = reducer(initialState, { type: "question/request", request: question(patch) });
      expect(state.pendingQuestion?.requestId, JSON.stringify(patch)).toBe("q");
      expect(state.heldQuestions).toHaveLength(0);
    }
  });

  it("does not take a mode picker off the screen when its route turns to Jarvis", () => {
    const request = { requestId: "q", sessionId: "s", route: "person", kind: "mode", personOnly: true, questions: [] } as never;
    const shown = reducer(initialState, { type: "question/request", request });
    const after = reducer(shown, { type: "question/route", requestId: "q", route: "jarvis" });
    expect(after.pendingQuestion?.requestId).toBe("q");
    expect(after.heldQuestions).toHaveLength(0);
  });

  it("does not hold a neverDelegable approval for Jarvis, nor withdraw it on a route change", () => {
    const request = { requestId: "a", sessionId: "s", tool: "shell", args: {}, route: "jarvis", neverDelegable: true } as never;
    const shown = reducer(initialState, { type: "approval/request", request });
    expect(shown.pendingApproval?.requestId).toBe("a");
    expect(shown.heldApprovals).toHaveLength(0);
    const after = reducer(shown, { type: "approval/route", requestId: "a", route: "jarvis" });
    expect(after.pendingApproval?.requestId).toBe("a");
  });
});

describe("approval.resolved", () => {
  const request = { requestId: "a", sessionId: "s", tool: "shell", args: { command: "pytest -q" } } as never;

  it("says nothing for a withdrawn request, whoever 'answered' it", () => {
    expect(approvalResolvedNote({ by: "rerouted", decision: null, outcome: "withdrawn", scope: null }, request)).toBeNull();
    expect(approvalResolvedNote({ by: "jarvis", decision: null }, request)).toBeNull();
    expect(approvalResolvedNote({ by: "jarvis", decision: "allow", outcome: "withdrawn" }, request)).toBeNull();
  });

  it("names Jarvis's answers as before", () => {
    expect(approvalResolvedNote({ by: "jarvis", decision: "deny" }, request)).toBe("Jarvis denied shell: pytest -q");
    expect(approvalResolvedNote({ by: "jarvis:auto", decision: "allow" }, request)).toBe("Jarvis (auto) allowed shell: pytest -q");
  });

  it("says allowed once only when the applied scope is narrower than chosen", () => {
    const note = approvalResolvedNote({ by: "tui", decision: "allow", scope: "once" }, request, "session");
    expect(note).toContain("Allowed once only");
    expect(note).toContain('"session"');
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "session" }, request, "always")).toContain("Allowed for this session only");
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "project" }, request, "always")).toContain("for this project only");
  });

  it("says nothing when the scope is the same, wider, unknown, or the answer was no", () => {
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "once" }, request, "once")).toBeNull();
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "always" }, request, "session")).toBeNull();
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "once" }, request, undefined)).toBeNull();
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: null }, request, "session")).toBeNull();
    expect(approvalResolvedNote({ by: "tui", decision: "deny", scope: null }, request, "once")).toBeNull();
    expect(approvalResolvedNote({ by: "tui", decision: "allow", scope: "forever" }, request, "session")).toBeNull();
  });

  it("compares scopes in their order", () => {
    expect(scopeNarrowed("always", "project")).toBe(true);
    expect(scopeNarrowed("project", "session")).toBe(true);
    expect(scopeNarrowed("session", "once")).toBe(true);
    expect(scopeNarrowed("once", "once")).toBe(false);
    expect(scopeNarrowed("once", "session")).toBe(false);
    expect(scopeNarrowed("site", "project")).toBe(false);
  });

  it("remembers only what was allowed, once", () => {
    rememberChosenScope("r1", "allow", "session");
    rememberChosenScope("r2", "deny", "once");
    expect(takeChosenScope("r1")).toBe("session");
    expect(takeChosenScope("r1")).toBeUndefined();
    expect(takeChosenScope("r2")).toBeUndefined();
  });
});

describe("which session is a browser's", () => {
  const base = { sessionId: "s-2", project: "/work/snowpea", title: "fix auth" };

  it("tags a row [web] from hostToolsFrom or originSurface, and no other", () => {
    expect(sessionLabel({ ...base, hostToolsFrom: "profile-1" } as never)).toBe("[web] snowpea · fix auth");
    expect(sessionLabel({ ...base, originSurface: "browser" } as never)).toBe("[web] snowpea · fix auth");
    expect(sessionLabel(base as never)).toBe("snowpea · fix auth");
    expect(sessionLabel({ ...base, originSurface: "tui", hostToolsFrom: null } as never)).toBe("snowpea · fix auth");
    expect(sessionLabel({ ...base, sessionId: null, external: { cli: "codex", id: "abc12345" } } as never)).toBe("[cx] snowpea · fix auth");
  });

  it("gives the row the browser colour in the panel", () => {
    const rows = [{ ...base, hostToolsFrom: "p1", status: "idle", level: "full" }] as never;
    const lines = panelLines({ ...initialJarvis, rows, status: { enabled: true, running: true, folder: "/j", delegationDefault: "staged", sessionId: "j", model: "m" } as never }, 80, 12, Date.now());
    const row = lines.find((line) => line.key === "row-0");
    const tag = row?.segments.find((segment) => segment.text === "[web]");
    expect(tag).toMatchObject({ bold: true });
    expect(tag?.color).toBe(agentOfSession({ hostToolsFrom: "x" }).color);
    expect(rowAgent(rows[0] as never).kind).toBe("web");
  });

  it("adds a no-security-lists chip only for securityListsApply: false", () => {
    expect(rowChips({ ...base, securityListsApply: false } as never, 80)).toContain("no security lists");
    expect(rowChips({ ...base, securityListsApply: null } as never, 80)).not.toContain("no security lists");
    expect(rowChips({ ...base, securityListsApply: true } as never, 80)).not.toContain("no security lists");
    expect(rowChips(base as never, 80)).not.toContain("no security lists");
  });
});

describe("glyphs one cell wide to Ink and to the layout", () => {
  const glyphs: Array<[string, string]> = [
    ["the diagnostics badge", diagnosticsBadge({ count: 3, errors: 1, warnings: 2 }) ?? ""],
    ["the tool summary", SUMMARY_GLYPH],
    ["the queued count", queuedLabel(2)],
    ["the update banner (available)", bannerText({ phase: "available", latest: "1.0.0", current: "0.9.0", message: "" } as never) ?? ""],
    ["the update banner (running)", bannerText({ phase: "running", latest: "1.0.0", current: "0.9.0", message: "" } as never) ?? ""],
    ["the update banner (failed)", bannerText({ phase: "failed", latest: "1.0.0", current: "0.9.0", message: "x" } as never) ?? ""],
  ];

  it("measures the same in Ink's string-width and in the layout's width function", () => {
    for (const [label, text] of glyphs) {
      expect(text, label).not.toBe("");
      expect(stringWidth(text), label).toBe(textWidth(text));
      expect(stringWidth(text), label).toBe(Array.from(text).length);
    }
  });

  it("keeps each of them inside 60 columns when fitted", () => {
    for (const [label, text] of glyphs) expect(stringWidth(fit(text, 60)), label).toBeLessThanOrEqual(60);
  });
});
