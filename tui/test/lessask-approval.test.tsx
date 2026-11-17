/**
 * 0.2.90: the approval prompt shows the whole command (wrapped, never cut with
 * an ellipsis), says an unreadable line in words that do not read as a block
 * list, and lists core's `uncheckedWhy` as plain text.
 *
 * Command strings here are data compared with text; nothing runs them.
 */

import React from "react";
import { render } from "ink";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/app.js";
import { formatArgs } from "../src/components/ApprovalPrompt.js";
import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import { askReasonWords, isUncheckable, readUncheckedWhy, uncheckedLine } from "../src/state/ask-why.js";
import { fakeStdin, fakeStdout, screen, sleep } from "./tty.js";

afterEach(() => resetUiLanguage());

const TAIL = "bash bin/try_native_s.sh --out build/out";
const LONG = `cd /home/me/projects/weather && ./gradlew :app:assembleDebug --no-daemon --stacktrace --console=plain -Pandroid.injected.build.abi=arm64-v8a && ${TAIL}`;
const COMMAND = `${LONG} ${"x".repeat(300 - LONG.length - TAIL.length - 2)} ${TAIL}`;
const WHY = [{ part: "bin/try_native_s.sh", file: "bin/try_native_s.sh", line: 15, reason: "the path is only known when it runs", text: '"$OUT" is written' }];

describe("the reason and uncheckedWhy, as words", () => {
  it("rewords the two unreadable-line reasons and leaves any other as core wrote it", () => {
    for (const reason of ["cannot be checked against your block list", "cannot tell what this code changes"]) {
      expect(isUncheckable(reason)).toBe(true);
      expect(askReasonWords(reason)).toBe("snowpea cannot tell what this will touch before it runs, so it asks you");
    }
    expect(isUncheckable("acts on snowpea's own controls")).toBe(false);
    expect(askReasonWords("acts on snowpea's own controls")).toBe("acts on snowpea's own controls");
    expect(askReasonWords(null)).toBeNull();
    setUiLanguage("ko");
    expect(askReasonWords("cannot be checked against your block list")).toBe("snowpea가 실행 전에 무엇을 건드릴지 알 수 없어 확인합니다");
  });

  it("reads uncheckedWhy defensively and words a line in both languages", () => {
    expect(readUncheckedWhy(undefined)).toEqual([]);
    expect(readUncheckedWhy("x")).toEqual([]);
    expect(readUncheckedWhy([null, 4, {}, { part: "p" }])).toHaveLength(1);
    const item = readUncheckedWhy(WHY)[0]!;
    expect(uncheckedLine(item)).toBe('bin/try_native_s.sh, line 15: "$OUT" is written — the path is only known when it runs');
    setUiLanguage("ko");
    expect(uncheckedLine(item)).toBe('bin/try_native_s.sh 15번째 줄: "$OUT" is written — the path is only known when it runs');
  });

  it("keeps the whole command in the argument line", () => {
    const line = formatArgs({ command: COMMAND, path: "x".repeat(200) });
    expect(line[0]).toBe(`command: ${COMMAND}`);
    expect(line[1]!.endsWith("…")).toBe(true);
  });
});

function fakeClient() {
  const client: any = {
    getStatus: () => "connected",
    setListeners: () => undefined,
    onApprovalRequest: (handler: any) => {
      client.ask = handler;
    },
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.89", latest: "0.2.89" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: vi.fn(async (_s: string, mode: string) => ({ mode })),
    respondApproval: async () => undefined,
  };
  return client;
}

async function mount(client: any, columns = 100, rows = 40) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="auto" workdir="/tmp/project" />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(200);
  return { view: () => screen(stdout.text(), rows).join("\n"), done: () => { instance.unmount(); stdin.end(); } };
}

describe("the approval prompt", () => {
  it("shows a 300-character compound command whole, the reworded reason, the lines and the setting", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.ask({
      requestId: "a-1", sessionId: "s-1", tool: "shell", risk: "high", args: { command: COMMAND },
      neverDelegable: true, reason: "cannot be checked against your block list", uncheckedWhy: WHY,
    });
    await sleep(300);
    const view = app.view();
    // Wrapped, so the whole text is there once the line breaks are taken out.
    const flat = view.replace(/[│╭╮╰╯─]/g, "").replace(/\s+/g, "");
    expect(flat).toContain(COMMAND.replace(/\s+/g, ""));
    expect(view).toContain("snowpea cannot tell what this will touch before it runs, so it asks you");
    expect(view).not.toContain("block list");
    expect(view).toContain('bin/try_native_s.sh, line 15: "$OUT" is written');
    expect(view).toContain("To turn this check off in auto mode: /security");
    app.done();
  });

  it("works without uncheckedWhy and adds no setting line for another reason", async () => {
    const client = fakeClient();
    const app = await mount(client);
    void client.ask({
      requestId: "a-2", sessionId: "s-1", tool: "shell", risk: "high", args: { command: "ls" },
      neverDelegable: true, reason: "acts on snowpea's own controls",
    });
    await sleep(300);
    const view = app.view();
    expect(view).toContain("acts on snowpea's own controls");
    expect(view).not.toContain("To turn this check off");
    app.done();
  });
});
