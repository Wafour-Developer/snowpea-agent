/**
 * `/skill` completion, the command line the form builds, and the thresholds
 * behind the "turn this into a skill" nudge.
 */

import { describe, expect, it } from "vitest";

import {
  createdSkillName,
  isBareSkillCreate,
  isValidSkillName,
  parseSkillEdit,
  SKILL_ACTIONS,
  skillCreateCommand,
  skillSubCommands,
} from "../src/state/skill-completion.js";
import { shouldSuggestSkill, SKILL_HINT_MS, SKILL_HINT_TOOL_CALLS } from "../src/state/skill-hint.js";
import { editorCommand } from "../src/util/editor.js";

const names = (draft: string) => skillSubCommands(draft).map((command) => command.name);

describe("the /skill sub-actions", () => {
  it("offers every one of them for a bare /skill", () => {
    expect(names("/skill")).toEqual(SKILL_ACTIONS.map((entry) => `skill ${entry.action}`));
  });

  it("offers them after the space too", () => {
    expect(names("/skill ")).toHaveLength(SKILL_ACTIONS.length);
  });

  it("narrows as the action is typed", () => {
    expect(names("/skill cr")).toEqual(["skill create"]);
    expect(names("/skill s")).toEqual(["skill sources"]);
  });

  it("stops once the action has an argument of its own", () => {
    expect(names("/skill create deploy")).toEqual([]);
    expect(names("/skill learn my-thing")).toEqual([]);
  });

  it("offers installed skill names for learn and edit", () => {
    const skills = [
      { name: "deploy", summary: "ship the site", kind: "skill", source: "project" },
      { name: "audit", summary: "review changes", kind: "skill", source: "global" },
    ];
    expect(skillSubCommands("/skill learn ", skills).map((row) => row.name)).toEqual([
      "skill learn deploy",
      "skill learn audit",
    ]);
    expect(skillSubCommands("/skill edit dep", skills).map((row) => row.name)).toEqual([
      "skill edit deploy",
    ]);
    expect(skillSubCommands("/skill learn dep", skills)[0]?.preview).toBe("ship the site");
  });

  it("says nothing about other commands", () => {
    expect(names("/model")).toEqual([]);
    expect(names("not a command")).toEqual([]);
  });

  it("describes each row, so the palette has something to show", () => {
    for (const command of skillSubCommands("/skill")) {
      expect(command.summary).not.toBe("");
      expect(command.source).toBe("core");
    }
  });
});

describe("the form's cue", () => {
  it("is `/skill create` with nothing after it", () => {
    expect(isBareSkillCreate("/skill create")).toBe(true);
    expect(isBareSkillCreate("  /skill create  ")).toBe(true);
  });

  it("is not the command with its arguments already typed", () => {
    expect(isBareSkillCreate('/skill create deploy "ship it"')).toBe(false);
    expect(isBareSkillCreate("/skill learn")).toBe(false);
  });
});

describe("the command the form builds", () => {
  it("quotes the description", () => {
    expect(
      skillCreateCommand({ name: "deploy", description: "ship the site", scope: "project" }),
    ).toBe('/skill create deploy "ship the site"');
  });

  it("adds --global for the wider scope", () => {
    expect(skillCreateCommand({ name: "deploy", description: "ship it", scope: "global" })).toBe(
      '/skill create deploy "ship it" --global',
    );
  });

  it("drops quotes that would end the argument early", () => {
    expect(
      skillCreateCommand({ name: "deploy", description: 'run "make test" first', scope: "project" }),
    ).toBe('/skill create deploy "run make test first"');
  });

  it("knows which names the daemon will take", () => {
    expect(isValidSkillName("deploy-site")).toBe(true);
    expect(isValidSkillName("deploy site")).toBe(false);
    expect(isValidSkillName("")).toBe(false);
    expect(isValidSkillName("-nope")).toBe(false);
  });
});

describe("reading the daemon's reply", () => {
  it("takes the name from the command it tells you to run", () => {
    expect(createdSkillName("Created skill 'deploy' at /x/SKILL.md. Run it with /deploy.")).toBe(
      "deploy",
    );
    expect(createdSkillName("Learned skill 'audit' at /x. Run it with /audit.")).toBe("audit");
  });

  it("falls back to the name in the sentence", () => {
    expect(createdSkillName("Created skill 'deploy' somewhere")).toBe("deploy");
  });

  it("stays quiet about any other reply", () => {
    expect(createdSkillName("here is the function you asked for")).toBeNull();
  });
});

describe("/skill edit", () => {
  it("reads the name", () => {
    expect(parseSkillEdit("/skill edit deploy")).toBe("deploy");
  });

  it("wants exactly one", () => {
    expect(parseSkillEdit("/skill edit")).toBeNull();
    expect(parseSkillEdit("/skill edit a b")).toBeNull();
    expect(parseSkillEdit("/skill learn deploy")).toBeNull();
  });

  it("prefers VISUAL, then EDITOR, then vi", () => {
    expect(editorCommand({ VISUAL: "nvim", EDITOR: "nano" })).toBe("nvim");
    expect(editorCommand({ EDITOR: "nano" })).toBe("nano");
    expect(editorCommand({})).toBe("vi");
    expect(editorCommand({ EDITOR: "  " })).toBe("vi");
  });
});

describe("the skill nudge", () => {
  const heavy = {
    toolCalls: SKILL_HINT_TOOL_CALLS,
    elapsedMs: SKILL_HINT_MS,
    ok: true,
    shown: false,
    dismissed: false,
  };

  it("follows a long tool-heavy turn", () => {
    expect(shouldSuggestSkill(heavy)).toBe(true);
  });

  it("stays away from short or quiet turns", () => {
    expect(shouldSuggestSkill({ ...heavy, toolCalls: 2 })).toBe(false);
    expect(shouldSuggestSkill({ ...heavy, elapsedMs: 1000 })).toBe(false);
  });

  it("says nothing about a turn that failed", () => {
    expect(shouldSuggestSkill({ ...heavy, ok: false })).toBe(false);
  });

  it("is offered once, and never again after it is waved away", () => {
    expect(shouldSuggestSkill({ ...heavy, shown: true })).toBe(false);
    expect(shouldSuggestSkill({ ...heavy, dismissed: true })).toBe(false);
  });
});
