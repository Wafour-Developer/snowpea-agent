/**
 * Resuming a long session: the replay must land at once, not re-type itself.
 *
 * `session.resume` answers with every event the session ever emitted, and a
 * fresh TUI asks from seq 0. Applied one event at a time that is thousands of
 * reducer passes and thousands of Ink frames, each promoting another finished
 * entry into `<Static>` — so the old transcript appears to be typed back out,
 * slowly, which is what the user reported.
 *
 * The replay is folded in a single reducer pass instead, so the whole
 * reconstructed transcript reaches the scrollback in one render.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import { __resetIdCounter, initialState, reducer } from "../src/state/store.js";
import type { State } from "../src/state/store.js";
import { countOf, fakeStdin, fakeStdout, sleep } from "./tty.js";

const OLD = "sess-old";

/** A saved session: 50 tool calls with diffs, and 300 lines of prose. */
function savedEvents(): SessionEvent[] {
  const events: SessionEvent[] = [];
  let seq = 0;
  const push = (kind: string, payload: Record<string, unknown>): void => {
    seq += 1;
    events.push({ sessionId: OLD, seq, kind, payload } as SessionEvent);
  };
  const patch = Array.from({ length: 10 }, (_, i) => `+  line ${i}`).join("\n");

  for (let round = 0; round < 50; round += 1) {
    // Prose, streamed a token at a time the way it originally arrived.
    for (let i = 0; i < 6; i += 1) {
      push("message.reasoning", { text: "thinking", chars: (round * 6 + i + 1) * 8 });
    }
    for (let i = 0; i < 6; i += 1) {
      push("message.delta", { text: `round ${round} line ${i} of the explanation\n` });
    }
    push("message.done", {
      role: "assistant",
      text: Array.from({ length: 6 }, (_, i) => `round ${round} line ${i} of the explanation`)
        .join("\n"),
    });
    const callId = `c-${round}`;
    push("tool.call", { callId, name: "write_file", args: { path: `m${round}.js` } });
    push("tool.result", { callId, ok: true, output: "written" });
    push("diff", { path: `m${round}.js`, patch });
    push("usage", { inputTokens: 500, outputTokens: 40 });
  }
  push("turn.done", { turnId: "t-old", reason: "complete" });
  return events;
}

const SAVED = savedEvents();

function fakeClient() {
  let onSessionEvent: ((event: SessionEvent) => void) | undefined;
  const calls: string[] = [];
  return {
    calls,
    getStatus: () => "connected",
    setListeners: (l: any) => {
      onSessionEvent = l.onSessionEvent;
    },
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string, params: any) => {
      calls.push(method);
      if (method === "session.resume") {
        return { sessionId: params.sessionId, events: SAVED };
      }
      if (method === "system.info") return { pid: 1, lifecycle: { summary: "idle" } };
      if (method === "session.list") {
        return { sessions: [{ sessionId: OLD, workdir: "/tmp/project", at: Date.now() }] };
      }
      return { commands: [], tools: [], agents: [] };
    },
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: SessionEvent) {
      onSessionEvent?.(event);
    },
  };
}

const live = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent =>
  ({ sessionId: OLD, seq, kind, payload }) as SessionEvent;

describe("resuming a long saved session", () => {
  it("lands the whole transcript at once instead of re-typing it", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdout.chunks.length = 0;

    // `/resume <id>` takes this same path.
    const started = Date.now();
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    // How long until the newest line of the old transcript is on the terminal —
    // that is "the session is back", as opposed to the fixed settle below.
    let visible = 0;
    for (let waited = 0; waited < 2000 && visible === 0; waited += 20) {
      if (stdout.text().includes("round 49 line 5 of the explanation")) visible = Date.now();
      else await sleep(20);
    }
    await sleep(400);
    const elapsed = Date.now() - started;

    const bytes = stdout.chunks.reduce((n, c) => n + c.length, 0);
    const text = stdout.text();
    console.log(
      "RESUME " + JSON.stringify({
        settleMs: elapsed,
        visibleMs: visible - started,
        writes: stdout.chunks.length,
        bytes,
        events: SAVED.length,
      }),
    );

    instance.unmount();
    stdin.end();

    // The transcript is there, and each line exactly once.
    expect(text).toContain("round 0 line 0 of the explanation");
    expect(text).toContain("round 49 line 5 of the explanation");
    expect(countOf(text, "round 25 line 3 of the explanation")).toBe(1);
    // One write per replayed event is what the user saw as re-typing.
    expect(stdout.chunks.length).toBeLessThan(40);
  }, 40000);

  it("keeps streaming live after the replay tail", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    await sleep(700);
    stdout.chunks.length = 0;

    // The session was still running: the tail arrives live, after the replay.
    const base = SAVED.length;
    client.emit(live(base + 1, "message.delta", { text: "STILL-ALIVE-ONE " }));
    await sleep(120);
    client.emit(live(base + 2, "message.delta", { text: "STILL-ALIVE-TWO" }));
    client.emit(live(base + 3, "message.done", {
      role: "assistant",
      text: "STILL-ALIVE-ONE STILL-ALIVE-TWO",
    }));
    await sleep(250);

    const text = stdout.text();
    instance.unmount();
    stdin.end();
    expect(text).toContain("STILL-ALIVE-ONE");
    expect(text).toContain("STILL-ALIVE-TWO");
  }, 40000);

  it("drops events the replay already applied", async () => {
    const client = fakeClient();
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    await sleep(700);
    stdout.chunks.length = 0;

    // The daemon re-sends the tail of what it already replayed.
    for (const event of SAVED.slice(-6)) client.emit(event);
    await sleep(250);

    const text = stdout.text();
    instance.unmount();
    stdin.end();
    // Nothing new was drawn for an event already accounted for.
    expect(countOf(text, "round 49 line 5 of the explanation")).toBe(0);
  }, 40000);
});


/**
 * A resumed session must show what was *asked*, not only what was answered.
 *
 * The prompt reaches the history server-side but used to reach no event, so a
 * replay rebuilt a transcript of answers talking to nobody. The daemon now
 * publishes `message.user` where the prompt joins the history, and the `✓ Done`
 * line — which a live turn gets from the surface's own clock — is rebuilt here
 * from the event timestamps.
 */

/** A two-turn session as the daemon would have logged it, timestamps included. */
function conversation(): SessionEvent[] {
  const at = (ms: number): string => new Date(Date.UTC(2026, 0, 1, 0, 0, 0) + ms).toISOString();
  const events: SessionEvent[] = [];
  let seq = 0;
  const push = (kind: string, payload: Record<string, unknown>, ms: number): void => {
    seq += 1;
    events.push({ sessionId: OLD, seq, kind, payload, ts: at(ms) } as SessionEvent);
  };
  push("message.user", { text: "PROMPT-ONE about the parser" }, 0);
  push("message.delta", { text: "ANSWER-ONE" }, 500);
  push("message.done", { role: "assistant", text: "ANSWER-ONE" }, 1000);
  push("turn.done", { turnId: "t-1", reason: "complete" }, 3000);
  push("message.user", { text: "PROMPT-TWO about the lexer" }, 10000);
  push("message.delta", { text: "ANSWER-TWO" }, 10500);
  push("message.done", { role: "assistant", text: "ANSWER-TWO" }, 11000);
  push("turn.done", { turnId: "t-2", reason: "complete" }, 17000);
  return events;
}

/** The transcript as plain text, in timeline order. */
function transcript(state: State): string[] {
  return state.timeline.flatMap((item) => {
    if (item.kind !== "message") return [];
    const message = state.messages.find((m) => m.id === item.id);
    return message ? [`${message.role}: ${message.text}`] : [];
  });
}

describe("a replayed transcript keeps the prompts", () => {
  it("puts every prompt in order, with its answer and its done line", () => {
    __resetIdCounter();
    const state = reducer(initialState, { type: "session/replay", events: conversation() });
    expect(transcript(state)).toEqual([
      "user: PROMPT-ONE about the parser",
      "assistant: ANSWER-ONE",
      "note: ✓ Done in 3s",
      "user: PROMPT-TWO about the lexer",
      "assistant: ANSWER-TWO",
      "note: ✓ Done in 7s",
    ]);
  });

  it("shows the prompt's attachments under it", () => {
    __resetIdCounter();
    const events = [
      {
        sessionId: OLD,
        seq: 1,
        kind: "message.user",
        payload: { text: "what is in this", attachments: [{ kind: "image", name: "shot.png" }] },
        ts: new Date(0).toISOString(),
      },
    ] as SessionEvent[];
    const state = reducer(initialState, { type: "session/replay", events });
    expect(state.messages[0].attachments).toEqual([{ name: "shot.png" }]);
  });

  it("says only ✓ Done when the log carries no timestamps", () => {
    __resetIdCounter();
    const events = conversation().map((event) => ({ ...event, ts: undefined })) as SessionEvent[];
    const state = reducer(initialState, { type: "session/replay", events });
    expect(transcript(state).filter((line) => line.startsWith("note:")))
      .toEqual(["note: ✓ Done", "note: ✓ Done"]);
  });

  it("marks an interrupted turn as stopped", () => {
    __resetIdCounter();
    const events = conversation()
      .slice(0, 4)
      .map((event) =>
        event.kind === "turn.done"
          ? ({ ...event, payload: { turnId: "t-1", reason: "interrupted" } } as SessionEvent)
          : event,
      );
    const state = reducer(initialState, { type: "session/replay", events });
    expect(transcript(state).at(-1)).toBe("note: ✗ Stopped after 3s");
  });

  it("does not show a live prompt twice", () => {
    __resetIdCounter();
    // The surface draws the prompt the moment it is sent — that is what makes
    // typing feel immediate, and a queued prompt has nothing else for the
    // minutes it waits — so the daemon's copy is swallowed.
    let state = reducer(initialState, { type: "user/message", text: "LIVE-PROMPT" });
    state = reducer(state, {
      type: "session/event",
      event: {
        sessionId: OLD,
        seq: 1,
        kind: "message.user",
        payload: { text: "LIVE-PROMPT" },
      } as SessionEvent,
    });
    expect(transcript(state)).toEqual(["user: LIVE-PROMPT"]);
    expect(state.pendingEchoes).toEqual([]);

    // A prompt this surface did not send is still drawn: another client, or the
    // same session picked up somewhere else.
    const elsewhere = reducer(state, {
      type: "session/event",
      event: {
        sessionId: OLD,
        seq: 2,
        kind: "message.user",
        payload: { text: "FROM-ANOTHER-CLIENT" },
      } as SessionEvent,
    });
    expect(transcript(elsewhere)).toEqual(["user: LIVE-PROMPT", "user: FROM-ANOTHER-CLIENT"]);
  });

  it("keeps no echo for a slash command, which is never published", () => {
    __resetIdCounter();
    const state = reducer(initialState, {
      type: "user/message",
      text: "/help",
      expectEvent: false,
    });
    expect(state.pendingEchoes).toEqual([]);
  });
});

describe("resuming draws the prompts on the terminal", () => {
  it("shows both questions and both answers in order", async () => {
    const events = conversation();
    const client = {
      getStatus: () => "connected",
      setListeners: () => undefined,
      onApprovalRequest: () => undefined,
      onQuestionRequest: () => undefined,
      listApprovals: async () => ({ requests: [] }),
      checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
      call: async (method: string, params: any) => {
        if (method === "session.resume") return { sessionId: params.sessionId, events };
        if (method === "system.info") return { pid: 1, lifecycle: { summary: "idle" } };
        if (method === "session.list") {
          return { sessions: [{ sessionId: OLD, workdir: "/tmp/project", at: Date.now() }] };
        }
        return { commands: [], tools: [], agents: [] };
      },
      prompt: async () => ({ turnId: "t" }),
      interrupt: async () => undefined,
      setMode: async () => ({ mode: "accept" }),
    };
    const stdin = fakeStdin();
    const stdout = fakeStdout(120, 35);
    const instance = render(
      <App client={client as any} sessionId="sess-new" mode="accept" workdir="/tmp/project" />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(200);
    stdout.chunks.length = 0;
    stdin.write(`/resume ${OLD}`);
    await sleep(120);
    stdin.write("\r");
    await sleep(700);

    const text = stdout.text();
    instance.unmount();
    stdin.end();

    const order = ["PROMPT-ONE", "ANSWER-ONE", "PROMPT-TWO", "ANSWER-TWO"].map((needle) =>
      text.indexOf(needle),
    );
    expect(order.every((index) => index >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(countOf(text, "PROMPT-ONE about the parser")).toBe(1);
    expect(text).toContain("✓ Done in 3s");
    expect(text).toContain("✓ Done in 7s");
  }, 40000);
});
