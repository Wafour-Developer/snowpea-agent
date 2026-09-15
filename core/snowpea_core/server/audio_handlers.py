"""RPC handlers for voice input and output (CORE-multimodal).

Five methods, all of them optional at the edges: ``audio.capabilities`` says
what this machine can do and why anything it cannot is off, and the other four
refuse with a named error rather than hanging or half-working.

The settings block is read defensively.  ``settings.audio`` does not exist as a
typed model yet — :class:`~snowpea_core.config.settings.Settings` allows extra
keys, so a hand-written ``"audio"`` object already works and this module keeps
reading it the same way once the model lands.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.attachments.model import decode_base64
from snowpea_core.audio import AudioConfig, capabilities
from snowpea_core.audio import install as audio_install
from snowpea_core.audio import tts as tts_backends
from snowpea_core.audio import voices as audio_voices
from snowpea_core.audio.player import AudioError
from snowpea_core.audio.player import play as play_audio
from snowpea_core.audio.recorder import Recorder
from snowpea_core.config.paths import utc_now
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    AudioCapabilitiesResult,
    AudioInstallParams,
    AudioInstallResult,
    AudioRecordResult,
    AudioRecordStartParams,
    AudioRecordStopParams,
    AudioSpeakParams,
    AudioSpeakResult,
    AudioTranscribeParams,
    AudioTranscribeResult,
    AudioVoice,
    AudioVoicesParams,
    AudioVoicesResult,
    Empty,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.tools import media as media_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.audio")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "audio.capabilities",
    "audio.transcribe",
    "audio.speak",
    "audio.record.start",
    "audio.record.stop",
    "audio.install",
    "audio.voices",
)

#: Notification carrying one line of an install's output to every surface.
INSTALL_PROGRESS = "audio.install.progress"

#: Audio error codes that mean "you asked for something impossible here".
_UNAVAILABLE = frozenset({"no_player", "no_recorder", "no_stt", "no_tts", "not_recording"})

#: One recorder per session; ``None`` is the key for a client with no session.
_RECORDERS: dict[str | None, Recorder] = {}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def _block(source: Any, name: str) -> Any:
    """``source.name`` or ``source["name"]``, whichever the object supports."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _get(source: Any, name: str, default: Any = None) -> Any:
    value = _block(source, name)
    return default if value is None else value


def audio_config(core: Core) -> AudioConfig:
    """Build an :class:`AudioConfig` from the daemon's settings.

    Reads ``settings.audio`` leniently so it works before the typed settings
    model exists, and borrows the OpenAI credentials the provider registry
    already resolved rather than asking the user for a second key.
    """
    block = _block(core.settings, "audio")
    stt = _block(block, "stt")
    tts = _block(block, "tts")
    registry = getattr(core, "providers", None)
    api_key = registry.api_key_for("openai") if registry is not None else None
    base_url = registry.base_url_for("openai") if registry is not None else None
    return AudioConfig(
        stt_provider=str(_get(stt, "provider", "auto")),
        stt_command=_block(stt, "command"),
        stt_model=_block(stt, "model"),
        stt_language=str(_get(stt, "language", "auto")),
        tts_enabled=bool(_get(tts, "enabled", True)),
        tts_command=_block(tts, "command"),
        tts_model=_block(tts, "model"),
        tts_provider=str(_get(tts, "provider", "auto")),
        voice=_block(tts, "voice"),
        voices=audio_voices.normalise(_block(tts, "voices"), _block(tts, "voice")),
        auto_speak=bool(_get(tts, "autoSpeak", False)),
        openai_api_key=api_key,
        openai_base_url=base_url,
        studio_configured=media_tools.configured(core),
        player=_block(block, "player"),
        recorder=_block(block, "recorder"),
        # The local engines look for their models under the daemon's own home.
        home=getattr(getattr(core, "paths", None), "home", None),
    )


def speech_caller(core: Core) -> tts_backends.SpeechCaller | None:
    """The callable that runs the studio ``text_to_speech`` MCP tool."""
    if not media_tools.configured(core):
        return None

    async def call(tool_name: str, args: dict[str, Any]) -> Any:
        from snowpea_core.tools import mcp_client

        entry = media_tools.server_config(core)
        if entry is None:  # pragma: no cover - configured() already checked
            raise AudioError("no_tts", "no snowpea-studio configuration")
        server = mcp_client.MANAGER.register(entry)
        return await server.call(media_tools.FORWARDS[tool_name], args)

    return call


def audio_dir_for(core: Core, session_id: str | None) -> Path:
    """Where this session's recordings and speech are written."""
    root = core.paths.audio_dir
    return root / session_id if session_id else root


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def _rpc_error(exc: AudioError) -> RpcError:
    """An :class:`AudioError` as the JSON-RPC error the client sees.

    The snowpea code stays in ``error.data.details`` so a client can branch on
    ``no_recorder`` versus ``record_failed`` without parsing English.
    """
    code = "invalid_params" if exc.code in _UNAVAILABLE else "internal"
    return RpcError(code, str(exc), {"audio": exc.code})


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def audio_capabilities_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> AudioCapabilitiesResult:
    """``audio.capabilities`` — what works here, and why anything does not."""
    report = capabilities(audio_config(core), caller=speech_caller(core))
    return AudioCapabilitiesResult.model_validate(report)


async def audio_transcribe_handler(
    _conn: RpcConnection, params: AudioTranscribeParams, core: Core
) -> AudioTranscribeResult:
    """``audio.transcribe`` — audio in, text out."""
    config = audio_config(core)
    provider = config.stt()
    if provider is None:
        reasons = capabilities(config, caller=speech_caller(core))["reasons"]
        raise RpcError(
            "invalid_params",
            str(reasons.get("stt", "no transcription backend is configured")),
            {"audio": "no_stt"},
        )
    path = _audio_input(core, params)
    try:
        transcript = await provider.transcribe(path, params.mime)
    except AudioError as exc:
        raise _rpc_error(exc) from exc
    return AudioTranscribeResult(text=transcript.text, provider=transcript.provider)


def _audio_input(core: Core, params: AudioTranscribeParams) -> Path:
    """The file to transcribe, writing inline audio to disk when needed."""
    if params.path:
        path = Path(params.path).expanduser()
        if not path.is_file():
            raise RpcError("invalid_params", f"no such audio file: {path}")
        return path
    if not params.data:
        raise RpcError("invalid_params", "audio.transcribe needs either path or data")
    directory = audio_dir_for(core, params.sessionId)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = tts_backends.extension_for(params.mime or "audio/wav")
    target = directory / f"upload-{utc_now().replace(':', '-')}{suffix}"
    try:
        target.write_bytes(decode_base64(params.data))
    except (OSError, ValueError) as exc:
        raise RpcError("invalid_params", f"audio.transcribe: {exc}") from exc
    return target


async def audio_speak_handler(
    _conn: RpcConnection, params: AudioSpeakParams, core: Core
) -> AudioSpeakResult:
    """``audio.speak`` — text in, an audio file out (played here on request)."""
    config = audio_config(core)
    caller = speech_caller(core)
    provider = config.tts(caller)
    if provider is None:
        reasons = capabilities(config, caller=caller)["reasons"]
        raise RpcError(
            "invalid_params",
            str(reasons.get("tts", "no speech backend is configured")),
            {"audio": "no_tts"},
        )
    try:
        # The caller's voice wins; otherwise the reply language picks one.
        language = config.stt_language_for()[0]
        speech = await provider.synthesize(
            params.text,
            out_dir=audio_dir_for(core, params.sessionId),
            voice=params.voice or config.voice_for(language),
        )
    except AudioError as exc:
        raise _rpc_error(exc) from exc
    played = False
    if params.play:
        try:
            await play_audio(speech.path, preferred=config.player)
            played = True
        except AudioError as exc:
            # The audio exists; failing to play it here is not failing the call.
            log.info("audio.speak could not play locally: %s", exc)
    return AudioSpeakResult(
        path=str(speech.path),
        mime=speech.mime,
        provider=speech.provider or (provider.name if provider else ""),
        voice=speech.voice,
        played=played,
    )


async def audio_voices_handler(
    _conn: RpcConnection, params: AudioVoicesParams, core: Core
) -> AudioVoicesResult:
    """``audio.voices`` — what one engine can speak with, here.

    An engine that is not installed still answers: a user choosing between
    engines wants to know what each *would* offer.  ``sample`` is what says
    whether a preview can actually be played.
    """
    report = capabilities(audio_config(core), caller=speech_caller(core))
    languages = tuple(params.languages) or _voice_languages(core)
    found = await audio_voices.voices_for(
        params.engine,
        home=core.paths.home,
        languages=languages,
        installed_engines=tuple(report["ttsProviders"]),
        openai_key=bool(audio_config(core).openai_api_key),
    )
    return AudioVoicesResult(
        voices=[AudioVoice(**voice.to_payload()) for voice in found]
    )


def _voice_languages(core: Core) -> tuple[str, ...]:
    """Korean and English, plus the configured reply language when it is another."""
    from snowpea_core.agent.agent import reply_language

    tags = list(audio_voices.DEFAULT_LANGUAGES)
    configured = (reply_language(core) or "").strip().lower().partition("-")[0]
    if configured and configured != "auto" and configured not in tags:
        tags.append(configured)
    return tuple(tags)


async def audio_install_handler(
    _conn: RpcConnection, params: AudioInstallParams, core: Core
) -> AudioInstallResult:
    """``audio.install`` — obtain one local voice engine, then re-detect.

    The output is streamed as :data:`INSTALL_PROGRESS` notifications to every
    attached surface while it runs, and returned in full as ``log`` when it is
    over, so a client that connected late still sees what happened.

    A failure is a result, never an exception: a system package answers
    ``ok=false`` with the command for this platform.  After a real install the
    engine is re-detected here, so ``audio.capabilities`` and ``setup.catalog``
    report it active without anyone restarting the daemon.
    """
    engine = audio_install.engine_for((params.engine or "").strip())
    voice = (params.voice or "").strip()
    # The pair identifies the row a surface draws: an engine install and one of
    # its voices are two jobs, and they must not share one progress line.
    label = {"engine": engine, "voice": voice} if voice else {"engine": engine}

    async def publish(payload: dict[str, Any]) -> None:
        try:
            await core.hub.notify(INSTALL_PROGRESS, payload)
        except Exception:  # noqa: BLE001 - a dead surface must not stop the install
            log.debug("could not publish %s", INSTALL_PROGRESS, exc_info=True)

    async def stages(event: Any) -> None:
        # One notification per event, not two: the stage payload already
        # carries the log line, so a client that only reads `line` is served
        # by the same message a client drawing a bar reads.
        await publish({**event.to_payload(), **label})

    if voice:
        result = await audio_install.install_voice(
            engine, voice, home=core.paths.home, stages=stages
        )
    else:
        result = await audio_install.install(engine, home=core.paths.home, stages=stages)
    if result.ok:
        _after_install(core, result)
    return AudioInstallResult(**result.to_payload())


def _after_install(core: Core, result: audio_install.InstallResult) -> None:
    """Record what the install produced and make detection see it.

    Detection is ``shutil.which`` behind an ``lru_cache`` in the standard
    library; clearing it is what makes a freshly installed binary visible to
    the very next ``audio.capabilities`` rather than after a restart.
    """
    import shutil as _shutil

    try:
        _shutil.which.cache_clear()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - which is not cached on this build
        pass
    voice = getattr(result, "voice_path", None)
    if voice is None:
        return
    # Piper needs a voice named before it can say anything, and this install is
    # the only moment we know which one landed.
    try:
        block = core.settings.audio if hasattr(core.settings, "audio") else None
        tts = _block(block, "tts")
        if isinstance(tts, dict):
            tts["voice"] = str(voice)
        elif tts is not None:
            tts.voice = str(voice)
        else:
            return
        core.settings.save(core.paths)
    except Exception:  # noqa: BLE001 - the engine is installed either way
        log.info("installed %s but could not record its voice in settings", result.engine)


async def audio_record_start_handler(
    _conn: RpcConnection, params: AudioRecordStartParams, core: Core
) -> AudioRecordResult:
    """``audio.record.start`` — begin capturing the microphone."""
    config = audio_config(core)
    recorder = _RECORDERS.get(params.sessionId)
    if recorder is None:
        recorder = Recorder(audio_dir_for(core, params.sessionId), preferred=config.recorder)
        _RECORDERS[params.sessionId] = recorder
    try:
        path = await recorder.start()
    except AudioError as exc:
        raise _rpc_error(exc) from exc
    return AudioRecordResult(path=str(path), recording=True)


async def audio_record_stop_handler(
    _conn: RpcConnection, params: AudioRecordStopParams, core: Core
) -> AudioRecordResult:
    """``audio.record.stop`` — finish the wav, optionally transcribing it."""
    recorder = _RECORDERS.get(params.sessionId)
    if recorder is None:
        raise RpcError("invalid_params", "no recording is running", {"audio": "not_recording"})
    try:
        path = await recorder.stop()
    except AudioError as exc:
        raise _rpc_error(exc) from exc
    result = AudioRecordResult(path=str(path), recording=False)
    if not params.transcribe:
        return result
    config = audio_config(core)
    provider = config.stt()
    if provider is None:
        # The recording is safe on disk; say so instead of throwing it away.
        log.info("audio.record.stop: nothing to transcribe with")
        return result
    try:
        transcript = await provider.transcribe(path, "audio/wav")
    except AudioError as exc:
        raise _rpc_error(exc) from exc
    return AudioRecordResult(
        path=str(path),
        recording=False,
        text=transcript.text,
        provider=transcript.provider,
    )


def register_audio_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("audio.capabilities", audio_capabilities_handler)
    dispatcher.register("audio.transcribe", audio_transcribe_handler)
    dispatcher.register("audio.speak", audio_speak_handler)
    dispatcher.register("audio.record.start", audio_record_start_handler)
    dispatcher.register("audio.record.stop", audio_record_stop_handler)
    dispatcher.register("audio.install", audio_install_handler)
    dispatcher.register("audio.voices", audio_voices_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "INSTALL_PROGRESS",
    "audio_capabilities_handler",
    "audio_install_handler",
    "audio_voices_handler",
    "audio_config",
    "audio_dir_for",
    "audio_record_start_handler",
    "audio_record_stop_handler",
    "audio_speak_handler",
    "audio_transcribe_handler",
    "register_audio_handlers",
    "speech_caller",
]
