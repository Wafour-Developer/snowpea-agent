import { describe, expect, it } from "vitest";

import { textWidth } from "../src/layout/text-width.js";
import type {
  ApprovalRequestParams,
  JarvisLedgerEntry,
  JarvisOverviewRow,
  JarvisStatus,
  QuestionRequestParams,
} from "../src/rpc/sdk.js";
import {
  formatRemaining,
  initialJarvis,
  isDelegationProposal,
  jarvisMarker,
  jarvisReducer,
  needsPerson,
  panelLines,
  parseProposal,
  proposalLines,
  requestLabel,
  requiresLine,
  rowStatus,
  sessionLabel,
  statusLines,
  supportOf,
  type JarvisState,
} from "../src/state/jarvis.js";

const NOW = Date.parse("2026-11-15T12:00:00Z");
const inMinutes = (minutes: number): string => new Date(NOW + minutes * 60_000).toISOString();

const status = (patch: Partial<JarvisStatus> = {}): JarvisStatus => ({
  enabled: true,
  running: true,
  folder: "/home/me/Snowpea/Jarvis",
  delegationDefault: "staged",
  sessionId: "jarvis-session",
  model: "fast",
  ...patch,
});

const row = (patch: Partial<JarvisOverviewRow> = {}): JarvisOverviewRow => ({
  sessionId: "s-1",
  project: "/work/snowpea",
  title: "fix auth",
  status: "running",
  ...patch,
});

const approval = (patch: Record<string, unknown> = {}): ApprovalRequestParams =>
  ({ requestId: "a-1", sessionId: "s-1", tool: "shell", args: { command: "git push" }, route: "person", ...patch }) as ApprovalRequestParams;

const question = (patch: Record<string, unknown> = {}): QuestionRequestParams =>
  ({ requestId: "q-1", sessionId: "s-2", questions: [{ question: "Which database?" }], route: "person", ...patch }) as QuestionRequestParams;

const entry = (id: string, ts: string, patch: Partial<JarvisLedgerEntry> = {}): JarvisLedgerEntry => ({
  id,
  ts,
  kind: "approval",
  decidedBy: "jarvis",
  summary: `allowed ${id}`,
  ...patch,
});

describe("capability gating", () => {
  it("reads the three feature flags out of system.hello", () => {
    expect(supportOf(["audio", "jarvis", "jarvisDelegation", "externalSessions"])).toEqual({
      jarvis: true,
      delegation: true,
      external: true,
    });
    expect(supportOf(["audio"])).toEqual({ jarvis: false, delegation: false, external: false });
    expect(supportOf(undefined)).toEqual({ jarvis: false, delegation: false, external: false });
  });

  it("says once, in one line, that core 1.53 is needed", () => {
    expect(requiresLine("jarvis")).toContain("requires core 1.53");
    expect(requiresLine("external")).toContain("requires core 1.53");
    expect(requiresLine("jarvis").split("\n")).toHaveLength(1);
  });
});

describe("jarvisReducer", () => {
  it("keeps the status and drops everything when Jarvis is turned off", () => {
    let state = jarvisReducer(initialJarvis, { type: "status", status: status() });
    state = jarvisReducer(state, { type: "overview", rows: [row()], at: 1 });
    expect(state.rows).toHaveLength(1);
    state = jarvisReducer(state, { type: "event/state", payload: { reason: "disabled", status: status({ enabled: false, running: false }) } });
    expect(state.status?.enabled).toBe(false);
    expect(state.rows).toEqual([]);
    expect(state.waiting).toEqual([]);
  });

  it("splits requests into waiting-for-you and Jarvis-deciding by route", () => {
    const state = jarvisReducer(
      { ...initialJarvis, rows: [row()] },
      {
        type: "waiting",
        approvals: [
          approval({ requestId: "a-person", route: "person" }),
          approval({ requestId: "a-jarvis", route: "jarvis" }),
          approval({ requestId: "a-auto", route: "auto" }),
          approval({ requestId: "a-off", route: null }),
        ],
        questions: [question()],
      },
    );
    expect(state.waiting.map((item) => item.requestId)).toEqual(["a-person", "a-off", "q-1"]);
    expect(state.deciding).toBe(1);
    expect(needsPerson(approval({ route: "jarvis" }))).toBe(false);
  });

  it("names the waiting item by its session, external ones by their CLI", () => {
    const rows = [
      row(),
      row({ sessionId: null, external: { cli: "codex", id: "019abcdef" }, title: "docs", project: "/work/web", host: "snowpea" }),
    ];
    const state = jarvisReducer(
      { ...initialJarvis, rows },
      {
        type: "waiting",
        approvals: [
          approval(),
          approval({ requestId: "a-2", sessionId: "ext:codex:019abcdef", external: { cli: "codex", id: "019abcdef" } }),
          approval({ requestId: "a-3", sessionId: "ext:claude:zzzzzzzzzz", external: { cli: "claude", id: "zzzzzzzzzz" } }),
        ],
        questions: [],
      },
    );
    expect(state.waiting.map((item) => item.label)).toEqual([
      "snowpea · fix auth",
      "[cx] web · docs",
      "[cc] zzzzzzzz",
    ]);
    expect(state.waiting[0].summary).toBe("shell: git push");
  });

  it("puts decisions newest first, once each, capped", () => {
    let state: JarvisState = initialJarvis;
    for (let i = 0; i < 15; i += 1) {
      state = jarvisReducer(state, {
        type: "event/decision",
        payload: { entry: entry(`d${i}`, new Date(NOW + i * 1000).toISOString()) },
      });
    }
    state = jarvisReducer(state, { type: "event/decision", payload: { entry: entry("d14", new Date(NOW + 14_000).toISOString()) } });
    expect(state.decisions).toHaveLength(12);
    expect(state.decisions[0].id).toBe("d14");
    expect(state.decisions.filter((e) => e.id === "d14")).toHaveLength(1);
  });

  it("toasts an escalation once on the Jarvis screen only, never for the session the person is in", () => {
    const base = { id: "e1", ts: inMinutes(0), reason: "finished" as const, summary: "git push needs you", importance: "high" as const, sessionId: "s-2" };
    const on = { onScreen: true, sessionId: "s-1" };
    // An ordinary session: silent.
    expect(jarvisReducer(initialJarvis, { type: "event/escalation", payload: base, sessionId: "s-1" }).notice).toBeNull();
    let state = jarvisReducer(initialJarvis, { type: "event/escalation", payload: base, ...on });
    expect(state.notice).toBe("Jarvis: git push needs you");
    state = jarvisReducer(state, { type: "notice/clear" });
    state = jarvisReducer(state, { type: "event/escalation", payload: base, ...on });
    expect(state.notice).toBeNull();
    expect(state.escalations).toHaveLength(1);
    // The same need under another id (no request id): same session + summary does not stack.
    state = jarvisReducer(state, { type: "event/escalation", payload: { ...base, id: "e9" }, ...on });
    expect(state.escalations).toHaveLength(1);
    expect(state.notice).toBeNull();
    // The session the person is in shows its own prompt.
    const own = jarvisReducer(initialJarvis, { type: "event/escalation", payload: { ...base, sessionId: "s-1" }, ...on });
    expect(own.notice).toBeNull();
    const quiet = jarvisReducer(initialJarvis, { type: "event/escalation", payload: { ...base, id: "e2", summary: "other", importance: "low" }, ...on });
    expect(quiet.notice).toBeNull();
    expect(quiet.escalations).toHaveLength(1);
  });

  it("does not stack waiting-request escalations with the same request id", () => {
    const wait = { id: "e1", ts: inMinutes(0), reason: "guarded" as const, summary: "approve shell ls", importance: "high" as const, sessionId: "s-2", requestId: "r-1" };
    let state = jarvisReducer(initialJarvis, { type: "event/escalation", payload: wait, onScreen: true, sessionId: "s-1" });
    expect(state.notice).toBe("Jarvis: approve shell ls");
    state = jarvisReducer(state, { type: "notice/clear" });
    state = jarvisReducer(state, { type: "event/escalation", payload: { ...wait, id: "e2" }, onScreen: true, sessionId: "s-1" });
    expect(state.notice).toBeNull();
  });

  it("says when a delegation changed, and forgets an answered request", () => {
    let state = jarvisReducer(initialJarvis, {
      type: "event/delegation",
      payload: { reason: "revoked", grant: { id: "g", scope: "session", level: "full", allow: [], grantedBy: "tui", grantedAt: inMinutes(0) } as never },
    });
    expect(state.notice).toBe("Jarvis delegation revoked");
    state = jarvisReducer({ ...state, rows: [row()] }, { type: "waiting", approvals: [approval()], questions: [] });
    state = jarvisReducer(state, { type: "resolved", requestId: "a-1" });
    expect(state.waiting).toEqual([]);
  });
});

describe("time and status words", () => {
  it("formats the time left", () => {
    expect(formatRemaining(inMinutes(7), NOW)).toBe("7 min");
    expect(formatRemaining(new Date(NOW + 20_000).toISOString(), NOW)).toBe("<1 min");
    expect(formatRemaining(inMinutes(65), NOW)).toBe("1 h 5 min");
    expect(formatRemaining(inMinutes(120), NOW)).toBe("2 h");
    expect(formatRemaining(inMinutes(-1), NOW)).toBeNull();
    expect(formatRemaining(null, NOW)).toBeNull();
  });

  it("says who a row waits on, then pauses, then what it is doing", () => {
    expect(rowStatus(row({ waitingOnPerson: 2 }), NOW).text).toBe("waits for you (2)");
    expect(rowStatus(row({ takenOver: true }), NOW).text).toBe("yours");
    expect(rowStatus(row({ pausedUntil: inMinutes(7) }), NOW).text).toBe("paused 7 min");
    expect(rowStatus(row({ pausedUntil: inMinutes(-3) }), NOW).text).toBe("running");
    expect(rowStatus(row({ status: "awaiting_approval" }), NOW).text).toBe("Jarvis deciding");
    expect(rowStatus(row({ status: "error" }), NOW).glyph).toBe("✗");
    expect(rowStatus(row({ status: "idle" }), NOW).text).toBe("idle");
  });
});

describe("the status-line marker", () => {
  it("shows deciding, paused and taken over; nothing when Jarvis is off or not exposed", () => {
    expect(jarvisMarker(status(), { level: "full" }, NOW)).toBe("Jarvis deciding · full");
    expect(jarvisMarker(status(), { level: null }, NOW)).toBe("Jarvis deciding");
    expect(jarvisMarker(status(), { level: "full", pausedUntil: inMinutes(7) }, NOW)).toBe("Jarvis paused, returns in 7 min");
    expect(jarvisMarker(status(), { takenOver: true }, NOW)).toContain("you have this session");
    expect(jarvisMarker(status({ enabled: false }), { level: "full" }, NOW)).toBeNull();
    expect(jarvisMarker(status(), null, NOW)).toBeNull();
    expect(jarvisMarker(null, { level: "full" }, NOW)).toBeNull();
  });
});

describe("the panel", () => {
  const rows: JarvisOverviewRow[] = [
    row({ level: "full" }),
    row({ sessionId: "s-2", title: "배포 스크립트 점검", project: "/work/웹서비스", status: "awaiting_approval", waitingOnPerson: 1, level: "staged" }),
    row({ sessionId: "s-3", title: "api migration", pausedUntil: inMinutes(7), level: "full" }),
    row({ sessionId: null, external: { cli: "codex", id: "019abc" }, host: "terminal", title: "docs", status: "idle" }),
  ];
  const full: JarvisState = {
    ...initialJarvis,
    status: status(),
    rows,
    waiting: [
      { requestId: "a-1", kind: "approval", sessionId: "s-2", external: null, label: "웹서비스 · 배포 스크립트 점검", summary: "shell: git push origin main", request: approval() },
    ],
    decisions: [entry("d1", inMinutes(-1), { summary: "allowed shell: pytest", project: "/work/snowpea" })],
  };

  it("lists every section, with project, status, level and pause", () => {
    const text = panelLines(full, 100, 20, NOW)
      .map((line) => line.segments.map((s) => s.text).join(""))
      .join("\n");
    expect(text).toContain("◆ Jarvis");
    expect(text).toContain("Sessions (4)");
    expect(text).toContain("snowpea · fix auth");
    expect(text).toContain("paused 7 min");
    expect(text).toContain("waits for you (1)");
    expect(text).toContain("other terminal");
    expect(text).toContain("Waiting for you (1) · Ctrl+W answers");
    expect(text).toContain("웹서비스 · 배포 스크립트 점검 — shell: git push origin main");
    expect(text).toContain("Recent decisions");
    expect(text).toContain("jarvis: allowed shell: pytest (snowpea)");
  });

  it("never runs past the width in terminal cells, wide characters included", () => {
    for (const width of [60, 44, 30]) {
      for (const line of panelLines(full, width, 30, NOW)) {
        const cells = textWidth(line.segments.map((s) => s.text).join(""));
        expect(cells, `${line.key} at ${width}`).toBeLessThanOrEqual(width);
      }
    }
  });

  it("stays inside the row budget and says how many sessions it hid", () => {
    const many = { ...full, rows: Array.from({ length: 30 }, (_, i) => row({ sessionId: `s-${i}`, title: `task ${i}` })) };
    const lines = panelLines(many, 80, 10, NOW);
    expect(lines.length).toBeLessThanOrEqual(10);
    const text = lines.map((line) => line.segments.map((s) => s.text).join("")).join("\n");
    expect(text).toMatch(/… \d+ more \(\/jarvis sessions\)/);
    expect(text).toContain("Waiting for you");
  });

  it("explains an empty overview and a used-up budget", () => {
    const text = panelLines(
      { ...initialJarvis, status: status({ budget: { dailyTokens: 10, wakesPerHour: 5, exhausted: true }, problems: ["model unreachable"] }) },
      80,
      10,
      NOW,
    )
      .map((line) => line.segments.map((s) => s.text).join(""))
      .join("\n");
    expect(text).toContain("No supervised sessions yet.");
    expect(text).toContain("budget used up");
    expect(text).toContain("! model unreachable");
  });
});

describe("labels", () => {
  it("names a session by project and title, falling back to the id", () => {
    expect(sessionLabel(row())).toBe("snowpea · fix auth");
    expect(sessionLabel(row({ title: null }))).toBe("snowpea");
    expect(sessionLabel(row({ title: null, project: null, sessionId: "abcdef123456" }))).toBe("abcdef12");
  });

  it("shows no label for the session on screen, one for any other and for externals", () => {
    const rows = [row()];
    expect(requestLabel(approval(), rows, "s-1")).toBeNull();
    expect(requestLabel(approval(), rows, "s-9")).toBe("snowpea · fix auth");
    expect(requestLabel(approval({ sessionId: undefined }), rows, "s-9")).toBeNull();
    expect(
      requestLabel(approval({ sessionId: "ext:agy:1234567890", external: { cli: "agy", id: "1234567890" } }), rows, "s-1"),
    ).toBe("[ag] 12345678");
    expect(requestLabel(null, rows, "s-1")).toBeNull();
  });
});

describe("the delegation proposal", () => {
  const proposal = approval({
    tool: "delegation_set",
    personOnly: true,
    args: { scope: "project", target: "/work/snowpea", level: "full", allow: ["deploy", "secret_send"], expiresAt: inMinutes(90), note: "ship the release" },
  });

  it("recognises Jarvis's delegation_set approval and reads scope, level, guarded classes and expiry", () => {
    expect(isDelegationProposal(proposal)).toBe(true);
    expect(isDelegationProposal(approval())).toBe(false);
    expect(parseProposal(approval())).toBeNull();
    expect(parseProposal(proposal)).toEqual({
      scope: "project",
      target: "/work/snowpea",
      level: "full",
      allow: ["deploy", "secret_send"],
      expiresAt: inMinutes(90),
      note: "ship the release",
    });
    const text = proposalLines(parseProposal(proposal)!, NOW).join("\n");
    expect(text).toContain("project /work/snowpea");
    expect(text).toContain("full");
    expect(text).toContain("deploy, release, push; secrets, sending data out");
    expect(text).toContain("(in 1 h 30 min)");
    expect(text).toContain("ship the release");
  });

  it("reads a short proposal as the narrowest thing it could be, never wider", () => {
    const parsed = parseProposal(approval({ tool: "delegation_set", personOnly: true, args: {} }))!;
    expect(parsed).toMatchObject({ scope: "session", level: "staged", allow: [], expiresAt: null });
    const lines = proposalLines(parsed, NOW).join("\n");
    expect(lines).toContain("none — deploy, outside-project and secret/send actions still ask you");
    expect(lines).toContain("until the session ends");
    const unknownScope = parseProposal(approval({ tool: "delegation_set", personOnly: true, args: { scope: "everything", level: "total" } }))!;
    expect(unknownScope.scope).toBe("session");
    expect(unknownScope.level).toBe("staged");
  });
});

describe("/jarvis status", () => {
  it("lists the state, budget, pauses and problems", () => {
    const text = statusLines(
      status({
        budget: { dailyTokens: 1000, tokensToday: 10, wakesPerHour: 30, wakesThisHour: 2 },
        paused: [{ sessionId: "s-1", reason: "typing", until: inMinutes(5) }],
        hiddenProjects: ["/work/private"],
        problems: ["no model"],
      }),
      NOW,
    ).join("\n");
    expect(text).toContain("on, running");
    expect(text).toContain("2/30 wakes this hour, 10/1000 tokens today");
    expect(text).toContain("paused (you typed): s-1, returns in 5 min");
    expect(text).toContain("hidden from Jarvis: private");
    expect(text).toContain("! no model");
    expect(statusLines(status({ enabled: false }), NOW).join("\n")).toContain("/jarvis on");
  });
});
