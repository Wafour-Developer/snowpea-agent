/**
 * Attachments and voice, driven through the real input.
 */

import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { App } from "../src/app.js";
import { MAX_ATTACHMENT_BYTES, type FileProbe } from "../src/state/attachments.js";
import type { AudioCapabilities } from "../src/state/voice.js";
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

const able: AudioCapabilities = { stt: true, tts: true, record: true, play: true, reasons: {} };

function fakeClient() {
  return {
    getStatus: () => "connected",
    setListeners: () => undefined,
    onApprovalRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.1.2", latest: "0.1.2" }),
    call: async () => ({ commands: [] }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
  };
}

async function open(options: Partial<React.ComponentProps<typeof App>> = {}) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(
    <App
      client={fakeClient() as any}
      sessionId="sess-1"
      mode="accept"
      workdir="/work"
      probe={probe}
      {...options}
    />,
    { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
  );
  await sleep(150);
  stdout.chunks.length = 0;
  return { stdin, stdout, instance };
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
    const { stdin, stdout, instance } = await open({ audio: able });
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
    const { stdin, stdout, instance } = await open({ audio: able });
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
});
