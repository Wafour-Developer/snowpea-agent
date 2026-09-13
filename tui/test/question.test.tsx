/**
 * The `ask_user` picker, driven the way a person drives it.
 *
 * The point of the tool is that the user never has to type a letter to answer
 * a closed question, and never has to wonder whether picking already committed
 * them, so every case here goes through the real `App`, a real Ink render and
 * real keystrokes, and checks what the daemon would be told.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { QuestionRequestParams, QuestionResponse } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const DOWN = "\u001b[B";
const RIGHT = "\u001b[C";
const LEFT = "\u001b[D";
const ESC = "\u001b";
const ENTER = "\r";

const RENDERER = {
  header: "렌더러",
  question: "렌더링을 무엇으로 할까요? 여기서 고른 것이 엔진 통제력을 정합니다.",
  options: [
    { label: "three.js (추천)", description: "빠른 시작, 엔진 통제력 낮음" },
    { label: "Raw WebGL2", description: "공수 큼, 완전한 통제" },
  ],
  allowOther: true,
};

const STORAGE = {
  header: "저장",
  question: "세계를 어디에 저장할까요?",
  options: [{ label: "IndexedDB" }, { label: "서버" }],
  allowOther: true,
};

const ONE: QuestionRequestParams = {
  requestId: "qu-1",
  sessionId: "sess-1",
  questions: [RENDERER],
};

const TWO: QuestionRequestParams = {
  requestId: "qu-2",
  sessionId: "sess-1",
  questions: [RENDERER, STORAGE],
};

/** A client that hands the test the question handler `App` installs. */
function fakeClient() {
  let onQuestion: ((request: QuestionRequestParams) => Promise<QuestionResponse>) | undefined;
  return {
    getStatus: () => "connected",
    setListeners: () => undefined,
    onApprovalRequest: () => undefined,
    onQuestionRequest: (handler: any) => {
      onQuestion = handler;
    },
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    ask(request: QuestionRequestParams = ONE): Promise<QuestionResponse> {
      return onQuestion!(request);
    },
  };
}

async function openPicker(request: QuestionRequestParams = ONE) {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 30);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(120);
  const answer = client.ask(request);
  await sleep(120);
  return { client, stdin, stdout, instance, answer };
}

/** Send keys one at a time, the way a person does. */
async function press(stdin: any, ...keys: string[]): Promise<void> {
  for (const key of keys) {
    stdin.write(key);
    await sleep(50);
  }
}

/** True once the promise has settled; used to prove a keystroke did NOT submit. */
function watch(promise: Promise<unknown>): () => boolean {
  let settled = false;
  void promise.then(
    () => {
      settled = true;
    },
    () => {
      settled = true;
    },
  );
  return () => settled;
}

describe("question picker", () => {
  it("shows the header, the question, every option and a confirm row", async () => {
    const { stdout, stdin, instance, answer } = await openPicker();
    const output = stdout.text();
    await press(stdin, ESC);
    await answer;
    instance.unmount();

    expect(output).toContain("렌더러");
    expect(output).toContain("three.js");
    expect(output).toContain("Raw WebGL2");
    expect(output).toContain("공수 큼, 완전한 통제");
    expect(output).toContain("기타 / Other");
    expect(output).toContain("확인 / Confirm");
    expect(output).toContain("Enter 확인");
  });

  it("needs the confirm row: Enter on an option marks it and does not submit", async () => {
    const { stdin, stdout, instance, answer } = await openPicker();
    const done = watch(answer);
    await press(stdin, ENTER);
    expect(stdout.text()).toContain("● three.js (추천)");
    expect(done()).toBe(false);

    await press(stdin, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([{ selected: ["three.js (추천)"], text: null }]);
  });

  it("a number key marks the row but never submits on its own", async () => {
    const { stdin, stdout, instance, answer } = await openPicker();
    const done = watch(answer);
    await press(stdin, "2");
    expect(stdout.text()).toContain("● Raw WebGL2");
    expect(done()).toBe(false);

    await press(stdin, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([{ selected: ["Raw WebGL2"], text: null }]);
  });

  it("moves the cursor with the arrows and with j/k", async () => {
    const { stdin, instance, answer } = await openPicker();
    await press(stdin, DOWN, "k", "j", ENTER, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers?.[0].selected).toEqual(["Raw WebGL2"]);
  });

  it("toggles with Space and submits the set from the confirm row", async () => {
    const { stdin, instance, answer } = await openPicker({
      ...ONE,
      questions: [{ ...RENDERER, multi: true }],
    });
    // Space ticks the first, down + Space ticks the second, then down to Other
    // and down again to Confirm.
    await press(stdin, " ", DOWN, " ", DOWN, DOWN, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([{ selected: ["three.js (추천)", "Raw WebGL2"], text: null }]);
  });

  it("opens a text line on the Other row and keeps what was typed", async () => {
    const { stdin, stdout, instance, answer } = await openPicker();
    await press(stdin, DOWN, DOWN, ENTER);
    expect(stdout.text()).toContain("Enter to keep");
    await press(stdin, "babylon.js");
    await press(stdin, ENTER);
    expect(stdout.text()).toContain("● babylon.js");
    await press(stdin, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([{ selected: [], text: "babylon.js" }]);
  });

  it("reports Esc as an empty answer set, so the tool can say declined", async () => {
    const { stdin, instance, answer } = await openPicker();
    await press(stdin, ESC);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([]);
  });

  it("asks free text when the question carries no options", async () => {
    const { stdin, instance, answer } = await openPicker({
      requestId: "qu-3",
      sessionId: "sess-1",
      questions: [{ question: "무엇을 먼저 만들까요?", options: [], allowOther: true }],
    });
    await press(stdin, ENTER);
    await press(stdin, "지형 생성기");
    await press(stdin, ENTER, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([{ selected: [], text: "지형 생성기" }]);
  });

  it("draws no tab strip for a single question", async () => {
    const { stdin, stdout, instance, answer } = await openPicker();
    const output = stdout.text();
    await press(stdin, ESC);
    await answer;
    instance.unmount();
    expect(output).not.toContain("다음 질문");
    expect(output).toContain("확인 / Confirm");
  });
});

describe("question picker with several questions", () => {
  it("shows a tab per question and walks them with the arrows", async () => {
    const { stdin, stdout, instance, answer } = await openPicker(TWO);
    expect(stdout.text()).toContain("렌더러");
    expect(stdout.text()).toContain("저장");

    await press(stdin, RIGHT);
    expect(stdout.text()).toContain("세계를 어디에 저장할까요?");

    await press(stdin, ESC);
    await answer;
    instance.unmount();
  });

  it("the bottom row moves on until the last tab, which submits everything", async () => {
    const { stdin, stdout, instance, answer } = await openPicker(TWO);
    // The first tab's bottom row says "next question", not "confirm".
    expect(stdout.text()).toContain("다음 질문 / Next question");

    const done = watch(answer);
    await press(stdin, ENTER, ENTER);
    expect(done()).toBe(false);
    expect(stdout.text()).toContain("세계를 어디에 저장할까요?");
    expect(stdout.text()).toContain("확인 / Confirm");

    await press(stdin, ENTER, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([
      { selected: ["three.js (추천)"], text: null },
      { selected: ["IndexedDB"], text: null },
    ]);
  });

  it("keeps each tab's answer and lets an earlier one be changed before submit", async () => {
    const { stdin, stdout, instance, answer } = await openPicker(TWO);
    // Answer the first, land on the second, answer it.
    await press(stdin, ENTER, ENTER);
    await press(stdin, DOWN, ENTER);
    expect(stdout.text()).toContain("✓ 렌더러");
    expect(stdout.text()).toContain("2/2");

    // Back to the first: its own selection is still there, and changeable.
    await press(stdin, LEFT);
    expect(stdout.text()).toContain("● three.js (추천)");
    await press(stdin, DOWN, ENTER);
    expect(stdout.text()).toContain("● Raw WebGL2");

    // Forward to the last tab and confirm from its bottom row.
    await press(stdin, RIGHT, DOWN, DOWN, DOWN, ENTER);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([
      { selected: ["Raw WebGL2"], text: null },
      { selected: ["서버"], text: null },
    ]);
  });

  it("Esc on any tab declines the whole batch", async () => {
    const { stdin, instance, answer } = await openPicker(TWO);
    await press(stdin, RIGHT, ESC);
    const result = await answer;
    instance.unmount();
    expect(result.answers).toEqual([]);
  });
});
