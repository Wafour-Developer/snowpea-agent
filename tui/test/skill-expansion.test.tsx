import React from "react";
import { render } from "ink";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { MessageView } from "../src/components/MessageStream.js";
import { entryRows } from "../src/layout/statics.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const SKILL_BODY = "Follow these instructions for /ralph\n\nDo the thing.\nAnd another line.";

beforeEach(() => {
  __resetIdCounter();
});

describe("skill expansion in the reducer", () => {
  it("attaches expansion to the local echo instead of dropping it", () => {
    let state = reducer(initialState, { type: "user/message", text: "/ralph hello" });
    state = apply(
      state,
      event(1, "message.user", {
        text: "/ralph hello",
        expansion: { kind: "skill", name: "ralph", text: SKILL_BODY },
      }),
    );

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe("/ralph hello");
    expect(state.messages[0].expansion).toEqual({
      kind: "skill",
      name: "ralph",
      chars: SKILL_BODY.length,
      lines: SKILL_BODY.split("\n").length,
    });
    expect(state.pendingEchoes).toEqual([]);
  });

  it("attaches expansion to a slash command drawn without a pending echo", () => {
    let state = reducer(initialState, {
      type: "user/message",
      text: "/ralph hello",
      expectEvent: false,
    });
    state = apply(
      state,
      event(1, "message.user", {
        text: "/ralph hello",
        expansion: { kind: "skill", name: "ralph", text: SKILL_BODY },
      }),
    );

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].expansion?.name).toBe("ralph");
    expect(state.pendingEchoes).toEqual([]);
  });

  it("creates a message with expansion when there is no echo", () => {
    const state = apply(
      initialState,
      event(1, "message.user", {
        text: "/ralph hello",
        expansion: { kind: "skill", name: "ralph", text: SKILL_BODY },
      }),
    );

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].expansion?.name).toBe("ralph");
    expect(state.pendingEchoes).toEqual([]);
  });

  it("leaves old daemon payloads unchanged", () => {
    let state = reducer(initialState, { type: "user/message", text: SKILL_BODY });
    state = apply(state, event(1, "message.user", { text: SKILL_BODY }));

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe(SKILL_BODY);
    expect(state.messages[0].expansion).toBeUndefined();
  });

  it("replays session.resume the same way", () => {
    const events = [
      event(1, "message.user", {
        text: "/ralph hello",
        expansion: { kind: "skill", name: "ralph", text: SKILL_BODY },
      }),
      event(2, "message.done", { role: "assistant", text: "ok" }),
    ];
    const state = reducer(initialState, { type: "session/replay", events });

    expect(state.messages[0].expansion?.lines).toBe(SKILL_BODY.split("\n").length);
    expect(state.messages[0].text).toBe("/ralph hello");
  });
});

describe("skill expansion rendering", () => {
  it("shows the fold line and not the skill body", async () => {
    const lines = SKILL_BODY.split("\n").length;
    const message = {
      id: "m1",
      role: "user" as const,
      text: "/ralph hello",
      streaming: false,
      expansion: { kind: "skill" as const, name: "ralph", chars: SKILL_BODY.length, lines },
    };
    const stdin = fakeStdin();
    const stdout = fakeStdout(40, 80);
    const instance = render(<MessageView message={message} width={40} />, {
      stdin,
      stdout: stdout.stream,
      exitOnCtrlC: false,
      patchConsole: false,
    });
    await sleep(40);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();

    expect(seen).toMatch(/⏺\s+skill ralph · 4 lines/);
    expect(seen).not.toContain("Follow these instructions for /ralph");
  });
});

describe("entryRows", () => {
  it("counts one extra row for a message with expansion", () => {
    const state = apply(
      initialState,
      event(1, "message.user", {
        text: "/ralph hello",
        expansion: { kind: "skill", name: "ralph", text: SKILL_BODY },
      }),
    );
    const item = state.timeline[0];
    expect(entryRows(state, item)).toBe(2);
  });
});

describe("inline App", () => {
  function fakeClient() {
    let onSessionEvent: ((event: SessionEvent) => void) | undefined;
    return {
      getStatus: () => "connected",
      setListeners: (listeners: any) => {
        onSessionEvent = listeners.onSessionEvent;
      },
      onApprovalRequest: () => undefined,
      onQuestionRequest: () => undefined,
      listApprovals: async () => ({ requests: [] }),
      checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
      call: async (method: string) =>
        method === "system.info"
          ? { pid: 4242, lifecycle: { summary: "idle" } }
          : method === "command.list"
            ? { commands: [{ name: "deep-interview", summary: "interview", source: "skill" }] }
            : method === "command.run"
              ? { turnId: "t-1" }
              : {},
      prompt: async () => ({ turnId: "turn-1" }),
      interrupt: async () => undefined,
      setMode: async () => ({ mode: "accept" }),
      emit(ev: SessionEvent) {
        onSessionEvent?.(ev);
      },
    };
  }

  it("shows the fold line after the daemon publishes the skill body", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 40);
    const instance = render(
      <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(120);
    await type(stdin, "/deep-interview --quick test", 10);
    stdin.write("\r");
    await sleep(120);
    client.emit({
      sessionId: "sess-1",
      seq: 500,
      kind: "message.user",
      payload: {
        text: "/deep-interview --quick test",
        expansion: { kind: "skill", name: "deep-interview", text: SKILL_BODY },
      },
    } as SessionEvent);
    await sleep(400);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();

    expect(seen).toContain("› /deep-interview --quick test");
    expect(seen).toMatch(/⏺\s*skill deep-interview · \d+ lines/);
    expect(seen).not.toContain("Follow these instructions for /ralph");
  });
});
