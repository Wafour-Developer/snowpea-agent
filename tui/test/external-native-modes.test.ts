import { describe, expect, it } from "vitest";

import type { ExternalCliInfo, ExternalNativeMode, ExternalSessionRow } from "../src/rpc/sdk.js";
import {
  chooseMode,
  confirmsUnrestricted,
  modesFor,
  nativeModeDescription,
  nativeModesFor,
  personOnlyPrompt,
  personOnlyWord,
  pickerLabel,
  sessionModeLabel,
} from "../src/state/external.js";

/** Claude Code's modes as a 1.56.0 daemon lists them (claude --help 2.1.326). */
const CLAUDE: ExternalNativeMode[] = [
  { id: "default", label: "Manual", asks: "always", writes: "workspace", approvals: "bridged", personOnly: false, legacy: "default" },
  { id: "acceptEdits", label: "Accept edits", asks: "sometimes", writes: "workspace", approvals: "bridged", personOnly: true, legacy: "default" },
  { id: "plan", label: "Plan", asks: "always", writes: "plan", approvals: "bridged", personOnly: false, legacy: "readonly" },
  { id: "auto", label: "Auto", asks: "sometimes", writes: "workspace", approvals: "bridged", personOnly: true, legacy: "default" },
  { id: "dontAsk", label: "Don't ask", asks: "never", writes: "workspace", approvals: "none", personOnly: true, legacy: "default" },
  { id: "bypassPermissions", label: "Bypass permissions", asks: "never", writes: "anywhere", approvals: "none", personOnly: true, legacy: "unrestricted" },
];

const claude = (patch: Partial<ExternalCliInfo> = {}): ExternalCliInfo => ({
  id: "claude",
  title: "Claude Code",
  installed: true,
  nativeModes: CLAUDE,
  defaultNativeMode: "default",
  ...patch,
});

const row = (patch: Partial<ExternalSessionRow> = {}): ExternalSessionRow => ({
  ref: { cli: "claude", id: "0b4f-1" },
  cwd: "/work/snowpea",
  status: "idle",
  host: "snowpea",
  canSend: true,
  approvals: "bridged",
  mode: "readonly",
  nativeMode: "plan",
  controlled: true,
  title: "plan the release",
  ...patch,
});

describe("a CLI's own modes (1.56.0)", () => {
  it("lists them as the CLI names them, its default first", () => {
    expect(nativeModesFor(claude()).map((mode) => mode.id)).toEqual([
      "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions",
    ]);
    const cursor = nativeModesFor({ id: "cursor", title: "Cursor", installed: true, defaultNativeMode: "ask", nativeModes: [
      { id: "agent", label: "Agent", asks: "never", writes: "workspace", approvals: "none", personOnly: false, legacy: "default" },
      { id: "ask", label: "Ask", asks: "never", writes: "none", approvals: "none", personOnly: false, legacy: "readonly" },
    ] });
    expect(cursor.map((mode) => mode.id)).toEqual(["ask", "agent"]);
  });

  it("keeps the three buckets for an older daemon", () => {
    const old: ExternalCliInfo = { id: "claude", title: "Claude Code", installed: true };
    expect(nativeModesFor(old)).toEqual([]);
    expect(modesFor(old)).toEqual(["default", "readonly", "unrestricted"]);
    expect(chooseMode(old, "readonly")).toEqual({ kind: "legacy", mode: "readonly" });
    expect(chooseMode(old, "plan")).toEqual({ kind: "unknown", offered: [] });
  });

  it("describes each one truthfully: read-only only when it writes nothing", () => {
    const by = Object.fromEntries(CLAUDE.map((mode) => [mode.id, nativeModeDescription(mode)]));
    expect(by.default).toBe("asks whenever a permission is needed");
    expect(by.plan).toContain("writes only its plan file");
    expect(by.plan).not.toContain("read-only");
    expect(by.auto).toBe("proceeds on its own, asks when unsure");
    expect(by.bypassPermissions).toBe("never asks — snowpea no longer controls what it runs");
    for (const id of ["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]) {
      expect(by[id]).not.toContain("read-only");
    }
    expect(nativeModeDescription({ asks: "never", writes: "none", approvals: "none" })).toContain("read-only");
  });

  it("chooses by the CLI's id; a bucket word still works", () => {
    expect(chooseMode(claude(), "plan")).toMatchObject({ kind: "native", mode: { id: "plan" } });
    expect(chooseMode(claude(), "acceptedits")).toMatchObject({ kind: "native", mode: { id: "acceptEdits" } });
    expect(chooseMode(claude(), "unrestricted")).toEqual({ kind: "legacy", mode: "unrestricted" });
    expect(chooseMode(claude(), "yolo")).toMatchObject({ kind: "unknown" });
  });

  it("asks a person to type the mode's own word for a person-only mode", () => {
    const auto = CLAUDE[3];
    expect(personOnlyWord(auto)).toBe("auto");
    expect(personOnlyWord(CLAUDE[5])).toBe("unrestricted");
    const lines = personOnlyPrompt({ cli: "claude", cwd: "/work/snowpea", mode: auto });
    expect(lines[0]).toBe("Switch Claude Code to Auto?");
    expect(lines.join("\n")).toContain("proceeds on its own, asks when unsure");
    expect(lines.join("\n")).toContain("Jarvis cannot");
    expect(lines.at(-1)).toContain('Type "auto"');
    expect(confirmsUnrestricted("Auto ", "auto")).toBe(true);
    expect(confirmsUnrestricted("unrestricted", "auto")).toBe(false);
    expect(personOnlyPrompt({ cli: "claude", cwd: "/w", mode: CLAUDE[5] })[0]).toContain("UNRESTRICTED");
  });

  it("labels the open session with the CLI's own name for its mode", () => {
    expect(sessionModeLabel(row(), [claude()])).toBe("Plan");
    expect(sessionModeLabel(row({ nativeMode: "auto", mode: "default" }), [claude()])).toBe("Auto");
    // Without the CLI's list, its id; without a native mode, the bucket.
    expect(sessionModeLabel(row(), null)).toBe("plan");
    expect(sessionModeLabel(row({ nativeMode: null }), null)).toBe("read-only");
    expect(sessionModeLabel(row({ nativeMode: "bypassPermissions", mode: "unrestricted" }), [claude()])).toBe("UNRESTRICTED");
  });

  it("shows a native mode in the picker when it is not the bucket's name", () => {
    expect(pickerLabel(row(), 80, false)).toContain("plan");
    expect(pickerLabel(row({ nativeMode: "default", mode: "default" }), 80, false)).not.toContain("default");
  });
});
