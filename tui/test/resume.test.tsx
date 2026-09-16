/**
 * Walking down into the panel, and opening a delegate's conversation.
 *
 * The drill-down is the part worth pinning: a subagent runs in its own child
 * session, so its messages must reach that agent's transcript and never the
 * main one.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const CHILD = "child-session-1";

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  const calls: Array<{ method: string; params: any }> = [];
  return {
    calls,
    getStatus: () => "connected",
    setListeners: (listeners: any) => {
      onSessionEvent = listeners.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "session.resume") {
        return {
          sessionId: params.sessionId,
          mode: "auto",
          events: [
            {
              sessionId: params.sessionId,
              seq: 1,
              kind: "message.done",
              payload: { role: "assistant", text: "CHILD-ONLY-ANSWER" },
            },
          ],
        };
      }
      if (method === "session.list") {
        return {
          sessions: [{
            sessionId: CHILD,
            workdir: "/tmp/project",
            createdAt: "2026-09-12T10:00:00Z",
            lastPrompt: "fix the failing build",
          }],
        };
      }
      if (method === "system.info") return { pid: 1, lifecycle: { summary: "idle" } };
      return { commands: [], tools: [], agents: [] };
    },
    prompt: async (sessionId: string, text: string) => {
      calls.push({ method: "session.prompt", params: { sessionId, text } });
      return { turnId: "t" };
    },
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}


describe("session resume", () => {
  it("clears a command error as soon as the next input begins", async () => {
    const client = fakeClient();
    const call = client.call;
    client.call = async (method, params) => {
      if (method === "command.run") throw new Error("not_found: unknown command: /nonesuch");
      return call(method, params);
    };
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 24);
    const instance = render(
      <App client={client as any} sessionId="current" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    try {
      await sleep(150);
      stdin.write("/nonesuch ");
      await sleep(50);
      stdin.write("\r");
      await sleep(150);
      expect(stdout.text()).toContain("unknown command: /nonesuch");
      stdin.write("n");
      await sleep(100);
      expect(stdout.chunks.at(-1)).not.toContain("unknown command: /nonesuch");
    } finally { instance.unmount(); }
  });

  it("keeps the current session when resume fails", async () => {
    const client = fakeClient();
    const call = client.call;
    client.call = async (method, params) => {
      if (method === "session.resume") throw new Error("no such session");
      return call(method, params);
    };
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 24);
    const instance = render(
      <App client={client as any} sessionId="current" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    try {
      await sleep(150);
      stdin.write("/resume missing");
      await sleep(60);
      stdin.write("\r");
      await sleep(150);
      expect(stdout.text()).toContain("resume failed");
      stdin.write("continue");
      await sleep(60);
      stdin.write("\r");
      await sleep(150);
      expect(client.calls.find(c => c.method === "session.prompt")?.params.sessionId).toBe("current");
    } finally { instance.unmount(); }
  });
  it.each(["/resume", "/sessions", `/resume ${CHILD}`])("switches subsequent prompts and commands with %s without duplicate replay", async (command) => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(100, 24);
    const instance = render(
      <App client={client as any} sessionId="new-session" mode="accept" workdir="/tmp/project"
        priorSession={{ sessionId: CHILD, workdir: "/tmp/project", firstPrompt: "", at: 1 }} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    try {
      await sleep(150);
      // A bare command name is typed with its accepting space: Enter on a
      // bare `/resume` accepts the completion instead of running it.
      const typed = command.includes(" ") ? command : `${command} `;
      stdin.write(typed);
      await sleep(60);
      stdin.write("\r");
      if (command === "/resume" || command === "/sessions") {
        await sleep(100);
        expect(stdout.text()).toContain(CHILD.slice(0, 8));
        expect(stdout.text()).toContain("fix the failing build");
        stdin.write("\r");
      }
      await sleep(200);
      expect(stdout.text()).toContain("CHILD-ONLY-ANSWER");
      // The HUD adopts the mode the daemon restored with the session.
      expect(stdout.text()).toContain("Mode: AUTO");
      stdin.write(typed);
      await sleep(60);
      stdin.write("\r");
      await sleep(100);
      expect(client.calls.filter(c => c.method === "session.resume")).toHaveLength(1);
      stdin.write("continue");
      await sleep(60);
      stdin.write("\r");
      await sleep(150);
      expect(client.calls.find(c => c.method === "session.prompt")?.params).toEqual({
        sessionId: CHILD, text: "continue",
      });
      stdin.write("/help");
      await sleep(60);
      stdin.write("\r");
      await sleep(150);
      expect(client.calls.find(c => c.method === "command.run")?.params.sessionId).toBe(CHILD);
    } finally { instance.unmount(); }
  });
});
