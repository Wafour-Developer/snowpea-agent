"""Voices per language, and the language transcription expects.

Two settings that look small and are not. `audio.tts.voices` is a mapping
because one engine can and should sound like a different person in Korean than
in English. `audio.stt.language` is not a hint for every engine: a sherpa
Zipformer has one model per language, so the answer decides which model runs.

Nothing here touches the network or a real engine: the voice lists are either
static tables or injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowpea_core.audio import (
    MISSING_ENGINE_REASON,
    AudioConfig,
    capabilities,
)
from snowpea_core.audio import install as audio_install
from snowpea_core.audio import stt as stt_mod
from snowpea_core.audio import voices as voice_catalog
from snowpea_core.config.settings import Settings

# ---------------------------------------------------------------------------
# what each engine offers
# ---------------------------------------------------------------------------


def test_supertonic_is_one_multilingual_model_not_one_per_language() -> None:
    """Verified upstream: 31 languages out of a single ~400MB model."""
    found = voice_catalog.supertonic_voices(installed=True)
    assert [voice.id for voice in found] == [
        "M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5",
    ]
    # Every style works in every language, so none of them is language-tagged
    # and none of them is a separate download.
    assert {voice.language for voice in found} == {voice_catalog.ANY}
    assert all(voice.installed for voice in found)
    assert all(voice.size_bytes is None for voice in found)
    assert {"ko", "en", "ja"} <= set(voice_catalog.SUPERTONIC_LANGUAGES)


def test_a_piper_voice_is_a_download_and_says_so(tmp_path: Path) -> None:
    found = voice_catalog.piper_voices(tmp_path)
    assert {voice.language for voice in found} == {"en", "ko"}
    assert all(not voice.installed for voice in found)
    assert all(voice.size_bytes for voice in found)
    # It cannot be previewed until it is there.
    assert all(not voice.sample for voice in found)

    from snowpea_core.audio.install import VOICES_DIRNAME

    (tmp_path / VOICES_DIRNAME).mkdir(parents=True)
    (tmp_path / VOICES_DIRNAME / "ko_KR-kss-medium.onnx").write_bytes(b"x")
    korean = next(v for v in voice_catalog.piper_voices(tmp_path) if v.language == "ko")
    assert korean.installed is True and korean.sample is True and korean.size_bytes is None


def test_every_curated_piper_voice_has_a_path() -> None:
    for name, _language, path, _label in voice_catalog.PIPER_VOICES:
        assert voice_catalog.piper_voice_path(name) == path
    assert voice_catalog.piper_voice_path("nope") is None


def test_the_openai_voices_need_a_key_before_they_are_usable() -> None:
    without = voice_catalog.openai_voices(configured=False)
    assert [voice.id for voice in without] == list(voice_catalog.OPENAI_VOICES)
    assert all(not voice.installed for voice in without)
    assert all(voice.installed for voice in voice_catalog.openai_voices(configured=True))


async def test_an_engine_with_no_voice_list_answers_empty(tmp_path: Path) -> None:
    """A custom command's voices are whatever its template does; not ours to guess."""
    assert await voice_catalog.voices_for("command", home=tmp_path) == []
    assert await voice_catalog.voices_for("nonsense", home=tmp_path) == []


async def test_installed_voices_are_listed_first(tmp_path: Path) -> None:
    from snowpea_core.audio.install import VOICES_DIRNAME

    (tmp_path / VOICES_DIRNAME).mkdir(parents=True)
    (tmp_path / VOICES_DIRNAME / "ko_KR-kss-medium.onnx").write_bytes(b"x")
    found = await voice_catalog.voices_for("piper", home=tmp_path)
    assert found[0].installed is True


# ---------------------------------------------------------------------------
# which voice speaks which language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "language", "expected", "source"),
    [
        ({"ko": "F2", "*": "M1"}, "ko", "F2", "language"),
        ({"ko": "F2", "*": "M1"}, "ko-KR", "F2", "language"),
        ({"ko": "F2", "*": "M1"}, "en", "M1", "any"),
        ({"*": "M1"}, "ko", "M1", "any"),
        ({}, "ko", None, "engine"),
        ({"ko": "F2"}, "en", None, "engine"),
        ({"ko": "F1"}, "auto", None, "engine"),
        ({"ko": "F1"}, None, None, "engine"),
    ],
)
def test_the_reply_language_picks_the_voice(
    table: dict[str, str], language: str, expected: str | None, source: str
) -> None:
    choice = voice_catalog.pick(table, language)
    assert choice.voice == expected
    assert choice.source == source


def test_a_language_the_engine_cannot_speak_is_noted_once() -> None:
    choice = voice_catalog.pick({"*": "M1"}, "sw", known=("en", "ko"))
    assert choice.voice == "M1", "it still speaks, with the default voice"
    assert "sw" in choice.note


def test_the_legacy_single_voice_reads_as_the_any_entry() -> None:
    """A settings file written before this was a mapping still works."""
    assert voice_catalog.normalise(None, legacy="M3") == {"*": "M3"}
    # An explicit mapping wins; the legacy field only fills a gap.
    assert voice_catalog.normalise({"*": "F1"}, legacy="M3") == {"*": "F1"}
    assert voice_catalog.normalise({"ko": "F1"}, legacy="M3") == {"ko": "F1", "*": "M3"}
    # Keys are normalised and empties dropped.
    assert voice_catalog.normalise({"KO": "F1", "en": "", "": "x"}) == {"ko": "F1"}


def test_settings_carry_the_mapping_and_the_legacy_field() -> None:
    settings = Settings.model_validate({"audio": {"tts": {"voice": "M3"}}})
    assert settings.audio.tts.voices == {}
    config = AudioConfig(voice=settings.audio.tts.voice, voices=settings.audio.tts.voices)
    assert config.voice_for("ko") == "M3"

    config = AudioConfig(voices={"ko": "F2", "*": "M1"})
    assert config.voice_for("ko") == "F2"
    assert config.voice_for("en") == "M1"
    assert config.voice_for(None) == "M1"


def test_grouping_offers_a_universal_voice_under_every_language() -> None:
    found = [
        voice_catalog.Voice(id="M1", label="M1"),
        voice_catalog.Voice(id="ko-KR-A", label="A", language="ko"),
        voice_catalog.Voice(id="en-US-B", label="B", language="en"),
    ]
    grouped = voice_catalog.by_language(found)
    assert set(grouped) == {"ko", "en", voice_catalog.ANY}
    assert "M1" in {voice.id for voice in grouped["ko"]}
    assert "M1" in {voice.id for voice in grouped["en"]}
    assert {voice.id for voice in grouped[voice_catalog.ANY]} == {"M1"}


# ---------------------------------------------------------------------------
# the transcription language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "reply", "expected", "source"),
    [
        ("auto", "ko", "ko", "reply"),
        ("auto", None, "auto", "detect"),
        ("auto", "auto", "auto", "detect"),
        ("ko", "en", "ko", "setting"),
        ("", "en", "en", "reply"),
    ],
)
def test_who_decides_the_transcription_language(
    setting: str, reply: str | None, expected: str, source: str
) -> None:
    """`auto` does not mean "no language": it means nobody forced one."""
    config = AudioConfig(stt_language=setting)
    assert config.stt_language_for(reply) == (expected, source)


def test_auto_is_never_passed_to_an_engine_as_a_language() -> None:
    """whisper would go looking for a language called "auto"."""
    assert stt_mod.build_provider("local-whisper", language="auto").language is None
    assert stt_mod.build_provider("local-whisper", language="ko").language == "ko"
    assert stt_mod.build_provider("openai", api_key="k", language="auto").language is None
    assert stt_mod.build_provider("openai", api_key="k", language="ko").language == "ko"


def test_the_language_picks_the_model_for_a_single_language_engine() -> None:
    assert stt_mod.MODEL_BY_LANGUAGE == {
        "ko": "sherpa-onnx-zipformer-ko",
        "en": "sherpa-onnx-zipformer-en",
    }


def test_a_zipformer_asked_for_the_wrong_language_says_which_model_to_install(
    tmp_path: Path,
) -> None:
    """Decoding Korean with an English model would report confident nonsense."""
    engine = stt_mod.SherpaOnnxSTT("sherpa-onnx-zipformer-en", home=tmp_path, language="ko")
    assert engine.wrong_language is True
    assert engine.available() is False
    reason = engine.missing_reason()
    assert "does not speak ko" in reason
    assert "sherpa-onnx-zipformer-ko" in reason


def test_a_multilingual_model_is_never_the_wrong_language(tmp_path: Path) -> None:
    for language in ("ko", "en", "ja", "auto"):
        engine = stt_mod.SherpaOnnxSTT(
            "sherpa-onnx-sensevoice", home=tmp_path, language=language
        )
        assert engine.wrong_language is False, language


def test_capabilities_reports_the_language_and_who_decided_it() -> None:
    report = capabilities(AudioConfig())
    assert report["sttLanguage"] == "auto"
    assert report["sttLanguageSource"] == "detect"

    forced = capabilities(AudioConfig(stt_language="ko"))
    assert forced["sttLanguage"] == "ko"
    assert forced["sttLanguageSource"] == "setting"


def test_the_capability_reason_names_the_model_a_language_needs(tmp_path: Path) -> None:
    report = capabilities(
        AudioConfig(
            stt_provider="sherpa-onnx-zipformer-en", stt_language="ko", home=tmp_path
        )
    )
    assert "sherpa-onnx-zipformer-ko" in report["reasons"]["stt"]
    # A plain missing engine still gets the plain message.
    plain = capabilities(AudioConfig(stt_provider="local-whisper"))
    assert plain["reasons"]["stt"] == MISSING_ENGINE_REASON.format(engine="local-whisper")


# ---------------------------------------------------------------------------
# installing a voice
# ---------------------------------------------------------------------------


class VoiceFetcher:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.urls: list[str] = []

    def __call__(self) -> VoiceFetcher:
        return self

    async def __aenter__(self) -> VoiceFetcher:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, url: str) -> Any:
        self.urls.append(url)
        outer = self

        class Response:
            status_code = outer.status_code
            content = b"voice-bytes"

        return Response()


async def test_installing_a_piper_voice_fetches_both_of_its_files(tmp_path: Path) -> None:
    fetcher = VoiceFetcher()
    result = await audio_install.install_voice(
        "piper", "ko_KR-kss-medium", home=tmp_path, fetch=fetcher
    )
    assert result.ok is True
    assert len(fetcher.urls) == 2
    assert all("ko/ko_KR/kss/medium" in url for url in fetcher.urls)
    from snowpea_core.audio.install import VOICES_DIRNAME

    assert (tmp_path / VOICES_DIRNAME / "ko_KR-kss-medium.onnx").is_file()


async def test_installing_a_voice_does_not_select_it(tmp_path: Path) -> None:
    """Install and configure are separate steps, for voices as for engines."""
    result = await audio_install.install_voice(
        "piper", "ko_KR-kss-medium", home=tmp_path, fetch=VoiceFetcher()
    )
    assert result.ok is True
    # Nothing on the result says "and this is now your voice".
    assert not hasattr(result, "selected")
    assert result.to_payload().keys() <= {"ok", "engine", "log", "hint", "voice"}
    # `engine` stays the bare id and `voice` names the voice, so a surface can
    # key its progress row on the pair.
    assert result.engine == "piper"
    assert result.voice == "ko_KR-kss-medium"


async def test_a_voice_that_is_not_a_download_is_explained(tmp_path: Path) -> None:
    result = await audio_install.install_voice("supertonic", "M1", home=tmp_path)
    assert result.ok is False
    assert result.hint is not None and "not downloads" in result.hint


async def test_an_unknown_piper_voice_lists_the_known_ones(tmp_path: Path) -> None:
    result = await audio_install.install_voice("piper", "ko_KR-nope", home=tmp_path)
    assert result.ok is False
    assert result.hint is not None
    assert "ko_KR-kss-medium" in result.hint


async def test_a_voice_install_reports_its_stages(tmp_path: Path) -> None:
    seen: list[Any] = []

    async def stages(event: Any) -> None:
        seen.append(event)

    await audio_install.install_voice(
        "piper", "en_US-lessac-medium", home=tmp_path, stages=stages, fetch=VoiceFetcher()
    )
    names: list[str] = []
    for event in seen:
        if event.stage not in names:
            names.append(event.stage)
    assert names == ["resolve", "download", "check"]
    assert all(event.steps == 3 for event in seen)


# ---------------------------------------------------------------------------
# through the RPC
# ---------------------------------------------------------------------------


class _Paths:
    def __init__(self, home: Path) -> None:
        self.home = home


class _Core:
    def __init__(self, home: Path) -> None:
        self.paths = _Paths(home)
        self.settings = Settings()
        self.providers = None
        self.tools = None


async def test_audio_voices_lists_an_engine_that_is_not_installed(tmp_path: Path) -> None:
    """Choosing between engines means seeing what each would offer."""
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioVoicesParams

    result = await audio_handlers.audio_voices_handler(
        None, AudioVoicesParams(engine="supertonic"), _Core(tmp_path)  # type: ignore[arg-type]
    )
    assert [voice.id for voice in result.voices][:3] == ["F1", "F2", "F3"]
    assert all(voice.language == "*" for voice in result.voices)
    # Not installed here, so it is listed but cannot be previewed.
    assert all(not voice.installed for voice in result.voices)


async def test_audio_voices_marks_a_downloaded_piper_voice(tmp_path: Path) -> None:
    from snowpea_core.audio.install import VOICES_DIRNAME
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioVoicesParams

    (tmp_path / VOICES_DIRNAME).mkdir(parents=True)
    (tmp_path / VOICES_DIRNAME / "ko_KR-kss-medium.onnx").write_bytes(b"x")

    result = await audio_handlers.audio_voices_handler(
        None, AudioVoicesParams(engine="piper"), _Core(tmp_path)  # type: ignore[arg-type]
    )
    rows = {voice.id: voice for voice in result.voices}
    assert rows["ko_KR-kss-medium"].installed is True
    assert rows["ko_KR-kss-medium"].sizeBytes is None
    assert rows["en_US-lessac-medium"].installed is False
    assert rows["en_US-lessac-medium"].sizeBytes
    # Installed first, so the useful row is the one already under the cursor.
    assert result.voices[0].id == "ko_KR-kss-medium"


async def test_audio_voices_answers_an_engine_it_has_no_list_for(tmp_path: Path) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioVoicesParams

    result = await audio_handlers.audio_voices_handler(
        None, AudioVoicesParams(engine="command"), _Core(tmp_path)  # type: ignore[arg-type]
    )
    assert result.voices == []


async def test_a_voice_install_is_labelled_with_the_engine_and_the_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surface keys its progress row on the pair, so both have to be there."""
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    sent: list[dict[str, Any]] = []

    class Hub:
        async def notify(self, _method: str, params: dict[str, Any], **_kw: Any) -> None:
            sent.append(params)

    class Paths:
        home = tmp_path

    class Core:
        hub = Hub()
        paths = Paths()
        settings = Settings()

    real = audio_install.install_voice

    async def fake_install(engine: str, voice: str, **kwargs: Any) -> Any:
        kwargs.setdefault("fetch", VoiceFetcher())
        return await real(engine, voice, **kwargs)

    monkeypatch.setattr(audio_handlers.audio_install, "install_voice", fake_install)

    result = await audio_handlers.audio_install_handler(
        None,  # type: ignore[arg-type]
        AudioInstallParams(engine="piper", voice="ko_KR-kss-medium"),
        Core(),  # type: ignore[arg-type]
    )

    assert result.ok is True
    assert result.engine == "piper", "the engine id stays bare"
    assert result.voice == "ko_KR-kss-medium"
    assert sent, "the install reported nothing"
    assert all(event["engine"] == "piper" for event in sent)
    assert all(event["voice"] == "ko_KR-kss-medium" for event in sent)
    # And the stages are still there, so the bar still moves.
    assert {event["stage"] for event in sent} == {"resolve", "download", "check"}


async def test_an_engine_install_carries_no_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    sent: list[dict[str, Any]] = []

    class Hub:
        async def notify(self, _method: str, params: dict[str, Any], **_kw: Any) -> None:
            sent.append(params)

    class Paths:
        home = tmp_path

    class Core:
        hub = Hub()
        paths = Paths()
        settings = Settings()

    real = audio_install.install

    async def fake_install(engine: str, **kwargs: Any) -> Any:
        async def runner(argv: Any, progress: Any = None) -> int:
            return 0

        kwargs.setdefault("runner", runner)
        return await real(engine, **kwargs)

    monkeypatch.setattr(audio_handlers.audio_install, "install", fake_install)

    result = await audio_handlers.audio_install_handler(
        None, AudioInstallParams(engine="edge-tts"), Core()  # type: ignore[arg-type]
    )

    assert result.ok is True and result.voice is None
    assert sent and all("voice" not in event for event in sent)
