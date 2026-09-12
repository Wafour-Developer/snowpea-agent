/**
 * Recording and speaking against a fake daemon: which side does the work, and
 * what the user is told when it fails.
 */

import { describe, expect, it, vi } from "vitest";

import {
  beginRecording,
  endRecording,
  speak,
  stopSpeaking,
  type AudioRuntime,
} from "../src/state/audio-runtime.js";
import { createAudioClient } from "../src/rpc/audio.js";
import { noAudio, type AudioCapabilities } from "../src/state/voice.js";
import type { LocalAudio, LocalProcess } from "../src/util/audio-tools.js";

const able: AudioCapabilities = {
  ...noAudio,
  stt: true,
  sttProvider: "whisper",
  tts: true,
  ttsProvider: "piper",
  record: true,
  play: true,
};

interface Scripted {
  [method: string]: unknown | ((params: any) => unknown);
}

function runtimeOf(
  scripted: Scripted,
  capabilities: AudioCapabilities,
  local: LocalAudio | null = null,
): AudioRuntime & { calls: Array<{ method: string; params: any }>; toasts: string[] } {
  const calls: Array<{ method: string; params: any }> = [];
  const toasts: string[] = [];
  const audio = createAudioClient({
    async call(method, params) {
      calls.push({ method, params });
      const answer = scripted[method];
      if (answer === undefined) throw new Error(`unexpected ${method}`);
      if (typeof answer === "function") return (answer as (p: any) => unknown)(params);
      if (answer instanceof Error) throw answer;
      return answer;
    },
  });
  return {
    calls,
    toasts,
    audio,
    local,
    capabilities,
    sessionId: "sess-1",
    localRecordingPath: "/state/tmp/rec.wav",
    onToast: (message) => toasts.push(message),
  };
}

function localAudioOf(options: { record?: boolean; play?: boolean } = {}): LocalAudio & {
  stopped: string[];
} {
  const stopped: string[] = [];
  const make = (command: string): LocalProcess => ({
    command,
    stop: () => stopped.push(command),
  });
  return {
    stopped,
    record: () => (options.record === false ? null : make("sox")),
    play: () => (options.play === false ? null : make("aplay")),
  };
}

describe("recording on the daemon", () => {
  it("starts and stops there, and hands back what was heard", async () => {
    const runtime = runtimeOf(
      {
        "audio.record.start": { path: "/daemon/rec.wav", recording: true },
        "audio.record.stop": { path: "/daemon/rec.wav", recording: false, text: "hello there" },
      },
      able,
    );

    const handle = await beginRecording(runtime);
    expect(handle).toMatchObject({ where: "daemon", path: "/daemon/rec.wav" });
    expect(await endRecording(runtime, handle!)).toBe("hello there");
    expect(runtime.calls.map((call) => call.method)).toEqual([
      "audio.record.start",
      "audio.record.stop",
    ]);
    expect(runtime.calls[1].params).toEqual({ sessionId: "sess-1", transcribe: true });
  });

  it("says so when the recording was silent", async () => {
    const runtime = runtimeOf(
      {
        "audio.record.start": { path: "/daemon/rec.wav", recording: true },
        "audio.record.stop": { path: "/daemon/rec.wav", recording: false, text: "  " },
      },
      able,
    );
    const handle = await beginRecording(runtime);
    expect(await endRecording(runtime, handle!)).toBeNull();
    expect(runtime.toasts).toContain("nothing was heard");
  });

  it("passes the daemon's refusal through to the user", async () => {
    const runtime = runtimeOf(
      { "audio.record.start": Object.assign(new Error("no microphone found"), { code: "no_recorder" }) },
      able,
    );
    expect(await beginRecording(runtime)).toBeNull();
    expect(runtime.toasts[0]).toBe("no_recorder: no microphone found");
  });
});

describe("recording on this machine", () => {
  it("records locally when the daemon cannot, then has it transcribed", async () => {
    const local = localAudioOf();
    const runtime = runtimeOf(
      { "audio.transcribe": { text: "locally heard", provider: "whisper" } },
      { ...able, record: false },
      local,
    );

    const handle = await beginRecording(runtime);
    expect(handle).toMatchObject({ where: "local", path: "/state/tmp/rec.wav" });
    expect(await endRecording(runtime, handle!)).toBe("locally heard");
    expect(local.stopped).toEqual(["sox"]);
    expect(runtime.calls[0]).toMatchObject({
      method: "audio.transcribe",
      params: { path: "/state/tmp/rec.wav", mime: "audio/wav", sessionId: "sess-1" },
    });
  });

  it("gives up when neither side can record", async () => {
    const runtime = runtimeOf({}, { ...able, record: false }, localAudioOf({ record: false }));
    expect(await beginRecording(runtime)).toBeNull();
    expect(runtime.toasts[0]).toContain("no recorder");
  });

  it("stops the recorder even when there is nothing to transcribe with", async () => {
    const local = localAudioOf();
    const runtime = runtimeOf(
      {},
      { ...noAudio, reasons: { stt: "no whisper model installed" } },
      local,
    );
    const handle = { where: "local" as const, path: "/state/tmp/rec.wav", process: local.record("") };
    expect(await endRecording(runtime, handle)).toBeNull();
    expect(local.stopped).toEqual(["sox"]);
    expect(runtime.toasts[0]).toBe("no whisper model installed");
  });
});

describe("speaking", () => {
  it("lets the daemon play it when the daemon has a speaker", async () => {
    const runtime = runtimeOf(
      { "audio.speak": { path: "/daemon/say.wav", mime: "audio/wav", provider: "piper", played: true } },
      able,
    );
    const handle = await speak(runtime, "the tests pass");
    expect(handle).toEqual({ where: "daemon" });
    expect(runtime.calls[0].params).toEqual({
      play: true,
      text: "the tests pass",
      sessionId: "sess-1",
    });
  });

  it("plays it here when the daemon has none", async () => {
    const local = localAudioOf();
    const runtime = runtimeOf(
      { "audio.speak": { path: "/daemon/say.wav", mime: "audio/wav", provider: "piper", played: false } },
      { ...able, play: false },
      local,
    );
    const handle = await speak(runtime, "the tests pass");
    expect(handle).toMatchObject({ where: "local" });
    expect(runtime.calls[0].params).toMatchObject({ play: false });

    stopSpeaking(runtime, handle);
    expect(local.stopped).toEqual(["aplay"]);
  });

  it("says so when the speech was made but nothing can play it", async () => {
    const runtime = runtimeOf(
      { "audio.speak": { path: "/daemon/say.wav", mime: "audio/wav", provider: "piper", played: false } },
      { ...able, play: false },
      localAudioOf({ play: false }),
    );
    expect(await speak(runtime, "hello")).toBeNull();
    expect(runtime.toasts[0]).toContain("no player");
  });

  it("refuses, with the daemon's reason, when there is no voice at all", async () => {
    const runtime = runtimeOf({}, { ...noAudio, reasons: { tts: "no tts backend configured" } });
    expect(await speak(runtime, "hello")).toBeNull();
    expect(runtime.toasts[0]).toBe("no tts backend configured");
  });

  it("says nothing about an empty message", async () => {
    const runtime = runtimeOf({}, able);
    expect(await speak(runtime, "   ")).toBeNull();
    expect(runtime.calls).toEqual([]);
  });

  it("admits it cannot stop audio playing on the daemon's host", () => {
    const runtime = runtimeOf({}, able);
    stopSpeaking(runtime, { where: "daemon" });
    expect(runtime.toasts[0]).toContain("finish on its own");
    // Nothing to stop is not an error.
    expect(() => stopSpeaking(runtime, null)).not.toThrow();
  });

  it("turns a failed synthesis into one sentence", async () => {
    const runtime = runtimeOf(
      { "audio.speak": Object.assign(new Error("the model is not downloaded"), { code: "tts_failed" }) },
      able,
    );
    expect(await speak(runtime, "hello")).toBeNull();
    expect(runtime.toasts[0]).toBe("tts_failed: the model is not downloaded");
  });
});
