import { describe, expect, it } from "vitest";

import {
  externalSubCommands,
  jarvisSubCommands,
  parseExternalCommand,
  parseJarvisCommand,
} from "../src/slash/jarvis.js";

describe("/jarvis", () => {
  it("is not a jarvis command unless it says so", () => {
    expect(parseJarvisCommand("/jarvisx")).toBeNull();
    expect(parseJarvisCommand("hello /jarvis")).toBeNull();
    expect(parseJarvisCommand("/model")).toBeNull();
  });

  it("parses every action", () => {
    expect(parseJarvisCommand("/jarvis")).toEqual({ kind: "enter" });
    expect(parseJarvisCommand("/jarvis on")).toEqual({ kind: "on" });
    expect(parseJarvisCommand("/jarvis off")).toEqual({ kind: "off" });
    expect(parseJarvisCommand("/jarvis status")).toEqual({ kind: "status" });
    expect(parseJarvisCommand("/jarvis back")).toEqual({ kind: "back" });
    expect(parseJarvisCommand("/jarvis sessions")).toEqual({ kind: "sessions" });
    expect(parseJarvisCommand("/jarvis list")).toEqual({ kind: "sessions" });
    expect(parseJarvisCommand("/jarvis waiting")).toEqual({ kind: "waiting" });
    expect(parseJarvisCommand("/jarvis open 3")).toEqual({ kind: "open", target: "3" });
    expect(parseJarvisCommand("/jarvis handback")).toEqual({ kind: "handback", target: null });
    expect(parseJarvisCommand("/jarvis handback 2")).toEqual({ kind: "handback", target: "2" });
  });

  it("reads takeover's numbers as minutes for this session, or row then minutes", () => {
    expect(parseJarvisCommand("/jarvis takeover")).toEqual({ kind: "takeover", target: null, minutes: null });
    expect(parseJarvisCommand("/jarvis takeover 30")).toEqual({ kind: "takeover", target: null, minutes: 30 });
    expect(parseJarvisCommand("/jarvis takeover 2 30")).toEqual({ kind: "takeover", target: "2", minutes: 30 });
    expect(parseJarvisCommand("/jarvis takeover soon")).toMatchObject({ kind: "invalid" });
    expect(parseJarvisCommand("/jarvis takeover 1 2 3")).toMatchObject({ kind: "invalid" });
  });

  it("rejects what it does not know with the usage line", () => {
    for (const line of ["/jarvis dance", "/jarvis on now", "/jarvis open", "/jarvis open 1 2", "/jarvis handback 1 2"]) {
      expect(parseJarvisCommand(line), line).toMatchObject({ kind: "invalid" });
    }
  });

  it("completes the actions while one is being typed", () => {
    expect(jarvisSubCommands("/jarvis ").length).toBe(0);
    expect(jarvisSubCommands("/jarvis h").map((c) => c.name)).toEqual(["jarvis handback"]);
    expect(jarvisSubCommands("/jarvis t").map((c) => c.name)).toEqual(["jarvis takeover"]);
    expect(jarvisSubCommands("/model")).toEqual([]);
  });
});

describe("/external", () => {
  it("parses the picker, new, close, mode and more", () => {
    expect(parseExternalCommand("/external")).toEqual({ kind: "picker" });
    expect(parseExternalCommand("/external new")).toEqual({ kind: "new" });
    expect(parseExternalCommand("/external close")).toEqual({ kind: "close" });
    expect(parseExternalCommand("/external more")).toEqual({ kind: "more" });
    expect(parseExternalCommand("/external mode readonly")).toEqual({ kind: "mode", mode: "readonly" });
    expect(parseExternalCommand("/external mode unrestricted")).toEqual({ kind: "mode", mode: "unrestricted" });
    // 1.56.0: any of a CLI's own mode ids; the open session's CLI says which exist.
    expect(parseExternalCommand("/external mode plan")).toEqual({ kind: "mode", mode: "plan" });
    expect(parseExternalCommand("/external mode acceptEdits")).toEqual({ kind: "mode", mode: "acceptEdits" });
    expect(parseExternalCommand("/external mode read-only")).toEqual({ kind: "mode", mode: "read-only" });
    expect(parseExternalCommand("/external mode --yolo")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/external mode plan auto")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/external mode")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/external new now")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/jarvis")).toBeNull();
  });

  it("completes the actions", () => {
    expect(externalSubCommands("/external m").map((c) => c.name).sort()).toEqual(["external mode", "external more"]);
  });
});
