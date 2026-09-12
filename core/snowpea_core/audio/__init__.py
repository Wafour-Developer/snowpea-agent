"""Voice I/O: speak the assistant's replies, take the microphone as input.

Every piece is optional.  The whole package is built so that a machine with no
microphone, no player and no transcription backend still runs the daemon
normally — :func:`capabilities` is the one call that says what actually works
here, and *why* anything that does not is off, so the TUI can show a reason
instead of a silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.audio import player, recorder, stt, tts
from snowpea_core.audio.player import AudioError, available_players, can_play, find_player, play
from snowpea_core.audio.recorder import Recorder, available_recorders, can_record, find_recorder
from snowpea_core.audio.stt import STTProvider, Transcript, resolve_provider
from snowpea_core.audio.tts import Speech, synthesize

#: Directory under ``SNOWPEA_HOME`` where recordings and speech are kept.
DIRNAME = "audio"


@dataclass(frozen=True)
class AudioConfig:
    """The audio settings, as a plain object the package can read.

    It mirrors the ``audio`` settings block but does not import it, so the
    audio code stays testable without a whole ``Settings`` tree.
    """

    stt_provider: str = "auto"
    stt_command: str | None = None
    stt_model: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    tts_enabled: bool = True
    auto_speak: bool = False
    voice: str | None = None
    player: str | None = None
    recorder: str | None = None

    def stt(self) -> STTProvider | None:
        """The transcription backend this configuration resolves to."""
        return resolve_provider(
            self.stt_provider or "auto",
            api_key=self.openai_api_key,
            model=self.stt_model,
            base_url=self.openai_base_url,
            command=self.stt_command,
        )


def audio_dir(home: Path | str, session_id: str | None = None) -> Path:
    """Where audio files for ``session_id`` live under a snowpea home."""
    root = Path(home).expanduser() / DIRNAME
    return root / session_id if session_id else root


def capabilities(
    config: AudioConfig | None = None,
    *,
    tts_available: bool = False,
) -> dict[str, Any]:
    """What voice I/O can do on this machine right now.

    ``tts_available`` is passed in by the caller because it depends on whether
    the media MCP server is configured, which lives in the daemon's settings
    rather than here.

    The shape is ``{"stt": name|None, "tts": bool, "record": bool,
    "play": bool, "reasons": {...}}``; ``reasons`` carries a short sentence for
    each capability that is off.
    """
    cfg = config or AudioConfig()
    reasons: dict[str, str] = {}

    provider = cfg.stt()
    stt_name = provider.name if provider is not None else None
    if provider is None:
        if cfg.stt_provider not in {"auto", ""}:
            reasons["stt"] = f"stt provider {cfg.stt_provider!r} is not usable here"
        else:
            reasons["stt"] = (
                "no transcription backend: set an OpenAI API key, install the whisper CLI, "
                "or configure audio.stt.command"
            )

    speech_ok = bool(tts_available and cfg.tts_enabled)
    if not speech_ok:
        reasons["tts"] = (
            "text to speech is switched off (audio.tts.enabled)"
            if tts_available
            else "text to speech needs the snowpea-studio MCP server (settings.media.mcp)"
        )

    record_backend = find_recorder(cfg.recorder)
    if record_backend is None:
        reasons["record"] = "no recorder found; install sox (rec), alsa-utils (arecord) or ffmpeg"

    play_backend = find_player(cfg.player)
    if play_backend is None:
        reasons["play"] = "no audio player found; install afplay, paplay, aplay, ffplay or mpv"

    return {
        "stt": stt_name,
        "tts": speech_ok,
        "record": record_backend is not None,
        "play": play_backend is not None,
        "autoSpeak": bool(speech_ok and cfg.auto_speak),
        "players": available_players(),
        "recorders": available_recorders(),
        "reasons": reasons,
    }


__all__ = [
    "DIRNAME",
    "AudioConfig",
    "AudioError",
    "Recorder",
    "STTProvider",
    "Speech",
    "Transcript",
    "audio_dir",
    "available_players",
    "available_recorders",
    "can_play",
    "can_record",
    "capabilities",
    "find_player",
    "find_recorder",
    "play",
    "player",
    "recorder",
    "resolve_provider",
    "stt",
    "synthesize",
    "tts",
]
