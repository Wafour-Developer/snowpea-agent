import { describe, expect, it } from "vitest";

import { parseWebCommand, webSubCommands } from "../src/slash/web.js";
import {
  MASK,
  MIN_PASSWORD,
  PLAIN_HTTP_WARNING,
  fieldError,
  hidden,
  initialForm,
  listenLine,
  listenParams,
  localPersonOnlyLine,
  maskLink,
  modeLine,
  normalizeStatus,
  osc52Sequence,
  osc52Supported,
  passwordProblem,
  sessionsLine,
  shownLink,
  summaryLines,
  validateForm,
  webRemoteSupported,
} from "../src/state/web-remote.js";

const LINK = "http://192.168.1.5:8787/web/?t=Zx9-secretTOKEN_value";

const listening = normalizeStatus({
  enabled: true,
  listening: true,
  host: "0.0.0.0",
  port: 8787,
  mode: "token",
  passwordSet: false,
  username: "",
  listenUrls: ["http://192.168.1.5:8787/web/", "http://10.0.0.7:8787/web/"],
  accessUrl: LINK,
  sessions: 2,
  turnsRunning: 1,
  plainHttp: true,
  warning: null,
});

describe("the status", () => {
  it("is gated on the webRemoteControl capability", () => {
    expect(webRemoteSupported(["webRemoteControl"])).toBe(true);
    expect(webRemoteSupported(["securityLists"])).toBe(false);
    expect(webRemoteSupported(undefined)).toBe(false);
  });

  it("normalises a missing or odd answer without throwing", () => {
    const empty = normalizeStatus(null);
    expect(empty).toMatchObject({ listening: false, enabled: false, port: 0, mode: "token", sessions: 0, listenUrls: [], accessUrl: null, plainHttp: true });
    expect(normalizeStatus({ port: "x" as never, listenUrls: [1, "http://a/"] as never }).listenUrls).toEqual(["http://a/"]);
  });

  it("strips terminal escapes from every string", () => {
    const dirty = "a\u001b]52;c;QQ==\u0007b";
    const status = normalizeStatus({ error: dirty, username: dirty, accessUrl: `http://h/web/?t=${dirty}` });
    expect(status.error).toBe("ab");
    expect(status.username).toBe("ab");
    expect(status.accessUrl).not.toContain("\u001b");
  });

  it("says whether it listens and why not", () => {
    expect(listenLine(listening)).toBe("Listening on 0.0.0.0:8787");
    expect(listenLine(normalizeStatus({ enabled: false, host: "0.0.0.0", port: 8787 }))).toBe("Not listening");
    expect(listenLine(normalizeStatus({ enabled: true, error: "port 8787 is in use" }))).toBe("Not listening — port 8787 is in use");
    expect(listenLine(normalizeStatus({ enabled: true }))).toContain("should be on");
  });

  it("describes the sign-in mode and who is in", () => {
    expect(modeLine(listening)).toBe("Sign-in: access link, no password");
    expect(modeLine(normalizeStatus({ mode: "password", username: "me", passwordSet: true }))).toContain("user me");
    expect(modeLine(normalizeStatus({ mode: "password", passwordSet: true }))).toContain("no username");
    expect(sessionsLine(listening)).toBe("2 signed in · 1 turn running");
    expect(sessionsLine({ ...listening, turnsRunning: 3 })).toContain("3 turns running");
    expect(sessionsLine({ ...listening, turnsRunning: 0 })).toBe("2 signed in");
  });
});

describe("the access link", () => {
  it("masks the token and shows it only when asked", () => {
    expect(maskLink(LINK)).toBe(`http://192.168.1.5:8787/web/?t=${MASK}`);
    expect(maskLink(LINK)).not.toContain("secretTOKEN");
    expect(shownLink(LINK, false)).toBe(maskLink(LINK));
    expect(shownLink(LINK, true)).toBe(LINK);
    expect(maskLink("http://h/web/")).toBe("http://h/web/");
  });

  it("keeps the token out of the one-screen summary and always carries the warning", () => {
    const lines = summaryLines(listening);
    const all = lines.join("\n");
    expect(all).not.toContain("secretTOKEN");
    expect(all).toContain(MASK);
    expect(all).toContain("Open: http://192.168.1.5:8787/web/");
    expect(all).toContain(PLAIN_HTTP_WARNING);
    // Even a stopped listener keeps the warning line.
    expect(summaryLines(normalizeStatus({})).join("\n")).toContain("Plain HTTP");
  });

  it("flags settings that differ from what runs", () => {
    expect(summaryLines({ ...listening, needsRestart: true }).join("\n")).toContain("next daemon start will differ");
  });
});

describe("the start form", () => {
  it("starts from what runs, else from core's defaults", () => {
    expect(initialForm(null)).toEqual({ host: "0.0.0.0", port: "8787", username: "", remember: false });
    expect(initialForm(listening)).toMatchObject({ host: "0.0.0.0", port: "8787", remember: true });
  });

  it("checks the address and the port before asking core", () => {
    expect(validateForm({ host: "0.0.0.0", port: "8787", username: "", remember: false })).toEqual({});
    expect(validateForm({ host: " ", port: "8787", username: "", remember: false }).host).toBeTruthy();
    expect(validateForm({ host: "a b", port: "8787", username: "", remember: false }).host).toBeTruthy();
    for (const port of ["", "abc", "70000", "-1", "80.5"]) {
      expect(validateForm({ host: "0.0.0.0", port, username: "", remember: false }).port, port).toBeTruthy();
    }
    expect(validateForm({ host: "::", port: "0", username: "", remember: false })).toEqual({});
  });

  it("builds web.remote.listen params: trimmed, numeric, username only when typed", () => {
    expect(listenParams({ host: " 0.0.0.0 ", port: "8787", username: "", remember: false })).toEqual({ host: "0.0.0.0", port: 8787, save: false });
    expect(listenParams({ host: "::", port: "0", username: " me ", remember: true })).toEqual({ host: "::", port: 0, save: true, username: "me" });
  });

  it("puts a core refusal on the field it belongs to", () => {
    const refusal = (message: string, details?: Record<string, unknown>) => Object.assign(new Error(message), { data: { code: "internal", details } });
    expect(fieldError(refusal("port 8787 is already in use"))).toEqual({ field: "port", message: "port 8787 is already in use" });
    expect(fieldError(refusal("[Errno 98] address already in use")).field).toBe("port");
    expect(fieldError(refusal("cannot assign requested address 10.9.9.9")).field).toBe("host");
    expect(fieldError(refusal("not a valid address: 'x y'")).field).toBe("host");
    expect(fieldError(refusal("the username may not be empty")).field).toBe("username");
    expect(fieldError(refusal("something else entirely")).field).toBeNull();
    // A field core names wins over the wording.
    expect(fieldError(refusal("bad", { field: "username" })).field).toBe("username");
  });
});

describe("passwords", () => {
  it("asks for the minimum length, then that both entries match", () => {
    expect(MIN_PASSWORD).toBe(8);
    expect(passwordProblem("short", "short")).toContain("at least 8");
    expect(passwordProblem("longenough1", "longenough2")).toContain("differ");
    expect(passwordProblem("longenough1", "longenough1")).toBeNull();
  });

  it("shows one star per character and never the text", () => {
    expect(hidden("hunter2!")).toBe("********");
    expect(hidden("héllo")).toBe("*****");
    expect(hidden("")).toBe("");
  });

  it("says why a non-local connection is refused", () => {
    const refused = (kind?: string) => Object.assign(new Error("m"), { data: { code: "jarvis_person_only", details: kind ? { kind } : {} } });
    expect(localPersonOnlyLine(refused("remote_login"))).toContain("remote login");
    expect(localPersonOnlyLine(refused())).toContain("Only a person on this machine");
  });
});

describe("copying the link", () => {
  it("trusts only terminals that document OSC 52, or an explicit override", () => {
    expect(osc52Supported({ TERM_PROGRAM: "iTerm.app" })).toBe(true);
    expect(osc52Supported({ TERM: "xterm-kitty" })).toBe(true);
    expect(osc52Supported({ WT_SESSION: "1" })).toBe(true);
    expect(osc52Supported({ TERM: "xterm-256color" })).toBe(false);
    expect(osc52Supported({})).toBe(false);
    expect(osc52Supported({ TERM_PROGRAM: "iTerm.app", TMUX: "/tmp/tmux" })).toBe(false);
    expect(osc52Supported({ SNOWPEA_OSC52: "1" })).toBe(true);
    expect(osc52Supported({ SNOWPEA_OSC52: "0", TERM_PROGRAM: "iTerm.app" })).toBe(false);
  });

  it("builds the clipboard escape with the link base64-encoded", () => {
    const sequence = osc52Sequence(LINK);
    expect(sequence.startsWith("\u001b]52;c;")).toBe(true);
    expect(sequence.endsWith("\u0007")).toBe(true);
    expect(Buffer.from(sequence.slice(7, -1), "base64").toString("utf8")).toBe(LINK);
  });
});

describe("/web parsing", () => {
  it("parses the two forms", () => {
    expect(parseWebCommand("/web")).toEqual({ kind: "enter" });
    expect(parseWebCommand("/web status")).toEqual({ kind: "status" });
    expect(parseWebCommand("/web  status ")).toEqual({ kind: "status" });
    expect(parseWebCommand("/web start")).toMatchObject({ kind: "invalid" });
    expect(parseWebCommand("/website")).toBeNull();
    expect(parseWebCommand("hello")).toBeNull();
  });

  it("completes status", () => {
    expect(webSubCommands("/web s").map((command) => command.name)).toEqual(["web status"]);
    expect(webSubCommands("/web ")).toEqual([]);
    expect(webSubCommands("/security s")).toEqual([]);
  });
});
