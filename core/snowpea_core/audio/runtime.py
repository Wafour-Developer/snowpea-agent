"""The interpreter snowpea owns, for voice engines that are Python APIs.

Two of the local voice families are not command line tools at all.  ``sherpa-
onnx`` ships a Python package and a ``sherpa-onnx-cli`` that does not even
import without ``click``; ``supertonic`` documents a Python API and nothing
else.  Both were being installed as if they were CLIs, which is why a
successful install left the wizard still offering to install them: nothing was
ever going to appear on ``PATH``, and the daemon's own interpreter — a ``uv
tool`` venv with no ``pip`` in it — could not be installed into either.

So snowpea keeps its own interpreter for them, at
``$SNOWPEA_HOME/audio-runtime``.  It is created on first use, by ``uv venv``
when uv is around and ``python -m venv`` otherwise, and the engines run their
documented Python API in a child of *that* interpreter.  The daemon's own
environment is never written to, nothing lands in a user site-packages, and
"is it installed?" becomes a question about a directory we control rather than
about ``PATH``.

Detection is a filesystem probe, never a subprocess: availability is asked on
every capabilities call, and spawning an interpreter to answer it would make
opening the voice screen cost a process per engine.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

log = logging.getLogger("snowpea.audio.runtime")

#: The runtime's directory name under ``$SNOWPEA_HOME``.
RUNTIME_DIRNAME = "audio-runtime"

#: How long creating the venv may take before it is given up on.  Creating a
#: venv is fast; ``uv venv`` downloading an interpreter is not, hence minutes
#: rather than seconds.
CREATE_TIMEOUT_SEC = 300.0

#: Where a venv puts its interpreter, per platform.
_BIN = "Scripts" if sys.platform == "win32" else "bin"
_PYTHON = "python.exe" if sys.platform == "win32" else "python"

#: Called with each line of output; the same shape as ``install.Progress``.
Progress = Callable[[str], Awaitable[None]]

#: Runs one argv and streams its lines, returning the exit status.  Swapped
#: out in tests so nothing is created.
Runner = Callable[[Sequence[str], Progress | None], Awaitable[int]]


def runtime_dir(home: Path | str) -> Path:
    """The runtime venv's directory for this home."""
    return Path(home).expanduser() / RUNTIME_DIRNAME


def runtime_python(home: Path | str) -> Path:
    """The interpreter inside the runtime venv, whether or not it exists yet."""
    return runtime_dir(home) / _BIN / _PYTHON


def runtime_ready(home: Path | str) -> bool:
    """True when the runtime venv is there and has an interpreter in it."""
    return runtime_python(home).is_file()


def site_packages(home: Path | str) -> list[Path]:
    """Every ``site-packages`` directory inside the runtime, newest layout first."""
    root = runtime_dir(home)
    found = sorted(root.glob("lib/python*/site-packages"))
    windows = root / "Lib" / "site-packages"
    if windows.is_dir():
        found.append(windows)
    return [path for path in found if path.is_dir()]


def has_module(home: Path | str, module: str) -> bool:
    """True when ``module`` is importable by the runtime interpreter.

    A directory listing rather than a subprocess: this is asked once per engine
    on every capabilities call, and a process per answer would be paid by every
    user who merely opens the voice screen.  The two shapes a top level module
    takes on disk are a package directory and a single ``.py``; both are here.
    """
    name = (module or "").strip()
    if not name:
        return False
    for parent in site_packages(home):
        candidate = parent / name
        if candidate.is_dir() or candidate.with_suffix(".py").is_file():
            return True
    return False


def create_argv(home: Path | str) -> list[list[str]]:
    """The commands that would create the runtime, best first.

    ``uv venv`` when uv is on PATH, because uv is what this project ships with
    and it brings its own interpreter if the machine has none.  Otherwise
    ``python -m venv``, which works even from inside a pip-less venv because
    ``venv`` runs ``ensurepip`` in the *new* environment rather than needing
    pip in the old one.  ``python3`` last, for the case where the daemon is
    running from something that cannot create a venv at all.
    """
    target = str(runtime_dir(home))
    candidates: list[list[str]] = []
    if shutil.which("uv"):
        candidates.append(["uv", "venv", target])
    candidates.append([sys.executable, "-m", "venv", target])
    fallback = shutil.which("python3")
    if fallback and fallback != sys.executable:
        candidates.append([fallback, "-m", "venv", target])
    return candidates


def runtime_install_argv(home: Path | str, package: str) -> list[str]:
    """The argv that installs ``package`` into the runtime.

    ``uv pip install --python`` when uv is there, plain ``pip`` in the runtime
    otherwise.  The runtime's own interpreter is named either way, so the
    package lands where the engines look for it and nowhere else.
    """
    python = str(runtime_python(home))
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", python, package]
    return [python, "-m", "pip", "install", package]


async def ensure_runtime(
    home: Path | str,
    progress: Progress | None = None,
    *,
    runner: Runner | None = None,
) -> bool:
    """Create the runtime venv if it is not already there.

    Idempotent: an existing directory with an interpreter in it is ready, and
    saying so costs a ``stat``.  Every candidate in :func:`create_argv` is
    tried in turn, so a machine without uv still gets a runtime.
    """
    target = runtime_dir(home)
    if runtime_ready(home):
        if progress is not None:
            await progress(f"audio runtime ready: {target}")
        return True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if progress is not None:
            await progress(f"cannot write {target.parent}: {exc}")
        return False

    execute = runner or _default_runner()
    for argv in create_argv(home):
        if progress is not None:
            await progress(f"$ {' '.join(argv)}")
        try:
            code = await asyncio.wait_for(execute(argv, progress), timeout=CREATE_TIMEOUT_SEC)
        except TimeoutError:
            if progress is not None:
                await progress(f"{argv[0]}: timed out after {CREATE_TIMEOUT_SEC:g}s")
            continue
        except Exception as exc:  # noqa: BLE001 - a broken creator is a failed create
            if progress is not None:
                await progress(f"{argv[0]}: {type(exc).__name__}: {exc}")
            continue
        if code == 0 and runtime_ready(home):
            if progress is not None:
                await progress(f"audio runtime created: {target}")
            return True
        if progress is not None:
            await progress(f"{argv[0]} exited {code}")
    if progress is not None:
        await progress(f"could not create the audio runtime at {target}")
    return False


def _default_runner() -> Runner:
    from snowpea_core.audio.install import run_argv

    return run_argv


__all__ = [
    "CREATE_TIMEOUT_SEC",
    "RUNTIME_DIRNAME",
    "Progress",
    "Runner",
    "create_argv",
    "ensure_runtime",
    "has_module",
    "runtime_dir",
    "runtime_install_argv",
    "runtime_python",
    "runtime_ready",
    "site_packages",
]
