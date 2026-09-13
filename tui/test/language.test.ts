/**
 * The harness's own wording follows the user's language.
 *
 * Two halves: which language is in force (`layout/language.ts`), and the verbs
 * the summary line is written with (`layout/summary.ts`). A delegation is the
 * one row the model labels itself, so its title wins over both.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  asUiLanguage,
  detectLanguage,
  resetUiLanguage,
  setUiLanguage,
  uiLanguage,
} from "../src/layout/language.js";
import { summarizeCalls, toolKind } from "../src/layout/summary.js";
import { __resetIdCounter, initialState, reducer, type State } from "../src/state/store.js";
import { buildAgentRows } from "../src/layout/agents.js";
import type { SessionEvent } from "../src/rpc/sdk.js";
import type { ToolCallEntry } from "../src/state/store.js";

function call(name: string, args: Record<string, unknown> = {}): ToolCallEntry {
  return { callId: "c1", name, args, state: "ok" };
}

afterEach(() => {
  resetUiLanguage();
  __resetIdCounter();
});

describe("uiLanguage", () => {
  it("is English until something says otherwise", () => {
    expect(uiLanguage()).toBe("en");
  });

  it("normalises tags and refuses the ones it has no catalog for", () => {
    expect(asUiLanguage("ko-KR")).toBe("ko");
    expect(asUiLanguage("JA")).toBe("ja");
    expect(asUiLanguage("zh-Hans")).toBe("zh");
    expect(asUiLanguage("ru")).toBe("en");
    expect(asUiLanguage(null)).toBe("en");
    expect(setUiLanguage("ko")).toBe("ko");
    expect(uiLanguage()).toBe("ko");
  });
});

describe("detectLanguage", () => {
  it("reads the script, not the vocabulary", () => {
    expect(detectLanguage("요약이 읽기 어려워")).toBe("ko");
    expect(detectLanguage("ログを読みやすくして")).toBe("ja");
    expect(detectLanguage("请重构这个文件")).toBe("zh");
    expect(detectLanguage("make the summaries readable")).toBe("en");
    expect(detectLanguage("")).toBe("en");
    expect(detectLanguage("delegate_task 의 summary 가 읽기 어려워")).toBe("ko");
  });
});

describe("summarizeCalls in another language", () => {
  it("uses the Korean verbs when the session is Korean", () => {
    setUiLanguage("ko");
    expect(summarizeCalls([call("read_file", { path: "/src/app.py" })])).toBe("app.py 읽음");
    expect(summarizeCalls([call("edit_file", { file_path: "/repo/README.md" })])).toBe(
      "README.md 수정",
    );
    expect(summarizeCalls([call("bash", { command: "npm test" })])).toBe("셸 실행: npm test");
    expect(summarizeCalls([call("grep", { pattern: "TODO" })])).toBe('"TODO" 검색');
    expect(
      summarizeCalls([
        { callId: "a", name: "read_file", args: { path: "a.py" }, state: "ok" },
        { callId: "b", name: "read_file", args: { path: "b.py" }, state: "ok" },
      ]),
    ).toBe("파일 2개 읽음");
  });

  it("takes an explicit language over the module setting", () => {
    setUiLanguage("ko");
    expect(summarizeCalls([call("read_file", { path: "a.py" })], "en")).toBe("Read a.py");
    expect(summarizeCalls([call("read_file", { path: "a.py" })], "ja")).toBe("a.py を読み込み");
    expect(summarizeCalls([call("read_file", { path: "a.py" })], "zh")).toBe("读取 a.py");
  });

  it("leaves English sessions exactly as they were", () => {
    expect(summarizeCalls([call("write", { path: "notes.txt" })])).toBe("Wrote notes.txt");
  });
});

describe("delegate rows", () => {
  it("is its own tool family", () => {
    expect(toolKind("delegate_task")).toBe("delegate");
  });

  it("shows the title the model wrote", () => {
    setUiLanguage("ko");
    expect(
      summarizeCalls([
        call("delegate_task", { task: "Investigate the summary rendering", title: "요약 표시 조사" }),
      ]),
    ).toBe("요약 표시 조사");
  });

  it("falls back to the verb and the agent name without one", () => {
    expect(summarizeCalls([call("delegate_task", { task: "do a thing", agent: "explorer" })])).toBe(
      "Delegated: explorer",
    );
    setUiLanguage("ko");
    expect(summarizeCalls([call("delegate_task", { task: "do a thing" })])).toBe("작업 위임");
  });
});

describe("the agent panel", () => {
  function event(seq: number, kind: string, payload: Record<string, unknown>): SessionEvent {
    return { sessionId: "sess-1", seq, kind, payload };
  }

  function apply(state: State, ...events: SessionEvent[]): State {
    return events.reduce((acc, e) => reducer(acc, { type: "session/event", event: e }), state);
  }

  it("prefers the title over the brief", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", {
        agentId: "a-1",
        name: "explorer",
        task: "Investigate how the summary line is rendered, in English, at length",
        title: "요약 표시 조사",
        status: "running",
      }),
    );
    const row = buildAgentRows({ state, now: 0 }).find((r) => r.key === "agent-a-1");
    expect(row?.task).toBe("요약 표시 조사");
  });

  it("keeps the brief when no title was written", () => {
    const state = apply(
      initialState,
      event(1, "subagent.spawn", { agentId: "a-2", name: "explorer", task: "look into it" }),
    );
    const row = buildAgentRows({ state, now: 0 }).find((r) => r.key === "agent-a-2");
    expect(row?.task).toBe("look into it");
  });
});
