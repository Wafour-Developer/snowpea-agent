/**
 * The `ask_user` picker, driven the way a person drives it.
 *
 * The point of the tool is that the user never has to type a letter to answer
 * a closed question, so every case here goes through the real `App`, a real
 * Ink render and real keystrokes, and checks what the daemon would be told.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import type { QuestionRequestParams, QuestionResponse } from "../src/rpc/sdk.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const RENDERER_QUESTION: QuestionRequestParams = {
  requestId: "qu-1",
  sessionId: "sess-1",
  header: "렌더러",
  question: "렌더링을 무엇으로 할까요? 여기서 고른 것이 엔진 통제력을 정합니다.",
  options: [
    { label: "three.js (추천)", description: "빠른 시작, 엔진 통제력 낮음" },
    { label: "Raw WebGL2", description: "공수 큼, 완전한 통제" },
  ],
  allowOther: true,
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
    ask(request: QuestionRequestParams = RENDERER_QUESTION): Promise<QuestionResponse> {
      return onQuestion!(request);
    },
  };
}

async function openPicker(request: QuestionRequestParams = RENDERER_QUESTION) {
  const client = fakeClient();
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App client={client as any} sessionId="sess-1" mode="accept" workdir="/tmp/project" />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(120);
  const answer = client.ask(request);
  await sleep(120);
  return { client, stdin, stdout, instance, answer };
}

describe("question picker", () => {
  it("shows the header, the question and every option with its description", async () => {
    const { stdout, stdin, instance, answer } = await openPicker();
    const output = stdout.text();
    stdin.write("\r");
    await answer;
    instance.unmount();

    expect(output).toContain("렌더러");
    expect(output).toContain("three.js");
    expect(output).toContain("Raw WebGL2");
    expect(output).toContain("공수 큼, 완전한 통제");
    expect(output).toContain("기타 / Other");
  });

  it("takes the highlighted row on Enter", async () => {
    const { stdin, instance, answer } = await openPicker();
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.selected).toEqual(["three.js (추천)"]);
    expect(result.text ?? null).toBeNull();
  });

  it("moves the cursor with the down arrow", async () => {
    const { stdin, instance, answer } = await openPicker();
    stdin.write("\u001b[B");
    await sleep(40);
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.selected).toEqual(["Raw WebGL2"]);
  });

  it("moves with j/k as well as the arrows", async () => {
    const { stdin, instance, answer } = await openPicker();
    stdin.write("j");
    await sleep(40);
    stdin.write("k");
    await sleep(40);
    stdin.write("j");
    await sleep(40);
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.selected).toEqual(["Raw WebGL2"]);
  });

  it("answers straight away when a number key is pressed", async () => {
    const { stdin, instance, answer } = await openPicker();
    stdin.write("2");
    const result = await answer;
    instance.unmount();
    expect(result.selected).toEqual(["Raw WebGL2"]);
  });

  it("toggles with Space and submits the set when multi is set", async () => {
    const { stdin, instance, answer } = await openPicker({ ...RENDERER_QUESTION, multi: true });
    stdin.write(" ");
    await sleep(40);
    stdin.write("\u001b[B");
    await sleep(40);
    stdin.write(" ");
    await sleep(40);
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.selected).toEqual(["three.js (추천)", "Raw WebGL2"]);
  });

  it("opens a text line on the Other row and sends what was typed", async () => {
    const { stdin, instance, answer, stdout } = await openPicker();
    stdin.write("\u001b[B");
    await sleep(40);
    stdin.write("\u001b[B");
    await sleep(40);
    stdin.write("\r");
    await sleep(60);
    expect(stdout.text()).toContain("Enter to send");
    stdin.write("babylon.js");
    await sleep(40);
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.selected ?? []).toEqual([]);
    expect(result.text).toBe("babylon.js");
  });

  it("reports Esc as an answer with nothing in it, so the tool can say declined", async () => {
    const { stdin, instance, answer } = await openPicker();
    stdin.write("\u001b");
    const result = await answer;
    instance.unmount();
    expect(result.selected ?? []).toEqual([]);
    expect(result.text ?? null).toBeNull();
  });

  it("asks free text when the question carries no options", async () => {
    const { stdin, instance, answer } = await openPicker({
      requestId: "qu-2",
      sessionId: "sess-1",
      question: "무엇을 먼저 만들까요?",
      options: [],
      allowOther: true,
    });
    stdin.write("\r");
    await sleep(60);
    stdin.write("지형 생성기");
    await sleep(40);
    stdin.write("\r");
    const result = await answer;
    instance.unmount();
    expect(result.text).toBe("지형 생성기");
  });
});
