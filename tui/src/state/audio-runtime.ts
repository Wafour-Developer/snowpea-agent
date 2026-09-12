/**
 * Recording and speaking, end to end.
 *
 * The daemon does the work wherever it can: it knows which backends are
 * installed and which voice is configured, and it is the only side that can
 * transcribe. The local tools are the fallback for the two things that are
 * physically here rather than there — the microphone and the speakers — and are
 * used only when the daemon says it has none.
 *
 * Everything is a plain function over an injected set of dependencies, so
 * `test/audio-runtime.test.ts` can drive every path, including the failures,
 * with no daemon and no sound card.
 */

import { audioErrorCode, describeAudioError, type AudioClient } from "../rpc/audio.js";
import type { LocalAudio, LocalProcess } from "../util/audio-tools.js";
import type { AudioCapabilities } from "./voice.js";

export interface AudioRuntime {
  audio: AudioClient;
  /** Tools on this machine, when there are any. */
  local?: LocalAudio | null;
  capabilities: AudioCapabilities;
  sessionId: string;
  /** Where the recording goes when this machine is doing the recording. */
  localRecordingPath?: string;
  onToast(message: string): void;
}

/** A recording in flight, on whichever side is doing it. */
export interface RecordingHandle {
  /** Which side is recording. */
  where: "daemon" | "local";
  path: string;
  /** The local tool, when this machine is recording. */
  process?: LocalProcess | null;
}

/** True when this machine could record even though the daemon cannot. */
export function hasLocalRecorder(runtime: Pick<AudioRuntime, "local">): boolean {
  return Boolean(runtime.local);
}

/**
 * Start recording, on the daemon when it can and here when it cannot.
 *
 * Null means nothing started, and the toast already says why.
 */
export async function beginRecording(runtime: AudioRuntime): Promise<RecordingHandle | null> {
  if (runtime.capabilities.record) {
    try {
      const recording = await runtime.audio.startRecording(runtime.sessionId);
      return { where: "daemon", path: recording.path };
    } catch (error) {
      // The daemon losing its recorder between the capability answer and now is
      // exactly the case the local tools are here for; anything else is a
      // failure the user needs to read.
      if (audioErrorCode(error) !== "no_recorder") {
        runtime.onToast(describeAudioError(error));
        return null;
      }
    }
  }

  const path = runtime.localRecordingPath;
  const process = runtime.local && path ? runtime.local.record(path) : null;
  if (!process || !path) {
    runtime.onToast("no recorder: neither the daemon nor this machine can record");
    return null;
  }
  return { where: "local", path, process };
}

/**
 * Stop, and hand back what was said.
 *
 * The daemon transcribes in one call when it is the one recording; a local
 * recording is handed to it afterwards, which is the only part it can do.
 */
export async function endRecording(
  runtime: AudioRuntime,
  handle: RecordingHandle,
): Promise<string | null> {
  if (handle.where === "local") {
    handle.process?.stop();
    if (!runtime.capabilities.stt) {
      runtime.onToast(
        runtime.capabilities.reasons.stt ?? "no speech-to-text backend is configured",
      );
      return null;
    }
    try {
      const transcript = await runtime.audio.transcribe({
        path: handle.path,
        mime: "audio/wav",
        sessionId: runtime.sessionId,
      });
      return transcript.text.trim().length > 0 ? transcript.text : null;
    } catch (error) {
      runtime.onToast(describeAudioError(error));
      return null;
    }
  }

  try {
    const stopped = await runtime.audio.stopRecording({
      sessionId: runtime.sessionId,
      transcribe: runtime.capabilities.stt,
    });
    if (!runtime.capabilities.stt) {
      runtime.onToast(
        runtime.capabilities.reasons.stt ?? "recorded, but there is no speech-to-text backend",
      );
      return null;
    }
    const text = (stopped.text ?? "").trim();
    if (text.length === 0) {
      runtime.onToast("nothing was heard");
      return null;
    }
    return text;
  } catch (error) {
    // Asking a daemon that is not recording to stop is not worth alarming
    // anyone about; the recording simply is not there.
    if (audioErrorCode(error) === "not_recording") return null;
    runtime.onToast(describeAudioError(error));
    return null;
  }
}

/** A reply being spoken, and how to stop it. */
export interface SpeechHandle {
  where: "daemon" | "local";
  process?: LocalProcess | null;
}

/**
 * Say something out loud.
 *
 * The daemon plays it when it has an output device; otherwise it only
 * synthesises and the file is played here. Null means nothing is playing.
 */
export async function speak(runtime: AudioRuntime, text: string): Promise<SpeechHandle | null> {
  const said = text.trim();
  if (said.length === 0) return null;
  if (!runtime.capabilities.tts) {
    runtime.onToast(runtime.capabilities.reasons.tts ?? "no text-to-speech backend is configured");
    return null;
  }

  try {
    const speech = await runtime.audio.speak({
      text: said,
      play: runtime.capabilities.play,
      sessionId: runtime.sessionId,
    });
    if (speech.played) return { where: "daemon" };
    const process = runtime.local?.play(speech.path) ?? null;
    if (!process) {
      runtime.onToast("no player: the speech was written but nothing here can play it");
      return null;
    }
    return { where: "local", process };
  } catch (error) {
    // The daemon can synthesise but not play: the file is still worth playing
    // here, and the error says which of the two it was.
    if (audioErrorCode(error) === "no_player") {
      runtime.onToast("no player: the daemon cannot play audio and neither can this machine");
      return null;
    }
    runtime.onToast(describeAudioError(error));
    return null;
  }
}

/**
 * Stop a reply mid-sentence.
 *
 * Only a local player can actually be stopped; audio the daemon is playing on
 * its own host is out of reach, so the honest thing is to say so.
 */
export function stopSpeaking(runtime: AudioRuntime, handle: SpeechHandle | null): void {
  if (!handle) return;
  if (handle.where === "local") {
    handle.process?.stop();
    return;
  }
  runtime.onToast("the daemon is playing this one; it will finish on its own");
}
