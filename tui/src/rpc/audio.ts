/**
 * The audio side of the daemon, as the TUI will call it.
 *
 * The protocol for this is still being written, so the shapes here are the
 * agreed ones and the implementation is a thin pass-through to `client.call`.
 * Nothing in the TUI calls it yet: the UI is built against this interface so
 * that turning the feature on is a matter of handing the real client in, not of
 * rewriting the components.
 */

import type { AudioCapabilities } from "../state/voice.js";

/** One transcription. */
export interface Transcription {
  text: string;
  /** Which engine produced it, for the status line. */
  provider?: string;
}

/** Where a spoken reply was written. */
export interface Speech {
  path: string;
  mime: string;
}

/** A recording in progress or just finished. */
export interface Recording {
  path: string;
}

export interface AudioClient {
  /** What this daemon can do, and why not for whatever it cannot. */
  capabilities(): Promise<AudioCapabilities>;
  /** Turn audio into text; one of `path` or `data` is given. */
  transcribe(input: { path?: string; data?: string; mime: string }): Promise<Transcription>;
  /** Turn text into audio, optionally playing it on the daemon's host. */
  speak(input: { text: string; play?: boolean }): Promise<Speech>;
  /** Begin recording from the daemon's microphone. */
  startRecording(): Promise<Recording>;
  /** Stop, and hand back the file. */
  stopRecording(): Promise<Recording>;
}

/** The subset of `TuiClient` this needs. */
export interface AudioRpc {
  call(method: string, params?: Record<string, unknown>): Promise<any>;
}

/** Method names, kept in one place so a protocol rename is a one-line change. */
export const AUDIO_METHODS = {
  capabilities: "audio.capabilities",
  transcribe: "audio.transcribe",
  speak: "audio.speak",
  startRecording: "audio.record.start",
  stopRecording: "audio.record.stop",
} as const;

/**
 * Read a capabilities answer defensively.
 *
 * A daemon that has never heard of audio answers with an error or with
 * nothing; either way the honest reading is that it can do none of it.
 */
export function readCapabilities(result: unknown): AudioCapabilities {
  const value = (result ?? {}) as Record<string, unknown>;
  const reasons = (value.reasons ?? {}) as Record<string, string>;
  return {
    stt: value.stt === true,
    tts: value.tts === true,
    record: value.record === true,
    play: value.play === true,
    reasons: typeof reasons === "object" && reasons !== null ? reasons : {},
  };
}

export function createAudioClient(client: AudioRpc): AudioClient {
  return {
    async capabilities() {
      return readCapabilities(await client.call(AUDIO_METHODS.capabilities, {}));
    },
    async transcribe(input) {
      const result = await client.call(AUDIO_METHODS.transcribe, { ...input });
      return { text: String(result?.text ?? ""), provider: result?.provider };
    },
    async speak(input) {
      const result = await client.call(AUDIO_METHODS.speak, { play: true, ...input });
      return { path: String(result?.path ?? ""), mime: String(result?.mime ?? "audio/wav") };
    },
    async startRecording() {
      const result = await client.call(AUDIO_METHODS.startRecording, {});
      return { path: String(result?.path ?? "") };
    },
    async stopRecording() {
      const result = await client.call(AUDIO_METHODS.stopRecording, {});
      return { path: String(result?.path ?? "") };
    },
  };
}
