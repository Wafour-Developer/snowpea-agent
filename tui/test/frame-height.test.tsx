/**
 * The inline live region never reaches the terminal's height.
 *
 * Ink redraws the live region in place only while it is shorter than
 * `stdout.rows`; at that height it writes `clearTerminal` and the whole
 * scrollback again on every frame (`ink/build/ink.js`, `onRender`). Under tmux
 * that duplicates the scrollback and makes the view jump, and the newest entry
 * — the prompt just sent — can land outside the visible rows.
 *
 * Each scenario below is one of the tall cases the budget has to cover, played
 * through the real `App` at several terminal sizes. Two things are asserted:
 * no clear-terminal sequence was ever written, and the newest entry is on the
 * screen a terminal would show (a tiny emulator replays the bytes).
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { ApprovalRequestParams, SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

const CLEAR_TERMINAL = "\u001B[2J";

function fakeClient(update = false) {
  let listeners: any;
  let onApproval: ((request: ApprovalRequestParams) => Promise<unknown>) | undefined;
  let turns = 0;
  return {
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: (handler: any) => {
      onApproval = handler;
    },
    onQuestionRequest: () => undefined,
    respondApproval: async () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () =>
      update
        ? { available: true, current: "0.1.0", latest: "9.9.9" }
        : { available: false, current: "0.1.0", latest: "0.1.0" },
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: `t-${++turns}` }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
    ask(args: Record<string, unknown>) {
      void onApproval?.({ requestId: "req-1", tool: "shell", risk: "high", args } as ApprovalRequestParams);
    },
  };
}

type Client = ReturnType<typeof fakeClient>;

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq: ++seq, kind, payload }) as SessionEvent;

const ko = "한국어 문장이 길게 이어져서 터미널 너비를 넘기면 줄이 감깁니다";

/** `count` lines of answer, the last one carrying `marker`. */
function answer(count: number, marker: string): string {
  return Array.from({ length: count }, (_, i) =>
    i === count - 1 ? marker : `${i + 1}. ${ko} and some English to wrap ${i + 1}`,
  ).join("\n");
}

async function streamText(client: Client, text: string, step = 60): Promise<void> {
  for (let at = 0; at < text.length; at += step) {
    client.emit(ev("message.delta", { text: text.slice(at, at + step) }));
    await sleep(8);
  }
}

async function runningCards(client: Client, count: number): Promise<void> {
  for (let c = 0; c < count; c += 1) {
    const callId = `call-${seq}-${c}`;
    client.emit(ev("tool.call", { callId, name: "shell", args: { command: `long running job ${c}` } }));
    client.emit(
      ev("tool.progress", {
        callId,
        stream: "stdout",
        chunk: Array.from({ length: 10 }, (_, i) => `output line ${i} of job ${c} ${ko}`).join("\n"),
      }),
    );
  }
  await sleep(60);
}

function bigDiff(lines: number): string {
  return [
    "--- a/src/file.ts",
    "+++ b/src/file.ts",
    "@@ -1,40 +1,40 @@",
    ...Array.from({ length: lines }, (_, i) => `${i % 2 ? "+" : "-"}const value${i} = ${i}; // ${ko}`),
  ].join("\n");
}

interface Scenario {
  name: string;
  update?: boolean;
  run(client: Client, stdin: any): Promise<string>;
}

const scenarios: Scenario[] = [
  {
    name: "several running tool cards under a streaming answer",
    async run(client) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      client.emit(ev("message.delta", { text: "Let me run a few things." }));
      await runningCards(client, 4);
      await streamText(client, answer(30, "NEWEST-CARDS"));
      return "NEWEST-CARDS";
    },
  },
  {
    name: "40-line diffs held while the turn runs",
    async run(client) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      for (let d = 0; d < 3; d += 1) {
        client.emit(ev("tool.call", { callId: `edit-${d}`, name: "edit", args: { path: "src/file.ts" } }));
        client.emit(ev("diff", { path: `src/file${d}.ts`, patch: bigDiff(60) }));
        client.emit(ev("tool.result", { callId: `edit-${d}`, ok: true, output: "edited" }));
        await sleep(30);
      }
      client.emit(ev("tool.call", { callId: "edit-last", name: "edit", args: { path: "src/last.ts" } }));
      client.emit(ev("diff", { path: "src/last.ts", patch: `${bigDiff(3)}\n+// NEWEST-DIFF` }));
      await sleep(100);
      return "NEWEST-DIFF";
    },
  },
  {
    name: "an expanded tool output (Ctrl+O)",
    async run(client, stdin) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      client.emit(ev("tool.call", { callId: "c1", name: "shell", args: { command: "cat big" } }));
      client.emit(
        ev("tool.result", {
          callId: "c1",
          ok: true,
          output: Array.from({ length: 60 }, (_, i) => `row ${i} ${ko}`).join("\n"),
        }),
      );
      await sleep(80);
      stdin.write("\u000F");
      await sleep(120);
      client.emit(ev("message.delta", { text: answer(6, "NEWEST-EXPANDED") }));
      await sleep(100);
      return "NEWEST-EXPANDED";
    },
  },
  {
    name: "the approval prompt with many arguments over a running card and an answer",
    async run(client) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      await streamText(client, answer(20, "first part"));
      await runningCards(client, 1);
      client.ask(
        Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`arg${i}`, `${ko} value ${i}`])),
      );
      await sleep(150);
      return "Approval required";
    },
  },
  {
    name: "many queued prompts under a streaming answer",
    async run(client) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      for (let q = 1; q <= 9; q += 1) client.emit(ev("turn.queued", { turnId: `q-${q}`, position: q }));
      await streamText(client, answer(30, "NEWEST-QUEUED"));
      return "NEWEST-QUEUED";
    },
  },
  {
    name: "a long multi-line draft in the composer",
    async run(client, stdin) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      await streamText(client, answer(30, "NEWEST-DRAFT"));
      for (let l = 0; l < 10; l += 1) {
        await type(stdin, `draft line ${l} ${ko}`, 0);
        stdin.write("\u001B\r");
        await sleep(20);
      }
      await sleep(100);
      return "NEWEST-DRAFT";
    },
  },
  {
    name: "the agent list in the status area",
    async run(client) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      for (let a = 0; a < 8; a += 1) {
        client.emit(
          ev("subagent.spawn", { agentId: `a${a}`, name: `agent${a}`, task: `${ko} task ${a}`, status: "running", sessionId: `child-${a}` }),
        );
      }
      await streamText(client, answer(30, "NEWEST-AGENTS"));
      return "NEWEST-AGENTS";
    },
  },
  {
    name: "the update banner while an answer streams",
    update: true,
    async run(client) {
      await sleep(200);
      client.emit(ev("turn.started", { turnId: "t-0" }));
      await streamText(client, answer(30, "NEWEST-BANNER"));
      return "NEWEST-BANNER";
    },
  },
  {
    name: "a turn that ends with cards and a long answer, then the next prompt",
    async run(client, stdin) {
      client.emit(ev("turn.started", { turnId: "t-0" }));
      await runningCards(client, 3);
      for (let c = 0; c < 3; c += 1) {
        client.emit(ev("tool.result", { callId: `call-${seq}-${c}`, ok: true, output: "ok" }));
      }
      await streamText(client, answer(40, "end of answer"));
      client.emit(ev("message.done", { text: answer(40, "end of answer") }));
      client.emit(ev("turn.done", { turnId: "t-0", reason: "complete" }));
      await sleep(100);
      await type(stdin, "NEWEST-PROMPT next question", 0);
      stdin.write("\r");
      await sleep(150);
      return "NEWEST-PROMPT";
    },
  },
];

const sizes: Array<[number, number]> = [
  [60, 15],
  [80, 20],
  [100, 24],
  [120, 40],
];

describe("the live region stays shorter than the terminal", () => {
  for (const scenario of scenarios) {
    for (const [columns, rows] of sizes) {
      it(`${scenario.name} — ${columns}x${rows}`, async () => {
        const client = fakeClient(scenario.update);
        const stdin = fakeStdin();
        const stdout = fakeStdout(columns, rows);
        const instance = render(
          <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
          { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
        );
        await sleep(150);
        const newest = await scenario.run(client, stdin);
        await sleep(150);
        const output = stdout.text();
        instance.unmount();
        stdin.end();

        const clears = stdout.chunks.filter((chunk) => chunk.includes(CLEAR_TERMINAL)).length;
        expect(clears, "frames that cleared the terminal").toBe(0);
        expect(screen(output, rows).join("\n")).toContain(newest);
      }, 30000);
    }
  }
});
