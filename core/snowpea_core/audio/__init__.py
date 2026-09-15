"""Voice I/O: speak the assistant's replies, take the microphone as input.

Every piece is optional.  The whole package is built so that a machine with no
microphone, no player, no transcription backend and no speech backend still
runs the daemon normally — :func:`capabilities` is the one call that says what
actually works here, and *why* anything that does not is off, so the TUI and
the setup wizard can show a reason instead of a silent no-op.

Nothing in here knows who is asking: the same resolved providers serve the
``audio.*`` RPCs the TUI calls and the ``transcribe_audio`` / ``text_to_speech``
tools the agent calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.audio import player, recorder, stt, tts
from snowpea_core.audio.player import AudioError, available_players, can_play, find_player, play
from snowpea_core.audio.recorder import Recorder, available_recorders, can_record, find_recorder
from snowpea_core.audio.stt import STTProvider, Transcript, resolve_provider
from snowpea_core.audio.tts import Speech, SpeechCaller, TTSProvider, synthesize

#: Directory under ``SNOWPEA_HOME`` where recordings and speech are kept.
DIRNAME = "audio"

#: The provider name that means "never, do not look for a backend".
OFF = "off"


@dataclass(frozen=True)
class AudioConfig:
    """The audio settings, as a plain object the package can read.

    It mirrors the ``audio`` settings block but does not import it, so the
    audio code stays testable without a whole ``Settings`` tree and the
    settings module never has to import the audio package back.
    """

    # -- speech to text
    stt_provider: str = "auto"
    stt_command: str | None = None
    stt_model: str | None = None
    #: ``audio.stt.language`` — what SenseVoice is told to expect, and which
    #: zipformer the ``auto`` chain reaches for first.  Empty means detect.
    stt_language: str | None = None
    # -- text to speech
    tts_enabled: bool = True
    tts_provider: str = "auto"
    tts_command: str | None = None
    tts_model: str | None = None
    voice: str | None = None
    auto_speak: bool = False
    # -- shared
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    #: True once ``settings.media.mcp`` names the snowpea-studio server.
    studio_configured: bool = False
    player: str | None = None
    recorder: str | None = None
    #: ``$SNOWPEA_HOME``: where downloaded speech and voice models live.  The
    #: local engines are unavailable without it, which is what makes a config
    #: built by hand in a test degrade to the hosted backends rather than
    #: pretending a model is there.
    home: Path | str | None = None

    @property
    def stt_off(self) -> bool:
        return self.stt_provider == OFF

    @property
    def tts_off(self) -> bool:
        return not self.tts_enabled or self.tts_provider == OFF

    def stt(self) -> STTProvider | None:
        """The transcription backend this configuration resolves to."""
        if self.stt_off:
            return None
        return resolve_provider(
            self.stt_provider or "auto",
            api_key=self.openai_api_key,
            model=self.stt_model,
            base_url=self.openai_base_url,
            command=self.stt_command,
            home=self.home,
            language=self.stt_language,
        )

    def tts(self, caller: SpeechCaller | None = None) -> TTSProvider | None:
        """The speech backend this configuration resolves to.

        ``caller`` runs the studio MCP tool; without one the studio backend is
        still *reported* as available (so the wizard can say so) but refuses to
        synthesise.
        """
        if self.tts_off:
            return None
        return tts.resolve_provider(
            self.tts_provider or "auto",
            caller=caller,
            studio_configured=self.studio_configured,
            api_key=self.openai_api_key,
            model=self.tts_model,
            base_url=self.openai_base_url,
            command=self.tts_command,
            language=self.stt_language,
        )


def audio_dir(home: Path | str, session_id: str | None = None) -> Path:
    """Where audio files for ``session_id`` live under a snowpea home."""
    root = Path(home).expanduser() / DIRNAME
    return root / session_id if session_id else root


def capabilities(
    config: AudioConfig | None = None,
    *,
    caller: SpeechCaller | None = None,
) -> dict[str, Any]:
    """What voice I/O can do on this machine right now.

    The shape is ``{"stt", "tts", "ttsProvider", "record", "play",
    "autoSpeak", "sttProviders", "ttsProviders", "players", "recorders",
    "reasons"}``; ``reasons`` carries a short sentence for every capability
    that is off, which is the whole point — a user who asks for voice and gets
    silence deserves to be told what to install.
    """
    cfg = config or AudioConfig()
    reasons: dict[str, str] = {}

    listener = cfg.stt()
    if listener is None:
        if cfg.stt_off:
            reasons["stt"] = "speech input is switched off (audio.stt.provider)"
        elif cfg.stt_provider not in {"auto", ""}:
            reasons["stt"] = f"stt provider {cfg.stt_provider!r} is not usable here"
        else:
            reasons["stt"] = (
                "no transcription backend: run `snowpea audio install "
                "sherpa-onnx-sensevoice` (CPU, recommended), install the whisper CLI, "
                "set an OpenAI API key, or configure audio.stt.command"
            )

    speaker = cfg.tts(caller)
    if speaker is None:
        if cfg.tts_off:
            reasons["tts"] = "text to speech is switched off (audio.tts.enabled)"
        elif cfg.tts_provider not in {"auto", ""}:
            reasons["tts"] = f"tts provider {cfg.tts_provider!r} is not usable here"
        else:
            reasons["tts"] = (
                "no speech backend: run `snowpea audio install supertonic` (CPU, "
                "recommended), install edge-tts or piper, or set an OpenAI API key"
            )

    record_backend = find_recorder(cfg.recorder)
    if record_backend is None:
        reasons["record"] = "no recorder found; install sox (rec), alsa-utils (arecord) or ffmpeg"

    play_backend = find_player(cfg.player)
    if play_backend is None:
        reasons["play"] = "no audio player found; install afplay, paplay, aplay, ffplay or mpv"

    return {
        "stt": listener.name if listener is not None else None,
        "tts": speaker is not None,
        "ttsProvider": speaker.name if speaker is not None else None,
        "voice": cfg.voice,
        "record": record_backend is not None,
        "play": play_backend is not None,
        "autoSpeak": bool(speaker is not None and cfg.auto_speak),
        "sttProviders": stt_providers(cfg),
        "ttsProviders": tts.available_providers(
            studio_configured=cfg.studio_configured,
            api_key=cfg.openai_api_key,
            command=cfg.tts_command,
            language=cfg.stt_language,
        ),
        "players": available_players(),
        "recorders": available_recorders(),
        "reasons": reasons,
    }


def stt_providers(config: AudioConfig) -> list[str]:
    """Every transcription backend that would work here, in preference order."""
    if config.stt_off:
        return []
    found: list[str] = []
    for candidate in stt.AUTO_ORDER:
        provider = stt.build_provider(
            candidate,
            api_key=config.openai_api_key,
            model=config.stt_model,
            base_url=config.openai_base_url,
            command=config.stt_command,
            home=config.home,
            language=config.stt_language,
        )
        if provider.available():
            found.append(provider.name)
    return found


__all__ = [
    "DIRNAME",
    "OFF",
    "AudioConfig",
    "AudioError",
    "Recorder",
    "STTProvider",
    "Speech",
    "SpeechCaller",
    "TTSProvider",
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
    "stt_providers",
    "synthesize",
    "tts",
]
