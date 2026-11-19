/**
 * 1.68.0 (jobApprovals): a scheduled routine's request in the unattended
 * backlog names the routine and offers "always in this routine" (scope task).
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { ApprovalQueue, scopesFor } from "../src/components/ApprovalQueue.js";
import type { ApprovalEntry } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const routine = {
  requestId: "ap-routine1",
  sessionId: "s-run",
  tool: "shell",
  args: { command: "git fetch" },
  risk: "high",
  timeoutSec: 900,
  job: { id: "j-1", title: "morning sync" },
  source: "queue",
} as unknown as ApprovalEntry;

describe("a routine's approval in the backlog", () => {
  it("offers the routine scope only for a scheduled run", () => {
    expect(scopesFor(routine)).toContain("task");
    expect(scopesFor({ ...routine, job: null } as ApprovalEntry)).not.toContain("task");
  });

  it("names the routine and answers 'always in this routine' as task", async () => {
    const answers: Array<[string, string, string]> = [];
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 20);
    const instance = render(
      <ApprovalQueue requests={[routine]} isActive onRespond={(id, decision, scope) => answers.push([id, decision, scope])} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(100);
    const view = stdout.text();
    expect(view).toContain("[routine: morning sync]");
    expect(view).toContain("always in this routine");
    stdin.write("\u001b[C"); // → to the routine scope
    await sleep(50);
    stdin.write("a");
    await sleep(50);
    expect(answers).toEqual([["ap-routine1", "allow", "task"]]);
    instance.unmount();
  });
});
