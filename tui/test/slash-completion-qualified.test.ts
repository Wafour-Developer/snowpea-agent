import { describe, expect, it } from "vitest";

import {
  commandNameMatches,
  rankCommandMatches,
  shouldShowSlashPalette,
} from "../src/state/slash-completion.js";

const ROWS = [
  { name: "oh-my-claudecode:ralph", summary: "plugin loop" },
  { name: "ralph", summary: "core loop" },
  { name: "oh-my-claudecode:autopilot", summary: "plugin" },
  { name: "review", summary: "core" },
];

describe("a qualified skill answers to its own name", () => {
  it("matches the whole name or the part after the last colon", () => {
    expect(commandNameMatches("oh-my-claudecode:ralph", "ralph")).toBe(true);
    expect(commandNameMatches("oh-my-claudecode:ralph", "RAL")).toBe(true);
    expect(commandNameMatches("oh-my-claudecode:ralph", "oh-my")).toBe(true);
    expect(commandNameMatches("oh-my-claudecode:ralph", "claudecode")).toBe(false);
    expect(commandNameMatches("review", "ralph")).toBe(false);
  });

  it("lists the core command first and the plugin's one after it", () => {
    expect(rankCommandMatches(ROWS, "ralph").map((row) => row.name)).toEqual([
      "ralph",
      "oh-my-claudecode:ralph",
    ]);
  });

  it("keeps the palette open when only a qualified skill matches", () => {
    expect(shouldShowSlashPalette("/autop", ROWS as never)).toBe(true);
    expect(shouldShowSlashPalette("/nothing-like-it", ROWS as never)).toBe(false);
  });
});
