import { afterEach, describe, expect, it } from "vitest";

import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import { commandResultText } from "../src/state/command-result.js";
import {
  DISK_NOTICE_LINE,
  choiceLines,
  normalizeWaiting,
  resultLine,
  sectionNotice,
} from "../src/state/disk-settings.js";
import { CLOSED_LINE, localOnlyLine, rpcErrorText, sessionErrorView } from "../src/state/errors.js";
import { parseSettingsCommand, settingsSubCommands } from "../src/slash/settings.js";
import { highEscalationLine, rememberChosenScope, respondResultNote } from "../src/state/jarvis.js";
import { SECURITY_CLAIM, statusLines, normalizeConfig } from "../src/state/security.js";
import { initialState, reducer, type State } from "../src/state/store.js";
import {
  TAMPER_LINE,
  concerns,
  limitLines,
  normalizeChanged,
  normalizeTrust,
  reasonWords,
  trustBody,
  type TrustScope,
} from "../src/state/trust.js";
import { MAX_MODE_EXPLAIN, maxModePatch, maxModeRow, nextMaxMode, normalizeStatus, summaryLines } from "../src/state/web-remote.js";

const failure = (code: string, message = "m", details?: unknown): unknown => Object.assign(new Error(message), { data: { code, details } });
const ev = (seq: number, kind: string, payload: Record<string, unknown>) => ({ sessionId: "s", seq, kind, payload }) as any;
const apply = (state: State, ...events: any[]): State => events.reduce((current, event) => reducer(current, { type: "session/event", event }), state);

afterEach(() => resetUiLanguage());

describe("web.remote.status: warning and maxMode", () => {
  it("keeps core's warning, puts it first in /web status, and reads the maxMode", () => {
    const status = normalizeStatus({ enabled: true, listening: true, host: "0.0.0.0", port: 8787, warning: "Anyone who has the link can use snowpea as you.", maxMode: "accept" } as never);
    expect(status.maxMode).toBe("accept");
    const lines = summaryLines(status);
    expect(lines[0]).toBe("Anyone who has the link can use snowpea as you.");
    expect(lines.find((line) => line.startsWith("Highest mode"))).toBe(`${maxModeRow("accept")}. ${MAX_MODE_EXPLAIN()}`);
    expect(maxModeRow("accept")).toBe("Highest mode for remote sign-ins: plan / [accept] / auto");
  });

  it("has no maxMode from an older core, cycles plan, accept, auto and patches web.remote.maxMode", () => {
    expect(normalizeStatus({} as never).maxMode).toBeNull();
    expect(summaryLines(normalizeStatus({} as never)).some((line) => line.startsWith("Highest mode"))).toBe(false);
    expect(["plan", "accept", "auto"].map((mode) => nextMaxMode(mode as never))).toEqual(["accept", "auto", "plan"]);
    expect(maxModePatch("plan")).toEqual({ web: { remote: { maxMode: "plan" } } });
  });

  it("the explanation is honest about who is stopped", () => {
    expect(MAX_MODE_EXPLAIN()).toContain("unattended runs");
    expect(MAX_MODE_EXPLAIN()).toContain("still approve");
    setUiLanguage("ko");
    expect(MAX_MODE_EXPLAIN()).toContain("무인 실행");
  });
});

describe("refusals in words (security review 7)", () => {
  it("mode_raise with details.max says the limit for remote sign-ins", () => {
    const text = rpcErrorText(failure("jarvis_person_only", "x", { reason: "mode_raise", kind: "remote_login", max: "accept", to: "auto" }));
    expect(text).toContain("remote sign-ins are limited to accept");
    expect(rpcErrorText(failure("jarvis_person_only", "x", { reason: "mode_raise" }))).toContain("raising the permission mode needs a person");
  });

  it("local_only names what, in words", () => {
    for (const [what, words] of [
      ["target", "where a saved server connects"],
      ["variable", "${VAR}"],
      ["agent", "global agent"],
      ["command", "global command"],
      ["builtin_name", "built-in"],
      ["front_matter", "front matter"],
    ] as const) {
      expect(localOnlyLine({ kind: "remote_login", what })).toContain(words);
    }
    expect(localOnlyLine({ what: "something_new" })).toContain("(something_new)");
    setUiLanguage("ko");
    expect(localOnlyLine({ kind: "remote_login", what: "target" })).toContain("접속 대상");
  });

  it("snowpea_home and details.kind closed", () => {
    expect(rpcErrorText(failure("invalid_params", "x", { reason: "snowpea_home", what: "home" }))).toContain("snowpea's own home");
    expect(rpcErrorText(failure("invalid_params", "x", { reason: "snowpea_home", what: "parent" }))).toContain("holds snowpea's home");
    expect(rpcErrorText(failure("jarvis_person_only", "x", { kind: "closed" }))).toBe(CLOSED_LINE());
  });

  it("an internal error shows its id, as a call failure, an error event and a command result", () => {
    expect(rpcErrorText(failure("internal", "internal error (id ab12cd)", { errorId: "ab12cd" }))).toContain("id ab12cd");
    const view = sessionErrorView("internal", "internal error (id ab12cd)", { errorId: "ab12cd" });
    expect(view.kind).toBe("error");
    expect(view.text).toContain("id ab12cd");
    expect(commandResultText({ command: "/review", ok: false, text: "internal error (id ab12cd)", details: { errorId: "ab12cd" } })).toContain("id ab12cd");
    // The id is added when the text lacks it.
    expect(commandResultText({ command: "/review", ok: false, text: "failed", details: { errorId: "zz9" } })).toContain("(id zz9)");
    // A command with no id is unchanged.
    expect(commandResultText({ command: "/effort", ok: true, text: "high" })).toBe("/effort — high");
  });
});

describe("settings edited on disk", () => {
  it("normalizes waitingKeys; absence means an older core", () => {
    expect(normalizeWaiting(undefined)).toBeUndefined();
    expect(normalizeWaiting([])).toEqual([]);
    expect(normalizeWaiting(["web.remote", "security", 3])).toEqual(["security", "web.remote"]);
  });

  it("words: the notice, the per-section line and the choice", () => {
    expect(DISK_NOTICE_LINE()).toBe(
      "settings.json was edited outside snowpea: security / Jarvis / remote access settings in it wait for you (/settings disk)",
    );
    expect(sectionNotice(["security"], "jarvis")).toBeNull();
    expect(sectionNotice(["jarvis"], "jarvis")).toContain("the Jarvis settings in it wait for you (/settings disk)");
    expect(choiceLines(["security", "jarvis"]).join(" ")).toContain("security, Jarvis");
    setUiLanguage("ko");
    expect(DISK_NOTICE_LINE()).toContain("/settings disk");
    expect(DISK_NOTICE_LINE()).toContain("수정");
  });

  it("prints {applied, discarded}", () => {
    expect(resultLine({ applied: ["security"], discarded: [] })).toBe("applied from the file: security");
    expect(resultLine({ applied: [], discarded: ["web.remote"] })).toContain("kept the current settings, file written back: remote access");
    expect(resultLine({ applied: [], discarded: [] })).toContain("Nothing was waiting");
  });

  it("/settings and /settings disk are the one command", () => {
    expect(parseSettingsCommand("/settings")).toEqual({ kind: "disk" });
    expect(parseSettingsCommand("/settings disk")).toEqual({ kind: "disk" });
    expect(parseSettingsCommand("/settings nope")).toEqual({ kind: "invalid", usage: "/settings disk" });
    expect(parseSettingsCommand("/set")).toBeNull();
    expect(settingsSubCommands("/settings d").map((c) => c.name)).toEqual(["settings disk"]);
  });
});

describe("project trust: limits, reasons, tamper", () => {
  const raw = {
    workdir: "/work/p",
    trusted: false,
    scopes: [],
    files: [],
    notPinned: [
      { command: { head: "../x.sh", tail: "", length: 7 }, reason: "outside-project", source: ".snowpea/hooks.json" },
      { command: { head: "./l.sh", tail: "", length: 6 }, reason: "symlink", source: ".snowpea/hooks.json" },
    ],
    limits: ["Only the listed files are pinned, not what they load."],
  } as never;

  it("reads limits and draws them under the lists", () => {
    const view = normalizeTrust(raw);
    expect(view.limits).toEqual(["Only the listed files are pinned, not what they load."]);
    expect(limitLines(view, 56).map((line) => line.segments.map((s) => s.text).join("")).join(" ")).toContain("Only the listed files are pinned");
    expect(normalizeTrust({ workdir: "/w" } as never).limits).toEqual([]);
  });

  it("outside-project and symlink are told in words, in the Not pinned block too", () => {
    expect(reasonWords("outside-project")).toContain("outside the project");
    expect(reasonWords("symlink")).toContain("symbolic link");
    const body = trustBody(normalizeTrust(raw), new Set<TrustScope>(), 0, 56).lines.map((line) => line.segments.map((s) => s.text).join("")).join("\n");
    expect(body).toContain("symbolic link");
    expect(body).toContain("outside the project");
  });

  it("a tamper is not a trust change", () => {
    const change = normalizeChanged({ workdir: "", scopes: [], by: null, reason: "tamper", projects: [] });
    expect(change.reason).toBe("tamper");
    expect(concerns(change, "/work/p")).toBe(false);
    expect(concerns(normalizeChanged({ workdir: "", reason: "store_lost" }), "/work/p")).toBe(true);
    expect(TAMPER_LINE()).toBe("Something outside snowpea tried to change the trust records; nothing was changed.");
  });

  it("the project_untrusted notice arrives with the first turn, once, in order", () => {
    // Nothing at creation; the notice, then turn.started, then the turn.
    const created = apply(initialState);
    expect(created.messages).toHaveLength(0);
    const state = apply(
      created,
      ev(1, "error", { code: "project_untrusted", message: "This project's own hooks, MCP servers and commands are switched off until you trust it. (/trust)" }),
      ev(2, "turn.started", { turnId: "t1" }),
      ev(3, "message.delta", { turnId: "t1", text: "hi" }),
      ev(4, "message.done", { turnId: "t1" }),
    );
    const notices = state.messages.filter((m) => m.text.includes("switched off until you trust it"));
    expect(notices).toHaveLength(1);
    expect(state.messages.indexOf(notices[0])).toBe(0);
    expect(state.lastTurnError).toBeNull();
    // A second turn adds no second notice.
    const again = apply(state, ev(5, "turn.started", { turnId: "t2" }));
    expect(again.messages.filter((m) => m.text.includes("switched off until you trust it"))).toHaveLength(1);
  });
});

describe("security status: the honest claim", () => {
  it("is two sentences and is in /security status", () => {
    const claim = SECURITY_CLAIM();
    expect(claim.split(". ").length).toBe(2);
    expect(claim).toContain("In auto mode the agent runs programs as you");
    expect(claim).toContain("use accept mode");
    const config = normalizeConfig({ blockedCommands: { presets: [], custom: [] }, protectedPaths: { presets: [], custom: [] }, effective: { commands: [], paths: [] } } as never);
    expect(statusLines(config, null)).toContain(claim);
    expect(statusLines(config, null, "settings.json was edited outside snowpea")[1]).toBe("! settings.json was edited outside snowpea");
    setUiLanguage("ko");
    expect(SECURITY_CLAIM()).toContain("accept 모드");
  });
});

describe("security.get problems that are not skipped entries", () => {
  it("tells the disk-change and tamper sentences as notices, not as skipped entries", () => {
    const disk = "settings.json changed on disk: the security settings in it are not applied and wait for you (settings.acceptDiskChange)";
    const config = normalizeConfig({ problems: [disk, "unknown preset 'x'"], problemItems: [{ list: "commands", entry: null, reason: "unknown preset 'x'" }] } as never);
    expect(config.notices).toEqual([disk]);
    expect(config.problems).toHaveLength(1);
    expect(statusLines(config, null).filter((line) => line.includes("settings.acceptDiskChange"))).toEqual([`! ${disk}`]);
    // An older core (no problemItems) keeps its sentences as skipped entries.
    const older = normalizeConfig({ problems: ["unknown preset 'x'"] } as never);
    expect(older.notices).toEqual([]);
    expect(older.problems).toEqual(["unknown preset 'x'"]);
  });
});

describe("approvals and notices", () => {
  it("the respond result names the applied scope when it is narrower, once", () => {
    rememberChosenScope("r1", "allow", "always");
    expect(respondResultNote("r1", { scope: "session" }, undefined)).toBe('Allowed for this session only — core kept it narrower than "always"');
    // Taken: the approval.resolved path finds nothing left to say.
    expect(respondResultNote("r1", { scope: "session" }, undefined)).toBeNull();
    rememberChosenScope("r2", "allow", "session");
    expect(respondResultNote("r2", { scope: "session" }, undefined)).toBeNull();
    expect(respondResultNote("r3", { scope: "session" }, undefined)).toBeNull();
  });

  it("a high-importance Jarvis notice is one line", () => {
    expect(highEscalationLine("Something  outside\nsnowpea")).toBe("Jarvis: Something outside snowpea");
  });
});
