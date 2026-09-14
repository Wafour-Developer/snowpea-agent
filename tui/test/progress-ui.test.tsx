/**
 * Live output on screen: the tail of a running tool, and the compacting phase.
 *
 * Assertions about what is *gone* read the last frame Ink wrote, not the whole
 * transcript of bytes — an earlier frame still holds the line that has since
 * been replaced.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { ToolCall } from "../src/components/ToolCall.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

function fakeClient() {
  let listeners: any;
  return {
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.9", latest: "0.1.9" }),
    call: async () => ({}),
    prompt: async () => ({ turnId: "t-1" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: "sess-1", seq, kind, payload }) as SessionEvent;

async function open() {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(120, 30);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(250);
  return { client, stdout, instance, frame: () => stdout.chunks[stdout.chunks.length - 1] ?? "" };
}

const SHELL = { callId: "c-1", name: "shell", args: { command: "make test" } };

describe("a running tool's live output", () => {
  it("shows the tail, marks stderr and says when it was cut", async () => {
    const { client, instance, frame } = await open();
    client.emit(event(1, "turn.started", { turnId: "t-1", queued: false }));
    client.emit(event(2, "tool.call", SHELL));
    client.emit(
      event(3, "tool.progress", {
        callId: "c-1",
        stream: "stdout",
        chunk: "compiling foo\ncompiling bar\n",
        seq: 1,
      }),
    );
    client.emit(
      event(4, "tool.progress", {
        callId: "c-1",
        stream: "stderr",
        chunk: "warning: unused\n",
        seq: 2,
        truncated: true,
      }),
    );
    await sleep(250);
    const live = frame();
    instance.unmount();

    expect(live).toContain("compiling bar");
    expect(live).toContain("warning: unused");
    expect(live).toContain("… truncated");
  });

  // The fake tty carries no colour, so the tones are checked on the element
  // tree the card builds.
  it("paints stderr amber and stdout dim", () => {
    const element = ToolCall({
      call: {
        callId: "c-1",
        name: "shell",
        args: {},
        state: "running",
        progress: [
          { text: "compiling bar", stream: "stdout" },
          { text: "warning: unused", stream: "stderr" },
        ],
      } as any,
    });
    const rows = React.Children.toArray((element.props as any).children)
      .flatMap((child) => (Array.isArray(child) ? child : [child]))
      .filter((child: any) => typeof child?.props?.children !== "undefined")
      .map((child: any) => child.props);
    const stderrRow = rows.find((props: any) => String(props.children).includes("warning"));
    const stdoutRow = rows.find((props: any) => String(props.children).includes("compiling"));
    expect(stderrRow.color).toBe("yellow");
    expect(stdoutRow.dimColor).toBe(true);
  });

  it("is replaced by the result when the tool finishes", async () => {
    const { client, instance, frame } = await open();
    client.emit(event(1, "turn.started", { turnId: "t-1", queued: false }));
    client.emit(event(2, "tool.call", SHELL));
    client.emit(
      event(3, "tool.progress", { callId: "c-1", stream: "stdout", chunk: "step one\n", seq: 1 }),
    );
    await sleep(200);
    expect(frame()).toContain("step one");

    client.emit(
      event(4, "tool.result", { callId: "c-1", name: "shell", ok: true, output: "all done" }),
    );
    await sleep(250);
    const after = frame();
    instance.unmount();

    expect(after).not.toContain("step one");
  });

  it("shows the latest line a delegate sent", async () => {
    const { client, instance, frame } = await open();
    client.emit(event(1, "turn.started", { turnId: "t-1", queued: false }));
    client.emit(
      event(2, "tool.call", { callId: "d-1", name: "delegate_task", args: { agent: "explorer" } }),
    );
    client.emit(
      event(3, "tool.progress", {
        callId: "d-1",
        stream: "stdout",
        chunk: "reading the router\n",
        seq: 1,
      }),
    );
    await sleep(250);
    const live = frame();
    instance.unmount();

    expect(live).toContain("reading the router");
  });
});

describe("compaction", () => {
  it("says it is compacting until the compaction lands", async () => {
    const { client, instance, frame } = await open();
    client.emit(event(1, "compaction.started", { reason: "manual", before: 90_000 }));
    await sleep(250);
    expect(frame()).toContain("Compacting");

    client.emit(event(2, "compaction", { before: 90_000, after: 20_000 }));
    await sleep(300);
    const after = frame();
    instance.unmount();

    expect(after).not.toContain("Compacting…");
    // The divider the compaction leaves behind stays.
    expect(after.toLowerCase()).toContain("compacted");
  });
});
