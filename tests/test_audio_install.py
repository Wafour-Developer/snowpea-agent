"""``audio.install``: obtaining the local voice engines, and what it refuses.

Every voice row used to say "not installed here" and stop there.  These cases
pin the other half: which argv each engine resolves to, which engines are a
system package we only explain, that the log is streamed as it arrives, and
that a successful install makes the capability flip without a restart.

Nothing here touches the network or installs anything: the runner and the
voice download are both injected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.audio import install as audio_install
from snowpea_core.audio import runtime as audio_runtime
from snowpea_core.audio import tts as tts_mod
from snowpea_core.server.errors import RpcError
from snowpea_core.setup.catalog import stt_catalog, tts_catalog

# ---------------------------------------------------------------------------
# a runner that records instead of running
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records the argv it was handed and answers with a canned exit code."""

    def __init__(self, code: int = 0, lines: tuple[str, ...] = ("collected", "installed")) -> None:
        self.code = code
        self.lines = lines
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Any, progress: Any = None) -> int:
        self.calls.append(list(argv))
        for line in self.lines:
            if progress is not None:
                await progress(line)
        return self.code


class FakeResponse:
    def __init__(self, content: bytes = b"voice", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


class FakeClient:
    """The two-file voice download, without the network."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.urls: list[str] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(status_code=self.status_code)


# ---------------------------------------------------------------------------
# the catalog no longer offers studio
# ---------------------------------------------------------------------------


def test_the_voice_catalog_has_no_studio_row() -> None:
    assert "studio" not in {item.id for item in tts_catalog(["studio"])}
    assert "studio" not in {item.id for item in stt_catalog()}


def test_there_is_no_automatic_row_at_all() -> None:
    """Two states now: nothing pinned (voice off), or one engine pinned."""
    assert "auto" not in {item.id for item in tts_catalog()}
    assert "auto" not in {item.id for item in stt_catalog()}


def test_studio_is_last_in_the_order_so_the_media_tool_still_reaches_it() -> None:
    """Dropping the *choice* must not break the ``text_to_speech`` tool."""
    assert tts_mod.RECOMMENDED_ORDER[-1] == "studio"
    assert tts_mod.RECOMMENDED_ORDER[0] == "supertonic"
    assert tts_mod.build_provider("studio", studio_configured=True).available() is True


# ---------------------------------------------------------------------------
# which rows offer an install
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["piper", "edge-tts", "local-whisper"])
def test_the_engines_the_daemon_can_fetch_are_marked_installable(engine: str) -> None:
    assert audio_install.is_installable(engine) is True
    assert audio_install.install_hint(engine) is None


@pytest.mark.parametrize(
    ("engine", "platform", "expected"),
    [
        ("espeak-ng", "linux", "sudo apt install espeak-ng"),
        ("espeak-ng", "darwin", "brew install espeak-ng"),
        ("espeak-ng", "win32", "winget install espeak-ng"),
        ("say", "darwin", "say is built into macOS; nothing to install"),
        ("powershell", "win32", "powershell is built into Windows; nothing to install"),
    ],
)
def test_a_system_package_is_explained_not_installed(
    engine: str, platform: str, expected: str
) -> None:
    assert audio_install.is_installable(engine) is False
    assert audio_install.install_hint(engine, platform) == expected


def test_the_whisper_row_installs_faster_whisper() -> None:
    """The catalog lists a family; one member is the one we can obtain."""
    assert audio_install.engine_for("local-whisper") == "faster-whisper"
    assert audio_install.engine_for("piper") == "piper"


def test_the_catalog_rows_carry_the_install_flags() -> None:
    rows = {item.id: item for item in tts_catalog([])}
    assert rows["piper"].installable is True and rows["piper"].install_hint is None
    assert rows["espeak-ng"].installable is False
    assert rows["espeak-ng"].install_hint  # a command the user runs themselves
    assert rows["openai"].installable is False
    stt = {item.id: item for item in stt_catalog([])}
    assert stt["local-whisper"].installable is True


# ---------------------------------------------------------------------------
# the argv each engine resolves to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "package"),
    [("faster-whisper", "faster-whisper"), ("edge-tts", "edge-tts"), ("piper", "piper-tts")],
)
async def test_each_engine_installs_its_own_package(
    tmp_path: Path, engine: str, package: str
) -> None:
    runner = FakeRunner()
    result = await audio_install.install(
        engine, home=tmp_path, runner=runner, fetch=FakeClient
    )
    assert result.ok is True
    assert len(runner.calls) == 1
    assert runner.calls[0][-1] == package
    # Whatever installer was picked, it is one of the three we know.
    assert runner.calls[0][:2] in (["uv", "tool"], ["pipx", "install"]) or "pip" in runner.calls[0]


def test_the_installer_chain_prefers_uv_then_pipx_then_pip(monkeypatch: Any) -> None:
    def only(*names: str) -> Any:
        return lambda binary: f"/usr/bin/{binary}" if binary in names else None

    monkeypatch.setattr(audio_install.shutil, "which", only("uv", "pipx"))
    assert audio_install.python_install_argv("edge-tts") == ["uv", "tool", "install", "edge-tts"]

    monkeypatch.setattr(audio_install.shutil, "which", only("pipx"))
    assert audio_install.python_install_argv("edge-tts") == ["pipx", "install", "edge-tts"]

    monkeypatch.setattr(audio_install.shutil, "which", only())
    argv = audio_install.python_install_argv("edge-tts")
    # The interpreter running us is always there, so pip --user is the floor.
    assert argv is not None
    assert argv[1:] == ["-m", "pip", "install", "--user", "edge-tts"]


# ---------------------------------------------------------------------------
# the audio runtime: snowpea's own interpreter for the Python-API engines
# ---------------------------------------------------------------------------


def test_the_runtime_lives_under_the_home_it_belongs_to(tmp_path: Path) -> None:
    assert audio_runtime.runtime_dir(tmp_path) == tmp_path / "audio-runtime"
    python = audio_runtime.runtime_python(tmp_path)
    assert python.parent.parent == audio_runtime.runtime_dir(tmp_path)
    assert python.name.startswith("python")
    assert audio_runtime.runtime_ready(tmp_path) is False


def test_the_runtime_is_created_with_uv_when_uv_is_there(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        audio_runtime.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    assert audio_runtime.create_argv(tmp_path)[0][:2] == ["uv", "venv"]


def test_without_uv_the_runtime_falls_to_the_stdlib_venv(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda _name: None)
    first = audio_runtime.create_argv(tmp_path)[0]
    assert first[1:3] == ["-m", "venv"] and first[-1] == str(audio_runtime.runtime_dir(tmp_path))


async def test_creating_the_runtime_runs_the_command_and_is_idempotent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        audio_runtime.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    runner = FakeRunner(lines=())

    async def creating(argv: Any, progress: Any = None) -> int:
        code = await runner(argv, progress)
        _make_runtime(tmp_path)
        return code

    assert await audio_runtime.ensure_runtime(tmp_path, runner=creating) is True
    assert runner.calls == [["uv", "venv", str(audio_runtime.runtime_dir(tmp_path))]]

    # Already there: nothing is run a second time.
    assert await audio_runtime.ensure_runtime(tmp_path, runner=creating) is True
    assert len(runner.calls) == 1


async def test_a_runtime_that_cannot_be_created_is_a_failed_install(tmp_path: Path) -> None:
    runner = FakeRunner(code=1, lines=())
    assert await audio_runtime.ensure_runtime(tmp_path, runner=runner) is False
    result = await audio_install.install(
        "supertonic", home=tmp_path, runner=FakeRunner(code=1, lines=())
    )
    assert result.ok is False
    assert result.hint is not None and "audio-runtime" in result.hint


def test_the_runtime_install_argv_names_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        audio_runtime.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    python = str(audio_runtime.runtime_python(tmp_path))
    assert audio_runtime.runtime_install_argv(tmp_path, "sherpa-onnx") == [
        "uv", "pip", "install", "--python", python, "sherpa-onnx",
    ]

    monkeypatch.setattr(audio_runtime.shutil, "which", lambda _name: None)
    assert audio_runtime.runtime_install_argv(tmp_path, "sherpa-onnx") == [
        python, "-m", "pip", "install", "sherpa-onnx",
    ]


def test_a_module_is_found_by_looking_rather_than_by_running(tmp_path: Path) -> None:
    """Availability is asked on every capabilities call; a process each is too much."""
    assert audio_runtime.has_module(tmp_path, "sherpa_onnx") is False
    site = _make_runtime(tmp_path)
    assert audio_runtime.has_module(tmp_path, "sherpa_onnx") is False
    (site / "sherpa_onnx").mkdir()
    assert audio_runtime.has_module(tmp_path, "sherpa_onnx") is True
    # A single-file module counts too.
    (site / "lonely.py").write_text("", encoding="utf-8")
    assert audio_runtime.has_module(tmp_path, "lonely") is True
    assert audio_runtime.has_module(tmp_path, "") is False


@pytest.mark.parametrize(
    "engine",
    [
        "sherpa-onnx-sensevoice",
        "sherpa-onnx-zipformer-ko",
        "sherpa-onnx-zipformer-en",
        "supertonic",
    ],
)
def test_the_python_api_engines_go_to_the_runtime_and_the_cli_ones_do_not(engine: str) -> None:
    assert audio_install.ENGINES[engine].runtime is True
    for other in ("piper", "edge-tts", "faster-whisper"):
        assert audio_install.ENGINES[other].runtime is False


def _make_runtime(home: Path) -> Path:
    """A runtime venv on disk, without creating a real one."""
    site = audio_runtime.runtime_dir(home) / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    python = audio_runtime.runtime_python(home)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    return site


def test_nothing_is_ever_a_shell_string() -> None:
    """Every install is argv, so there is nothing to quote and nothing to escape."""
    argv = audio_install.python_install_argv("edge-tts")
    assert argv is not None and all(isinstance(part, str) for part in argv)
    assert not any(any(ch in part for ch in ";|&$`") for part in argv)


# ---------------------------------------------------------------------------
# progress, failure and the timeout
# ---------------------------------------------------------------------------


async def test_the_log_is_streamed_as_it_arrives_and_returned_whole(tmp_path: Path) -> None:
    seen: list[str] = []

    async def progress(line: str) -> None:
        seen.append(line)

    runner = FakeRunner(lines=("downloading", "building", "done"))
    result = await audio_install.install(
        "edge-tts", home=tmp_path, runner=runner, progress=progress, fetch=FakeClient
    )
    assert result.ok is True
    # The command itself opens the log, so a reader knows what ran.
    assert seen[0].startswith("$ ")
    assert ["downloading", "building", "done"] == seen[1:4]
    assert seen == result.log.splitlines()
    assert result.log.endswith("edge-tts installed")


async def test_a_failing_installer_is_a_result_not_an_exception(tmp_path: Path) -> None:
    result = await audio_install.install(
        "edge-tts", home=tmp_path, runner=FakeRunner(code=1), fetch=FakeClient
    )
    assert result.ok is False
    assert result.engine == "edge-tts"
    assert "exited 1" in result.log


async def test_an_unknown_engine_says_what_it_knows(tmp_path: Path) -> None:
    result = await audio_install.install("kazoo", home=tmp_path, runner=FakeRunner())
    assert result.ok is False
    assert result.hint is not None and "unknown audio engine" in result.hint
    assert "piper" in result.hint


async def test_a_system_engine_installs_nothing_and_hands_back_the_command(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    result = await audio_install.install(
        "espeak-ng", home=tmp_path, runner=runner, platform="linux"
    )
    assert result.ok is False
    assert runner.calls == [], "a system package must never be installed for the user"
    assert result.hint == "sudo apt install espeak-ng"


async def test_a_hung_installer_is_given_up_on(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(audio_install, "INSTALL_TIMEOUT_SEC", 0.05)

    async def forever(argv: Any, progress: Any = None) -> int:
        await asyncio.sleep(5)
        return 0

    result = await audio_install.install("edge-tts", home=tmp_path, runner=forever)
    assert result.ok is False
    assert "timed out" in result.log


# ---------------------------------------------------------------------------
# piper brings a voice with it
# ---------------------------------------------------------------------------


async def test_installing_piper_downloads_a_default_voice(tmp_path: Path) -> None:
    client = FakeClient()
    result = await audio_install.install(
        "piper", home=tmp_path, runner=FakeRunner(), fetch=lambda: client
    )
    assert result.ok is True
    voices = tmp_path / audio_install.VOICES_DIRNAME
    model = voices / f"{audio_install.DEFAULT_PIPER_VOICE}.onnx"
    assert model.is_file() and (voices / f"{model.name}.json").is_file()
    assert len(client.urls) == 2
    assert getattr(result, "voice_path", None) == model


async def test_a_voice_that_will_not_download_does_not_fail_the_install(
    tmp_path: Path,
) -> None:
    """The binary is installed either way; the log says the voice is missing."""
    result = await audio_install.install(
        "piper", home=tmp_path, runner=FakeRunner(), fetch=lambda: FakeClient(status_code=404)
    )
    assert result.ok is True
    assert "404" in result.log
    assert getattr(result, "voice_path", None) is None


async def test_a_voice_already_on_disk_is_not_downloaded_again(tmp_path: Path) -> None:
    voices = tmp_path / audio_install.VOICES_DIRNAME
    voices.mkdir(parents=True)
    name = audio_install.DEFAULT_PIPER_VOICE
    (voices / f"{name}.onnx").write_bytes(b"x")
    (voices / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    client = FakeClient()
    await audio_install.download_voice(tmp_path, fetch=lambda: client)
    assert client.urls == []


# ---------------------------------------------------------------------------
# through the RPC handler
# ---------------------------------------------------------------------------


class _Hub:
    """Records the session-less notifications the handler broadcasts."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any], **_: Any) -> None:
        self.sent.append((method, params))


class _Paths:
    def __init__(self, home: Path) -> None:
        self.home = home


class _Core:
    """The three attributes ``audio_install_handler`` reaches for."""

    def __init__(self, home: Path) -> None:
        self.hub = _Hub()
        self.paths = _Paths(home)
        self.settings = type("S", (), {"audio": {"tts": {}}})()


async def test_the_handler_broadcasts_every_line_and_answers_with_the_log(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    runner = FakeRunner(lines=("resolving", "installed edge-tts"))

    real = audio_install.install

    async def fake_install(engine: str, **kwargs: Any) -> Any:
        kwargs.setdefault("runner", runner)
        kwargs.setdefault("fetch", FakeClient)
        return await real(engine, **kwargs)

    monkeypatch.setattr(audio_handlers.audio_install, "install", fake_install)
    core = _Core(tmp_path)
    result = await audio_handlers.audio_install_handler(
        None, AudioInstallParams(engine="edge-tts"), core  # type: ignore[arg-type]
    )

    assert result.ok is True and result.engine == "edge-tts"
    methods = {method for method, _ in core.hub.sent}
    assert methods == {audio_handlers.INSTALL_PROGRESS}
    lines = [params["line"] for _, params in core.hub.sent]
    assert "resolving" in lines and "installed edge-tts" in lines
    assert all(params["engine"] == "edge-tts" for _, params in core.hub.sent)
    # An engine install carries no `voice`: the pair is what a surface keys a
    # progress row on, and an engine is not one of its own voices.
    assert all("voice" not in params for _, params in core.hub.sent)
    # Whatever a late client missed on the wire is still in the answer. A stage
    # that moved the bar without printing anything sends an empty line, which
    # belongs on the wire and not in the log.
    assert [line for line in lines if line] == result.log.splitlines()


async def test_the_handler_normalises_the_catalog_id_it_was_given(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    real = audio_install.install

    async def fake_install(engine: str, **kwargs: Any) -> Any:
        kwargs.setdefault("runner", FakeRunner())
        return await real(engine, **kwargs)

    monkeypatch.setattr(audio_handlers.audio_install, "install", fake_install)
    core = _Core(tmp_path)
    result = await audio_handlers.audio_install_handler(
        None, AudioInstallParams(engine="local-whisper"), core  # type: ignore[arg-type]
    )
    assert result.engine == "faster-whisper"
    assert all(params["engine"] == "faster-whisper" for _, params in core.hub.sent)


async def test_a_successful_install_makes_detection_see_the_new_binary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``shutil.which`` is cached; without clearing it the row stays inactive."""
    from snowpea_core.server import audio_handlers

    cleared: list[bool] = []

    class _Which:
        @staticmethod
        def cache_clear() -> None:
            cleared.append(True)

    monkeypatch.setattr(audio_handlers, "_block", lambda *_: None)
    monkeypatch.setattr("shutil.which", _Which)
    audio_handlers._after_install(
        _Core(tmp_path), audio_install.InstallResult(ok=True, engine="edge-tts")
    )
    assert cleared == [True]


async def test_the_handler_records_the_voice_piper_downloaded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    saved: list[Any] = []

    real = audio_install.install

    async def fake_install(engine: str, **kwargs: Any) -> Any:
        kwargs.setdefault("runner", FakeRunner())
        kwargs.setdefault("fetch", FakeClient)
        return await real(engine, **kwargs)

    core = _Core(tmp_path)
    core.settings.save = lambda paths: saved.append(paths)  # type: ignore[attr-defined]
    monkeypatch.setattr(audio_handlers.audio_install, "install", fake_install)

    result = await audio_handlers.audio_install_handler(
        None, AudioInstallParams(engine="piper"), core  # type: ignore[arg-type]
    )
    assert result.ok is True
    voice = core.settings.audio["tts"].get("voice")
    assert voice and voice.endswith(f"{audio_install.DEFAULT_PIPER_VOICE}.onnx")
    assert saved, "the voice has to be written, or piper has nothing to say"


async def test_a_second_handler_call_for_the_same_engine_is_refused_as_running(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from snowpea_core.server import audio_handlers
    from snowpea_core.server.protocol import AudioInstallParams

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_install(engine: str, **_kwargs: Any) -> Any:
        calls.append(engine)
        stages = _kwargs.get("stages")
        if callable(stages):
            await stages(
                audio_install.StageEvent(
                    engine=engine,
                    stage="resolve",
                    step=1,
                    steps=3,
                    line="$ uv tool install edge-tts",
                )
            )
        started.set()
        await release.wait()
        return audio_install.InstallResult(ok=True, engine=engine, log=f"{engine} installed")

    monkeypatch.setattr(audio_handlers.audio_install, "install", fake_install)
    core = _Core(tmp_path)

    first = asyncio.create_task(
        audio_handlers.audio_install_handler(
            None, AudioInstallParams(engine="edge-tts"), core  # type: ignore[arg-type]
        )
    )
    await started.wait()

    with pytest.raises(RpcError) as second:
        await audio_handlers.audio_install_handler(
            None, AudioInstallParams(engine="edge-tts"), core  # type: ignore[arg-type]
        )
    assert second.value.code == "install_running"
    assert second.value.data["engine"] == "edge-tts"
    assert second.value.data["progress"]["engine"] == "edge-tts"
    assert second.value.data["progress"]["stage"] == "resolve"

    release.set()
    result = await first
    assert result.ok is True
    assert calls == ["edge-tts"], "the second request must not start a second install"


# ---------------------------------------------------------------------------
# supertonic first-run model download
# ---------------------------------------------------------------------------


async def test_supertonic_warmup_streams_progress(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(audio_install, "supertonic_models_ready", lambda _home: False)
    monkeypatch.setattr(audio_runtime, "has_module", lambda _home, _name: True)
    monkeypatch.setattr(
        audio_runtime,
        "runtime_python",
        lambda _home: tmp_path / "python",
    )
    runner = FakeRunner(lines=("Downloading voice models…", "ready"))
    result = await audio_install.warmup("supertonic", home=tmp_path, runner=runner)
    assert result.ok is True
    stamp = audio_runtime.runtime_dir(tmp_path) / audio_install.SUPERTONIC_WARMUP_STAMP
    assert stamp.is_file()
    assert runner.calls


async def test_supertonic_warmup_skips_when_the_stamp_exists(tmp_path: Path) -> None:
    stamp = audio_runtime.runtime_dir(tmp_path) / audio_install.SUPERTONIC_WARMUP_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("ok", encoding="utf-8")
    runner = FakeRunner()
    result = await audio_install.warmup("supertonic", home=tmp_path, runner=runner)
    assert result.ok is True
    assert runner.calls == []
