/**
 * The audio side of the daemon.
 *
 * Five methods, and one rule about all of them: the daemon is the side that
 * knows what is installed and configured, so the TUI asks rather than guesses,
 * and every answer is read defensively. A daemon that has never heard of audio
 * answers with an error, and that reads as "can do none of it" rather than as a
 * crash.
 */

import type { AudioCapabilities } from "../state/voice.js";

/** One transcription. */
export interface Transcription {
  text: string;
  /** Which backend produced it, for the status line. */
  provider?: string;
}

/** Where a spoken reply was written, and whether the daemon played it. */
export interface Speech {
  path: string;
  mime: string;
  provider?: string;
  played: boolean;
}

/** A recording in progress or just finished. */
export interface Recording {
  path: string;
  mime?: string;
  recording: boolean;
  /** Present when `stop` was asked to transcribe. */
  text?: string;
  provider?: string;
}

export interface AudioClient {
  /** What this daemon can do, and why not for whatever it cannot. */
  capabilities(): Promise<AudioCapabilities>;
  /** Turn audio into text; one of `path` or `data` is given. */
  transcribe(input: {
    path?: string;
    data?: string;
    mime?: string;
    language?: string;
    sessionId?: string;
  }): Promise<Transcription>;
  /** Turn text into audio, optionally playing it on the daemon's host. */
  speak(input: {
    text: string;
    play?: boolean;
    voice?: string;
    sessionId?: string;
  }): Promise<Speech>;
  /** Begin recording from the daemon's microphone. */
  startRecording(sessionId?: string): Promise<Recording>;
  /** Stop, and hand back the file — with its transcript when asked for one. */
  stopRecording(input?: { sessionId?: string; transcribe?: boolean }): Promise<Recording>;
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
 * Read a capabilities answer.
 *
 * `stt` is the name of the backend in use or null, which the TUI reduces to a
 * yes/no for gating while keeping the name for the status line.
 */
export function readCapabilities(result: unknown): AudioCapabilities {
  const value = (result ?? {}) as Record<string, unknown>;
  const reasons = value.reasons;
  const list = (name: string): string[] =>
    Array.isArray(value[name]) ? (value[name] as string[]).map(String) : [];
  const sttProvider = typeof value.stt === "string" && value.stt.length > 0 ? value.stt : null;
  return {
    stt: sttProvider !== null,
    sttProvider,
    tts: value.tts === true,
    ttsProvider: typeof value.ttsProvider === "string" ? value.ttsProvider : null,
    voice: typeof value.voice === "string" ? value.voice : null,
    record: value.record === true,
    play: value.play === true,
    autoSpeak: value.autoSpeak === true,
    sttProviders: list("sttProviders"),
    ttsProviders: list("ttsProviders"),
    players: list("players"),
    recorders: list("recorders"),
    reasons:
      typeof reasons === "object" && reasons !== null ? (reasons as Record<string, string>) : {},
  };
}

/**
 * What to show the user when one of these calls fails.
 *
 * The daemon's own message is the useful part — "no speech-to-text backend is
 * configured" says more than any wording invented here — so it is kept, with
 * the error code in front of it when there is one.
 */
export function describeAudioError(error: unknown): string {
  const value = error as { code?: unknown; message?: unknown } | null;
  const message =
    typeof value?.message === "string" && value.message.length > 0
      ? value.message
      : String(error);
  const code = typeof value?.code === "string" ? value.code : null;
  return code && !message.includes(code) ? `${code}: ${message}` : message;
}

export function createAudioClient(client: AudioRpc): AudioClient {
  return {
    async capabilities() {
      return readCapabilities(await client.call(AUDIO_METHODS.capabilities, {}));
    },
    async transcribe(input) {
      const result = await client.call(AUDIO_METHODS.transcribe, { ...input });
      return { text: String(result?.text ?? ""), provider: result?.provider ?? undefined };
    },
    async speak(input) {
      const result = await client.call(AUDIO_METHODS.speak, { play: true, ...input });
      return {
        path: String(result?.path ?? ""),
        mime: String(result?.mime ?? "audio/wav"),
        provider: result?.provider ?? undefined,
        played: result?.played === true,
      };
    },
    async startRecording(sessionId) {
      const result = await client.call(AUDIO_METHODS.startRecording, { sessionId });
      return {
        path: String(result?.path ?? ""),
        mime: result?.mime ?? undefined,
        recording: result?.recording !== false,
      };
    },
    async stopRecording(input = {}) {
      const result = await client.call(AUDIO_METHODS.stopRecording, {
        transcribe: true,
        ...input,
      });
      return {
        path: String(result?.path ?? ""),
        mime: result?.mime ?? undefined,
        recording: result?.recording === true,
        text: typeof result?.text === "string" ? result.text : undefined,
        provider: result?.provider ?? undefined,
      };
    },
  };
}
