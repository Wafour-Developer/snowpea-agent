import { describe, expect, it } from "vitest";

import { autoCompactSegment, compactionDivider } from "../src/layout/bottom.js";
import { waitingText } from "../src/layout/transcript.js";
import { commandResultText, appliesFromWords, commandsBesideTurnSupported, ranBesideTurn } from "../src/state/command-result.js";
import {
  globalOnlyLine,
  internalErrorLine,
  localOnlyLine,
  projectUntrustedLine,
  refusalOf,
  refusalWords,
  rpcErrorText,
  sessionErrorView,
} from "../src/state/errors.js";
import { recordOfListRow } from "../src/state/history.js";
import {
  OWN_FILES_LINE,
  normalizeConfig,
  problemItemText,
  saveProblems,
  statusLines,
  unknownCodePatch,
  unknownCodeSegments,
} from "../src/state/security.js";
import { initialState, reducer, waitingPrompts, type State } from "../src/state/store.js";
import {
  DEFAULT_TRUST_SCOPES,
  changedFiles,
  changedLines,
  commandText,
  concerns,
  initialSelection,
  normalizeChanged,
  normalizeTrust,
  quietScopes,
  reasonWords,
  setParams,
  statusLine,
  trustBody,
  trustRows,
  trustSupported,
  type TrustScope,
} from "../src/state/trust.js";
import { fieldError, jobTag, normalizeStatus, remoteJobsLine, summaryLines } from "../src/state/web-remote.js";

const failure = (code: string, message = "m", details?: unknown): unknown => Object.assign(new Error(message), { data: { code, details } });
const ev = (seq: number, kind: string, payload: Record<string, unknown>) => ({ sessionId: "s", seq, kind, payload }) as any;
const apply = (state: State, ...events: any[]): State => events.reduce((current, event) => reducer(current, { type: "session/event", event }), state);

describe("refusals in words (1.55.0)", () => {
  it("jarvis_person_only / local_only names the connection and what", () => {
    const text = rpcErrorText(failure("jarvis_person_only", "x", { reason: "local_only", kind: "remote_login", what: "command" }));
    expect(text).toBe("Only snowpea on this machine can do that (command) — not a remote login.");
    expect(localOnlyLine({ kind: "browser" })).toContain("not a browser host");
    expect(localOnlyLine({})).toBe("Only snowpea on this machine can do that — not this connection.");
  });

  it("invalid_params / global_only says the key is global", () => {
    expect(rpcErrorText(failure("invalid_params", "x", { reason: "global_only", key: "security.unknownCode" }))).toBe(globalOnlyLine({ key: "security.unknownCode" }));
    expect(globalOnlyLine({ key: "security.unknownCode" })).toBe("security.unknownCode can only be set for all projects (global), not for one project.");
  });

  it("an internal error shows its id, from details.errorId and not from the message", () => {
    const text = rpcErrorText(failure("internal", "anything at all", { errorId: "a1b2c3d4e5f6" }));
    expect(text).toBe(internalErrorLine("a1b2c3d4e5f6"));
    expect(text).toContain("a1b2c3d4e5f6");
  });

  it("trust_changed is a plain sentence", () => {
    expect(rpcErrorText(failure("trust_changed", "x", { changed: ["a"] }))).toContain("Nothing was trusted");
  });

  it("every detail is read defensively", () => {
    expect(rpcErrorText(failure("jarvis_person_only", "x", { reason: "local_only", what: 3 }))).toContain("Only snowpea on this machine");
    expect(rpcErrorText(failure("internal", "x", { errorId: 7 }))).toContain("x");
  });

  it("project_untrusted is one calm notice ending (/trust), once", () => {
    const view = sessionErrorView("project_untrusted", "This project's hooks are off until you trust it");
    expect(view).toEqual({ kind: "notice", text: "This project's hooks are off until you trust it  (/trust)" });
    expect(projectUntrustedLine("Already says it (/trust)")).toBe("Already says it (/trust)");
    expect(projectUntrustedLine("")).toMatch(/\(\/trust\)$/);
  });

  it("reads a refused reply from the pending announcement", () => {
    expect(refusalOf(null)).toBeNull();
    expect(refusalOf({})).toBeNull();
    const refused = refusalOf({ code: "jarvis_person_only", message: "needs a person", reason: "person_only" });
    expect(refused).toEqual({ code: "jarvis_person_only", message: "needs a person", reason: "person_only" });
    expect(refusalWords(refused!)).toContain("only a person on this machine can decide this");
    expect(refusalWords({ code: "x", message: "", reason: "mode_raise" })).toContain("raising the mode needs a person");
  });
});

describe("commands beside a turn", () => {
  it("knows the capability and the answer", () => {
    expect(commandsBesideTurnSupported(["commandsBesideTurn"])).toBe(true);
    expect(commandsBesideTurnSupported(undefined)).toBe(false);
    expect(ranBesideTurn({ turnId: "c-1", besideTurn: true })).toBe(true);
    expect(ranBesideTurn({ turnId: "t-1" })).toBe(false);
    expect(ranBesideTurn({ turnId: "t-1", besideTurn: null })).toBe(false);
    expect(ranBesideTurn(null)).toBe(false);
  });

  it("says when a change applies", () => {
    expect(appliesFromWords("next_tool_call")).toBe("applies from the next tool call");
    expect(appliesFromWords("next_request")).toBe("applies from the next request");
    expect(appliesFromWords("next_turn")).toBe("applies from the next turn");
    expect(appliesFromWords(null)).toBeNull();
    expect(appliesFromWords("later")).toBeNull();
  });

  it("builds a compact aside and folds a long answer", () => {
    expect(commandResultText({ command: "/model fast", ok: true, text: "model: fast", appliesFrom: "next_turn" })).toBe("/model fast — model: fast (applies from the next turn)");
    expect(commandResultText({ command: "/x", ok: false, text: "unknown command: /x", appliesFrom: "next_turn" })).toBe("✗ /x — unknown command: /x");
    const long = commandResultText({ command: "/tools", ok: true, text: Array.from({ length: 10 }, (_, i) => `tool ${i}`).join("\n") });
    expect(long.split("\n")).toHaveLength(7);
    expect(long).toContain("… 4 more lines");
  });

  it("is an aside: no turn starts, nothing is queued, a streaming answer is untouched and the aside lands after it", () => {
    let state = apply(
      initialState,
      ev(1, "turn.started", { turnId: "t-1", prompt: "go" }),
      ev(2, "message.delta", { text: "Hello" }),
      ev(3, "command.result", { commandId: "c-1", command: "/effort high", ok: true, text: "effort: high", appliesFrom: "next_turn" }),
      ev(4, "message.delta", { text: " world" }),
    );
    expect(state.turnActive).toBe(true);
    expect(state.queued).toEqual([]);
    expect(state.messages.filter((m) => m.role === "assistant")).toHaveLength(1);
    expect(state.messages.find((m) => m.role === "assistant")?.text).toBe("Hello world");
    // Waiting behind the open answer, but not listed as a queued prompt.
    expect(state.deferredPrompts).toHaveLength(1);
    expect(waitingPrompts(state)).toEqual([]);
    state = apply(state, ev(5, "message.done", { text: "Hello world" }));
    const aside = state.messages.find((m) => m.side);
    expect(aside?.text).toBe("/effort high — effort: high (applies from the next turn)");
    expect(state.turnActive).toBe(true);
  });

  it("lands at once when nothing is streaming", () => {
    const state = apply(initialState, ev(1, "command.result", { commandId: "c-1", command: "/tools", ok: true, text: "3 tools", appliesFrom: null }));
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ role: "system", side: true, text: "/tools — 3 tools" });
    expect(state.turnActive).toBe(false);
  });
});

describe("tool progress, context and compaction", () => {
  const withCall = apply(initialState, ev(1, "tool.call", { callId: "c1", name: "browser_snapshot", args: {} }));

  it("keeps a waiting state with its note until the host says running", () => {
    const waiting = apply(withCall, ev(2, "tool.progress", { callId: "c1", name: "t", state: "waiting", note: "waiting for the page" }));
    expect(waiting.toolCalls[0].waiting).toEqual({ note: "waiting for the page" });
    // An ordinary output chunk does not clear it.
    const chunk = apply(waiting, ev(3, "tool.progress", { callId: "c1", name: "t", chunk: "line\n" }));
    expect(chunk.toolCalls[0].waiting).toEqual({ note: "waiting for the page" });
    const running = apply(chunk, ev(4, "tool.progress", { callId: "c1", name: "t", state: "running" }));
    expect(running.toolCalls[0].waiting).toBeUndefined();
    expect(waitingText("waiting for the page")).toBe("waiting: waiting for the page");
    expect(waitingText("  ")).toBe("waiting…");
  });

  it("reads autoCompactAt, null and absent", () => {
    expect(apply(initialState, ev(1, "context", { used: 10, window: 100, autoCompactAt: 85 })).context?.autoCompactAt).toBe(85);
    expect(apply(initialState, ev(1, "context", { used: 10, window: 100, autoCompactAt: null })).context?.autoCompactAt).toBeNull();
    expect(apply(initialState, ev(1, "context", { used: 10, window: 100 })).context?.autoCompactAt).toBeNull();
  });

  it("marks where the next turn compacts, in percent or tokens, and nothing without it", () => {
    const base = { used: 68000, window: 200000, percent: 34, estimated: false };
    expect(autoCompactSegment({ ...base, autoCompactAt: 170000 })?.text).toBe("auto-compact at 85%");
    expect(autoCompactSegment({ ...base, window: null, autoCompactAt: 170000 })?.text).toBe("auto-compact at 170.0k");
    expect(autoCompactSegment({ ...base, autoCompactAt: null })).toBeNull();
    expect(autoCompactSegment({ ...base })).toBeNull();
    expect(autoCompactSegment(null)).toBeNull();
  });

  it("keeps the compaction reason and words an overflow", () => {
    const state = apply(initialState, ev(1, "compaction", { before: 190000, after: 30000, reason: "overflow", auto: true }));
    expect(state.compactions[0].reason).toBe("overflow");
    expect(compactionDivider(190000, 30000, 80, "overflow")).toContain("compacted: request too long (190.0k → 30.0k tokens)");
    expect(compactionDivider(190000, 30000, 80, "auto")).toContain(" compacted (190.0k → 30.0k tokens) ");
    expect(apply(initialState, ev(1, "compaction.started", { before: 1, reason: "overflow" })).compacting?.reason).toBe("overflow");
  });
});

describe("refusals are kept for the open request and dropped when it is answered", () => {
  it("stores a refusal and clears it on resolution", () => {
    const refused = { code: "jarvis_person_only", message: "m", reason: "person_only" };
    let state = reducer(initialState, { type: "refusal", requestId: "a-1", refused });
    expect(state.refusals["a-1"]).toEqual(refused);
    state = reducer(state, { type: "approval/resolved", requestId: "a-1" });
    expect(state.refusals).toEqual({});
    state = reducer(initialState, { type: "refusal", requestId: "q-1", refused });
    state = reducer(state, { type: "question/resolved", requestId: "q-1" });
    expect(state.refusals).toEqual({});
    expect(reducer(state, { type: "refusal", requestId: "x", refused: null })).toBe(state);
  });
});

describe("security.get 1.55.0", () => {
  it("defaults unknownCode to ask and reads allow", () => {
    expect(normalizeConfig({} as never).unknownCode).toBe("ask");
    expect(normalizeConfig({ unknownCode: "allow" } as never).unknownCode).toBe("allow");
    expect(normalizeConfig({ unknownCode: "maybe" } as never).unknownCode).toBe("ask");
  });

  it("draws the row and says it in the status", () => {
    expect(unknownCodeSegments("ask").map((s) => s.text).join("")).toBe("Code snowpea can't tell about: [ask] / allow");
    expect(unknownCodeSegments("allow").map((s) => s.text).join("")).toBe("Code snowpea can't tell about: ask / [allow]");
    expect(unknownCodePatch("allow")).toEqual({ security: { unknownCode: "allow" } });
    const text = statusLines(normalizeConfig({ unknownCode: "allow" } as never), null).join("\n");
    expect(text).toContain("allow (runs without asking)");
    expect(text).toContain(OWN_FILES_LINE);
  });

  it("uses problemItems, not the sentences", () => {
    const config = normalizeConfig({
      problems: ["ignored sentence"],
      problemItems: [{ entry: "rm", list: "commands", reason: "the program cannot be only a wildcard" }, { list: null, reason: "file unreadable" }, { reason: "" }],
    } as never);
    expect(config.problemItems).toHaveLength(2);
    expect(config.problems).toEqual(['blocked command "rm": the program cannot be only a wildcard', "entry: file unreadable"]);
    expect(problemItemText({ list: "paths", entry: "/x", reason: "must be absolute" })).toBe('protected path "/x": must be absolute');
    // An older core: the sentences still show.
    expect(normalizeConfig({ problems: ["old sentence"] } as never).problems).toEqual(["old sentence"]);
  });

  it("splits a settings.set refusal by its structured items", () => {
    const error = Object.assign(new Error("whatever English"), {
      data: {
        code: "invalid_params",
        details: {
          problems: ["wording that is not parsed"],
          problemItems: [
            { entry: "*", list: "commands", reason: "that would block every command" },
            { entry: "rel/path", list: "paths", reason: "must be absolute" },
            { list: null, reason: "something else" },
          ],
        },
      },
    });
    const { entries, others } = saveProblems(error);
    expect(entries).toEqual([
      { tab: "commands", text: "*", reason: "that would block every command" },
      { tab: "paths", text: "rel/path", reason: "must be absolute" },
    ]);
    expect(others).toEqual(["entry: something else"]);
  });
});

describe("remote web access 1.55.0", () => {
  it("maps listen failures to a field from details.reason, without reading the message", () => {
    const at = (reason: string, field?: string) => fieldError(failure("unavailable", "completely unrelated words", { ...(field ? { field } : {}), reason }));
    expect(at("in_use").field).toBe("port");
    expect(at("permission").field).toBe("port");
    expect(at("out_of_range").field).toBe("port");
    expect(at("bad_address").field).toBe("host");
    expect(at("required").field).toBe("username");
    // The field core names wins.
    expect(at("in_use", "host").field).toBe("host");
    expect(at("bad_address", "host").field).toBe("host");
    expect(at("required", "username").field).toBe("username");
    // Older cores still fall back to the message.
    expect(fieldError(failure("unavailable", "address already in use")).field).toBe("port");
  });

  it("counts jobs made from a remote login and says they keep running", () => {
    expect(normalizeStatus({ remoteJobs: 2 }).remoteJobs).toBe(2);
    expect(normalizeStatus({}).remoteJobs).toBe(0);
    expect(normalizeStatus({ remoteJobs: -1 }).remoteJobs).toBe(0);
    expect(remoteJobsLine(normalizeStatus({ remoteJobs: 1 }))).toContain("1 scheduled job made from a remote login keeps running");
    expect(remoteJobsLine(normalizeStatus({ remoteJobs: 3 }))).toContain("3 scheduled jobs");
    expect(remoteJobsLine(normalizeStatus({}))).toBeNull();
    expect(summaryLines(normalizeStatus({ remoteJobs: 2 })).join("\n")).toContain("2 scheduled jobs");
  });

  it("tags a remote login's job", () => {
    expect(jobTag("remote_login")).toBe("[remote]");
    expect(jobTag(null)).toBe("");
    expect(jobTag(undefined)).toBe("");
  });
});

describe("closed sessions in the picker", () => {
  it("uses title and lastActivityAt when core sent them", () => {
    const row = {
      sessionId: "s-9",
      workdir: "/w",
      title: "Fix the build",
      lastPrompt: "old prompt",
      createdAt: "2026-11-01T00:00:00Z",
      lastActivityAt: "2026-11-15T10:00:00Z",
      status: "idle",
      running: false,
    };
    const record = recordOfListRow(row);
    expect(record.firstPrompt).toBe("Fix the build");
    expect(record.at).toBe(Date.parse("2026-11-15T10:00:00Z"));
  });

  it("falls back to the last prompt and the creation time (an older core)", () => {
    const record = recordOfListRow({ sessionId: "s-1", workdir: "/w", lastPrompt: "hello", createdAt: "2026-11-01T00:00:00Z" });
    expect(record.firstPrompt).toBe("hello");
    expect(record.at).toBe(Date.parse("2026-11-01T00:00:00Z"));
    expect(recordOfListRow({ sessionId: "s", workdir: "/w", title: "  ", lastPrompt: "p" }).firstPrompt).toBe("p");
  });
});

describe("project trust, pure", () => {
  const raw = {
    workdir: "/work/p",
    trusted: true,
    scopes: ["hooks"],
    by: "tui",
    at: "2026-11-16T07:00:00Z",
    files: [
      { path: ".snowpea/hooks.json", scope: "hooks", state: "changed", summary: "x", commands: [{ head: "a".repeat(120), tail: "z".repeat(60), length: 400, label: "Stop" }] },
      { path: ".mcp.json", scope: "mcp", state: "bogus", summary: "" },
    ],
    digests: { ".snowpea/hooks.json": "sha256:1", ".mcp.json": "sha256:2", bad: 3 },
    notPinned: [{ command: { head: "npx tool", tail: "", length: 8 }, reason: "package-runner", source: ".mcp.json" }, { command: null, reason: "computed", source: "x" }],
    runs: [{ path: "s.sh", digest: "sha256:3", state: "new", by: [".snowpea/hooks.json"] }],
  };

  it("knows the capability", () => {
    expect(trustSupported(["projectTrust"])).toBe(true);
    expect(trustSupported([])).toBe(false);
    expect(trustSupported(undefined)).toBe(false);
  });

  it("normalises the status defensively", () => {
    const view = normalizeTrust(raw as never);
    expect(view.files[0].commands[0]).toEqual({ head: "a".repeat(120), tail: "z".repeat(60), length: 400, label: "Stop" });
    expect(view.files[1].state).toBe("untrusted");
    expect(view.digests).toEqual({ ".snowpea/hooks.json": "sha256:1", ".mcp.json": "sha256:2" });
    expect(view.notPinned).toHaveLength(1);
    expect(view.runs[0]).toEqual({ path: "s.sh", state: "new", by: [".snowpea/hooks.json"] });
    expect(normalizeTrust(null)).toMatchObject({ trusted: false, files: [], digests: {}, notPinned: [], runs: [] });
  });

  it("preselects the default scopes (plus what is trusted), never mode or externalCli on its own", () => {
    const view = normalizeTrust(raw as never);
    expect([...initialSelection(view)].sort()).toEqual([...DEFAULT_TRUST_SCOPES].sort());
    expect(initialSelection(view).has("mode")).toBe(false);
    expect(initialSelection(view).has("externalCli")).toBe(false);
    expect(initialSelection(normalizeTrust({ scopes: ["mode"] } as never)).has("mode")).toBe(true);
  });

  it("sends exactly the digests of the status shown, in scope order, as a copy", () => {
    const view = normalizeTrust(raw as never);
    const params = setParams(view, new Set<TrustScope>(["settings", "hooks"]));
    expect(params).toEqual({ workdir: "/work/p", scopes: ["hooks", "settings"], digests: view.digests });
    expect(params.digests).not.toBe(view.digests);
    expect(setParams(normalizeTrust({ workdir: "/w" } as never), new Set<TrustScope>(["hooks"])).digests).toEqual({});
  });

  it("shows a command's head and tail and its full length; a short one just itself", () => {
    const view = normalizeTrust(raw as never);
    const text = commandText(view.files[0].commands[0]);
    expect(text.startsWith("a".repeat(120))).toBe(true);
    expect(text).toContain(" … ");
    expect(text.endsWith(`${"z".repeat(60)} (400 characters)`)).toBe(true);
    expect(commandText({ head: "npm run lint", tail: "", length: 12, label: "" })).toBe("npm run lint");
  });

  it("words every unpinned reason and keeps one it does not know", () => {
    for (const reason of ["computed", "project-script", "package-runner", "module", "missing", "too-large", "too-many", "unreadable"]) {
      expect(reasonWords(reason)).not.toBe(reason);
    }
    expect(reasonWords("from-the-future")).toBe("from-the-future");
  });

  it("lists the rows the cursor visits: scopes with files, mode, externalCli, runs, unpinned", () => {
    const view = normalizeTrust(raw as never);
    expect(trustRows(view)).toEqual([
      { kind: "scope", scope: "hooks" },
      { kind: "scope", scope: "mcp" },
      { kind: "scope", scope: "mode" },
      { kind: "scope", scope: "externalCli" },
      { kind: "runs" },
      { kind: "notPinned" },
    ]);
    expect(quietScopes(view)).toEqual(["commands", "agents", "prompts", "settings"]);
  });

  it("wraps every body line to the width, with the tail present", () => {
    const view = normalizeTrust(raw as never);
    const body = trustBody(view, initialSelection(view), 0, 40);
    const text = body.lines.map((line) => line.segments.map((s) => s.text).join(""));
    for (const line of text) expect(Array.from(line).length).toBeLessThanOrEqual(40);
    // The 60-character tail is all there, wrapped over lines but never cut.
    expect((text.join("").match(/z/g) ?? []).length).toBe(60);
    expect(body.start).toBe(0);
    expect(body.end).toBeGreaterThan(body.start);
  });

  it("summarises in one line", () => {
    expect(statusLine(normalizeTrust({ workdir: "/work/p", trusted: false, files: [{ path: "a", scope: "hooks", state: "untrusted" }] } as never))).toBe(
      "Project p: not trusted — 1 control file waits for you (/trust)",
    );
    expect(statusLine(normalizeTrust(raw as never))).toBe("Project p: trusted for Hooks · 2 to review (/trust)");
    expect(statusLine(normalizeTrust({ workdir: "/work/p", trusted: true, scopes: ["hooks", "mcp"], files: [] } as never))).toBe("Project p: trusted for Hooks, MCP servers");
  });

  it("reads trust_changed's files and says nothing was trusted", () => {
    const files = changedFiles(failure("trust_changed", "m", { changed: ["a"], added: ["b", 3], missing: ["c"] }));
    expect(files).toEqual({ changed: ["a"], added: ["b"], missing: ["c"] });
    const lines = changedLines(files);
    expect(lines[0]).toContain("Nothing was trusted");
    expect(lines[1]).toBe("changed: a · new: b · gone: c");
    expect(lines[2]).toContain("press t again");
    expect(changedFiles(null)).toEqual({ changed: [], added: [], missing: [] });
  });

  it("reads project.trust.changed and which project it concerns", () => {
    const set = normalizeChanged({ workdir: "/work/p", scopes: ["hooks", "x"], by: "cli", reason: "set" });
    expect(set).toEqual({ workdir: "/work/p", scopes: ["hooks"], by: "cli", reason: "set", projects: [] });
    expect(concerns(set, "/work/p")).toBe(true);
    expect(concerns(set, "/work/q")).toBe(false);
    expect(concerns(normalizeChanged({ workdir: "", reason: "store_lost" }), "/work/q")).toBe(true);
    expect(concerns(normalizeChanged({ reason: "migration", projects: ["/a"] }), "/work/q")).toBe(true);
    expect(normalizeChanged(undefined).reason).toBe("set");
  });
});
