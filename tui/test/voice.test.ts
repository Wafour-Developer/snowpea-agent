/**
 * The voice switches: what can be turned on, and what the user is told when it
 * cannot be.
 */

import { describe, expect, it } from "vitest";

import {
  initialVoice,
  noAudio,
  recordingLabel,
  reasonFor,
  setTts,
  startRecording,
  stopRecording,
  toggleVoiceInput,
  type AudioCapabilities,
} from "../src/state/voice.js";
import { AUDIO_METHODS, createAudioClient, readCapabilities } from "../src/rpc/audio.js";

const able: AudioCapabilities = { stt: true, tts: true, record: true, play: true, reasons: {} };

describe("with a daemon that can do none of it", () => {
  it("refuses voice input and says why", () => {
    const outcome = toggleVoiceInput(initialVoice, noAudio);
    expect(outcome.ok).toBe(false);
    expect(outcome.state).toEqual(initialVoice);
    expect(outcome.message).toContain("speech-to-text");
  });

  it("passes the daemon's own reason through when it gave one", () => {
    const capabilities = { ...noAudio, reasons: { stt: "no whisper model installed" } };
    expect(toggleVoiceInput(initialVoice, capabilities).message).toContain(
      "no whisper model installed",
    );
    expect(reasonFor(capabilities, "stt", "fallback")).toBe("no whisper model installed");
    expect(reasonFor(capabilities, "tts", "fallback")).toBe("fallback");
  });

  it("refuses speech and refuses to record", () => {
    expect(setTts(initialVoice, noAudio, true).ok).toBe(false);
    expect(startRecording(initialVoice, noAudio, 0).ok).toBe(false);
  });

  it("still lets everything be turned off", () => {
    const on = { input: true, tts: true, recording: false, startedAt: null };
    expect(setTts(on, noAudio, false).state.tts).toBe(false);
    expect(toggleVoiceInput(on, noAudio).state.input).toBe(false);
  });
});

describe("with a daemon that can", () => {
  it("arms and disarms voice input", () => {
    const armed = toggleVoiceInput(initialVoice, able);
    expect(armed.ok).toBe(true);
    expect(armed.state.input).toBe(true);
    expect(armed.message).toContain("Ctrl+Space");
    expect(toggleVoiceInput(armed.state, able).state.input).toBe(false);
  });

  it("records, then stops with the time it ran", () => {
    const started = startRecording(initialVoice, able, 1000);
    expect(started.state).toMatchObject({ recording: true, startedAt: 1000, input: true });

    const stopped = stopRecording(started.state, 8000);
    expect(stopped.elapsedMs).toBe(7000);
    expect(stopped.state).toMatchObject({ recording: false, startedAt: null });
    expect(stopped.message).toBe("transcribing…");
  });

  it("will not start twice or stop when it never started", () => {
    const started = startRecording(initialVoice, able, 0);
    expect(startRecording(started.state, able, 1).ok).toBe(false);
    expect(stopRecording(initialVoice, 1).ok).toBe(false);
  });

  it("turning voice input off also drops a recording in progress", () => {
    const started = startRecording(initialVoice, able, 0);
    expect(toggleVoiceInput(started.state, able).state.recording).toBe(false);
  });

  it("switches speech on and off", () => {
    const on = setTts(initialVoice, able, true);
    expect(on.state.tts).toBe(true);
    expect(setTts(on.state, able, false).state.tts).toBe(false);
  });
});

describe("recordingLabel", () => {
  it("counts up in minutes and seconds", () => {
    expect(recordingLabel(1000, 1000)).toBe("● REC 00:00");
    expect(recordingLabel(1000, 8000)).toBe("● REC 00:07");
    expect(recordingLabel(0, 75_000)).toBe("● REC 01:15");
    expect(recordingLabel(null, 5000)).toBe("● REC 00:00");
  });
});

describe("the audio client", () => {
  it("reads a capabilities answer, and a missing one as all false", () => {
    expect(readCapabilities({ stt: true, reasons: { tts: "no voice" } })).toEqual({
      stt: true,
      tts: false,
      record: false,
      play: false,
      reasons: { tts: "no voice" },
    });
    expect(readCapabilities(undefined)).toEqual(noAudio);
  });

  it("calls the methods the protocol names", async () => {
    const calls: Array<{ method: string; params: any }> = [];
    const audio = createAudioClient({
      async call(method, params) {
        calls.push({ method, params });
        if (method === AUDIO_METHODS.transcribe) return { text: "hello there", provider: "whisper" };
        if (method === AUDIO_METHODS.speak) return { path: "/tmp/a.wav", mime: "audio/wav" };
        return { path: "/tmp/rec.wav", stt: true };
      },
    });

    expect(await audio.transcribe({ path: "/tmp/rec.wav", mime: "audio/wav" })).toEqual({
      text: "hello there",
      provider: "whisper",
    });
    expect(await audio.speak({ text: "hi" })).toEqual({ path: "/tmp/a.wav", mime: "audio/wav" });
    expect((await audio.startRecording()).path).toBe("/tmp/rec.wav");
    expect((await audio.stopRecording()).path).toBe("/tmp/rec.wav");

    expect(calls.map((call) => call.method)).toEqual([
      AUDIO_METHODS.transcribe,
      AUDIO_METHODS.speak,
      AUDIO_METHODS.startRecording,
      AUDIO_METHODS.stopRecording,
    ]);
    expect(calls[1].params).toEqual({ play: true, text: "hi" });
  });
});
