import { describe, expect, it } from "vitest";

import { messageLines } from "../src/layout/transcript.js";
import { textWidth } from "../src/layout/text-width.js";
import { parseSecurityCommand, securitySubCommands } from "../src/slash/security.js";
import {
  addCustom,
  blockedLine,
  byLabel,
  PATTERN_HELP,
  cycleLevel,
  draftOf,
  editCustom,
  hasCustom,
  listsAreSet,
  listsOf,
  normalizeBlocked,
  normalizeCatalog,
  normalizeConfig,
  normalizeVerdict,
  problemNote,
  removeCustom,
  saveProblems,
  sameLists,
  scrollInto,
  wrapSegments,
  securitySupported,
  settingsPatch,
  statusLines,
  tabRows,
  togglePreset,
  verdictLine,
  weakening,
} from "../src/state/security.js";
import { initialJarvis, panelLines } from "../src/state/jarvis.js";

const catalog = normalizeCatalog({
  locale: "en",
  commands: [
    { id: "essential", title: "Essential safety", description: "d", examples: ["rm -rf ~"], default: true },
    { id: "power", title: "Power", description: "d", examples: ["shutdown"], default: false },
  ],
  paths: [
    { id: "essential", title: "System", description: "d", examples: [], default: true, level: "write", paths: ["/etc", "/usr"] },
    { id: "credentials", title: "Credentials", description: "d", examples: [], default: false, level: "all", paths: ["~/.ssh"] },
  ],
});

// The real shapes: `by` is `commands:<id>` / `paths:<id>` / `custom`, a path carries its scope.
const config = normalizeConfig({
  writable: true,
  blockedCommands: { presets: ["essential"], custom: ["npm publish"] },
  protectedPaths: { presets: ["essential"], custom: [{ path: "/srv/data", level: "write" }] },
  effective: {
    commands: [
      { pattern: "rm -rf ~", by: "commands:essential" },
      { pattern: "npm publish", by: "custom" },
    ],
    paths: [
      { path: "/etc", level: "write", by: "paths:essential", scope: "tree" },
      { path: "/home/me", level: "write", by: "paths:essential", scope: "self", exempt: ["/home/me/project"] },
    ],
  },
});

describe("security contract normalisation", () => {
  it("keeps the shapes and drops what is not a string", () => {
    expect(catalog.commands[0]).toEqual({ id: "essential", title: "Essential safety", description: "d", examples: ["rm -rf ~"], default: true });
    expect(catalog.paths[1]).toMatchObject({ level: "all", paths: ["~/.ssh"] });
    expect(normalizeConfig(null)).toEqual({
      blockedCommands: { presets: [], custom: [] },
      protectedPaths: { presets: [], custom: [] },
      effective: { commands: [], paths: [] },
      writable: true,
      problems: [],
      projectAdds: 0,
    });
    expect(normalizeVerdict({ blocked: true, by: "x", why: "y", level: "all" })).toEqual({ blocked: true, opaque: false, by: "x", why: "y", level: "all" });
    expect(normalizeVerdict({ blocked: "yes" } as never).blocked).toBe(false);
  });

  it("reads writable, the skipped entries and the project's additions from security.get", () => {
    const read = normalizeConfig({
      writable: false,
      problems: ["unknown preset 'x'", 3 as never],
      project: { blockedCommands: { custom: ["a", "b"] }, protectedPaths: { presets: ["credentials"] } },
    });
    expect(read.writable).toBe(false);
    expect(read.problems).toEqual(["unknown preset 'x'"]);
    expect(read.projectAdds).toBe(3);
    expect(normalizeConfig({ writable: true, project: null }).projectAdds).toBe(0);
  });

  it("keeps a path's scope and exemptions", () => {
    expect(config.effective.paths[1]).toEqual({ path: "/home/me", level: "write", by: "paths:essential", scope: "self", exempt: ["/home/me/project"] });
    expect(config.effective.paths[0]).toMatchObject({ scope: "tree", exempt: [] });
  });

  it("tells blocked, opaque and not blocked apart (opaque is never also blocked)", () => {
    expect(normalizeVerdict({ blocked: false, opaque: true, reason: "cannot be checked against your block list" })).toMatchObject({
      blocked: false,
      opaque: true,
      why: "cannot be checked against your block list",
    });
    expect(normalizeVerdict({ blocked: true, opaque: true, by: "custom", why: "w" })).toMatchObject({ blocked: true, opaque: false });
    expect(normalizeVerdict({ blocked: false })).toMatchObject({ blocked: false, opaque: false, by: null });
    expect(normalizeVerdict({ blocked: true, by: "paths:credentials", match: "~/.ssh", level: "all", why: "w" })).toMatchObject({ match: "~/.ssh" });
  });

  it("strips terminal escapes from every server string", () => {
    const dirty = "a\u001b]52;c;QQ==\u0007b‮c";
    expect(normalizeCatalog({ commands: [{ id: "x", title: dirty, description: dirty, examples: [dirty] }] }).commands[0]).toMatchObject({ title: "abc", description: "abc", examples: ["abc"] });
    expect(normalizeBlocked({ sessionId: "s", tool: "shell", summary: dirty, by: dirty }).summary).toBe("abc");
    expect(normalizeVerdict({ blocked: true, by: dirty, why: dirty }).why).toBe("abc");
    expect(normalizeConfig({ effective: { paths: [{ path: dirty, level: "write", by: dirty }] } }).effective.paths[0].path).toBe("abc");
  });

  it("gates on the securityLists capability and reads whether lists are in force", () => {
    expect(securitySupported(["securityLists"])).toBe(true);
    expect(securitySupported(["jarvis"])).toBe(false);
    expect(securitySupported(undefined)).toBe(false);
    expect(listsAreSet(config)).toBe(true);
    expect(listsAreSet(normalizeConfig({}))).toBe(false);
    expect(listsAreSet(null)).toBe(false);
  });
});

describe("editing the lists", () => {
  it("builds rows: presets first with their state, then manual entries", () => {
    const rows = tabRows("commands", catalog, listsOf(config));
    expect(rows.map((row) => (row.kind === "preset" ? `${row.id}:${row.on}` : `custom:${row.text}`))).toEqual(["essential:true", "power:false", "custom:npm publish"]);
    const paths = tabRows("paths", catalog, listsOf(config));
    expect(paths[0]).toMatchObject({ kind: "preset", level: "write", examples: ["/etc", "/usr"] });
    expect(paths[2]).toMatchObject({ kind: "custom", text: "/srv/data", level: "write" });
  });

  it("keeps a preset the catalog does not know visible so it can be switched off", () => {
    const lists = { ...listsOf(config), commandPresets: ["essential", "from-the-future"] };
    const rows = tabRows("commands", catalog, lists);
    expect(rows.find((row) => row.kind === "preset" && row.id === "from-the-future")).toMatchObject({ on: true, known: false });
  });

  it("toggles, adds, edits, removes and cycles without touching the original", () => {
    const base = listsOf(config);
    const toggled = togglePreset(base, "commands", "power");
    expect(toggled.commandPresets).toEqual(["essential", "power"]);
    expect(togglePreset(toggled, "commands", "power").commandPresets).toEqual(["essential"]);
    expect(base.commandPresets).toEqual(["essential"]);
    expect(addCustom(base, "paths", "/mnt/x").pathCustom).toEqual([{ path: "/srv/data", level: "write" }, { path: "/mnt/x", level: "write" }]);
    expect(editCustom(base, "commands", 0, "npm publish --tag x").commandCustom).toEqual(["npm publish --tag x"]);
    expect(removeCustom(base, "commands", 0).commandCustom).toEqual([]);
    expect(cycleLevel(base, 0).pathCustom[0].level).toBe("all");
    expect(cycleLevel(cycleLevel(base, 0), 0).pathCustom[0].level).toBe("write");
    expect(sameLists(base, listsOf(config))).toBe(true);
    expect(sameLists(base, toggled)).toBe(false);
  });

  it("finds duplicates, ignoring the entry being edited", () => {
    const base = listsOf(config);
    expect(hasCustom(base, "commands", "  npm publish ")).toBe(true);
    expect(hasCustom(base, "commands", "npm publish", 0)).toBe(false);
    expect(hasCustom(base, "paths", "/srv/data")).toBe(true);
  });

  it("builds one patch with both tabs", () => {
    expect(settingsPatch(listsOf(config))).toEqual({
      security: {
        blockedCommands: { presets: ["essential"], custom: ["npm publish"] },
        protectedPaths: { presets: ["essential"], custom: [{ path: "/srv/data", level: "write" }] },
      },
    });
  });
});

describe("weakening", () => {
  const base = listsOf(config);

  it("names what a default preset's removal stops covering", () => {
    const after = togglePreset(base, "commands", "essential");
    expect(weakening("commands", catalog, base, after)).toContain('"Essential safety" is a default list. rm -rf ~ will no longer be blocked.');
    const paths = togglePreset(base, "paths", "essential");
    expect(weakening("paths", catalog, base, paths)).toContain("/etc, /usr will no longer be protected");
  });

  it("is silent for a non-default preset while something else remains", () => {
    const withPower = togglePreset(base, "commands", "power");
    expect(weakening("commands", catalog, withPower, base)).toBeNull();
  });

  it("asks when a tab ends up with nothing, and says what goes", () => {
    const only = { ...base, commandPresets: ["power"], commandCustom: [] };
    const none = togglePreset(only, "commands", "power");
    expect(weakening("commands", catalog, only, none)).toContain("Power will no longer be blocked");
    const lastCustom = { ...base, pathPresets: [], pathCustom: [{ path: "/srv/data", level: "write" as const }] };
    expect(weakening("paths", catalog, lastCustom, removeCustom(lastCustom, "paths", 0), "/srv/data")).toContain("/srv/data will no longer be protected");
  });

  it("does not ask when the tab was already empty", () => {
    const empty = { ...base, commandPresets: [], commandCustom: [] };
    expect(weakening("commands", catalog, empty, empty)).toBeNull();
  });
});

describe("verdicts and status", () => {
  const verdict = (over: Record<string, unknown>) => ({ blocked: false, opaque: false, by: null, why: "", ...over }) as never;

  it("writes BLOCKED by <preset title>: <why>, with the level for a folder", () => {
    expect(verdictLine(verdict({ blocked: true, by: "commands:essential", why: "deletes your home" }), catalog)).toEqual({
      text: "BLOCKED by Essential safety: deletes your home",
      kind: "blocked",
      blocked: true,
    });
    expect(verdictLine(verdict({ blocked: true, by: "paths:credentials", why: "keys", level: "all" }), catalog).text).toBe("BLOCKED by Credentials (no access): keys");
    expect(verdictLine(verdict({ blocked: true, by: "custom", why: "your entry", match: "npm publish" }), catalog).text).toBe(
      "BLOCKED by your own entry [npm publish]: your entry",
    );
    expect(verdictLine(verdict({ blocked: true, by: "security", why: "the lists guard themselves" }), catalog).text).toContain("the security lists' own protection");
    expect(verdictLine(verdict({}), catalog)).toEqual({ text: "not blocked", kind: "ok", blocked: false });
    expect(byLabel(null, catalog)).toBe("your list");
  });

  it("names the preset for each `by` form, in the catalog the id belongs to", () => {
    // `essential` is a commands preset AND a paths preset: the prefix says which.
    expect(byLabel("commands:essential", catalog)).toBe("Essential safety");
    expect(byLabel("paths:essential", catalog)).toBe("System");
    expect(byLabel("paths:credentials", catalog)).toBe("Credentials");
    expect(byLabel("commands:unknown", catalog)).toBe("unknown");
    expect(byLabel("custom", catalog)).toBe("your own entry");
    expect(byLabel("security", catalog)).toContain("security lists");
    expect(byLabel("commands:essential", null)).toBe("essential");
  });

  it("words an opaque verdict as not refused but put to you", () => {
    const line = verdictLine(verdict({ opaque: true, why: "cannot be checked against your block list" }), catalog);
    expect(line.kind).toBe("opaque");
    expect(line.blocked).toBe(false);
    expect(line.text).toContain("cannot be checked against your list; snowpea will ask you");
  });

  it("sends unsaved edits as the draft of a check", () => {
    const lists = listsOf(config);
    expect(draftOf({ ...lists, commandCustom: ["git push --force"], pathCustom: [{ path: "~/x", level: "all" }] })).toEqual({
      blockedCommands: { presets: ["essential"], custom: ["git push --force"] },
      protectedPaths: { presets: ["essential"], custom: [{ path: "~/x", level: "all" }] },
    });
  });

  it("summarises counts and the presets that are on", () => {
    expect(statusLines(config, catalog)).toEqual([
      "Security lists",
      "Blocked commands: 1 presets on (Essential safety), 1 of your own",
      "Protected folders: 1 presets on (System), 1 of your own",
      "In force now: 2 command patterns, 2 folders",
      "Change them with /security. They apply to sessions that go through snowpea.",
    ]);
  });

  it("says when the connection can only read, and what core skipped", () => {
    const readOnly = normalizeConfig({ writable: false, problems: ["unknown preset 'x'"] });
    const text = statusLines(readOnly, catalog).join("\n");
    expect(text).toContain("Read-only");
    expect(text).toContain("core skipped an unusable entry: unknown preset 'x'");
  });
});

describe("what core says about an entry it refused", () => {
  const refusal = (problems: string[]) => Object.assign(new Error(problems.join("; ")), { data: { code: "invalid_params", details: { problems } } });

  it("splits the reasons into the ones that name an entry and the rest", () => {
    const { entries, others } = saveProblems(
      refusal([
        "blocked command 'rm': the program cannot be only a wildcard: that would block every command",
        "protected path '/tmp/x y': must be absolute",
        "security.blockedCommands.presets: unknown preset 'nope' (known: essential)",
      ]),
    );
    expect(entries).toEqual([
      { tab: "commands", text: "rm", reason: "the program cannot be only a wildcard: that would block every command" },
      { tab: "paths", text: "/tmp/x y", reason: "must be absolute" },
    ]);
    expect(others).toEqual(["security.blockedCommands.presets: unknown preset 'nope' (known: essential)"]);
  });

  it("reads a Python-quoted entry with an apostrophe or a backslash", () => {
    const { entries } = saveProblems(refusal([`blocked command "it's": a pattern is one line`, `blocked command 're:a\\\\b(': the regular expression does not compile: x`]));
    expect(entries.map((entry) => entry.text)).toEqual(["it's", "re:a\\b("]);
  });

  it("falls back to the message when there are no details", () => {
    expect(saveProblems(new Error("boom"))).toEqual({ entries: [], others: ["boom"] });
  });

  it("has a one-screen syntax help that fits 60 columns once wrapped", () => {
    const text = PATTERN_HELP.map((entry) => entry.text).join("\n");
    expect(text).toContain("re:");
    expect(text).toContain("wildcard");
    expect(text).toContain("rm -rf");
    for (const entry of PATTERN_HELP) {
      for (const line of wrapSegments("h", [{ text: entry.text }], 56, entry.kind === "example" ? "                     " : "  ")) {
        expect(textWidth(line.segments.map((segment) => segment.text).join(""))).toBeLessThanOrEqual(56);
      }
    }
  });

  it("notes the skipped entries once, briefly", () => {
    expect(problemNote([])).toBeNull();
    expect(problemNote(["a"])).toBe("core skipped an unusable entry: a");
    expect(problemNote(["a", "b"])).toBe("core skipped 2 unusable entries, e.g. a");
  });

  it("words the block line for the session on screen", () => {
    expect(blockedLine({ sessionId: "s", tool: "shell", summary: "git push --force", by: "git-destructive" })).toBe(
      "Blocked by your security list: git push --force (/security)",
    );
  });
});

describe("/security parsing", () => {
  it("parses the four forms and keeps the text after check verbatim", () => {
    expect(parseSecurityCommand("/security")).toEqual({ kind: "enter" });
    expect(parseSecurityCommand("/security status")).toEqual({ kind: "status" });
    expect(parseSecurityCommand("/security check rm  -rf   ~")).toEqual({ kind: "check", what: "command", text: "rm  -rf   ~" });
    expect(parseSecurityCommand("/security check-path ~/my folder/x")).toEqual({ kind: "check", what: "path", text: "~/my folder/x" });
    expect(parseSecurityCommand("/security check")).toMatchObject({ kind: "invalid" });
    expect(parseSecurityCommand("/security status now")).toMatchObject({ kind: "invalid" });
    expect(parseSecurityCommand("/security wat")).toMatchObject({ kind: "invalid" });
    expect(parseSecurityCommand("/securitylists")).toBeNull();
    expect(parseSecurityCommand("hello")).toBeNull();
  });

  it("completes the sub-actions", () => {
    expect(securitySubCommands("/security c").map((c) => c.name)).toEqual(["security check", "security check-path"]);
    expect(securitySubCommands("/security check-").map((c) => c.name)).toEqual(["security check-path"]);
    expect(securitySubCommands("/security ")).toEqual([]);
    expect(securitySubCommands("/jarvis c")).toEqual([]);
  });
});

describe("scrolling", () => {
  it("moves as little as it can and keeps the row in the window", () => {
    expect(scrollInto(0, 0, 2, 5, 20)).toBe(0);
    expect(scrollInto(0, 6, 8, 5, 20)).toBe(3);
    expect(scrollInto(3, 1, 2, 5, 20)).toBe(1);
    expect(scrollInto(3, 5, 6, 5, 20)).toBe(3);
    // A row taller than the window starts at its top; the end never leaves blank rows.
    expect(scrollInto(0, 4, 12, 5, 20)).toBe(4);
    expect(scrollInto(30, 0, 1, 5, 8)).toBe(0);
  });
});

describe("red lines", () => {
  it("draws a blocked note in red with a cross, nothing else", () => {
    const message = { id: "m1", role: "system" as const, text: "Blocked by your security list: x (/security)", streaming: false, blocked: true };
    const [line] = messageLines(message, 80);
    expect(line.segments[0]).toMatchObject({ text: "✗ ", color: "red" });
    expect(line.segments.slice(1).every((segment) => segment.color === "red")).toBe(true);
    const plain = messageLines({ ...message, blocked: undefined }, 80)[0];
    expect(plain.segments[0]).toMatchObject({ text: "! ", color: "yellow" });
  });
});

describe("Jarvis panel", () => {
  const row = (apply: boolean | null | undefined) => ({
    sessionId: "ext:claude:a",
    project: "/p",
    title: "t",
    status: "running",
    level: "full",
    external: { cli: "claude", id: "a" },
    controlled: false,
    ...(apply === undefined ? {} : { securityListsApply: apply }),
  });
  const state = (apply: boolean | null | undefined) => ({
    ...initialJarvis,
    status: { enabled: true, running: true, folder: "/j", delegationDefault: "staged", sessionId: "j", model: "fast" } as any,
    rows: [row(apply)] as any,
  });
  const text = (apply: boolean | null | undefined) => panelLines(state(apply), 80, 12, Date.now()).map((line) => line.segments.map((s) => s.text).join(""));

  it("takes the not-covered line from core's securityListsApply, not from a guess", () => {
    expect(text(false).join("\n")).toContain("UNCONTROLLED");
    expect(text(false).join("\n")).toContain("security lists do not apply to this session");
    expect(text(false).join("\n")).toContain("no security lists");
    // null: both lists are empty; true: they reach the session; absent: an older core.
    for (const apply of [null, true, undefined]) {
      expect(text(apply).join("\n"), String(apply)).not.toContain("security lists do not apply");
      expect(text(apply).join("\n"), String(apply)).not.toContain("no security lists");
    }
  });
});
