import { afterEach, describe, expect, it } from "vitest";

import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import { isSettingsUnreadable, rpcErrorText } from "../src/state/errors.js";
import { fromSystemInfo } from "../src/hooks/useDaemonInfo.js";
import { isTamperNotice, noticeLine } from "../src/state/jarvis.js";
import { normalizeConfig, statusLines } from "../src/state/security.js";

const failure = (code: string, message = "m", details?: unknown): unknown => Object.assign(new Error(message), { data: { code, details } });

afterEach(() => resetUiLanguage());

describe("settings_unreadable", () => {
  const error = failure("invalid_params", "raw core text with settings.json.invalid", { reason: "settings_unreadable", path: "/h/settings.json", invalid: "/h/settings.json.invalid" });

  it("is a friendly sentence naming the copy, never the raw message", () => {
    const text = rpcErrorText(error);
    expect(isSettingsUnreadable(error)).toBe(true);
    expect(text).toBe(
      "snowpea could not read settings.json, so it does not write over it. Fix the file (a copy of it as it was: /h/settings.json.invalid), then reload settings.",
    );
    expect(text).not.toContain("raw core text");
  });

  it("works without a copy path and in Korean", () => {
    const bare = failure("invalid_params", "x", { reason: "settings_unreadable" });
    expect(rpcErrorText(bare)).toBe("snowpea could not read settings.json, so it does not write over it. Fix the file, then reload settings.");
    setUiLanguage("ko");
    expect(rpcErrorText(error)).toContain("settings.json.invalid");
  });

  it("does not catch other invalid_params", () => {
    expect(isSettingsUnreadable(failure("invalid_params", "x", { reason: "global_only" }))).toBe(false);
  });
});

describe("degraded list after a reload", () => {
  it("system.info with a cleared list empties the banner state", () => {
    expect(fromSystemInfo({ pid: 1, settingsDegraded: ["*"] }).degraded).toEqual(["*"]);
    expect(fromSystemInfo({ pid: 1, settingsDegraded: [] }).degraded).toEqual([]);
    expect(fromSystemInfo({ pid: 1 }).degraded).toEqual([]);
  });
});

describe("tamper escalation kind", () => {
  const base = { id: "je-1", reason: "error", summary: "Rules changed on disk", sessionId: "abcdef123456" } as any;

  it("is recognised by kind first, then by the older id", () => {
    expect(isTamperNotice({ ...base, kind: "tamper" })).toBe(true);
    expect(isTamperNotice({ ...base, id: "je-tamper-9" })).toBe(true);
    expect(isTamperNotice({ ...base, kind: null })).toBe(false);
    expect(isTamperNotice(base)).toBe(false);
  });

  it("draws as a security notice, not a failed turn", () => {
    expect(noticeLine({ ...base, kind: "tamper" })).toMatch(/^! /);
    expect(noticeLine(base)).toMatch(/^✗ /);
  });
});

describe("security.get problems about a linked credentials.json", () => {
  const sentence = "/h/credentials.json is a symbolic link: no stored credential is used; replace the link with the file itself";

  it("is a notice, whole, not a skipped entry (older core, sentences only)", () => {
    const config = normalizeConfig({ problems: [sentence] } as any);
    expect(config.notices).toEqual([sentence]);
    expect(config.problems).toEqual([]);
    expect(statusLines(config, null)).toContain(`! ${sentence}`);
  });

  it("is a notice with problemItems too", () => {
    const config = normalizeConfig({ problems: [sentence], problemItems: [] } as any);
    expect(config.notices).toEqual([sentence]);
  });
});
