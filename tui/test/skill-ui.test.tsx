/**
 * `/skill` in front of a person: the sub-action palette, the three-question
 * form behind a bare `/skill create`, the chip after the daemon answers, and
 * `/skill edit` handing the terminal to an editor.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const COMMANDS = [
  { name: "help", summary: "list commands", source: "builtin" },
  { name: "skill", summary: "Turn this session into a reusable skill.", source: "builtin" },
];

function fakeClient(overrides: Record<string, any> = {}) {
  let listeners: any;
  const calls: Array<{ method: string; params: any }> = [];
  const prompts: string[] = [];
  return {
    calls,
    prompts,
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
      if (method === "command.list") return { commands: COMMANDS };
      if (method in overrides) return overrides[method];
      return {};
    },
    prompt: async (_session: string, text: string) => {
      prompts.push(text);
      return { turnId: "t-1" };
    },
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

async function open(client: ReturnType<typeof fakeClient>, editor?: any) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(120, 30);
  const instance = render(
    <App
      client={client as any}
      sessionId="sess-1"
      mode="accept"
      workdir="/work"
      editor={editor}
    />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(300);
  return { stdin, stdout, instance, frame: () => stdout.chunks[stdout.chunks.length - 1] ?? "" };
}

describe("the /skill palette", () => {
  it("offers the sub-actions once /skill is typed", async () => {
    const { stdin, instance, frame } = await open(fakeClient());
    await type(stdin, "/skill ", 15);
    await sleep(200);
    const shown = frame();
    instance.unmount();

    expect(shown).toContain("/skill create");
    expect(shown).toContain("/skill learn");
    expect(shown).toContain("/skill publish");
  });

  it("narrows to the one being typed", async () => {
    const { stdin, instance, frame } = await open(fakeClient());
    await type(stdin, "/skill cr", 15);
    await sleep(200);
    const shown = frame();
    instance.unmount();

    expect(shown).toContain("/skill create");
    expect(shown).not.toContain("/skill publish");
  });
});

describe("the /skill create form", () => {
  it("asks name, description and scope, then submits the command", async () => {
    const client = fakeClient();
    const { stdin, instance, frame } = await open(client);

    // Enter on `/skill create` completes rather than running: type past it.
    await type(stdin, "/skill create ", 12);
    stdin.write("\r");
    await sleep(250);
    expect(frame()).toContain("New skill");

    await type(stdin, "deploy", 12);
    stdin.write("\r");
    await sleep(200);
    await type(stdin, "ship the site", 12);
    stdin.write("\r");
    await sleep(200);
    const scopes = frame();
    expect(scopes).toContain("this project");
    expect(scopes).toContain("everywhere");

    stdin.write("\r");
    await sleep(250);
    const after = frame();
    instance.unmount();

    expect(client.prompts).toEqual(['/skill create deploy "ship the site"']);
    expect(after).not.toContain("New skill");
  });

  it("takes --global from the second row", async () => {
    const client = fakeClient();
    const { stdin, instance } = await open(client);
    await type(stdin, "/skill create ", 12);
    stdin.write("\r");
    await sleep(250);
    await type(stdin, "deploy", 12);
    stdin.write("\r");
    await sleep(150);
    await type(stdin, "ship it", 12);
    stdin.write("\r");
    await sleep(150);
    stdin.write("[B"); // ↓ to "everywhere"
    await sleep(120);
    stdin.write("\r");
    await sleep(250);
    instance.unmount();

    expect(client.prompts).toEqual(['/skill create deploy "ship it" --global']);
  });

  it("refuses a name that is not a command name", async () => {
    const client = fakeClient();
    const { stdin, instance, frame } = await open(client);
    await type(stdin, "/skill create ", 12);
    stdin.write("\r");
    await sleep(250);
    await type(stdin, "not a name", 12);
    stdin.write("\r");
    await sleep(200);
    const shown = frame();
    instance.unmount();

    expect(shown).toContain("a skill name is a command name");
    expect(client.prompts).toEqual([]);
  });

  it("closes on Esc without sending anything", async () => {
    const client = fakeClient();
    const { stdin, instance, frame } = await open(client);
    await type(stdin, "/skill create ", 12);
    stdin.write("\r");
    await sleep(250);
    stdin.write("");
    await sleep(250);
    const shown = frame();
    instance.unmount();

    expect(shown).not.toContain("New skill");
    expect(client.prompts).toEqual([]);
  });
});

describe("after the daemon writes the skill", () => {
  it("shows the chip and re-reads the command table", async () => {
    const client = fakeClient();
    const { instance, frame } = await open(client);
    const before = client.calls.filter((call) => call.method === "command.list").length;

    const reply =
      "Created skill 'deploy' at /work/.snowpea/skills/deploy/SKILL.md. Run it with /deploy.";
    client.emit({ sessionId: "sess-1", seq: 1, kind: "message.delta", payload: { text: reply } } as SessionEvent);
    client.emit({ sessionId: "sess-1", seq: 2, kind: "message.done", payload: { text: reply } } as SessionEvent);
    await sleep(300);
    const shown = frame();
    const after = client.calls.filter((call) => call.method === "command.list").length;
    instance.unmount();

    expect(shown).toContain("run /deploy");
    expect(after).toBeGreaterThan(before);
  });
});

describe("/skill edit", () => {
  it("opens the file the daemon names and reloads afterwards", async () => {
    const client = fakeClient({ "skill.read": { path: "/work/.snowpea/skills/deploy/SKILL.md" } });
    const run = vi.fn(async () => 0);
    const { stdin, instance } = await open(client, { run, command: () => "vi" });

    await type(stdin, "/skill edit deploy", 8);
    stdin.write("\r");
    await sleep(400);
    instance.unmount();

    expect(run).toHaveBeenCalledWith("/work/.snowpea/skills/deploy/SKILL.md");
    const methods = client.calls.map((call) => call.method);
    expect(methods).toContain("skill.read");
    expect(methods).toContain("skill.reload");
  });

  it("says so when the daemon cannot place the skill", async () => {
    const client = fakeClient({ "skill.read": {} });
    const run = vi.fn(async () => 0);
    const { stdin, stdout, instance } = await open(client, { run, command: () => "vi" });

    await type(stdin, "/skill edit ghost", 8);
    stdin.write("\r");
    await sleep(350);
    const output = stdout.text();
    instance.unmount();

    expect(run).not.toHaveBeenCalled();
    expect(output).toContain("did not say where ghost is kept");
  });
});
