import { describe, expect, it } from "vitest";

import type { SessionEvent } from "../src/rpc/sdk.js";
import { initialState, reducer } from "../src/state/store.js";

let seq = 0;
function run(events: [string, Record<string, unknown>][]): string[] {
  let state = initialState;
  for (const [kind, payload] of events) {
    seq += 1;
    const event = { sessionId: "s-retry", seq, kind, payload } as SessionEvent;
    state = reducer(state, { type: "session/event", event });
  }
  return state.messages.filter((m) => m.role === "system").map((m) => m.text);
}

const retry = (p: Record<string, unknown>): [string, Record<string, unknown>] => ["provider.retry", p];
const cont = (p: Record<string, unknown>): [string, Record<string, unknown>] => ["hook.continue", p];

describe("provider.retry line", () => {
  it("unreachable uses attempt and minutes left, never N/null", () => {
    const [line] = run([retry({ attempt: 3, max: null, delaySec: 30, error: "ConnectError", reason: "unreachable", waitLeftSec: 540 })]);
    expect(line).toBe("Network or model server unreachable — retrying (attempt 3, up to 9 min left)");
    expect(line).not.toMatch(/null|undefined|\d\/\d/);
  });
  it("silent and transient show N/MAX in Ds", () => {
    expect(run([retry({ attempt: 2, max: 7, delaySec: 30, error: "x", reason: "silent", waitLeftSec: null })])[0]).toBe(
      "Model server is silent — retry 2/7 in 30s",
    );
    expect(run([retry({ attempt: 1, max: 7, delaySec: 5, error: "ReadTimeout", reason: "transient" })])[0]).toBe(
      "Model server error (ReadTimeout) — retry 1/7 in 5s",
    );
  });
  it("updates one line in place over several retries", () => {
    const lines = run([1, 2, 3].map((n) => retry({ attempt: n, max: 7, delaySec: 10, error: "E", reason: "transient" })));
    expect(lines).toEqual(["Model server error (E) — retry 3/7 in 10s"]);
  });
  it("no reason (old daemon): no line", () => {
    expect(run([retry({ attempt: 1, max: 7, delaySec: 5, error: "E" })])).toEqual([]);
  });
});

describe("hook.continue line", () => {
  it("provider_retry adds nothing after provider.retry", () => {
    const lines = run([
      retry({ attempt: 1, max: 7, delaySec: 5, error: "E", reason: "transient" }),
      cont({ hookKind: "provider_retry", count: 1, limit: 7, reason: "The model server did not answer; retrying 1/7 in 5s." }),
    ]);
    expect(lines).toHaveLength(1);
  });
  it("labels per kind", () => {
    const l = (hookKind: string, count: number | null, limit: number | null) =>
      run([cont({ hookKind, count, limit, reason: "why" })])[0];
    expect(l("unfinished_work", 1, 2)).toBe("↻ continuing (unfinished work 1/2): why");
    expect(l("verify_gate", 1, 3)).toBe("↻ continuing (checks 1/3): why");
    expect(l("overflow", 1, 1)).toBe("↻ continuing (context trimmed)");
    expect(l("other", null, null)).toBe("↻ continuing: why");
    expect(l("budget", 2, null)).toBe("↻ continuing (budget 2): why");
  });
  it("missing kind keeps the plain text", () => {
    expect(run([cont({ reason: "keep going", count: 0 })])).toEqual(["keep going"]);
  });
});
