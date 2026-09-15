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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.audio import player, recorder, stt, tts
from snowpea_core.audio.player import AudioError, available_players, can_play, find_player, play
from snowpea_core.audio.recorder import Recorder, available_recorders, can_record, find_recorder
from snowpea_core.audio.stt import STTProvider, Transcript, resolve_provider
from snowpea_core.audio.tts import Speech, SpeechCaller, TTSProvider, synthesize

log = logging.getLogger("snowpea.audio")

#: Directory under ``SNOWPEA_HOME`` where recordings and speech are kept.
DIRNAME = "audio"

#: The provider name that means "never, do not look for a backend".  It is the
#: same thing as leaving the setting unset; both are kept because ``off`` is
#: what the wizard's own row writes and what a user expects to be able to type.
OFF = "off"

#: Values that mean "no engine is pinned".  ``"auto"`` is here for one reason:
#: an older settings file may still carry it, and it now means *off* rather
#: than "try everything" (``config/settings.LEGACY_AUTO``).
UNSET: frozenset[str] = frozenset({"", OFF, "auto"})

#: What a surface shows when nothing is pinned.
NO_ENGINE_REASON = "no engine set — install or pick one in setup"

#: What it shows when the pinned engine is not there.
MISSING_ENGINE_REASON = "engine {engine} is not installed"


def pinned(provider: str | None) -> str | None:
    """The engine a setting pins, or ``None`` when it pins nothing.

    There is no chain behind this any more.  A direction of voice is either
    pointed at one engine or it is off, and "off" is the honest answer to a
    machine with nothing installed — the old chain tried five backends and,
    when none worked, could only report silence.
    """
    name = (provider or "").strip()
    return None if name.lower() in UNSET else name


@dataclass(frozen=True)
class AudioConfig:
    """The audio settings, as a plain object the package can read.

    It mirrors the ``audio`` settings block but does not import it, so the
    audio code stays testable without a whole ``Settings`` tree and the
    settings module never has to import the audio package back.
    """

    # -- speech to text
    stt_provider: str | None = None
    stt_command: str | None = None
    stt_model: str | None = None
    #: ``audio.stt.language`` — what SenseVoice is told to expect, and which
    #: zipformer the ``auto`` chain reaches for first.  Empty means detect.
    stt_language: str = "auto"
    # -- text to speech
    tts_enabled: bool = True
    tts_provider: str | None = None
    tts_command: str | None = None
    tts_model: str | None = None
    voice: str | None = None
    #: Voice per language, ``{"ko": "F2", "*": "M1"}``; see :mod:`.voices`.
    voices: dict[str, str] = field(default_factory=dict)
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
    def stt_pinned(self) -> str | None:
        """The transcription engine this configuration names, or ``None``."""
        return pinned(self.stt_provider)

    @property
    def tts_pinned(self) -> str | None:
        """The speech engine this configuration names, or ``None``."""
        return None if not self.tts_enabled else pinned(self.tts_provider)

    @property
    def stt_off(self) -> bool:
        return self.stt_pinned is None

    @property
    def tts_off(self) -> bool:
        return self.tts_pinned is None

    def stt(self) -> STTProvider | None:
        """The pinned transcription backend, when it is usable here."""
        name = self.stt_pinned
        if name is None:
            return None
        return resolve_provider(
            name,
            api_key=self.openai_api_key,
            model=self.stt_model,
            base_url=self.openai_base_url,
            command=self.stt_command,
            home=self.home,
            language=self.stt_language_for()[0],
        )

    def tts(self, caller: SpeechCaller | None = None) -> TTSProvider | None:
        """The speech backend this configuration resolves to.

        ``caller`` runs the studio MCP tool; without one the studio backend is
        still *reported* as available (so the wizard can say so) but refuses to
        synthesise.
        """
        name = self.tts_pinned
        if name is None:
            return None
        return tts.resolve_provider(
            name,
            caller=caller,
            studio_configured=self.studio_configured,
            api_key=self.openai_api_key,
            model=self.tts_model,
            base_url=self.openai_base_url,
            command=self.tts_command,
            home=self.home,
            language=self.stt_language,
        )


    def stt_any(self) -> STTProvider | None:
        """Whatever transcription backend exists here, for the media tool.

        ``transcribe_audio`` is a tool the model reaches for deliberately, so
        it uses what the machine has.  Voice input goes through :meth:`stt`,
        which only ever returns the engine the user pinned.
        """
        return stt.resolve_any(
            api_key=self.openai_api_key,
            model=self.stt_model,
            base_url=self.openai_base_url,
            command=self.stt_command,
            home=self.home,
            language=self.stt_language,
        )

    def tts_any(self, caller: SpeechCaller | None = None) -> TTSProvider | None:
        """Whatever speech backend exists here, for the media tool.

        ``text_to_speech`` is a tool the model reaches for deliberately, so it
        uses what the machine has.  Voice output goes through :meth:`tts`,
        which only ever returns the engine the user pinned.
        """
        return tts.resolve_any(
            caller=caller,
            studio_configured=self.studio_configured,
            api_key=self.openai_api_key,
            command=self.tts_command,
            home=self.home,
            language=self.stt_language,
        )


    def voice_for(self, language: str | None = None, engine: str | None = None) -> str | None:
        """The voice to speak ``language`` with, or ``None`` for the engine's own.

        The reply's own language first, then ``*``, then nothing — and nothing
        is a good answer: every engine has a default, and forcing one of its
        voices on a language it was not recorded for sounds worse than letting
        it choose.
        """
        from snowpea_core.audio import voices as voice_catalog

        table = voice_catalog.normalise(self.voices, self.voice)
        known = (
            voice_catalog.SUPERTONIC_LANGUAGES
            if (engine or self.tts_provider) == "supertonic"
            else ()
        )
        choice = voice_catalog.pick(table, language, known)
        if choice.note:
            # Once, at INFO: a language the engine cannot do is worth knowing
            # about, and worth knowing about only once.
            log.info("%s", choice.note)
        return choice.voice

    def stt_language_for(self, reply_language: str | None = None) -> tuple[str, str]:
        """``(language, source)`` for transcription: what to expect, and who said so.

        ``auto`` does not mean "no language". It means nobody forced one, so
        an engine that detects for itself does that, and one that cannot —
        a single-language Zipformer has one model per language — takes the
        language the session is replying in.
        """
        forced = (self.stt_language or "").strip()
        if forced and forced.lower() != "auto":
            return forced, "setting"
        tag = (reply_language or "").strip()
        if tag and tag.lower() != "auto":
            return tag, "reply"
        return "auto", "detect"


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

    # Two states, and the report says which: nothing pinned (voice off), or one
    # engine pinned that either works or does not.  There is no third answer
    # where something might be tried — that was the chain, and it is gone.
    listener = cfg.stt()
    stt_pin = cfg.stt_pinned
    if listener is None:
        if stt_pin is None:
            reasons["stt"] = NO_ENGINE_REASON
        else:
            # The engine may know exactly what is wrong — a Zipformer asked for
            # a language it does not speak can name the model that does.
            reasons["stt"] = _engine_reason(cfg, stt_pin) or MISSING_ENGINE_REASON.format(
                engine=stt_pin
            )

    speaker = cfg.tts(caller)
    tts_pin = cfg.tts_pinned
    if speaker is None:
        if not cfg.tts_enabled:
            reasons["tts"] = "text to speech is switched off (audio.tts.enabled)"
        else:
            reasons["tts"] = (
                NO_ENGINE_REASON
                if tts_pin is None
                else MISSING_ENGINE_REASON.format(engine=tts_pin)
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
        # What is actually pinned, and whether anything is: a surface can say
        # "Not set" rather than inventing a default it would never get.
        "sttLanguage": cfg.stt_language_for()[0],
        "sttLanguageSource": cfg.stt_language_for()[1],
        "sttPinned": stt_pin is not None,
        "ttsPinned": tts_pin is not None,
        "sttEffective": listener.name if listener is not None else None,
        "ttsEffective": speaker.name if speaker is not None else None,
        "voice": cfg.voice,
        "record": record_backend is not None,
        "play": play_backend is not None,
        "autoSpeak": bool(speaker is not None and cfg.auto_speak),
        "sttProviders": stt_providers(cfg),
        "ttsProviders": tts.available_providers(
            studio_configured=cfg.studio_configured,
            api_key=cfg.openai_api_key,
            command=cfg.tts_command,
            home=cfg.home,
            language=cfg.stt_language,
        ),
        "players": available_players(),
        "recorders": available_recorders(),
        "reasons": reasons,
    }


def _engine_reason(config: AudioConfig, engine: str) -> str:
    """The pinned engine's own account of why it cannot run, when it has one."""
    try:
        built = stt.build_provider(
            engine,
            api_key=config.openai_api_key,
            model=config.stt_model,
            base_url=config.openai_base_url,
            command=config.stt_command,
            home=config.home,
            language=config.stt_language_for()[0],
        )
    except AudioError:
        return ""
    reason = getattr(built, "missing_reason", None)
    return str(reason()) if callable(reason) else ""


def stt_providers(config: AudioConfig) -> list[str]:
    """Every transcription engine installed here, in recommendation order.

    This is *detection*, not resolution: it answers "what could you pick",
    which is what the wizard needs to draw its list and what the recommendation
    order is for.  Whether any of them is actually in use is
    :attr:`AudioConfig.stt_pinned`'s business.
    """
    found: list[str] = []
    for candidate in stt.RECOMMENDED_ORDER:
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
    "MISSING_ENGINE_REASON",
    "NO_ENGINE_REASON",
    "UNSET",
    "pinned",
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
