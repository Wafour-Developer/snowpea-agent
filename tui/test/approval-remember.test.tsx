/**
 * The approval prompt's project row for a shell command (1.34.0, `shellRemember`).
 *
 * The daemon says what "remember" would store (`remember`); the row names it,
 * or is left out when only "once" can be kept. An older daemon sends no
 * `remember`, and the row stays as it was. Whatever the rows, the prompt keeps
 * its height while it is shown.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import {
  APPROVAL_OPTIONS,
  ApprovalPrompt,
  approvalOptions,
  type ApprovalAnswer,
} from "../src/components/ApprovalPrompt.js";
import type { ApprovalEntry } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

function entry(command: string, remember: ApprovalEntry["remember"]): ApprovalEntry {
  return {
    requestId: "req-r",
    sessionId: "sess-1",
    tool: "shell",
    risk: "high",
    args: { command },
    remember,
    source: "interactive",
  } as ApprovalEntry;
}

const GIT_STATUS = {
  pattern: "^git status( .*)?$",
  display: "git status …",
  scopes: ["project", "always"] as Array<"project" | "always">,
};

async function show(request: ApprovalEntry) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 30);
  const answers: ApprovalAnswer[] = [];
  const instance = render(
    <ApprovalPrompt request={request} onDecide={(answer) => answers.push(answer)} />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false, debug: true },
  );
  await sleep(80);
  return { stdin, stdout, instance, answers };
}

describe("remembering a shell command", () => {
  it("names the rule the project row stores", async () => {
    const { stdin, stdout, instance, answers } = await show(entry("git status -s", GIT_STATUS));
    expect(stdout.text()).toContain('Yes, allow "git status …" in this project');
    expect(stdout.text()).not.toContain("Yes, add to project allowlist");
    stdin.write("p");
    await sleep(60);
    instance.unmount();
    expect(answers).toEqual([{ decision: "allow", scope: "project" }]);
  });

  it("leaves the project row out when nothing may be remembered", async () => {
    const { stdin, stdout, instance, answers } = await show(entry("git push --force", null));
    expect(stdout.text()).not.toContain("project");
    stdin.write("p");
    await sleep(60);
    stdin.write("y");
    await sleep(60);
    instance.unmount();
    expect(answers).toEqual([{ decision: "allow", scope: "once" }]);
  });

  it("keeps the old rows for a daemon that sends no remember", () => {
    const old = { args: { command: "ls" } } as ApprovalEntry;
    expect(approvalOptions("accept", old)).toBe(APPROVAL_OPTIONS);
    // A non-shell request with remember: null is not a shell rule either.
    const tool = { args: { url: "https://example.test" }, remember: null } as ApprovalEntry;
    expect(approvalOptions("accept", tool)).toBe(APPROVAL_OPTIONS);
  });

  it("keeps its height while the cursor moves", async () => {
    for (const remember of [GIT_STATUS, null]) {
      const { stdin, stdout, instance } = await show(entry("git status", remember));
      for (let i = 0; i < 6; i += 1) {
        stdin.write("\u001B[B");
        await sleep(40);
      }
      instance.unmount();
      const heights = new Set(
        stdout.chunks.filter((chunk) => chunk.includes("Approval required")).map(
          (chunk) => chunk.split("\n").length,
        ),
      );
      expect(heights.size).toBe(1);
    }
  });
});
