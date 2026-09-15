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

The installer chain for a Python package is the same one a user would try by
hand, best first: ``uv tool install``, then ``pipx install``, then
``python -m pip install --user``.  Whichever is on PATH first wins, and the log
says which one ran.
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
PIPER_VOICE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
)

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
    #: False when the package has to be importable by *this* interpreter, so
    #: an isolated ``uv tool`` / ``pipx`` venv will not do.
    isolated: bool = True

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

#: The distribution that provides the ``sherpa-onnx`` / ``sherpa-onnx-offline``
#: CLIs and the Python package behind them.
SHERPA_PACKAGE = "sherpa-onnx"

#: Every engine the voice screens can offer, keyed by id.  An engine absent
#: from this table is not installable and has no hint — the UI shows the row
#: inactive, as it did before.
ENGINES: dict[str, EngineInstall] = {
    # -- speech to text
    "faster-whisper": EngineInstall(engine="faster-whisper", package="faster-whisper"),
    # -- speech to text, sherpa-onnx: one package, three models
    "sherpa-onnx-sensevoice": EngineInstall(
        engine="sherpa-onnx-sensevoice", package=SHERPA_PACKAGE
    ),
    "sherpa-onnx-zipformer-ko": EngineInstall(
        engine="sherpa-onnx-zipformer-ko", package=SHERPA_PACKAGE
    ),
    "sherpa-onnx-zipformer-en": EngineInstall(
        engine="sherpa-onnx-zipformer-en", package=SHERPA_PACKAGE
    ),
    # -- text to speech
    # Supertonic must be importable by *our* interpreter rather than only on
    # PATH, because the engine runs its documented Python API in a child of
    # this interpreter; an isolated `uv tool` venv would hide it.
    "supertonic": EngineInstall(engine="supertonic", package="supertonic", isolated=False),
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

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "engine": self.engine, "log": self.log}
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


def stages_for(engine: str) -> tuple[str, ...]:
    """The stage sequence this engine really walks."""
    name = engine_for(engine)
    if name in MODEL_ENGINES:
        return STAGES_WITH_MODEL
    if name == "piper":
        return STAGES_WITH_VOICE
    return STAGES_PACKAGE


@dataclass
class StageEvent:
    """One progress report: where the install is, and how far into it."""

    engine: str
    stage: str
    step: int
    steps: int
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
    ) -> None:
        self.lines: list[str] = []
        self._progress = progress
        self._stages = stages
        self.engine = engine
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


def python_install_argv(package: str, *, isolated: bool = True) -> list[str] | None:
    """The argv that installs ``package``, or ``None`` with nothing to run it.

    ``uv tool install`` first because it is what this project ships with,
    ``pipx`` next, and ``pip install --user`` last — the same order a person
    would try, and the log names which one was used.
    """
    if not isolated:
        # The daemon has to be able to import it, and `uv tool` / `pipx` put it
        # in a venv of their own where we never would.
        return [*PIP_FALLBACK, package]
    for runner, prefix in PYTHON_INSTALLERS:
        if shutil.which(runner):
            return [*prefix, package]
    if shutil.which(PIP_FALLBACK[0]) or Path(PIP_FALLBACK[0]).exists():
        return [*PIP_FALLBACK, package]
    return None


def voice_files(voice: str = DEFAULT_PIPER_VOICE) -> tuple[str, str]:
    """``(model url, config url)`` for one piper voice."""
    return (f"{PIPER_VOICE_BASE}/{voice}.onnx", f"{PIPER_VOICE_BASE}/{voice}.onnx.json")


async def download_voice(
    home: Path,
    voice: str = DEFAULT_PIPER_VOICE,
    *,
    progress: Progress | None = None,
    fetch: Any = None,
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
            for url, path in zip(voice_files(voice), (model, config), strict=True):
                if progress is not None:
                    await progress(f"downloading {url}")
                response = await client.get(url)
                if getattr(response, "status_code", 500) >= 400:
                    if progress is not None:
                        await progress(f"HTTP {response.status_code} for {url}")
                    return None
                path.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001 - a missing voice is not a failed install
        if progress is not None:
            await progress(f"could not download the voice: {type(exc).__name__}: {exc}")
        return None
    return model


def _httpx_client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=120.0, follow_redirects=True)


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

    argv = python_install_argv(spec.package, isolated=spec.isolated)
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
    await collected.stage(STAGE_CHECK)
    await collected.say(f"{name} installed")
    result = InstallResult(ok=True, engine=name, log=collected.text)
    # The caller records the voice; returning it on the result would widen the
    # wire shape for one engine.
    setattr(result, "voice_path", voice)  # noqa: B010 - deliberate side channel
    return result


__all__ = [
    "CATALOG_ENGINE",
    "MODEL_ENGINES",
    "SHERPA_PACKAGE",
    "DEFAULT_PIPER_VOICE",
    "ENGINES",
    "INSTALL_TIMEOUT_SEC",
    "LOCAL_WHISPER",
    "MAX_LOG_LINES",
    "PIPER_VOICE_BASE",
    "PIP_FALLBACK",
    "PYTHON_INSTALLERS",
    "VOICES_DIRNAME",
    "EngineInstall",
    "InstallResult",
    "Progress",
    "Runner",
    "download_voice",
    "engine_for",
    "install",
    "install_hint",
    "is_installable",
    "python_install_argv",
    "run_argv",
    "spec_for",
    "voice_files",
]
