import { describe, expect, it } from "vitest";

import { jarvisSubCommands, parseJarvisCommand } from "../src/slash/jarvis.js";
import {
  SHARED_MARK,
  hasSessionSwitch,
  hiddenLine,
  lockedLine,
  shareLine,
  switchedLine,
  switchesFromRows,
} from "../src/state/jarvis-switch.js";

describe("/jarvis share (the per-session switch, core 1.56.0)", () => {
  it("shows, or turns this or a named session on and off", () => {
    expect(parseJarvisCommand("/jarvis share")).toEqual({ kind: "share", enabled: null, target: null });
    expect(parseJarvisCommand("/jarvis share on")).toEqual({ kind: "share", enabled: true, target: null });
    expect(parseJarvisCommand("/jarvis share off")).toEqual({ kind: "share", enabled: false, target: null });
    expect(parseJarvisCommand("/jarvis share on 2")).toEqual({ kind: "share", enabled: true, target: "2" });
  });

  it("refuses anything else with the usage line", () => {
    expect(parseJarvisCommand("/jarvis share maybe")?.kind).toBe("invalid");
    expect(parseJarvisCommand("/jarvis share on 2 3")?.kind).toBe("invalid");
  });

  it("does not change what /jarvis on and off mean (Jarvis mode)", () => {
    expect(parseJarvisCommand("/jarvis on")).toEqual({ kind: "on" });
    expect(parseJarvisCommand("/jarvis off")).toEqual({ kind: "off" });
  });

  it("is offered as a sub-command", () => {
    expect(jarvisSubCommands("/jarvis sh").map((entry) => entry.name)).toEqual(["jarvis share"]);
  });
});

describe("the switch's words", () => {
  it("needs the capability (an older daemon shows nothing)", () => {
    expect(hasSessionSwitch(["jarvis", "jarvisSessionSwitch"])).toBe(true);
    expect(hasSessionSwitch(["jarvis"])).toBe(false);
    expect(hasSessionSwitch(undefined)).toBe(false);
  });

  it("reads session.list rows that carry the switch", () => {
    expect(
      switchesFromRows([
        { sessionId: "a", jarvisOn: true, jarvisLocked: null },
        { sessionId: "b", jarvisOn: false, jarvisLocked: "project_hidden" },
        { sessionId: "c" },
      ]),
    ).toEqual({ a: { on: true, locked: null }, b: { on: false, locked: "project_hidden" } });
  });

  it("says what the switch is and how to change it", () => {
    expect(shareLine({ on: false, locked: null })).toContain("/jarvis share on");
    expect(shareLine({ on: true, locked: null })).toContain("/jarvis share off");
    expect(shareLine({ on: true, locked: "project_hidden" })).toContain("cannot be changed");
    expect(shareLine({ on: true, locked: "jarvis_session" })).toContain("Jarvis's own work");
    expect(shareLine(undefined)).toContain("not known");
    expect(switchedLine(true)).toContain("shared with Jarvis");
    expect(switchedLine(false)).toContain("not shared");
    expect(lockedLine("browser_hidden")).toContain("exposeBrowser");
    expect(lockedLine("other")).toBeNull();
    expect(SHARED_MARK).toBe("Jarvis: shared");
  });

  it("counts the sessions Jarvis does not see", () => {
    expect(hiddenLine(0)).toBeNull();
    expect(hiddenLine(null)).toBeNull();
    expect(hiddenLine(1)).toBe("1 session is not shared with Jarvis — in a session: /jarvis share on");
    expect(hiddenLine(3)).toContain("3 sessions are not shared");
  });
});
