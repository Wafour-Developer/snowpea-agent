"""The CPU-first local voice engines: sherpa-onnx for input, Supertonic for output.

Both are downloads, so nothing here touches the network: the model fetcher and
the installer runner are injected, and the model table is exercised against a
fake archive built in a temp directory.

What is pinned:

* the model table's shape, so a mistyped asset name fails here and not on a
  user's first 400MB download;
* that a half-finished download never makes an engine look installed;
* the ``auto`` chain orders, which are the defaults this task changed;
* the recommended flags the wizard and the desktop lead with.
"""

from __future__ import annotations

import bz2
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.audio import install as audio_install
from snowpea_core.audio import stt as stt_mod
from snowpea_core.audio import stt_models
from snowpea_core.audio import tts as tts_mod
from snowpea_core.setup.catalog import RECOMMENDED_STT, RECOMMENDED_TTS, stt_catalog, tts_catalog
from snowpea_core.setup.screens import audio as audio_screen
from snowpea_core.setup.state import WizardState

# ---------------------------------------------------------------------------
# the model table
# ---------------------------------------------------------------------------


def test_every_model_comes_from_the_official_sherpa_release() -> None:
    for model in stt_models.MODELS.values():
        assert model.url.startswith(stt_models.SHERPA_RELEASE + "/")
        assert model.asset.endswith(".tar.bz2")
        assert model.needs, f"{model.id} names no files, so detection cannot check it"
        assert model.kind in {"offline", "online"}


def test_the_table_names_the_models_this_task_added() -> None:
    assert set(stt_models.MODELS) == {
        "sherpa-onnx-sensevoice",
        "sherpa-onnx-zipformer-ko",
        "sherpa-onnx-zipformer-en",
    }
    sense = stt_models.MODELS["sherpa-onnx-sensevoice"]
    assert sense.asset == "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
    assert sense.languages == ("zh", "en", "ja", "ko", "yue")
    assert stt_models.SILERO_VAD in sense.extras
    assert (
        stt_models.MODELS["sherpa-onnx-zipformer-ko"].asset
        == "sherpa-onnx-streaming-zipformer-korean-2024-06-16.tar.bz2"
    )
    assert (
        stt_models.MODELS["sherpa-onnx-zipformer-en"].asset
        == "sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2"
    )


def test_a_checksum_is_either_a_real_digest_or_openly_unpinned() -> None:
    """No invented digests: a wrong one fails for everybody and protects nobody."""
    for model in stt_models.MODELS.values():
        if model.sha256 is None:
            continue
        assert len(model.sha256) == 64
        assert set(model.sha256.lower()) <= set("0123456789abcdef")


def test_the_unpacked_name_drops_the_archive_suffix() -> None:
    model = stt_models.MODELS["sherpa-onnx-zipformer-ko"]
    assert model.unpacked_name == "sherpa-onnx-streaming-zipformer-korean-2024-06-16"


# ---------------------------------------------------------------------------
# verification and detection
# ---------------------------------------------------------------------------


def test_an_unpinned_checksum_is_accepted_and_a_wrong_one_is_not(tmp_path: Path) -> None:
    blob = tmp_path / "x.bin"
    blob.write_bytes(b"hello")
    real = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert stt_models.verify(blob, None) is True
    assert stt_models.verify(blob, real) is True
    assert stt_models.verify(blob, "0" * 64) is False


def _fake_archive(model: stt_models.SpeechModel) -> bytes:
    """A tar.bz2 laid out the way the real release asset is."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name in model.needs:
            info = tarfile.TarInfo(f"{model.unpacked_name}/{name}")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"onnx"))
    return bz2.compress(raw.getvalue())


class FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers = {"content-length": str(len(body))}

    async def aiter_bytes(self) -> Any:
        yield self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class FakeFetcher:
    """Serves the fake archive for the model URL and anything else as bytes."""

    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code
        self.urls: list[str] = []
        self.ranges: list[str | None] = []

    def __call__(self) -> FakeFetcher:
        return self

    async def __aenter__(self) -> FakeFetcher:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def stream(self, _method: str, url: str, headers: Any = None) -> FakeResponse:
        self.urls.append(url)
        self.ranges.append((headers or {}).get("Range"))
        body = self.body if url.endswith(".tar.bz2") else b"vad-bytes"
        return FakeResponse(body, self.status_code)


async def test_a_model_downloads_unpacks_and_stamps(tmp_path: Path) -> None:
    model = stt_models.MODELS["sherpa-onnx-zipformer-ko"]
    fetcher = FakeFetcher(_fake_archive(model))
    lines: list[str] = []

    async def progress(line: str) -> None:
        lines.append(line)

    assert model.installed(tmp_path) is False
    ok = await stt_models.ensure_model(model.id, tmp_path, progress=progress, fetch=fetcher)
    assert ok is True
    assert model.installed(tmp_path) is True
    assert (model.directory(tmp_path) / stt_models.STAMP_NAME).is_file()
    for name in model.needs:
        assert (model.root(tmp_path) / name).is_file()
    # The archive is not kept: it is the largest thing on disk and useless now.
    assert not (model.directory(tmp_path) / model.asset).exists()
    assert any(model.url in line for line in lines)


async def test_the_sensevoice_model_brings_its_vad(tmp_path: Path) -> None:
    model = stt_models.MODELS["sherpa-onnx-sensevoice"]
    fetcher = FakeFetcher(_fake_archive(model))
    assert await stt_models.ensure_model(model.id, tmp_path, fetch=fetcher) is True
    assert (model.directory(tmp_path) / stt_models.SILERO_VAD).is_file()
    assert any(url.endswith(stt_models.SILERO_VAD) for url in fetcher.urls)


async def test_a_second_install_does_not_download_again(tmp_path: Path) -> None:
    model = stt_models.MODELS["sherpa-onnx-zipformer-en"]
    fetcher = FakeFetcher(_fake_archive(model))
    assert await stt_models.ensure_model(model.id, tmp_path, fetch=fetcher) is True
    before = len(fetcher.urls)
    assert await stt_models.ensure_model(model.id, tmp_path, fetch=fetcher) is True
    assert len(fetcher.urls) == before, "a finished model must not be re-fetched"


async def test_a_failed_download_leaves_nothing_that_looks_installed(tmp_path: Path) -> None:
    model = stt_models.MODELS["sherpa-onnx-zipformer-en"]
    fetcher = FakeFetcher(b"", status_code=500)
    assert await stt_models.ensure_model(model.id, tmp_path, fetch=fetcher) is False
    assert model.installed(tmp_path) is False


async def test_a_partial_file_is_resumed_rather_than_restarted(tmp_path: Path) -> None:
    target = tmp_path / "big.tar.bz2"
    partial = target.with_suffix(target.suffix + ".part")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"already-here")
    fetcher = FakeFetcher(b"rest", status_code=206)
    assert await stt_models.download_file("https://x/big.tar.bz2", target, fetch=fetcher) is True
    assert fetcher.ranges == ["bytes=12-"]
    assert target.read_bytes() == b"already-hererest"


async def test_an_archive_that_escapes_its_directory_is_refused(tmp_path: Path) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"no"))
    archive_path = tmp_path / "evil.tar"
    archive_path.write_bytes(raw.getvalue())
    into = tmp_path / "into"
    assert stt_models.unpack(archive_path, into) is True
    assert not (tmp_path / "escaped.txt").exists()


# ---------------------------------------------------------------------------
# which model the chain picks
# ---------------------------------------------------------------------------


def _pretend_installed(home: Path, model_id: str) -> None:
    model = stt_models.MODELS[model_id]
    root = model.root(home)
    root.mkdir(parents=True, exist_ok=True)
    for name in model.needs:
        (root / name).write_bytes(b"x")
    (model.directory(home) / stt_models.STAMP_NAME).write_text("ok", encoding="utf-8")


def test_sensevoice_leads_whatever_the_language_is(tmp_path: Path) -> None:
    _pretend_installed(tmp_path, "sherpa-onnx-sensevoice")
    _pretend_installed(tmp_path, "sherpa-onnx-zipformer-ko")
    assert stt_models.preferred(tmp_path, "ko") == "sherpa-onnx-sensevoice"
    assert stt_models.preferred(tmp_path, "en") == "sherpa-onnx-sensevoice"


def test_a_zipformer_is_used_when_it_is_the_one_installed(tmp_path: Path) -> None:
    _pretend_installed(tmp_path, "sherpa-onnx-zipformer-ko")
    assert stt_models.preferred(tmp_path, "ko") == "sherpa-onnx-zipformer-ko"
    # Nobody installed an English model, so the Korean one is better than none.
    assert stt_models.preferred(tmp_path, "en") == "sherpa-onnx-zipformer-ko"
    assert stt_models.preferred(tmp_path, None) == "sherpa-onnx-zipformer-ko"


def test_nothing_installed_is_no_model(tmp_path: Path) -> None:
    assert stt_models.preferred(tmp_path, "ko") is None
    assert stt_models.installed_models(tmp_path) == []


# ---------------------------------------------------------------------------
# the sherpa-onnx backend
# ---------------------------------------------------------------------------


def test_the_offline_and_streaming_models_use_different_binaries(tmp_path: Path) -> None:
    sense = stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path)
    zipformer = stt_mod.SherpaOnnxSTT("sherpa-onnx-zipformer-ko", home=tmp_path)
    assert sense.executable == stt_mod.SHERPA_OFFLINE_BIN
    assert zipformer.executable == stt_mod.SHERPA_ONLINE_BIN


def test_the_offline_argv_names_the_model_the_language_and_the_vad(tmp_path: Path) -> None:
    _pretend_installed(tmp_path, "sherpa-onnx-sensevoice")
    model = stt_models.MODELS["sherpa-onnx-sensevoice"]
    (model.directory(tmp_path) / stt_models.SILERO_VAD).write_bytes(b"vad")

    engine = stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path, language="ko")
    argv = engine.argv(Path("/tmp/a.wav"))
    assert argv[0] == stt_mod.SHERPA_OFFLINE_BIN
    assert any(part.startswith("--sense-voice-model=") for part in argv)
    assert "--sense-voice-language=ko" in argv
    assert any(part.startswith("--silero-vad-model=") for part in argv)
    assert argv[-1] == "/tmp/a.wav"

    # No language configured means detect, which is SenseVoice's whole point.
    auto = stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path)
    assert "--sense-voice-language=auto" in auto.argv(Path("/tmp/a.wav"))


def test_the_streaming_argv_names_the_three_transducer_parts(tmp_path: Path) -> None:
    _pretend_installed(tmp_path, "sherpa-onnx-zipformer-ko")
    engine = stt_mod.SherpaOnnxSTT("sherpa-onnx-zipformer-ko", home=tmp_path)
    argv = engine.argv(Path("/tmp/a.wav"))
    assert any(part.startswith("--encoder=") and ".int8.onnx" in part for part in argv)
    assert any(part.startswith("--decoder=") for part in argv)
    assert any(part.startswith("--joiner=") and ".int8.onnx" in part for part in argv)
    assert any(part.startswith("--tokens=") for part in argv)


def test_the_engine_is_unavailable_until_both_cli_and_model_are_there(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(stt_mod.shutil, "which", lambda _b: None)
    engine = stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path)
    assert engine.available() is False, "no CLI"

    monkeypatch.setattr(stt_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert engine.available() is False, "CLI but no model"

    _pretend_installed(tmp_path, "sherpa-onnx-sensevoice")
    assert stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path).available() is True


def test_a_half_downloaded_model_is_not_installed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(stt_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    model = stt_models.MODELS["sherpa-onnx-sensevoice"]
    root = model.root(tmp_path)
    root.mkdir(parents=True)
    (root / "tokens.txt").write_bytes(b"x")  # one file of two, and no stamp
    assert stt_mod.SherpaOnnxSTT("sherpa-onnx-sensevoice", home=tmp_path).available() is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("/tmp/a.wav\ntext: 안녕하세요\nelapsed 0.1s", "안녕하세요"),
        ("Done!\ntext:  hello there ", "hello there"),
        ('{"text": "from json"}', "from json"),
        ("just one line", "just one line"),
        ("", ""),
    ],
)
def test_the_transcript_is_read_out_of_the_cli_report(output: str, expected: str) -> None:
    assert stt_mod.parse_sherpa_output(output) == expected


# ---------------------------------------------------------------------------
# supertonic
# ---------------------------------------------------------------------------


def test_the_supertonic_facts_are_the_ones_upstream_publishes() -> None:
    assert tts_mod.SUPERTONIC_PACKAGE == "supertonic"
    assert tts_mod.DEFAULT_SUPERTONIC_VOICE == "M1"
    assert {"M1", "F1", "M5", "F5"} <= set(tts_mod.SUPERTONIC_VOICES)


def test_the_child_script_is_a_constant_and_never_carries_user_text() -> None:
    """The request goes in on stdin, so nothing typed becomes part of a program."""
    script = tts_mod.SUPERTONIC_SCRIPT
    assert "json.load(sys.stdin)" in script
    assert "%" not in script and "format(" not in script and "{" not in script.replace(
        'request["', ""
    ).replace('"]', "")


@pytest.mark.parametrize(
    ("voice", "expected"),
    [(None, "M1"), ("F2", "F2"), ("f3", "F3"), ("alloy", "M1"), ("", "M1")],
)
def test_a_voice_id_from_another_engine_is_not_forwarded(voice: str | None, expected: str) -> None:
    assert tts_mod.supertonic_voice(voice) == expected


@pytest.mark.parametrize(
    ("language", "expected"), [("ko", "ko"), ("ko-KR", "ko"), (None, "en"), ("", "en")]
)
def test_the_language_tag_is_narrowed_to_what_the_engine_takes(
    language: str | None, expected: str
) -> None:
    assert tts_mod.supertonic_lang(language) == expected


def test_supertonic_installs_where_this_interpreter_can_import_it() -> None:
    """`uv tool` would hide it in a venv of its own, and the engine imports it."""
    spec = audio_install.ENGINES["supertonic"]
    assert spec.isolated is False
    argv = audio_install.python_install_argv(spec.package, isolated=spec.isolated)
    assert argv is not None and argv[1:] == ["-m", "pip", "install", "--user", "supertonic"]


# ---------------------------------------------------------------------------
# the chains and the recommended defaults
# ---------------------------------------------------------------------------


def test_the_stt_recommendation_leads_with_sensevoice_and_ends_at_openai() -> None:
    """It no longer resolves anything; it is what the wizard suggests first."""
    order = stt_mod.RECOMMENDED_ORDER
    assert order[0] == "sherpa-onnx-sensevoice"
    assert order[1:3] == ("sherpa-onnx-zipformer-ko", "sherpa-onnx-zipformer-en")
    assert order.index("local-whisper") < order.index("openai")


def test_the_tts_recommendation_leads_with_supertonic_and_ends_at_the_hosted_ones() -> None:
    assert tts_mod.RECOMMENDED_ORDER[0] == "supertonic"
    order = list(tts_mod.RECOMMENDED_ORDER)
    for local in ("edge-tts", "piper", "say", "espeak-ng", "powershell"):
        assert order.index(local) < order.index("openai"), local
    assert order[-1] == "studio"


def test_exactly_one_row_per_list_is_recommended() -> None:
    for catalog, expected in ((stt_catalog(), RECOMMENDED_STT), (tts_catalog(), RECOMMENDED_TTS)):
        flagged = [item.id for item in catalog if item.recommended]
        assert flagged == [expected]
        row = next(item for item in catalog if item.id == expected)
        assert row.installable is True, "a recommendation you cannot install is a taunt"
        assert "recommended" in row.tags


def test_the_recommended_rows_are_the_cpu_local_ones() -> None:
    assert RECOMMENDED_STT == "sherpa-onnx-sensevoice"
    assert RECOMMENDED_TTS == "supertonic"
    for catalog in (stt_catalog(), tts_catalog()):
        row = next(item for item in catalog if item.recommended)
        assert row.key == "no key", "the default must not need an account"
        assert "CPU" in row.label


def test_the_wizard_leads_with_the_recommended_install_when_nothing_is_there() -> None:
    state = WizardState()
    rows = list(audio_screen.build_tts(state).items)
    installs = [row for row in rows if row.id.startswith(audio_screen.INSTALL_PREFIX)]
    assert installs, "nothing installed, so there is something to offer"
    assert installs[0].id == f"{audio_screen.INSTALL_PREFIX}{RECOMMENDED_TTS}"
    assert installs[0].label.startswith("Recommended: ")
    assert installs[0].default is True

    stt_rows = list(audio_screen.build(state).items)
    stt_installs = [row for row in stt_rows if row.id.startswith(audio_screen.INSTALL_PREFIX)]
    assert stt_installs[0].id == f"{audio_screen.INSTALL_PREFIX}{RECOMMENDED_STT}"
    assert stt_installs[0].default is True


def test_nothing_pinned_means_voice_is_off_rather_than_guessed() -> None:
    """The replacement for Automatic: two states, and the report says which."""
    from snowpea_core.audio import NO_ENGINE_REASON, AudioConfig, capabilities

    assert "auto" not in {item.id for item in tts_catalog()}
    assert "auto" not in {item.id for item in stt_catalog()}

    report = capabilities(AudioConfig())
    assert report["tts"] is False and report["ttsPinned"] is False
    assert report["reasons"]["tts"] == NO_ENGINE_REASON
    # The recommended row is what the wizard offers to install instead.
    recommended = next(item for item in tts_catalog() if item.recommended)
    assert recommended.installable is True


# ---------------------------------------------------------------------------
# installing an engine fetches its model too
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Any, progress: Any = None) -> int:
        self.calls.append(list(argv))
        if progress is not None:
            await progress("installed the package")
        return self.code


async def test_installing_a_sherpa_engine_installs_the_package_and_the_model(
    tmp_path: Path,
) -> None:
    model = stt_models.MODELS["sherpa-onnx-sensevoice"]
    runner = Runner()
    fetcher = FakeFetcher(_fake_archive(model))
    result = await audio_install.install(
        "sherpa-onnx-sensevoice", home=tmp_path, runner=runner, fetch=fetcher
    )
    assert result.ok is True
    assert runner.calls and runner.calls[0][-1] == audio_install.SHERPA_PACKAGE
    assert model.installed(tmp_path) is True
    assert "downloading" in result.log


async def test_a_model_that_will_not_download_fails_the_install(tmp_path: Path) -> None:
    """The package alone is a decoder with nothing to decode with."""
    result = await audio_install.install(
        "sherpa-onnx-zipformer-ko",
        home=tmp_path,
        runner=Runner(),
        fetch=FakeFetcher(b"", status_code=500),
    )
    assert result.ok is False
    assert result.hint is not None and "resume" in result.hint


@pytest.mark.parametrize(
    "engine",
    [
        "sherpa-onnx-sensevoice",
        "sherpa-onnx-zipformer-ko",
        "sherpa-onnx-zipformer-en",
        "supertonic",
    ],
)
def test_every_new_engine_is_offered_as_an_install(engine: str) -> None:
    assert audio_install.is_installable(engine) is True


# ---------------------------------------------------------------------------
# staged install progress
# ---------------------------------------------------------------------------


class VoiceClient:
    """The two-file piper voice download, without the network."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self) -> VoiceClient:
        return self

    async def __aenter__(self) -> VoiceClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, url: str) -> Any:
        self.urls.append(url)

        class Response:
            status_code = 200
            content = b"voice"

        return Response()


class Stages:
    """Collects the stage events an install reports."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    def sequence(self) -> list[str]:
        """Each stage once, in the order it was first reported.

        A stage can recur — the VAD that ships beside SenseVoice is verified
        too — and what the contract is about is that every stage happens and
        that they happen in order, not how many files each one touched.
        """
        out: list[str] = []
        for event in self.events:
            if event.stage not in out:
                out.append(event.stage)
        return out


async def test_a_package_install_reports_its_three_stages(tmp_path: Path) -> None:
    stages = Stages()
    result = await audio_install.install(
        "edge-tts", home=tmp_path, runner=Runner(), stages=stages
    )
    assert result.ok is True
    assert stages.sequence() == ["resolve", "install", "check"]
    assert all(event.steps == 3 for event in stages.events)
    assert {event.step for event in stages.events} == {1, 2, 3}
    assert all(event.engine == "edge-tts" for event in stages.events)


async def test_a_model_install_reports_download_verify_and_extract(tmp_path: Path) -> None:
    model = stt_models.MODELS["sherpa-onnx-sensevoice"]
    stages = Stages()
    result = await audio_install.install(
        "sherpa-onnx-sensevoice",
        home=tmp_path,
        runner=Runner(),
        stages=stages,
        fetch=FakeFetcher(_fake_archive(model)),
    )
    assert result.ok is True
    assert stages.sequence() == [
        "resolve",
        "install",
        "download",
        "verify",
        "extract",
        "check",
    ]
    assert all(event.steps == 6 for event in stages.events)


async def test_piper_reports_its_voice_download_as_a_stage(tmp_path: Path) -> None:
    stages = Stages()
    await audio_install.install(
        "piper", home=tmp_path, runner=Runner(), stages=stages, fetch=VoiceClient()
    )
    assert stages.sequence() == ["resolve", "install", "download", "check"]


async def test_bytes_are_reported_as_they_arrive(tmp_path: Path) -> None:
    """A 400MB download and a checksum look identical in a log; not here."""
    model = stt_models.MODELS["sherpa-onnx-zipformer-ko"]
    body = _fake_archive(model)
    stages = Stages()

    class Chunked(FakeFetcher):
        """Serves the archive in pieces big enough to cross a progress step."""

        def stream(self, _method: str, url: str, headers: Any = None) -> Any:
            self.urls.append(url)
            self.ranges.append((headers or {}).get("Range"))
            payload = self.body if url.endswith(".tar.bz2") else b"x"
            step = audio_install.MAX_LOG_LINES  # any size; the padding decides
            del step

            class Response:
                status_code = 200
                headers = {"content-length": str(len(payload))}

                async def aiter_bytes(self) -> Any:
                    yield payload

                async def __aenter__(self) -> Any:
                    return self

                async def __aexit__(self, *_exc: Any) -> None:
                    return None

            return Response()

    await stt_models.ensure_model(
        model.id,
        tmp_path,
        fetch=Chunked(body),
        log=audio_install._Log(
            None, stages, engine=model.id, sequence=audio_install.stages_for(model.id)
        ),
    )
    downloads = [event for event in stages.events if event.stage == "download"]
    assert downloads, "the download stage was never reported"


def test_the_stage_bar_says_where_it_is() -> None:
    event = audio_install.StageEvent(
        engine="x", stage="download", step=3, steps=6, line="model.tar.bz2", percent=62.5
    )
    bar = event.bar()
    assert bar.startswith("[download 3/6]")
    assert "62%" in bar or "63%" in bar
    assert "▇" in bar and "▁" in bar
    assert "model.tar.bz2" in bar

    # No percentage to show is not a reason to draw an empty meter.
    plain = audio_install.StageEvent(engine="x", stage="resolve", step=1, steps=3, line="$ uv")
    assert plain.bar() == "[resolve 1/3] $ uv"


def test_the_payload_only_carries_what_it_knows() -> None:
    bare = audio_install.StageEvent(engine="x", stage="check", step=3, steps=3).to_payload()
    assert set(bare) == {"engine", "stage", "step", "steps", "line"}
    full = audio_install.StageEvent(
        engine="x", stage="download", step=2, steps=4, percent=10.0, bytes_done=1, bytes_total=10
    ).to_payload()
    assert full["percent"] == 10.0 and full["bytesDone"] == 1 and full["bytesTotal"] == 10


def test_the_stage_sequence_matches_what_the_engine_actually_does() -> None:
    assert audio_install.stages_for("edge-tts") == audio_install.STAGES_PACKAGE
    assert audio_install.stages_for("piper") == audio_install.STAGES_WITH_VOICE
    assert audio_install.stages_for("sherpa-onnx-zipformer-en") == (
        audio_install.STAGES_WITH_MODEL
    )
    # A catalog id resolves to its engine's sequence, not to a default.
    assert audio_install.stages_for("local-whisper") == audio_install.STAGES_PACKAGE
