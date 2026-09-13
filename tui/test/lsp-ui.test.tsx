/**
 * The language-server surfaces, wired: the HUD segment, `/lsp`, and the badge a
 * diagnostics event puts on a diff.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { countOf, fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const READY = { id: "pyright", root: "/work", state: "ready", languageId: "python", pid: 4242 };
const BROKEN = { id: "gopls", root: "/work", state: "broken", languageId: "go", pid: null };

function fakeClient(servers: unknown[] = []) {
  let listeners: any;
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    serverCapabilities: () => [],
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.9", latest: "0.1.9" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "lsp.status") return { servers };
      return { commands: [] };
    },
    prompt: async () => ({ turnId: "t-1" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({
  sessionId: "sess-1",
  seq,
  kind,
  payload,
});

async function open(servers: unknown[] = []) {
  const client = fakeClient(servers);
  const stdin = fakeStdin();
  const stdout = fakeStdout(120, 26);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/work" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(250);
  return { client, stdin, stdout, instance };
}

describe("the lsp segment", () => {
  it("counts the ready servers once the daemon answers", async () => {
    const { stdout, instance } = await open([READY]);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("lsp 1");
  });

  it("marks a broken one", async () => {
    const { stdout, instance } = await open([READY, BROKEN]);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("lsp 1!");
  });

  it("stays out of the way when nothing is running", async () => {
    const { stdout, instance } = await open([]);
    const output = stdout.text();
    instance.unmount();
    expect(output).not.toContain("lsp ");
  });

  it("asks again when a server publishes diagnostics", async () => {
    const { client, instance } = await open([READY]);
    const before = client.calls.filter((call) => call.method === "lsp.status").length;
    client.emit(event(1, "lsp.diagnostics", { path: "/work/a.py", count: 2, errors: 1 }));
    await sleep(250);
    const after = client.calls.filter((call) => call.method === "lsp.status").length;
    instance.unmount();
    expect(after).toBeGreaterThan(before);
  });
});

describe("/lsp", () => {
  it("prints the table into the transcript", async () => {
    const { stdin, stdout, instance } = await open([READY, BROKEN]);
    stdout.chunks.length = 0;
    await type(stdin, "/lsp", 15);
    stdin.write("\r");
    await sleep(300);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("language servers (1 ready, 1 broken)");
    expect(output).toContain("pyright");
    expect(output).toContain("gopls");
    // It is this surface's own answer, not a turn.
    expect(countOf(output, "language servers (1 ready")).toBeGreaterThan(0);
  });

  it("says why there is nothing to show", async () => {
    const { stdin, stdout, instance } = await open([]);
    stdout.chunks.length = 0;
    await type(stdin, "/lsp", 15);
    stdin.write("\r");
    await sleep(300);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("no language servers are running");
  });
});

describe("the diff badge", () => {
  it("appears on the file a language server complained about", async () => {
    const { client, stdin, stdout, instance } = await open([READY]);
    // A diff arriving mid-turn stays in the live region, which is what lets a
    // badge land on it a moment later.
    await type(stdin, "edit it", 15);
    stdin.write("\r");
    await sleep(150);
    client.emit(
      event(1, "diff", { path: "/work/a.py", patch: "--- a/a.py\n+++ b/a.py\n@@\n-x\n+y" }),
    );
    await sleep(150);
    stdout.chunks.length = 0;
    client.emit(
      event(2, "lsp.diagnostics", { path: "/work/a.py", count: 2, errors: 1, warnings: 1 }),
    );
    await sleep(250);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("Edited /work/a.py");
    expect(output).toContain("⚠ 2");
  });
});
