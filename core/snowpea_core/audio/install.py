"""Installing the local voice engines the daemon can obtain by itself.

Every voice row in the setup wizard used to say "not installed here" and stop
there, which tells the user what is wrong and nothing about what to do.  This
module is the "do" half: for the engines that are ordinary user-space packages
it runs the install, streams the output back, and re-runs detection so the
capability flips to active without a restart.

What it will and will not do:

* **Only argv, never a shell string.**  Nothing here interpolates a user value
  into a command line, so there is nothing to quote and nothing to escape.
* **Never root.**  ``espeak-ng``, macOS ``say`` and Windows ``powershell`` are
  system packages or built in; asking for them returns ``ok=False`` with the
  command the user should run themselves, per platform.
* **Bounded.**  :data:`INSTALL_TIMEOUT_SEC` is a hard cap, after which the
  process is killed and the partial log is returned.
* **A failure is a result, not an exception.**  Every path answers with an
  :class:`InstallResult`; the caller renders it.

The installer chain for a *command line* engine is the same one a user would
try by hand, best first: ``uv tool install``, then ``pipx install``, then
``python -m pip install --user``.  Whichever is on PATH first wins, and the log
says which one ran.

Engines that are a Python API rather than a command take a different route:
they go into snowpea's own interpreter at ``$SNOWPEA_HOME/audio-runtime``
(:mod:`snowpea_core.audio.runtime`), because there is no binary for ``PATH`` to
find and the daemon's own venv has no pip to install into.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.audio import runtime

log = logging.getLogger("snowpea.audio.install")

#: A whole install, however many commands it takes, gives up after this.
INSTALL_TIMEOUT_SEC = 600.0

#: Lines of output kept in the returned log.  A pip install of a wheel with
#: CUDA in it prints thousands; a UI wants the end of that, not all of it.
MAX_LOG_LINES = 200

#: Where a downloaded piper voice is written, under ``$SNOWPEA_HOME``.
VOICES_DIRNAME = "voices"

#: The piper voice installed alongside the binary, so "install piper" leaves a
#: working engine rather than a binary with nothing to say.
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"

#: Where that voice comes from.  Two files: the model and its config.
PIPER_VOICES_ROOT = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICE_BASE = f"{PIPER_VOICES_ROOT}/en/en_US/lessac/medium"

#: ``(runner, argv-prefix)`` tried in order for a Python package.  The first
#: runner on PATH is the one used.
PYTHON_INSTALLERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("uv", ("uv", "tool", "install")),
    ("pipx", ("pipx", "install")),
)

#: Last resort when neither uv nor pipx is there; ``--user`` keeps it out of a
#: system site-packages we have no right to write to.
PIP_FALLBACK: tuple[str, ...] = (sys.executable, "-m", "pip", "install", "--user")


@dataclass(frozen=True)
class EngineInstall:
    """How one engine is obtained, or why it cannot be."""

    #: Engine id, matching the audio catalog and ``audio.capabilities``.
    engine: str
    #: PyPI distribution to install; empty for a system package.
    package: str = ""
    #: Per-platform command the user runs themselves; empty means "we can".
    hints: dict[str, str] = field(default_factory=dict)
    #: True when the engine is a Python API rather than a command, so it goes
    #: into snowpea's audio runtime instead of onto ``PATH``.
    runtime: bool = False

    @property
    def installable(self) -> bool:
        return bool(self.package)

    def hint(self, platform: str | None = None) -> str | None:
        """The manual command for this platform, or ``None``."""
        key = platform or sys.platform
        for candidate in (key, "linux" if key.startswith("linux") else key, "default"):
            found = self.hints.get(candidate)
            if found:
                return found
        return None


#: Engines whose install also downloads a model, keyed by the model table id.
#: The package alone is a decoder with nothing to decode with.
MODEL_ENGINES: dict[str, str] = {
    "sherpa-onnx-sensevoice": "sherpa-onnx-sensevoice",
    "sherpa-onnx-zipformer-ko": "sherpa-onnx-zipformer-ko",
    "sherpa-onnx-zipformer-en": "sherpa-onnx-zipformer-en",
}

#: The distribution that provides the ``sherpa_onnx`` Python package.  It has
#: no usable command line: the wheel's only script is ``sherpa-onnx-cli``,
#: which does not import without ``click``, so the engine runs the Python API.
SHERPA_PACKAGE = "sherpa-onnx"

#: The module each runtime engine's package provides, for detection.
RUNTIME_MODULES: dict[str, str] = {
    "sherpa-onnx-sensevoice": "sherpa_onnx",
    "sherpa-onnx-zipformer-ko": "sherpa_onnx",
    "sherpa-onnx-zipformer-en": "sherpa_onnx",
    "supertonic": "supertonic",
}

#: Every engine the voice screens can offer, keyed by id.  An engine absent
#: from this table is not installable and has no hint — the UI shows the row
#: inactive, as it did before.
ENGINES: dict[str, EngineInstall] = {
    # -- speech to text
    "faster-whisper": EngineInstall(engine="faster-whisper", package="faster-whisper"),
    # -- speech to text, sherpa-onnx: one package, three models
    "sherpa-onnx-sensevoice": EngineInstall(
        engine="sherpa-onnx-sensevoice", package=SHERPA_PACKAGE, runtime=True
    ),
    "sherpa-onnx-zipformer-ko": EngineInstall(
        engine="sherpa-onnx-zipformer-ko", package=SHERPA_PACKAGE, runtime=True
    ),
    "sherpa-onnx-zipformer-en": EngineInstall(
        engine="sherpa-onnx-zipformer-en", package=SHERPA_PACKAGE, runtime=True
    ),
    # -- text to speech
    # Supertonic is a Python API with no command line, so it goes into the
    # audio runtime and the engine runs a child of that interpreter.
    "supertonic": EngineInstall(engine="supertonic", package="supertonic", runtime=True),
    "piper": EngineInstall(engine="piper", package="piper-tts"),
    "edge-tts": EngineInstall(engine="edge-tts", package="edge-tts"),
    # -- system packages: ours to explain, not to install
    "espeak-ng": EngineInstall(
        engine="espeak-ng",
        hints={
            "linux": "sudo apt install espeak-ng",
            "darwin": "brew install espeak-ng",
            "win32": "winget install espeak-ng",
            "default": "install espeak-ng with your system package manager",
        },
    ),
    "say": EngineInstall(
        engine="say",
        hints={
            "darwin": "say is built into macOS; nothing to install",
            "default": "say is macOS only",
        },
    ),
    "powershell": EngineInstall(
        engine="powershell",
        hints={
            "win32": "powershell is built into Windows; nothing to install",
            "default": "Windows SAPI needs Windows",
        },
    ),
}

#: The id the catalog uses for the whisper row, whose engine is the CLI.
LOCAL_WHISPER = "local-whisper"

#: Catalog id -> the engine that actually gets installed for it.  The voice
#: screens list backends; some of those are a family with one obtainable
#: member, and this is where that indirection lives.
CATALOG_ENGINE: dict[str, str] = {LOCAL_WHISPER: "faster-whisper"}


def engine_for(catalog_id: str) -> str:
    """The installable engine behind a catalog row id."""
    return CATALOG_ENGINE.get(catalog_id, catalog_id)


def spec_for(engine: str) -> EngineInstall | None:
    """The install spec for an engine or catalog id, or ``None``."""
    return ENGINES.get(engine_for(engine))


def is_installable(catalog_id: str) -> bool:
    """True when :func:`install` could obtain this row without root."""
    spec = spec_for(catalog_id)
    return spec is not None and spec.installable


def install_hint(catalog_id: str, platform: str | None = None) -> str | None:
    """The command a user would run by hand for this row, if any."""
    spec = spec_for(catalog_id)
    return None if spec is None else spec.hint(platform)


@dataclass
class InstallResult:
    """What one install attempt did."""

    ok: bool
    engine: str
    log: str = ""
    hint: str | None = None
    #: The voice this was, when the request named one.  ``engine`` stays the
    #: bare id either way, so a surface can key a row on the pair.
    voice: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "engine": self.engine, "log": self.log}
        if self.voice:
            payload["voice"] = self.voice
        if self.hint:
            payload["hint"] = self.hint
        return payload


#: The stages an install moves through, in order.  Named rather than counted
#: because "step 2 of 4" tells a user nothing about what is taking the time,
#: and a 400MB download and a checksum look identical in a log.
STAGE_RESOLVE = "resolve"
STAGE_DOWNLOAD = "download"
STAGE_EXTRACT = "extract"
STAGE_VERIFY = "verify"
STAGE_INSTALL = "install"
STAGE_CHECK = "check"
#: Creating ``$SNOWPEA_HOME/audio-runtime``, for the engines that need it.
STAGE_RUNTIME = "runtime"

#: Which stages each kind of engine actually has, so ``steps`` is the truth
#: rather than a constant every engine pretends to.
STAGES_PACKAGE: tuple[str, ...] = (STAGE_RESOLVE, STAGE_INSTALL, STAGE_CHECK)
STAGES_WITH_MODEL: tuple[str, ...] = (
    STAGE_RESOLVE,
    STAGE_INSTALL,
    STAGE_DOWNLOAD,
    STAGE_VERIFY,
    STAGE_EXTRACT,
    STAGE_CHECK,
)
STAGES_WITH_VOICE: tuple[str, ...] = (
    STAGE_RESOLVE,
    STAGE_INSTALL,
    STAGE_DOWNLOAD,
    STAGE_CHECK,
)
#: Supertonic's Python package installs quickly; the ONNX assets download on first
#: use unless this warmup step runs first.
STAGES_SUPERTONIC: tuple[str, ...] = (
    STAGE_RESOLVE,
    STAGE_INSTALL,
    STAGE_DOWNLOAD,
    STAGE_CHECK,
)

#: Engines whose first speak downloads assets; ``audio.install`` with
#: ``warmup=true`` runs only that download with staged progress.
WARMUP_ENGINES: frozenset[str] = frozenset({"supertonic"})

SUPERTONIC_WARMUP_STAMP = ".supertonic-warmed"

#: A one-line synthesis that forces ``TTS(auto_download=True)`` to fetch models.
SUPERTONIC_WARMUP_SCRIPT = """
import os, sys, tempfile
print("Downloading voice models…", flush=True)
from supertonic import TTS
tts = TTS(auto_download=True)
print("Loading a voice style…", flush=True)
style = tts.get_voice_style(voice_name="M1")
print("Verifying synthesis…", flush=True)
wav, _ = tts.synthesize(text=".", voice_style=style, lang="en")
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
    path = handle.name
tts.save_audio(wav, path)
os.unlink(path)
print("ready", flush=True)
"""


def supertonic_models_ready(home: Path | str) -> bool:
    """True when the Supertonic warmup stamp exists under the audio runtime."""
    stamp = runtime.runtime_dir(home) / SUPERTONIC_WARMUP_STAMP
    return stamp.is_file()


def stages_for(engine: str) -> tuple[str, ...]:
    """The stage sequence this engine really walks."""
    name = engine_for(engine)
    if name in MODEL_ENGINES:
        sequence = STAGES_WITH_MODEL
    elif name == "supertonic":
        sequence = STAGES_SUPERTONIC
    elif name == "piper":
        sequence = STAGES_WITH_VOICE
    else:
        sequence = STAGES_PACKAGE
    spec = ENGINES.get(name)
    if spec is not None and spec.runtime:
        # The runtime is created between resolving and installing, and a stage
        # the user waits through is a stage the bar has to count.
        head, *tail = sequence
        return (head, STAGE_RUNTIME, *tail)
    return sequence


@dataclass
class StageEvent:
    """One progress report: where the install is, and how far into it."""

    engine: str
    stage: str
    step: int
    steps: int
    #: Set while a *voice* of that engine is installing; empty for the engine.
    voice: str = ""
    line: str = ""
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "engine": self.engine,
            "stage": self.stage,
            "step": self.step,
            "steps": self.steps,
            "line": self.line,
        }
        if self.voice:
            payload["voice"] = self.voice
        if self.percent is not None:
            payload["percent"] = round(float(self.percent), 1)
        if self.bytes_done is not None:
            payload["bytesDone"] = int(self.bytes_done)
        if self.bytes_total is not None:
            payload["bytesTotal"] = int(self.bytes_total)
        return payload

    def bar(self, width: int = 8) -> str:
        """``[download 3/6] 63% ▇▇▇▇▇▁▁▁ name`` — the one line a wizard draws."""
        head = f"[{self.stage} {self.step}/{self.steps}]"
        if self.percent is None:
            return f"{head} {self.line}".rstrip()
        filled = int(round(width * max(0.0, min(100.0, self.percent)) / 100))
        meter = "▇" * filled + "▁" * (width - filled)
        return f"{head} {self.percent:3.0f}% {meter} {self.line}".rstrip()


#: Called with each line of output as it arrives, for ``audio.install.progress``.
#: A caller that wants stages passes a :class:`Reporter` instead.
Progress = Callable[[str], Awaitable[None]]

#: Called with each :class:`StageEvent`.
Stages = Callable[["StageEvent"], Awaitable[None]]

#: Runs one argv and streams its lines; swapped out in tests so nothing is
#: downloaded.  Returns the process exit status.
Runner = Callable[[Sequence[str], Progress | None], Awaitable[int]]


async def run_argv(argv: Sequence[str], progress: Progress | None = None) -> int:
    """Run one command, streaming stdout+stderr line by line.

    Never a shell: ``argv`` goes to ``execve`` as it is written.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        if progress is not None:
            await progress(f"{argv[0]}: {exc}")
        return 127
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if progress is not None and line:
            await progress(line)
    return int(await process.wait())


class _Log:
    """Collects the lines an install printed, and reports where it is.

    It is callable as a plain ``Progress`` so everything that only knows how to
    emit a line keeps working; :meth:`stage` is what moves the bar. The stage
    it is currently in is carried on the object, so a line emitted from deep
    inside a download is still labelled ``download`` without every caller
    having to pass it along.
    """

    def __init__(
        self,
        progress: Progress | None = None,
        stages: Stages | None = None,
        *,
        engine: str = "",
        sequence: tuple[str, ...] = (),
        voice: str = "",
    ) -> None:
        self.lines: list[str] = []
        self._progress = progress
        self._stages = stages
        self.engine = engine
        self.voice = voice
        self.sequence = sequence
        self.current = sequence[0] if sequence else ""

    # -- the line side --------------------------------------------------
    async def __call__(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-MAX_LOG_LINES]
        if self._progress is not None:
            await self._progress(line)
        await self._emit(line=line)

    async def say(self, line: str) -> None:
        """Add a line of our own, so the log reads as one story."""
        await self(line)

    # -- the stage side -------------------------------------------------
    async def stage(self, name: str, line: str = "") -> None:
        """Move to a stage and report it."""
        self.current = name
        if line:
            self.lines.append(line)
            del self.lines[:-MAX_LOG_LINES]
            if self._progress is not None:
                await self._progress(line)
        await self._emit(line=line)

    async def bytes(self, done: int, total: int, line: str = "") -> None:
        """Report transfer progress inside the current stage."""
        percent = (100.0 * done / total) if total else None
        await self._emit(line=line, percent=percent, done=done, total=total)

    async def _emit(
        self,
        *,
        line: str = "",
        percent: float | None = None,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        if self._stages is None or not self.sequence:
            return
        try:
            step = self.sequence.index(self.current) + 1
        except ValueError:  # pragma: no cover - a stage outside the sequence
            step = 0
        await self._stages(
            StageEvent(
                engine=self.engine,
                voice=self.voice,
                stage=self.current,
                step=step,
                steps=len(self.sequence),
                line=line,
                percent=percent,
                bytes_done=done,
                bytes_total=total,
            )
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def python_install_argv(package: str) -> list[str] | None:
    """The argv that installs a *command* ``package``, or ``None`` with nothing
    to run it.

    ``uv tool install`` first because it is what this project ships with,
    ``pipx`` next, and ``pip install --user`` last — the same order a person
    would try, and the log names which one was used.  An engine that is a
    Python API does not come through here; it goes to
    :func:`snowpea_core.audio.runtime.runtime_install_argv`.
    """
    for runner, prefix in PYTHON_INSTALLERS:
        if shutil.which(runner):
            return [*prefix, package]
    if shutil.which(PIP_FALLBACK[0]) or Path(PIP_FALLBACK[0]).exists():
        return [*PIP_FALLBACK, package]
    return None


def voice_files(voice: str = DEFAULT_PIPER_VOICE, path: str | None = None) -> tuple[str, str]:
    """``(model url, config url)`` for one piper voice.

    ``path`` is the voice's directory in the upstream repo; the default one is
    the only voice with its base URL baked in, because it is the one an engine
    install fetches on its own.
    """
    base = f"{PIPER_VOICES_ROOT}/{path}" if path else PIPER_VOICE_BASE
    return (f"{base}/{voice}.onnx", f"{base}/{voice}.onnx.json")


async def download_voice(
    home: Path,
    voice: str = DEFAULT_PIPER_VOICE,
    *,
    progress: Progress | None = None,
    fetch: Any = None,
    path: str | None = None,
) -> Path | None:
    """Fetch one piper voice into ``$SNOWPEA_HOME/voices`` and return its path.

    A piper binary with no voice cannot speak, so "install piper" means both.
    A download failure is not fatal: the binary is still installed, the log
    says the voice is missing, and the user can point ``audio.tts.voice`` at
    one themselves.
    """
    target_dir = Path(home).expanduser() / VOICES_DIRNAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if progress is not None:
            await progress(f"cannot write {target_dir}: {exc}")
        return None
    model = target_dir / f"{voice}.onnx"
    config = target_dir / f"{voice}.onnx.json"
    if model.is_file() and config.is_file():
        if progress is not None:
            await progress(f"voice {voice} is already in {target_dir}")
        return model

    client_factory = fetch or _httpx_client
    try:
        async with client_factory() as client:
            for url, target in zip(voice_files(voice, path), (model, config), strict=True):
                if progress is not None:
                    await progress(f"downloading {url}")
                response = await client.get(url)
                if getattr(response, "status_code", 500) >= 400:
                    if progress is not None:
                        await progress(f"HTTP {response.status_code} for {url}")
                    return None
                target.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001 - a missing voice is not a failed install
        if progress is not None:
            await progress(f"could not download the voice: {type(exc).__name__}: {exc}")
        return None
    return model


def _httpx_client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=120.0, follow_redirects=True)


#: The stages a voice download walks: fetch the two files, then re-detect.
STAGES_VOICE: tuple[str, ...] = (STAGE_RESOLVE, STAGE_DOWNLOAD, STAGE_CHECK)


async def install_voice(
    engine: str,
    voice: str,
    *,
    home: Path,
    progress: Progress | None = None,
    stages: Stages | None = None,
    fetch: Any = None,
) -> InstallResult:
    """Fetch one of an engine's voices.

    Only piper has voices that are downloads; everything else either ships its
    voices with the engine (Supertonic's ten presets come out of the one
    multilingual model) or reads them off the system, and asking to install
    one of those is answered rather than attempted.

    Installing a voice does **not** select it, for the same reason installing
    an engine does not pin it: the two are separate decisions and running them
    together is how a user ends up with a setting they did not choose.
    """
    name = engine_for((engine or "").strip())
    wanted = (voice or "").strip()
    if name != "piper":
        return InstallResult(
            ok=False,
            engine=name,
            voice=wanted,
            hint=f"{name} voices are not downloads; pick one and it is ready",
        )
    from snowpea_core.audio import voices as voice_catalog

    path = voice_catalog.piper_voice_path(wanted)
    if path is None:
        known = ", ".join(entry[0] for entry in voice_catalog.PIPER_VOICES)
        return InstallResult(
            ok=False,
            engine=name,
            voice=wanted,
            hint=f"unknown piper voice {wanted!r}; known: {known}",
        )
    collected = _Log(progress, stages, engine=name, sequence=STAGES_VOICE, voice=wanted)
    await collected.stage(STAGE_RESOLVE, f"voice {wanted}")
    await collected.stage(STAGE_DOWNLOAD)
    landed = await download_voice(home, wanted, progress=collected, fetch=fetch, path=path)
    if landed is None:
        return InstallResult(
            ok=False,
            engine=name,
            voice=wanted,
            log=collected.text,
            hint="re-run the install to resume the download",
        )
    await collected.stage(STAGE_CHECK, f"voice ready: {landed}")
    result = InstallResult(ok=True, engine=name, voice=wanted, log=collected.text)
    setattr(result, "voice_path", landed)  # noqa: B010 - deliberate side channel
    return result


async def install(
    engine: str,
    *,
    home: Path,
    progress: Progress | None = None,
    stages: Stages | None = None,
    runner: Runner | None = None,
    fetch: Any = None,
    platform: str | None = None,
) -> InstallResult:
    """Install one voice engine, streaming its output through ``progress``.

    Answers rather than raises.  A system package answers ``ok=False`` with the
    command to run by hand; an unknown engine answers ``ok=False`` saying so.
    """
    name = engine_for((engine or "").strip())
    spec = ENGINES.get(name)
    if spec is None:
        return InstallResult(
            ok=False,
            engine=name or str(engine),
            log="",
            hint=f"unknown audio engine {engine!r}; known: {', '.join(sorted(ENGINES))}",
        )
    if not spec.installable:
        # A system package: telling the user the exact command is the whole of
        # what we can honestly do here.
        return InstallResult(ok=False, engine=name, log="", hint=spec.hint(platform))

    argv = (
        runtime.runtime_install_argv(home, spec.package)
        if spec.runtime
        else python_install_argv(spec.package)
    )
    if argv is None:
        return InstallResult(
            ok=False,
            engine=name,
            log="",
            hint=f"no installer found; install python and run: pip install {spec.package}",
        )

    collected = _Log(progress, stages, engine=name, sequence=stages_for(name))
    execute = runner or run_argv
    await collected.stage(STAGE_RESOLVE, f"$ {' '.join(argv)}")
    if spec.runtime:
        await collected.stage(STAGE_RUNTIME)
        if not await runtime.ensure_runtime(home, collected, runner=execute):
            return InstallResult(
                ok=False,
                engine=name,
                log=collected.text,
                hint=f"could not create {runtime.runtime_dir(home)}; is python venv available?",
            )
    await collected.stage(STAGE_INSTALL)
    try:
        code = await asyncio.wait_for(execute(argv, collected), timeout=INSTALL_TIMEOUT_SEC)
    except TimeoutError:
        await collected.say(f"timed out after {INSTALL_TIMEOUT_SEC:g}s")
        return InstallResult(ok=False, engine=name, log=collected.text, hint=spec.hint(platform))
    except Exception as exc:  # noqa: BLE001 - a broken installer is a failed install
        await collected.say(f"{type(exc).__name__}: {exc}")
        return InstallResult(ok=False, engine=name, log=collected.text, hint=spec.hint(platform))
    if code != 0:
        await collected.say(f"exited {code}")
        return InstallResult(ok=False, engine=name, log=collected.text, hint=spec.hint(platform))

    model_id = MODEL_ENGINES.get(name)
    if model_id is not None:
        from snowpea_core.audio import stt_models

        if not await stt_models.ensure_model(
            model_id, home, progress=collected, fetch=fetch, log=collected
        ):
            await collected.say(f"{name}: the package is installed but its model is not")
            return InstallResult(
                ok=False,
                engine=name,
                log=collected.text,
                hint="re-run the install to resume the download",
            )

    voice: Path | None = None
    if name == "piper":
        await collected.stage(STAGE_DOWNLOAD)
        voice = await download_voice(home, progress=collected, fetch=fetch)
        if voice is not None:
            await collected.say(f"voice ready: {voice}")
    if name == "supertonic":
        if not await warmup_supertonic(home, collected, runner=execute):
            return InstallResult(
                ok=False,
                engine=name,
                log=collected.text,
                hint="re-run the install to resume the model download",
            )
    await collected.stage(STAGE_CHECK)
    await collected.say(f"{name} installed")
    result = InstallResult(ok=True, engine=name, log=collected.text)
    # The caller records the voice; returning it on the result would widen the
    # wire shape for one engine.
    setattr(result, "voice_path", voice)  # noqa: B010 - deliberate side channel
    return result


async def warmup_supertonic(
    home: Path | str,
    log: _Log,
    *,
    runner: Runner | None = None,
) -> bool:
    """Download Supertonic's ONNX assets with streamed progress, or skip if ready."""
    home_path = Path(home).expanduser()
    if supertonic_models_ready(home_path):
        await log.say("voice models are already downloaded")
        return True
    if not runtime.has_module(home_path, RUNTIME_MODULES["supertonic"]):
        await log.say("supertonic is not installed in the audio runtime")
        return False
    execute = runner or run_argv
    await log.stage(STAGE_DOWNLOAD, "downloading voice models from Hugging Face")
    argv = [str(runtime.runtime_python(home_path)), "-c", SUPERTONIC_WARMUP_SCRIPT]
    try:
        code = await asyncio.wait_for(execute(argv, log), timeout=INSTALL_TIMEOUT_SEC)
    except TimeoutError:
        await log.say(f"timed out after {INSTALL_TIMEOUT_SEC:g}s")
        return False
    if code != 0:
        await log.say(f"warmup exited {code}")
        return False
    stamp = runtime.runtime_dir(home_path) / SUPERTONIC_WARMUP_STAMP
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("ok", encoding="utf-8")
    except OSError as exc:
        await log.say(f"could not write warmup stamp: {exc}")
        return False
    return True


async def warmup(
    engine: str,
    home: Path | str,
    *,
    stages: Stages | None = None,
    runner: Runner | None = None,
) -> InstallResult:
    """Download first-run assets for one engine without reinstalling the package."""
    name = engine_for((engine or "").strip())
    if name not in WARMUP_ENGINES:
        return InstallResult(
            ok=False,
            engine=name or str(engine),
            log="",
            hint=f"{name or engine} has no separate warmup step",
        )
    collected = _Log(stages=stages, engine=name, sequence=(STAGE_DOWNLOAD, STAGE_CHECK))
    ok = await warmup_supertonic(home, collected, runner=runner)
    if not ok:
        return InstallResult(
            ok=False,
            engine=name,
            log=collected.text,
            hint="re-run to resume the model download",
        )
    await collected.stage(STAGE_CHECK)
    await collected.say(f"{name} is ready to speak")
    return InstallResult(ok=True, engine=name, log=collected.text)


__all__ = [
    "CATALOG_ENGINE",
    "MODEL_ENGINES",
    "SHERPA_PACKAGE",
    "DEFAULT_PIPER_VOICE",
    "ENGINES",
    "INSTALL_TIMEOUT_SEC",
    "LOCAL_WHISPER",
    "MAX_LOG_LINES",
    "PIPER_VOICES_ROOT",
    "PIPER_VOICE_BASE",
    "PIP_FALLBACK",
    "PYTHON_INSTALLERS",
    "RUNTIME_MODULES",
    "STAGE_RUNTIME",
    "VOICES_DIRNAME",
    "EngineInstall",
    "InstallResult",
    "Progress",
    "Runner",
    "WARMUP_ENGINES",
    "SUPERTONIC_WARMUP_STAMP",
    "download_voice",
    "supertonic_models_ready",
    "warmup",
    "warmup_supertonic",
    "engine_for",
    "install",
    "install_hint",
    "install_voice",
    "is_installable",
    "python_install_argv",
    "run_argv",
    "spec_for",
    "voice_files",
]
