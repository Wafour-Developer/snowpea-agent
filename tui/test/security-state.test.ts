import { describe, expect, it } from "vitest";

import { messageLines } from "../src/layout/transcript.js";
import { parseSecurityCommand, securitySubCommands } from "../src/slash/security.js";
import {
  addCustom,
  blockedLine,
  byLabel,
  cycleLevel,
  editCustom,
  hasCustom,
  listsAreSet,
  listsOf,
  normalizeBlocked,
  normalizeCatalog,
  normalizeConfig,
  normalizeVerdict,
  removeCustom,
  sameLists,
  scrollInto,
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

const config = normalizeConfig({
  blockedCommands: { presets: ["essential"], custom: ["npm publish"] },
  protectedPaths: { presets: ["essential"], custom: [{ path: "/srv/data", level: "write" }] },
  effective: { commands: [{ pattern: "rm -rf ~", by: "essential" }], paths: [{ path: "/etc", level: "write", by: "essential" }] },
});

describe("security contract normalisation", () => {
  it("keeps the shapes and drops what is not a string", () => {
    expect(catalog.commands[0]).toEqual({ id: "essential", title: "Essential safety", description: "d", examples: ["rm -rf ~"], default: true });
    expect(catalog.paths[1]).toMatchObject({ level: "all", paths: ["~/.ssh"] });
    expect(normalizeConfig(null)).toEqual({
      blockedCommands: { presets: [], custom: [] },
      protectedPaths: { presets: [], custom: [] },
      effective: { commands: [], paths: [] },
    });
    expect(normalizeVerdict({ blocked: true, by: "x", why: "y", level: "all" })).toEqual({ blocked: true, by: "x", why: "y", level: "all" });
    expect(normalizeVerdict({ blocked: "yes" }).blocked).toBe(false);
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
  it("writes BLOCKED by <preset title>: <why>, with the level for a folder", () => {
    expect(verdictLine({ blocked: true, by: "essential", why: "deletes your home", level: undefined }, catalog)).toEqual({
      text: "BLOCKED by Essential safety: deletes your home",
      blocked: true,
    });
    expect(verdictLine({ blocked: true, by: "credentials", why: "keys", level: "all" }, catalog).text).toBe("BLOCKED by Credentials (no access): keys");
    expect(verdictLine({ blocked: true, by: "npm publish", why: "your entry" }, catalog).text).toBe("BLOCKED by npm publish: your entry");
    expect(verdictLine({ blocked: false, by: null, why: "" }, catalog)).toEqual({ text: "not blocked", blocked: false });
    expect(byLabel(null, catalog)).toBe("your list");
  });

  it("summarises counts and the presets that are on", () => {
    expect(statusLines(config, catalog)).toEqual([
      "Security lists",
      "Blocked commands: 1 presets on (Essential safety), 1 of your own",
      "Protected folders: 1 presets on (System), 1 of your own",
      "In force now: 1 command patterns, 1 folders",
      "Change them with /security. They apply to sessions that go through snowpea.",
    ]);
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
  const state = {
    ...initialJarvis,
    status: { enabled: true, running: true, folder: "/j", delegationDefault: "staged", sessionId: "j", model: "fast" } as any,
    rows: [{ sessionId: "ext:claude:a", project: "/p", title: "t", status: "running", level: "full", external: { cli: "claude", id: "a" }, controlled: false }] as any,
  };
  const text = (securityLists: boolean) => panelLines(state, 80, 12, Date.now(), securityLists).map((line) => line.segments.map((s) => s.text).join(""));

  it("adds the not-covered line under UNCONTROLLED only when lists are set", () => {
    expect(text(false).join("\n")).toContain("UNCONTROLLED");
    expect(text(false).join("\n")).not.toContain("security lists do not apply");
    expect(text(true).join("\n")).toContain("security lists do not apply to this session");
  });
});
