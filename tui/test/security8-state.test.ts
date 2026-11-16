import { afterEach, describe, expect, it } from "vitest";

import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import { compactWords, choiceLines, normalizeWaiting, resultLine } from "../src/state/disk-settings.js";
import { localOnlyLine, rpcErrorText } from "../src/state/errors.js";
import { DEGRADED_BANNER, degradedLine, normalizeDegraded } from "../src/state/settings-degraded.js";
import { fromSystemInfo } from "../src/hooks/useDaemonInfo.js";
import { normalizeStatus, remoteLeftLine, summaryLines } from "../src/state/web-remote.js";
import { textWidth } from "../src/layout/text-width.js";

const failure = (code: string, message = "m", details?: unknown): unknown => Object.assign(new Error(message), { data: { code, details } });

afterEach(() => resetUiLanguage());

describe("settingsDegraded", () => {
  it("is empty unless core sends a list of names", () => {
    expect(normalizeDegraded(undefined)).toEqual([]);
    expect(normalizeDegraded("security")).toEqual([]);
    expect(normalizeDegraded(["security", "", 3, "web"])).toEqual(["security", "web"]);
    expect(degradedLine([])).toBeNull();
  });

  it("is one line naming the sections; * is the whole file; a long list is counted", () => {
    expect(degradedLine(["security"])).toBe("settings.json: snowpea could not read security; safe defaults are in use");
    expect(degradedLine(["*"])).toBe("settings.json: snowpea could not read the whole file; safe defaults are in use");
    expect(degradedLine(["a", "b", "c", "d", "e"])).toBe("settings.json: snowpea could not read a, b, c and 2 more; safe defaults are in use");
    setUiLanguage("ko");
    expect(degradedLine(["security"])).toContain("안전한 기본값");
  });

  it("the banner fits 60 columns in both languages", () => {
    expect(textWidth(DEGRADED_BANNER())).toBeLessThanOrEqual(58);
    setUiLanguage("ko");
    expect(textWidth(DEGRADED_BANNER())).toBeLessThanOrEqual(58);
  });

  it("reads system.info's list for the daemon info", () => {
    expect(fromSystemInfo({ pid: 4, settingsDegraded: ["web"] }).degraded).toEqual(["web"]);
    expect(fromSystemInfo({ pid: 4 }).degraded).toEqual([]);
  });
});

describe("web.remote.status: remoteLeft and settingsDegraded", () => {
  it("reads the counts and keeps remoteJobs", () => {
    const status = normalizeStatus({ enabled: true, remoteJobs: 1, remoteLeft: { jobs: 2, mcpServers: 1, skills: 0 }, settingsDegraded: ["web"] } as never);
    expect(status.remoteLeft).toEqual({ jobs: 2, mcpServers: 1, skills: 0 });
    expect(status.remoteJobs).toBe(2);
    expect(status.settingsDegraded).toEqual(["web"]);
    expect(normalizeStatus({} as never).remoteLeft).toEqual({ jobs: 0, mcpServers: 0, skills: 0 });
  });

  it("says what is left beside the jobs line, and nothing when there is none", () => {
    const status = normalizeStatus({ remoteLeft: { jobs: 1, mcpServers: 2, skills: 1 } } as never);
    expect(remoteLeftLine(status)).toBe("Remote logins also left 2 MCP servers and 1 skill that keep working after the login ends (a count, not a guarantee).");
    expect(remoteLeftLine(normalizeStatus({ remoteLeft: { jobs: 3, mcpServers: 0, skills: 0 } } as never))).toBeNull();
    const lines = summaryLines(status);
    const jobs = lines.findIndex((line) => line.includes("scheduled job"));
    expect(lines[jobs + 1]).toContain("Remote logins also left");
  });

  it("/web status prints the degraded line", () => {
    const lines = summaryLines(normalizeStatus({ settingsDegraded: ["*"] } as never));
    expect(lines).toContain("! settings.json: snowpea could not read the whole file; safe defaults are in use");
  });
});

describe("a long waitingKeys list", () => {
  const keys = ["security", "audio.player", "audio.stt.command", "mcp.servers.fs.command", "updates.channel"];

  it("keeps every key and lists the first three, then counts", () => {
    expect(normalizeWaiting(keys)).toEqual(["security", "audio.player", "audio.stt.command", "mcp.servers.fs.command", "updates.channel"]);
    expect(compactWords(keys)).toBe("security, audio.player, audio.stt.command and 2 more");
    expect(compactWords(["security", "jarvis"])).toBe("security, Jarvis");
  });

  it("the choice and the result use the compact form", () => {
    expect(choiceLines(keys)[1]).toBe("These sections wait for you: security, audio.player, audio.stt.command and 2 more.");
    expect(resultLine({ applied: keys, discarded: [] })).toBe("applied from the file: security, audio.player, audio.stt.command and 2 more");
    setUiLanguage("ko");
    expect(compactWords(keys)).toContain("외 2개");
  });
});

describe("refusals from review 8", () => {
  it("names what a remote login may not do", () => {
    for (const what of ["credential", "briefing", "provider.loginWeb", "mcp.authorize.complete", "jarvis.delegation.set", "settings"]) {
      const line = localOnlyLine({ reason: "local_only", what, kind: "remote_login" });
      expect(line).toContain("Only snowpea on this machine can do that (");
      expect(line).not.toContain(`(${what})`);
      expect(line).toContain("not a remote login");
    }
    expect(localOnlyLine({ what: "elsewhere" })).toContain("(elsewhere)");
  });

  it("jarvis_person_only / local_only with a new what is a sentence, not JSON", () => {
    const text = rpcErrorText(failure("jarvis_person_only", "m", { reason: "local_only", what: "credential", kind: "remote_login" }));
    expect(text).toBe("Only snowpea on this machine can do that (saving a key or token) — not a remote login.");
  });

  it("fs.write protected with local_only is the same sentence; other protected refusals say the path is protected", () => {
    expect(rpcErrorText(failure("protected", "m", { reason: "local_only", check: "paths", kind: "remote_login" }))).toContain("not a remote login");
    expect(rpcErrorText(failure("protected", "~/.ssh is protected", { reason: "path" }))).toBe("That path is protected: ~/.ssh is protected");
    expect(rpcErrorText(failure("security_blocked", "rm -rf blocked", { blocked: true }))).toBe("Blocked by your security list: rm -rf blocked");
  });
});
