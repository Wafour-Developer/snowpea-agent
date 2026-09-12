/**
 * The agent panel: which rows it shows, how it collapses, where the columns
 * land.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import {
  AGENT_GLYPH,
  CURRENT_GLYPH,
  MAX_AGENT_ROWS,
  MAX_IDLE_ROWS,
  agentStatusText,
  buildAgentRows,
  cells,
  layoutAgentRow,
  type KnownAgent,
} from "../src/layout/agents.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";

const NOW = 1_000_000;

function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
  return { sessionId: "sess-1", seq, kind, payload };
}

function apply(state: State, ...events: SessionEvent[]): State {
  return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
}

const known = (...names: string[]): KnownAgent[] =>
  names.map((name) => ({ name, kind: "definition", description: `${name} does things` }));

beforeEach(() => {
  __resetIdCounter();
});

describe("buildAgentRows", () => {
  it("puts the current session first", () => {
    const rows = buildAgentRows({ state: initialState, now: NOW });
    expect(rows[0]).toMatchObject({ key: "current", glyph: CURRENT_GLYPH, name: "main" });
  });

  it("lists every delegate by name with its task and status", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", {
        agentId: "a1",
        name: "executor",
        task: "add failing test",
        status: "running",
        at: NOW - 12_000,
      }),
      event(2, "subagent.spawn", {
        agentId: "a2",
        name: "reviewer",
        task: "review the diff",
        status: "queued",
        at: NOW,
      }),
    );
    const rows = buildAgentRows({ state, now: NOW });
    expect(rows[1]).toMatchObject({ name: "executor", task: "add failing test" });
    expect(rows[1].status).toBe("running · 12s");
    expect(rows[2]).toMatchObject({ name: "reviewer", status: "queued" });
  });

  it("reports the time and the tokens a finished delegate spent", () => {
    expect(
      agentStatusText(
        { status: "running", startedAt: NOW - 520_000, endedAt: null, outputTokens: 316_600 },
        NOW,
      ),
    ).toBe("running · 8m 40s · ↓ 316.6k tokens");
    expect(
      agentStatusText(
        { status: "done", startedAt: NOW - 8000, endedAt: NOW, outputTokens: 0 },
        NOW,
      ),
    ).toBe("done · 8s");
  });

  it("counts idle agents past the third instead of listing them", () => {
    const rows = buildAgentRows({
      state: initialState,
      known: known("one", "two", "three", "four", "five"),
      now: NOW,
    });
    const collapsed = rows.find((row) => row.key === "idle-more");
    expect(rows.filter((row) => row.status === "idle")).toHaveLength(MAX_IDLE_ROWS);
    expect(collapsed?.name).toBe("2 idle agents");
    expect(collapsed?.glyph).toBe(AGENT_GLYPH);
  });

  it("lists every idle agent once the panel is opened out", () => {
    const rows = buildAgentRows({
      state: initialState,
      known: known("one", "two", "three", "four", "five"),
      now: NOW,
      expanded: true,
    });
    expect(rows.filter((row) => row.status === "idle")).toHaveLength(5);
    expect(rows.some((row) => row.key === "idle-more")).toBe(false);
  });

  it("turns everything past six rows into an overflow line", () => {
    let state = initialState;
    for (let index = 0; index < 10; index += 1) {
      state = apply(
        state,
        event(index + 1, "subagent.spawn", {
          agentId: `a${index}`,
          name: `agent-${index}`,
          task: "work",
          status: "running",
          at: NOW,
        }),
      );
    }
    const rows = buildAgentRows({ state, now: NOW });
    expect(rows).toHaveLength(MAX_AGENT_ROWS + 1);
    expect(rows[rows.length - 1]).toMatchObject({ key: "overflow", name: "5 more" });
  });

  it("shows team tasks as rows of their own", () => {
    const state = apply(
      initialState,
      event(1, "team.task.update", {
        taskId: "3",
        teamId: "team-2",
        status: "claimed",
        assignee: "team-2",
      }),
    );
    const rows = buildAgentRows({ state, now: NOW });
    expect(rows[1]).toMatchObject({ name: "team-2", task: "task 3", status: "claimed" });
  });
});

describe("layoutAgentRow", () => {
  const row = {
    key: "a",
    glyph: AGENT_GLYPH,
    name: "reviewer",
    task: "Review the diff for the inline layout work",
    status: "running · 8m 40s",
    dim: false,
  };

  it("right-aligns the status and gives the task what is left", () => {
    const line = layoutAgentRow(row, 60);
    expect(cells(line.left) + cells(line.task) + line.gap.length + cells(line.status)).toBe(60);
    expect(line.status).toBe("running · 8m 40s");
  });

  it("cuts the task rather than the status", () => {
    const line = layoutAgentRow(row, 40);
    expect(line.status).toBe("running · 8m 40s");
    expect(line.task.endsWith("…")).toBe(true);
  });

  it("starts every task in the same column, wide glyphs included", () => {
    const short = layoutAgentRow({ ...row, name: "a" }, 60);
    const long = layoutAgentRow({ ...row, name: "architect" }, 60);
    const wide = layoutAgentRow({ ...row, glyph: "⏳", name: "architect" }, 60);
    expect(cells(short.left)).toBe(cells(long.left));
    expect(cells(wide.left)).toBe(cells(long.left));
  });
});
