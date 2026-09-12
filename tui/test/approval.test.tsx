/**
 * The approval prompt, driven the way a person drives it.
 *
 * The bug this pins: the prompt could not be confirmed with Enter. Every case
 * here goes through the real `App`, the real menu and a real Ink render, and
 * checks what the daemon would be told.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { ApprovalRequestParams, ApprovalResponse } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

/** A client that hands the test the approval handler `App` installs. */
function fakeClient() {
  let onApproval: ((request: ApprovalRequestParams) => Promise<ApprovalResponse>) | undefined;
  const responded: Array<{ requestId: string; decision: string; scope: string }> = [];
  return {
    responded,
    getStatus: () => "connected",
    setListeners: () => undefined,
    onApprovalRequest: (handler: any) => {
      onApproval = handler;
    },
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async (requestId: string, decision: string, scope: string) => {
      responded.push({ requestId, decision, scope });
    },
    ask(): Promise<ApprovalResponse> {
      return onApproval!({
        requestId: "req-1",
        tool: "shell",
        risk: "high",
        args: { command: "rm -rf build" },
      } as ApprovalRequestParams);
    },
  };
}

async function openPrompt() {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(120);
  const answer = client.ask();
  await sleep(120);
  return { client, stdin, stdout, instance, answer };
}

describe("approval prompt", () => {
  it("shows the tool, its arguments and the risk", async () => {
    const { stdout, instance, answer, stdin } = await openPrompt();
    const output = stdout.text();
    stdin.write("\r");
    await answer;
    instance.unmount();

    expect(output).toContain("Approval required");
    expect(output).toContain("shell");
    expect(output).toContain("command: rm -rf build");
    expect(output).toContain("risk=");
    expect(output).toContain("Yes");
    expect(output).toContain("No");
  });

  it("confirms the highlighted option with Enter, which is Yes", async () => {
    const { stdin, instance, answer } = await openPrompt();
    stdin.write("\r");
    const decided = await answer;
    instance.unmount();
    expect(decided).toEqual({ decision: "allow", scope: "once" });
  });

  it("walks to No with the arrow keys and confirms that instead", async () => {
    const { stdin, instance, answer } = await openPrompt();
    // Down three rows: Yes → session → project → No.
    for (let i = 0; i < 3; i += 1) {
      stdin.write("\u001B[B");
      await sleep(40);
    }
    stdin.write("\r");
    const decided = await answer;
    instance.unmount();
    expect(decided).toEqual({ decision: "deny", scope: "once" });
  });

  it("wraps around with Up, so Enter on the first row reaches No", async () => {
    const { stdin, instance, answer } = await openPrompt();
    stdin.write("\u001B[A");
    await sleep(40);
    stdin.write("\r");
    const decided = await answer;
    instance.unmount();
    expect(decided).toEqual({ decision: "deny", scope: "once" });
  });

  it("keeps the letter shortcuts", async () => {
    for (const [key, expected] of [
      ["y", { decision: "allow", scope: "once" }],
      ["a", { decision: "allow", scope: "session" }],
      ["p", { decision: "allow", scope: "project" }],
      ["n", { decision: "deny", scope: "once" }],
    ] as const) {
      const { stdin, instance, answer } = await openPrompt();
      stdin.write(key);
      const decided = await answer;
      instance.unmount();
      expect(decided).toEqual(expected);
    }
  });

  it("takes Escape as a refusal", async () => {
    const { stdin, instance, answer } = await openPrompt();
    stdin.write("\u001B"); // Escape
    const decided = await answer;
    instance.unmount();
    expect(decided).toEqual({ decision: "deny", scope: "once" });
  });

  it("swallows every other key instead of leaking it into the draft", async () => {
    const { stdin, stdout, instance, answer } = await openPrompt();
    stdout.chunks.length = 0;
    await type(stdin, "hello");
    const afterTyping = stdout.text();
    stdin.write("\r");
    await answer;
    instance.unmount();

    // Nothing typed reached the chat line, and the prompt is still up.
    expect(afterTyping).not.toContain("> hello");
    expect(afterTyping).not.toContain("> h");
  });

  it("does not let Shift+Tab change mode while the question is open", async () => {
    const { stdin, stdout, instance, answer } = await openPrompt();
    stdout.chunks.length = 0;
    stdin.write("\u001B[Z");
    await sleep(80);
    const after = stdout.text();
    stdin.write("\r");
    await answer;
    instance.unmount();
    expect(after).not.toContain("mode: AUTO");
  });
});
