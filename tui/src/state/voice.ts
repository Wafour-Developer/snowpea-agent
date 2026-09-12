/**
 * Speaking and listening: what is on, what is recording, and why not.
 *
 * The daemon owns the actual audio — this is the state the TUI keeps about it,
 * plus the rule that nothing can be switched on that the daemon has not said it
 * can do. When a capability is missing the answer is a sentence the user can
 * act on, never a silently ignored command.
 *
 * Pure, so `test/voice.test.ts` can drive the whole machine.
 */

/**
 * What the daemon says it can do with audio; everything off until it answers.
 *
 * The booleans are what the switches are gated on; the names beside them are
 * what the status line says, and what makes "speech is on" mean something
 * ("piper", "openai") rather than nothing.
 */
export interface AudioCapabilities {
  /** Speech to text is available. */
  stt: boolean;
  /** Backend doing it, when there is one. */
  sttProvider: string | null;
  /** Text to speech is available. */
  tts: boolean;
  ttsProvider: string | null;
  /** Voice the daemon is configured to use. */
  voice: string | null;
  /** The daemon can record from a microphone. */
  record: boolean;
  /** The daemon can play audio out. */
  play: boolean;
  /** The daemon speaks replies without being asked. */
  autoSpeak: boolean;
  sttProviders: string[];
  ttsProviders: string[];
  players: string[];
  recorders: string[];
  /** Per-capability explanation for whatever is false. */
  reasons: Record<string, string>;
}

export const noAudio: AudioCapabilities = {
  stt: false,
  sttProvider: null,
  tts: false,
  ttsProvider: null,
  voice: null,
  record: false,
  play: false,
  autoSpeak: false,
  sttProviders: [],
  ttsProviders: [],
  players: [],
  recorders: [],
  reasons: {},
};

export interface VoiceState {
  /** Voice input is armed: Ctrl+Space starts and stops a recording. */
  input: boolean;
  /** Every assistant message is spoken as it finishes. */
  tts: boolean;
  recording: boolean;
  /** Epoch milliseconds the recording started, for the timer. */
  startedAt: number | null;
  /** True while a reply is being spoken. */
  speaking: boolean;
}

export const initialVoice: VoiceState = {
  input: false,
  tts: false,
  recording: false,
  startedAt: null,
  speaking: false,
};

/** One transition: the new state, and what to tell the user about it. */
export interface VoiceOutcome {
  state: VoiceState;
  message: string;
  /** False when nothing changed because the daemon cannot do it. */
  ok: boolean;
}

/** Why a capability is off, in the daemon's words when it gave any. */
export function reasonFor(capabilities: AudioCapabilities, name: string, fallback: string): string {
  const reason = capabilities.reasons?.[name];
  return reason && reason.length > 0 ? reason : fallback;
}

/** `/voice` — arm or disarm voice input. */
export function toggleVoiceInput(
  state: VoiceState,
  capabilities: AudioCapabilities,
  { localRecorder = false }: { localRecorder?: boolean } = {},
): VoiceOutcome {
  if (state.input) {
    return { state: { ...state, input: false, recording: false, startedAt: null }, message: "voice input off", ok: true };
  }
  if (!capabilities.stt) {
    return {
      state,
      message: `voice input needs speech-to-text: ${reasonFor(capabilities, "stt", "the daemon reports none")}`,
      ok: false,
    };
  }
  if (!capabilities.record && !localRecorder) {
    return {
      state,
      message: `voice input needs a microphone: ${reasonFor(capabilities, "record", "the daemon cannot record")}`,
      ok: false,
    };
  }
  const backend = capabilities.sttProvider ? ` (${capabilities.sttProvider})` : "";
  return {
    state: { ...state, input: true },
    message: `voice input on${backend} · Ctrl+Space to record`,
    ok: true,
  };
}

/** `/tts on|off` — speak assistant replies, or stop. */
export function setTts(
  state: VoiceState,
  capabilities: AudioCapabilities,
  on: boolean,
  { localPlayer = false }: { localPlayer?: boolean } = {},
): VoiceOutcome {
  if (!on) return { state: { ...state, tts: false }, message: "speech off", ok: true };
  if (!capabilities.tts) {
    return {
      state,
      message: `speech needs text-to-speech: ${reasonFor(capabilities, "tts", "the daemon reports none")}`,
      ok: false,
    };
  }
  if (!capabilities.play && !localPlayer) {
    return {
      state,
      message: `speech needs an output device: ${reasonFor(capabilities, "play", "the daemon cannot play audio")}`,
      ok: false,
    };
  }
  const backend = capabilities.ttsProvider ? ` (${capabilities.ttsProvider})` : "";
  return { state: { ...state, tts: true }, message: `speech on${backend}`, ok: true };
}

/** Ctrl+Space or `/rec` — begin recording. */
export function startRecording(
  state: VoiceState,
  capabilities: AudioCapabilities,
  now: number,
  { localRecorder = false }: { localRecorder?: boolean } = {},
): VoiceOutcome {
  if (state.recording) return { state, message: "already recording", ok: false };
  if (!capabilities.record && !localRecorder) {
    return {
      state,
      message: `recording needs a microphone: ${reasonFor(capabilities, "record", "the daemon cannot record")}`,
      ok: false,
    };
  }
  return {
    state: { ...state, input: true, recording: true, startedAt: now },
    message: "recording · Ctrl+Space to stop",
    ok: true,
  };
}

/** Ctrl+Space again — stop, and say how long it ran. */
export function stopRecording(state: VoiceState, now: number): VoiceOutcome & { elapsedMs: number } {
  if (!state.recording) {
    return { state, message: "not recording", ok: false, elapsedMs: 0 };
  }
  const elapsedMs = state.startedAt === null ? 0 : Math.max(0, now - state.startedAt);
  return {
    state: { ...state, recording: false, startedAt: null },
    message: "transcribing…",
    ok: true,
    elapsedMs,
  };
}

/** What the indicator slot says while a reply is being read out. */
export const SPEAKING_LABEL = "🔊 speaking · esc to stop";

/** `● REC 00:07`, for the working-indicator slot. */
export function recordingLabel(startedAt: number | null, now: number): string {
  const seconds = startedAt === null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `● REC ${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}
