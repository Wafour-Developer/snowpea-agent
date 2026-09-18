/**
 * When the slash palette stays open, and when it closes after a pick.
 */

import { describe, expect, it } from "vitest";

import {
  isSlashCompletionAccepted,
  shouldShowSlashPalette,
} from "../src/state/slash-completion.js";

const rows = [
  { name: "skill create", summary: "write one", source: "core" },
  { name: "skill learn deploy", summary: "learn it", source: "core" },
];

describe("shouldShowSlashPalette", () => {
  it("stays open while a sub-action is still being typed", () => {
    expect(shouldShowSlashPalette("/skill cr", rows)).toBe(true);
  });

  it("closes once a row is accepted with a trailing space", () => {
    expect(shouldShowSlashPalette("/skill create ", rows)).toBe(false);
  });

  it("closes when argument text no longer matches any row", () => {
    expect(shouldShowSlashPalette("/skill create deploy", [])).toBe(false);
  });

  it("stays open while a skill name is being typed", () => {
    expect(shouldShowSlashPalette("/skill learn dep", rows)).toBe(true);
  });

  it("closes once the skill name is accepted", () => {
    expect(shouldShowSlashPalette("/skill learn deploy ", rows)).toBe(false);
  });
});

describe("isSlashCompletionAccepted", () => {
  it("is true only with a trailing space after the full name", () => {
    expect(isSlashCompletionAccepted("/skill create ", "skill create")).toBe(true);
    expect(isSlashCompletionAccepted("/skill create", "skill create")).toBe(false);
  });
});
