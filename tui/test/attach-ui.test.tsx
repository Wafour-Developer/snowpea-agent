/**
 * Attachments and voice, driven through the real input.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { MAX_ATTACHMENT_BYTES, type FileProbe } from "../src/state/attachments.js";
import { fakeStdin, fakeStdout, sleep, type } from "./tty.js";

const FILES: Record<string, number> = {
  "/work/shot.png": 1_260_000,
  "/work/spec.pdf": 8_000,
  "/work/huge.png": MAX_ATTACHMENT_BYTES + 1,
  "/state/tmp/paste-1.png": 2048,
};

const probe: FileProbe = {
  resolve: (path) => (path.startsWith("/") ? path : `/work/${path}`),
  size: (path) => FILES[path] ?? null,
  basename: (path) => path.slice(path.lastIndexOf("/") + 1),
};

/** A daemon that answers `audio.capabilities` with whatever the test wants. */
function fakeClient(audio: Record<string, unknown> | null = null) {
  const calls: Array<{ method: string; params: any }> = [];
  let listeners: any;
  return {
    calls,
    getStatus: () => "connected",
    setListeners: (given: any) => {
      listeners = given;
    },
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async (method: string, params: any) => {
      calls.push({ method, params });
      if (method === "audio.capabilities") {
        if (!audio) throw new Error("no_audio: this daemon has no audio support");
        return audio;
      }
      if (method === "audio.record.start") return { path: "/tmp/rec.wav", recording: true };
      if (method === "audio.record.stop") {
        return { path: "/tmp/rec.wav", recording: false, text: "spoken words", provider: "whisper" };
      }
      if (method === "audio.speak") {
        return { path: "/tmp/say.wav", mime: "audio/wav", provider: "piper", played: true };
      }
      return { commands: [] };
    },
    prompt: async (sessionId: string, text: string, attachments: unknown[] = []) => {
      calls.push({ method: "session.prompt", params: { sessionId, text, attachments } });
      return { turnId: "t" };
    },
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    emit(event: any) {
      listeners?.onSessionEvent?.(event);
    },
  };
}

/** What a daemon with working audio answers. */
const ABLE_ANSWER = {
  stt: "whisper",
  tts: true,
  ttsProvider: "piper",
  record: true,
  play: true,
  autoSpeak: false,
  reasons: {},
};

async function open(
  options: Partial<React.ComponentProps<typeof App>> = {},
  audio: Record<string, unknown> | null = null,
) {
  const client = fakeClient(audio);
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App
      client={client as any}
      sessionId="sess-1"
      mode="accept"
      workdir="/work"
      probe={probe}
      {...options}
    />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(200);
  stdout.chunks.length = 0;
  return { client, stdin, stdout, instance };
}

describe("attachments", () => {
  it("turns a pasted path into a chip instead of text", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/shot.png");
    await sleep(120);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("📎 shot.png 1.2MB");
    // The path itself never reaches the draft.
    expect(output).not.toContain("> /work/shot.png");
  });

  it("takes several files from one drop", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/shot.png /work/spec.pdf");
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("📎 shot.png");
    expect(output).toContain("📎 spec.pdf");
  });

  it("leaves ordinary pasted text alone", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("just some pasted words");
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("just some pasted words");
    expect(output).not.toContain("📎");
  });

  it("says why a file it found is refused", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/huge.png");
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("too large");
    expect(output).not.toContain("📎 huge.png");
  });

  it("backspace on an empty input takes the newest chip off", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/shot.png");
    await sleep(80);
    stdin.write("/work/spec.pdf");
    await sleep(80);
    stdout.chunks.length = 0;

    stdin.write("\u007F"); // backspace
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("📎 shot.png");
    expect(output).not.toContain("📎 spec.pdf");
  });

  it("Ctrl+X clears them all", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/shot.png /work/spec.pdf");
    await sleep(120);
    stdout.chunks.length = 0;

    stdin.write("\u0018"); // Ctrl+X
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).not.toContain("📎");
  });

  it("/attach takes a path typed by hand", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "/attach spec.pdf", 20);
    stdin.write("\r");
    await sleep(150);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("📎 spec.pdf");
  });

  it("sends the chips with the prompt and shows them under it", async () => {
    const { stdin, stdout, instance } = await open();
    stdin.write("/work/shot.png");
    await sleep(80);
    await type(stdin, "look at this", 20);
    stdin.write("\r");
    await sleep(200);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("look at this");
    // The transcript entry carries the file, and the chip row is empty again.
    expect(output).toContain("📎 shot.png");
    expect(output.lastIndexOf("📎 shot.png 1.2MB")).toBeLessThan(output.lastIndexOf("look at this"));
  });

  it("Ctrl+V saves an image off the clipboard", async () => {
    const { stdin, stdout, instance } = await open({
      captureClipboard: () => "/state/tmp/paste-1.png",
    });
    stdin.write("\u0016"); // Ctrl+V
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("📎 paste-1.png");
  });

  it("says so when the clipboard has no image", async () => {
    const { stdin, stdout, instance } = await open({ captureClipboard: () => null });
    stdin.write("\u0016"); // Ctrl+V
    await sleep(120);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("no image on the clipboard");
  });
});

describe("voice", () => {
  it("explains itself when the daemon cannot listen", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "/voice", 20);
    stdin.write("\r");
    await sleep(150);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("speech-to-text");
  });

  it("arms voice input and records with Ctrl+Space", async () => {
    const { stdin, stdout, instance } = await open({}, ABLE_ANSWER);
    await type(stdin, "/voice", 20);
    stdin.write("\r");
    await sleep(150);
    expect(stdout.text()).toContain("voice input on");

    stdout.chunks.length = 0;
    stdin.write("\u0000"); // Ctrl+Space
    await sleep(300);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("● REC 00:0");
  });

  it("turns speech on, and says so in the status line", async () => {
    const { stdin, stdout, instance } = await open({}, ABLE_ANSWER);
    await type(stdin, "/tts on", 20);
    stdin.write("\r");
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("speech on");
    expect(output).toContain("🔊");
  });

  it("refuses speech when the daemon has no voice", async () => {
    const { stdin, stdout, instance } = await open();
    await type(stdin, "/tts on", 20);
    stdin.write("\r");
    await sleep(200);
    const output = stdout.text();
    instance.unmount();
    expect(output).toContain("text-to-speech");
    expect(output).not.toContain("🔊");
  });

  it("sends the attachment with the prompt, as a path the daemon can read", async () => {
    const { client, stdin, instance } = await open();
    stdin.write("/work/shot.png");
    await sleep(80);
    await type(stdin, "describe it", 20);
    stdin.write("\r");
    await sleep(200);
    instance.unmount();

    const prompt = client.calls.find((call) => call.method === "session.prompt");
    expect(prompt?.params.text).toBe("describe it");
    expect(prompt?.params.attachments).toEqual([
      {
        kind: "image",
        name: "shot.png",
        path: "/work/shot.png",
        mimeType: "image/png",
        size: 1_260_000,
      },
    ]);
  });

  it("records through the daemon and puts the transcript in the draft", async () => {
    const { client, stdin, stdout, instance } = await open({}, ABLE_ANSWER);
    await type(stdin, "/voice", 20);
    stdin.write("\r");
    await sleep(150);

    stdin.write("\u0000"); // Ctrl+Space starts
    await sleep(250);
    expect(stdout.text()).toContain("● REC 00:0");

    stdin.write("\u0000"); // and stops
    await sleep(300);
    const output = stdout.text();
    instance.unmount();

    expect(client.calls.map((call) => call.method)).toContain("audio.record.start");
    const stopped = client.calls.find((call) => call.method === "audio.record.stop");
    expect(stopped?.params).toEqual({ sessionId: "sess-1", transcribe: true });
    // What was heard is offered for review rather than sent.
    expect(output).toContain("spoken words");
  });

  it("speaks a finished reply once /tts is on", async () => {
    const { client, stdin, stdout, instance } = await open({}, ABLE_ANSWER);
    await type(stdin, "/tts on", 20);
    stdin.write("\r");
    await sleep(200);

    client.emit({
      sessionId: "sess-1",
      seq: 1,
      kind: "message.done",
      payload: { role: "assistant", text: "the tests pass" },
    });
    await sleep(250);
    const output = stdout.text();
    instance.unmount();

    const spoke = client.calls.filter((call) => call.method === "audio.speak");
    expect(spoke).toHaveLength(1);
    expect(spoke[0].params).toMatchObject({ text: "the tests pass", play: true });
    expect(output).toContain("🔊 speaking");
  });

  it("keeps quiet when the daemon reports no audio at all", async () => {
    const { client, stdin, instance } = await open();
    await type(stdin, "/tts on", 20);
    stdin.write("\r");
    await sleep(200);
    client.emit({
      sessionId: "sess-1",
      seq: 1,
      kind: "message.done",
      payload: { role: "assistant", text: "the tests pass" },
    });
    await sleep(200);
    instance.unmount();
    expect(client.calls.some((call) => call.method === "audio.speak")).toBe(false);
  });
});
