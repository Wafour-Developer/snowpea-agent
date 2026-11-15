import { describe, expect, it } from "vitest";

import { fit, wrapWords } from "../src/layout/fit.js";
import { textWidth } from "../src/layout/text-width.js";
import { externalSubCommands, parseExternalCommand } from "../src/slash/jarvis.js";
import {
  PENDING_PATH_ROWS,
  confirmPathPrompt,
  describeExternalError,
  isUntrustedProject,
  modelsNote,
  pendingPathLine,
  pendingPathLines,
  personOnlyLine,
  rpcErrorFacts,
  tailPath,
  trustPrompt,
  unavailableReason,
} from "../src/state/external.js";
import { unavailableOf } from "../src/hooks/useJarvisScreens.js";

const failure = (code: string, details?: Record<string, unknown>, message = "m"): unknown =>
  Object.assign(new Error(message), { data: { code, details } });

describe("rpc error facts", () => {
  it("reads data.code and data.details", () => {
    expect(rpcErrorFacts(failure("session_busy", { reason: "terminal_busy", project: "/p", kind: "browser" }))).toEqual({
      code: "session_busy",
      reason: "terminal_busy",
      project: "/p",
      kind: "browser",
    });
    expect(rpcErrorFacts(new Error("plain"))).toEqual({ code: undefined, reason: undefined, project: undefined, kind: undefined });
    expect(rpcErrorFacts(null).code).toBeUndefined();
  });

  it("tells the trust question from the person-only refusal", () => {
    expect(isUntrustedProject(failure("external_unavailable", { reason: "untrusted_project", project: "/p" }))).toBe(true);
    expect(isUntrustedProject(failure("external_unavailable", { reason: "untrusted_project_person_only" }))).toBe(false);
    expect(isUntrustedProject(failure("external_unavailable"))).toBe(false);
    expect(describeExternalError(failure("external_unavailable", { reason: "untrusted_project_person_only" }), "claude")).toContain(
      "only you can allow it",
    );
  });

  it("gives each busy tmux reason its own words and keeps the daemon's text otherwise", () => {
    const words = ["terminal_prompt", "terminal_busy", "terminal_typing"].map((reason) =>
      describeExternalError(failure("session_busy", { reason }, "busy"), "claude"),
    );
    expect(new Set(words).size).toBe(3);
    expect(words[0]).toContain("permission prompt");
    expect(describeExternalError(failure("session_busy", { reason: "other" }, "a turn is running"), "claude")).toBe("a turn is running");
    expect(describeExternalError(failure("session_busy", undefined, "session_busy"), "claude")).toContain("busy");
  });

  it("says who was refused for jarvis_person_only", () => {
    expect(personOnlyLine(failure("jarvis_person_only", { kind: "remote_login" }))).toContain("remote login");
    expect(personOnlyLine(failure("jarvis_person_only", { kind: "browser" }))).toContain("browser host");
    expect(personOnlyLine(failure("jarvis_person_only", { kind: "connector" }))).toContain("connector");
    expect(personOnlyLine(failure("jarvis_person_only"))).toContain("Only a person");
    expect(describeExternalError(failure("jarvis_person_only", { kind: "remote_login" }))).toBe(
      personOnlyLine(failure("jarvis_person_only", { kind: "remote_login" })),
    );
  });
});

describe("trust and path confirmation texts", () => {
  it("asks the contract's question and keeps the end of a long path", () => {
    const lines = trustPrompt("/home/me/projects/some/very/deep/folder/that/keeps/going/on/and/on/project");
    expect(lines[0]).toBe("This CLI will load this project's own settings and hooks.");
    expect(lines[1]).toMatch(/^Open external sessions in ….*project\?$/);
    expect(trustPrompt("/tmp/p")[1]).toBe("Open external sessions in /tmp/p?");
    expect(tailPath("/a/b", 10)).toBe("/a/b");
  });

  it("splits a path across rows without cutting a character of it", () => {
    const path = `/opt/${"x".repeat(120)}/claude`;
    const lines = confirmPathPrompt({ id: "claude", pendingPath: path });
    expect(lines[0]).toContain("Claude Code");
    expect(lines.slice(1).join("")).toBe(path);
    for (const line of lines.slice(1)) expect(textWidth(line)).toBeLessThanOrEqual(50);
  });

  it("names the pending path in the words the contract gives", () => {
    expect(pendingPathLine({ id: "codex", pendingPath: "/x" })).toBe(
      "Codex: path not confirmed — confirm in Settings or with /external confirm-path codex",
    );
    expect(pendingPathLine({ id: "codex", pendingPath: null })).toBeNull();
    expect(unavailableReason({ id: "claude", installed: false, pendingPath: "/x", title: "Claude" })).toContain("path not confirmed");
    expect(unavailableReason({ id: "claude", installed: false, title: "Claude" })).toBe("not installed");
  });

  it("wraps the pending lines into the rows reserved for them, at 60 columns", () => {
    const one = pendingPathLines([{ id: "claude", pendingPath: "/x", installed: false, title: "c" }], 56);
    expect(one.length).toBeLessThanOrEqual(PENDING_PATH_ROWS);
    expect(one.join(" ")).toContain("/external confirm-path claude");
    for (const line of one) expect(textWidth(line)).toBeLessThanOrEqual(56);
    const many = pendingPathLines(
      ["claude", "codex", "agy"].map((id) => ({ id, pendingPath: "/x", installed: false, title: id }) as never),
      56,
    );
    expect(many).toHaveLength(PENDING_PATH_ROWS);
    for (const line of many) expect(textWidth(line)).toBeLessThanOrEqual(56);
  });

  it("keeps a pending CLI out of the 'not installed' list", () => {
    const split = unavailableOf([
      { id: "claude", title: "Claude Code", installed: false, pendingPath: "/x" },
      { id: "codex", title: "Codex", installed: false },
    ] as never);
    expect(split.unavailable).toEqual(["Codex (not installed)"]);
    expect(split.pending.map((info) => info.id)).toEqual(["claude"]);
  });
});

describe("the model list note", () => {
  it("says loading, or that the CLI lists none; nothing when it cannot tell", () => {
    expect(modelsNote({ models: [], modelsLoaded: false })).toBe("loading models…");
    expect(modelsNote({ models: [], modelsLoaded: true })).toBe("no model list");
    expect(modelsNote({ models: ["opus"], modelsLoaded: true })).toBeNull();
    expect(modelsNote({ models: [] })).toBeNull();
    expect(modelsNote(null)).toBeNull();
  });
});

describe("/external confirm-path", () => {
  it("parses a known CLI only, and is offered in the palette", () => {
    expect(parseExternalCommand("/external confirm-path claude")).toEqual({ kind: "confirmPath", cli: "claude" });
    expect(parseExternalCommand("/external confirm-path")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/external confirm-path vim")).toMatchObject({ kind: "invalid" });
    expect(parseExternalCommand("/external confirm-path claude now")).toMatchObject({ kind: "invalid" });
    expect(externalSubCommands("/external conf").map((entry) => entry.name)).toEqual(["external confirm-path"]);
  });
});

describe("wrapWords", () => {
  it("breaks at spaces, counts cells, and cuts the last row", () => {
    expect(wrapWords("one two three", 7, 3)).toEqual(["one two", "three"]);
    expect(wrapWords("alpha beta gamma delta", 11, 2)).toEqual(["alpha beta", "gamma delta"]);
    const cut = wrapWords("alpha beta gamma delta epsilon", 11, 2);
    expect(cut).toHaveLength(2);
    expect(cut[1].endsWith("…")).toBe(true);
    expect(wrapWords("한국어 문장 입니다", 8, 3).every((line) => textWidth(line) <= 8)).toBe(true);
    expect(wrapWords("", 10, 2)).toEqual([]);
    expect(fit("x", 3)).toBe("x");
  });
});
