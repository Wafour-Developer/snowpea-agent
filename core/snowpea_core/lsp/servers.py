"""The language-server registry: what to launch, and where its root is.

Ported from opencode's ``packages/opencode/src/lsp/server.ts`` and
``lsp/launch.ts`` (MIT, commit 95daf90).  Upstream writes one ``spawn()``
closure per server, each with its own bespoke download logic; here a server is
*data* — the binaries to look for, the arguments they take, the markers that
locate their project root — and one :func:`spawn` drives all of them.  That is
what makes ``lsp.servers`` in settings.json able to add a server without any
code, and it is what keeps AC-46 checkable: there is exactly one place that
starts a process, and it takes an argv list.

Auto-install (``lsp.autoInstall``, default off, or an explicit ``lsp.install``)
is limited to package managers that fetch the server themselves — npm, pip, go,
``dotnet tool`` and ``rustup component`` — into ``$SNOWPEA_HOME/lsp/`` (rustup
installs into its own toolchain).  Servers upstream only knows how to obtain by
downloading and unzipping a release (zls, elixir-ls, terraform, …) are PATH-only
here; see ``docs/design/deviations/CORE-lsp.md``.

# MIT License
#
# Copyright (c) 2025 opencode
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("snowpea.lsp.servers")

#: How long an auto-install may take before it is given up on.
INSTALL_TIMEOUT_SEC = 300.0

#: A binary on PATH (or in the managed bin directory) plus its stdio arguments.
Candidate = tuple[str, tuple[str, ...]]

#: Package managers :func:`spawn` will drive when ``lsp.autoInstall`` is on.
InstallKind = Literal["npm", "pip", "go", "dotnet", "rustup"]

#: The tool each install kind runs, which must be on PATH (pip makes its own venv).
INSTALLER_TOOLS: dict[str, str | None] = {
    "npm": "npm",
    "pip": None,
    "go": "go",
    "dotnet": "dotnet",
    "rustup": "rustup",
}

#: Called with each line an installer prints.
OnLine = Callable[[str], Any]


@dataclass(frozen=True)
class Install:
    """How to obtain a server that is not on PATH (opt-in, ``lsp.autoInstall``)."""

    kind: InstallKind
    #: What the package manager is asked for.
    package: str
    #: Executable the package drops, looked for under the managed prefix.
    binary: str
    #: Arguments the installed binary takes; defaults to the candidate's.
    args: tuple[str, ...] = ()
    #: More packages installed in the same call (npm only), e.g. ``typescript``
    #: next to ``typescript-language-server``.
    extra: tuple[str, ...] = ()
    #: dotnet only: ``(newest SDK major, version)`` pairs, oldest first; an SDK
    #: at or below a major installs that version instead of the latest.
    sdk_pins: tuple[tuple[int, str], ...] = ()
    #: Auto-install only in a project that has one of these files (a linter's
    #: config), and leave it out of ``lsp.install``'s default set.
    when: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerInfo:
    """One language server: what it serves, where it lives, how it starts."""

    id: str
    #: Extensions (``".py"``) or whole filenames (``"Dockerfile"``) it serves.
    #: Empty means "every file", which only a user-defined server should be.
    extensions: tuple[str, ...] = ()
    #: Filenames whose nearest containing directory is the project root.
    root_markers: tuple[str, ...] = ()
    #: When one of these is found at or above the file first, this server
    #: declines the file entirely (upstream's ``excludePatterns``).
    exclude_markers: tuple[str, ...] = ()
    #: True: no marker means "not my project" (upstream ``StrictNearestRoot``).
    #: False: fall back to the session working directory (``NearestRoot``).
    strict_root: bool = False
    #: PATH binaries tried in order with the arguments each one needs.
    candidates: tuple[Candidate, ...] = ()
    install: Install | None = None
    #: Builds ``initializationOptions`` from the resolved root.
    initialization: Callable[[Path], dict[str, Any]] | None = None
    #: A check the project itself must pass before the server is worth
    #: starting — upstream returns ``undefined`` from ``spawn`` for these.
    #: typescript-language-server, for one, exits during ``initialize`` unless
    #: the workspace has its own TypeScript.
    precondition: Callable[[Path], bool] | None = None
    #: Extra environment for the child process, merged over ``os.environ``.
    env: dict[str, str] = field(default_factory=dict)
    #: For a server with no :class:`Install`: the toolchain that provides it,
    #: shown by ``lsp.catalog`` as ``needs`` while it is not found.
    needs: str | None = None

    def serves(self, extension: str) -> bool:
        """True when this server claims files with ``extension``."""
        if not self.extensions:
            return True
        lowered = extension.lower()
        return any(item.lower() == lowered for item in self.extensions)


# ---------------------------------------------------------------------------
# roots
# ---------------------------------------------------------------------------


def _walk_up(start: Path, stop: Path) -> Iterable[Path]:
    """``start`` and every parent up to and including ``stop``."""
    current = start
    seen: set[Path] = set()
    while True:
        if current in seen:
            break
        seen.add(current)
        yield current
        if current == stop or current.parent == current:
            break
        current = current.parent


def _first_marker(start: Path, stop: Path, markers: Iterable[str]) -> Path | None:
    """Nearest directory at or above ``start`` holding one of ``markers``."""
    names = tuple(markers)
    if not names:
        return None
    for directory in _walk_up(start, stop):
        for name in names:
            if "*" in name:
                if next(directory.glob(name), None) is not None:
                    return directory
            elif (directory / name).exists():
                return directory
    return None


def find_root(server: ServerInfo, file: Path, workdir: Path) -> Path | None:
    """Project root for ``file``, or ``None`` when ``server`` declines it.

    Mirrors upstream's ``NearestRoot`` / ``StrictNearestRoot``: an exclude
    marker found at or above the file takes the server out of the running, and
    a non-strict server with no marker falls back to the working directory.
    """
    start = file.parent if file.parent != file else file
    if server.exclude_markers and _first_marker(start, workdir, server.exclude_markers):
        return None
    found = _first_marker(start, workdir, server.root_markers)
    if found is not None:
        return found
    return None if server.strict_root else workdir


# ---------------------------------------------------------------------------
# binaries
# ---------------------------------------------------------------------------


def lsp_home(home: Path) -> Path:
    """``$SNOWPEA_HOME/lsp`` — where auto-installed servers land."""
    return Path(home) / "lsp"


def _managed_prefixes(home: Path) -> tuple[Path, ...]:
    """Directories an auto-installed binary can be found in, PATH order."""
    root = lsp_home(home)
    return (
        root / "bin",
        root / "node_modules" / ".bin",
        root / "py" / "bin",
        root / "py" / "Scripts",
    )


def which(binary: str, home: Path | None = None) -> str | None:
    """``shutil.which`` widened to the managed install directories.

    ``shutil.which`` with a ``path`` also applies ``PATHEXT`` on Windows, so
    ``npm``'s ``.cmd`` shim and a ``dotnet tool``'s ``.exe`` are found too.
    """
    found = shutil.which(binary)
    if found:
        return found
    if home is None:
        return None
    for prefix in _managed_prefixes(home):
        if prefix.is_dir():
            found = shutil.which(binary, path=str(prefix))
            if found:
                return found
    return None


def is_managed(path: str | None, home: Path) -> bool:
    """True when ``path`` lies under ``$SNOWPEA_HOME/lsp``."""
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(lsp_home(home).resolve())
    except (OSError, ValueError):
        return False


async def _exec(
    argv: list[str], *, env: dict[str, str] | None = None, on_line: OnLine | None = None
) -> tuple[bool, str]:
    """Run ``argv`` to completion: ``(exit 0, output tail)``.  Never raises, never shells out.

    Each output line goes to ``on_line`` as it is printed.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )
    except (OSError, ValueError) as exc:
        log.debug("could not run %s: %s", argv[0], exc)
        return False, f"could not run {Path(argv[0]).name}: {exc}"
    lines: list[str] = []

    async def read() -> None:
        assert process.stdout is not None
        while True:
            try:
                raw = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # A line past the stream limit: drop what is buffered, go on.
                raw = await process.stdout.read(64 * 1024)
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            del lines[:-40]
            if on_line is not None and line:
                with contextlib.suppress(Exception):
                    result = on_line(line)
                    if asyncio.iscoroutine(result):
                        await result
        await process.wait()

    try:
        await asyncio.wait_for(read(), timeout=INSTALL_TIMEOUT_SEC)
    except TimeoutError:
        await _kill(process)
        log.warning("%s timed out after %ss", argv[0], INSTALL_TIMEOUT_SEC)
        return False, f"{Path(argv[0]).name} timed out after {INSTALL_TIMEOUT_SEC:g}s"
    except BaseException:
        # Cancelled: an installer is never left running unwatched.
        await asyncio.shield(_kill(process))
        raise
    tail = "\n".join(lines)[-400:]
    if process.returncode != 0:
        log.warning("%s failed (%s): %s", argv[0], process.returncode, tail)
        last = next((line for line in reversed(lines) if line.strip()), "")
        return False, f"{Path(argv[0]).name} exited {process.returncode}: {last[:200]}"
    return True, tail


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        await process.wait()


async def _run(argv: list[str], *, env: dict[str, str] | None = None) -> bool:
    """Run ``argv`` to completion; True on exit 0."""
    ok, _ = await _exec(argv, env=env)
    return ok


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_pip(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "pip.exe"
    return venv / "bin" / "pip"


def _base_python() -> str | None:
    """A Python that can make a venv: this one, unless core is a frozen bundle."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python3") or shutil.which("python")


async def _pip_install(spec: Install, root: Path, on_line: OnLine | None) -> str | None:
    """Install a pip package into ``<root>/py``; ``None`` on success, else why not."""
    venv = root / "py"
    uv = shutil.which("uv")
    if not _venv_python(venv).is_file():
        frozen = getattr(sys, "frozen", False)
        python = _base_python()
        ok, error = False, "needs Python 3 (python3 or uv on PATH)"
        # A frozen core has no venv of its own to copy: uv first, since a
        # PATH python3 may be the Windows Store alias or the macOS CLT stub.
        if frozen and uv is not None:
            ok, error = await _exec([uv, "venv", str(venv)], on_line=on_line)
        if not ok and python is not None:
            ok, error = await _exec([python, "-m", "venv", str(venv)], on_line=on_line)
        if not ok and not frozen and uv is not None:
            ok, error = await _exec([uv, "venv", str(venv)], on_line=on_line)
        if not ok:
            return error
    pip = _venv_pip(venv)
    if pip.is_file():
        argv = [str(pip), "install", "--upgrade", spec.package]
    elif uv is not None:
        # ``uv venv`` makes a venv without pip.
        argv = [uv, "pip", "install", "--python", str(_venv_python(venv)), spec.package]
    else:
        return f"no pip in {venv}"
    ok, error = await _exec(argv, on_line=on_line)
    return None if ok else error


async def _dotnet_sdk_major(dotnet: str) -> int | None:
    """The major version of the SDK ``dotnet --version`` picks, or ``None``."""
    ok, output = await _exec([dotnet, "--version"])
    if not ok:
        return None
    head = output.strip().splitlines()[-1:] or [""]
    major = head[0].split(".", 1)[0]
    return int(major) if major.isdigit() else None


def _pinned_version(spec: Install, sdk_major: int | None) -> str | None:
    if sdk_major is None:
        return None
    for newest, version in spec.sdk_pins:
        if sdk_major <= newest:
            return version
    return None


async def install_server(
    spec: Install, home: Path, *, on_line: OnLine | None = None
) -> tuple[str | None, str | None]:
    """Install ``spec`` and return ``(binary path, None)``, or ``(None, why not)``.

    npm, pip, go and dotnet install under ``$SNOWPEA_HOME/lsp``; rustup adds a
    component to the active toolchain.  Every tool is resolved on PATH first,
    so Windows runs ``npm.cmd``.  One install at a time per event loop, for
    ``lsp.install`` and auto-install alike.  Never raises.
    """
    async with _install_lock():
        return await _install_server(spec, home, on_line=on_line)


_INSTALL_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _install_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _INSTALL_LOCKS.get(loop)
    if lock is None:
        lock = _INSTALL_LOCKS[loop] = asyncio.Lock()
    return lock


async def _install_server(
    spec: Install, home: Path, *, on_line: OnLine | None = None
) -> tuple[str | None, str | None]:
    root = lsp_home(home)
    root.mkdir(parents=True, exist_ok=True)
    tool_name = INSTALLER_TOOLS.get(spec.kind)
    tool = shutil.which(tool_name) if tool_name else None
    if tool_name and tool is None:
        return None, f"needs {tool_name} on PATH"
    error: str | None
    if spec.kind == "npm":
        # Saved into a package.json of its own: npm prunes an unsaved package
        # on the next install into the same prefix.
        manifest = root / "package.json"
        if not manifest.exists():
            manifest.write_text('{"name": "snowpea-lsp", "private": true}\n')
        ok, error = await _exec(
            [tool, "install", "--prefix", str(root), spec.package, *spec.extra],  # type: ignore[list-item]
            on_line=on_line,
        )
        error = None if ok else error
    elif spec.kind == "pip":
        error = await _pip_install(spec, root, on_line)
    elif spec.kind == "go":
        ok, error = await _exec(
            [tool, "install", spec.package],  # type: ignore[list-item]
            env={"GOBIN": str(root / "bin")},
            on_line=on_line,
        )
        error = None if ok else error
    elif spec.kind == "dotnet":
        bin_dir = str(root / "bin")
        version = _pinned_version(spec, await _dotnet_sdk_major(tool))  # type: ignore[arg-type]
        pin = ["--version", version] if version else []
        ok, error = await _exec(
            [tool, "tool", "install", "--tool-path", bin_dir, spec.package, *pin],  # type: ignore[list-item]
            on_line=on_line,
        )
        if not ok:
            # Already there: ``install`` refuses, ``update`` brings it current.
            ok, error = await _exec(
                [tool, "tool", "update", "--tool-path", bin_dir, spec.package, *pin],  # type: ignore[list-item]
                on_line=on_line,
            )
        error = None if ok else error
    elif spec.kind == "rustup":
        ok, error = await _exec(
            [tool, "component", "add", spec.package],  # type: ignore[list-item]
            on_line=on_line,
        )
        if not ok:
            return None, error
        path = await rustup_which(spec.binary)
        return (path, None) if path else (None, f"rustup has no {spec.binary} after the install")
    else:  # pragma: no cover - InstallKind is exhaustive
        return None, f"unknown install kind {spec.kind}"
    if error is not None:
        return None, error
    path = which(spec.binary, home)
    return (path, None) if path else (None, f"{spec.binary} not found after the install")


async def auto_install(spec: Install, home: Path) -> str | None:
    """Install ``spec`` and return the binary path, or ``None``.

    Called when ``lsp.autoInstall`` is true (AC-46) and by an explicit
    ``lsp.install``.  Any failure — missing package manager, network, non-zero
    exit — returns ``None``; a language server that cannot be installed is
    never an error.
    """
    path, _error = await install_server(spec, home)
    return path


# ---------------------------------------------------------------------------
# rustup proxies
# ---------------------------------------------------------------------------


def is_rustup_proxy(path: str) -> bool:
    """True when ``path`` is rustup's proxy for a tool (``~/.cargo/bin/rust-analyzer``).

    The proxy is the ``rustup`` binary itself, linked or copied under the
    tool's name; it exits with "Unknown binary" when the component is missing.
    """
    candidate = Path(path)
    rustup = candidate.with_name("rustup" + candidate.suffix)
    try:
        if candidate.resolve().stem == "rustup":
            return True
        return rustup.is_file() and os.path.samefile(candidate, rustup)
    except OSError:
        return False


async def rustup_which(binary: str, cwd: Path | None = None) -> str | None:
    """``rustup which <binary>``: the toolchain's real binary, or ``None`` when absent."""
    rustup = shutil.which("rustup")
    if rustup is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            rustup,
            "which",
            binary,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
    except (OSError, ValueError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    found = output.decode(errors="replace").strip()
    return found if found and Path(found).is_file() else None


async def locate(
    server: ServerInfo, home: Path, cwd: Path | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """The first candidate binary that is really there, with its arguments.

    A rustup proxy whose component is not installed does not count.
    """
    for binary, args in server.candidates:
        found = which(binary, home)
        if not found:
            continue
        if is_rustup_proxy(found) and await rustup_which(binary, cwd) is None:
            continue
        return found, args
    return None


def needs(server: ServerInfo) -> str | None:
    """What has to be on PATH before ``server`` can be had at all."""
    if server.install is None:
        return server.needs or (server.candidates[0][0] if server.candidates else None)
    tool = INSTALLER_TOOLS.get(server.install.kind)
    if tool and shutil.which(tool) is None:
        return tool
    if server.install.kind == "pip" and _base_python() is None and shutil.which("uv") is None:
        return "python3"
    return None


async def resolve_command(
    server: ServerInfo, *, home: Path, allow_install: bool, root: Path | None = None
) -> list[str] | None:
    """The argv that starts ``server``, or ``None`` when it is unavailable.

    PATH is always consulted first.  ``allow_install`` is the ``lsp.autoInstall``
    setting; when it is false nothing is downloaded, ever.  ``root`` is the
    project: rustup answers for its pinned toolchain, and a linter is only
    installed where its config is.
    """
    found = await locate(server, home, root)
    if found is not None:
        return [found[0], *found[1]]
    if not allow_install or server.install is None:
        return None
    if server.install.when and (
        root is None or _first_marker(root, Path(root.anchor), server.install.when) is None
    ):
        return None
    log.info("installing language server %s (%s)", server.id, server.install.package)
    path = await auto_install(server.install, home)
    if path is None:
        return None
    return [path, *install_args(server)]


def install_args(server: ServerInfo) -> tuple[str, ...]:
    """The arguments the installed binary takes: the install's, else its candidate's."""
    spec = server.install
    if spec is None:
        return ()
    if spec.args:
        return spec.args
    for binary, args in server.candidates:
        if binary == spec.binary:
            return args
    return server.candidates[0][1] if server.candidates else ()


#: ``ServerInfo.precondition`` hooks that also look in the managed install.
_HOME_AWARE: set[Callable[..., Any]] = set()


def precondition_holds(server: ServerInfo, root: Path, home: Path | None = None) -> bool:
    if server.precondition is None:
        return True
    if server.precondition in _HOME_AWARE:
        return bool(server.precondition(root, home=home))
    return bool(server.precondition(root))


def initialization_for(
    server: ServerInfo, root: Path, home: Path | None = None
) -> dict[str, Any]:
    if server.initialization is None:
        return {}
    if server.initialization in _HOME_AWARE:
        return server.initialization(root, home=home)
    return server.initialization(root)


async def start_server(
    server: ServerInfo,
    root: Path,
    *,
    home: Path,
    allow_install: bool,
) -> tuple[asyncio.subprocess.Process | None, str | None]:
    """Start ``server`` in ``root``: ``(process, None)`` or ``(None, why not)``.

    The single process-creating call in the LSP layer.  It is
    ``create_subprocess_exec`` with an argv list and never a shell string
    (AC-46); a missing server is a reason, never an exception (contract §2).
    The binary is resolved (and maybe installed) before the precondition is
    checked, since the install can bring the managed TypeScript with it.
    """
    argv = await resolve_command(
        server, home=home, allow_install=allow_install, root=root
    )
    if argv is None:
        log.debug("no binary for language server %s", server.id)
        return None, "not installed"
    if not precondition_holds(server, root, home):
        log.debug("%s declines %s: its precondition does not hold", server.id, root)
        return None, "not for this project"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **server.env},
        )
    except (OSError, ValueError) as exc:
        log.warning("could not start language server %s: %s", server.id, exc)
        return None, f"could not start: {exc}"
    return process, None


async def spawn(
    server: ServerInfo,
    root: Path,
    *,
    home: Path,
    allow_install: bool,
) -> asyncio.subprocess.Process | None:
    """Start ``server`` in ``root``, or return ``None`` when it is not installed."""
    process, _reason = await start_server(
        server, root, home=home, allow_install=allow_install
    )
    return process


# ---------------------------------------------------------------------------
# initialization options
# ---------------------------------------------------------------------------


def find_tsserver(root: Path, home: Path | None = None) -> Path | None:
    """The workspace's own ``typescript/lib/tsserver.js``, searching upwards.

    typescript-language-server refuses to initialize without it, and astro-ls
    wants the same directory as its ``typescript.tsdk``.  With ``home``, a
    project without its own TypeScript falls back to the one installed next
    to typescript-language-server under ``$SNOWPEA_HOME/lsp``.
    """
    for directory in _walk_up(root, Path(root.anchor)):
        candidate = directory / "node_modules" / "typescript" / "lib" / "tsserver.js"
        if candidate.is_file():
            return candidate
    if home is not None:
        managed = lsp_home(home) / "node_modules" / "typescript" / "lib" / "tsserver.js"
        if managed.is_file():
            return managed
    return None


def has_tsserver(root: Path, home: Path | None = None) -> bool:
    """Precondition for the TypeScript-backed servers."""
    return find_tsserver(root, home) is not None


def tsserver_options(root: Path, home: Path | None = None) -> dict[str, Any]:
    """``initializationOptions`` pointing at the workspace's tsserver."""
    found = find_tsserver(root, home)
    return {"tsserver": {"path": str(found)}} if found else {}


def tsdk_options(root: Path, home: Path | None = None) -> dict[str, Any]:
    """astro-ls wants the *directory* holding tsserver.js, as ``typescript.tsdk``."""
    found = find_tsserver(root, home)
    return {"typescript": {"tsdk": str(found.parent)}} if found else {}


_HOME_AWARE.update({has_tsserver, tsserver_options, tsdk_options})


def python_path(root: Path) -> dict[str, Any]:
    """``{"pythonPath": ...}`` for the virtualenv serving ``root``, if any.

    Upstream does this for pyright and ty; without it a server analyses the
    project against the system interpreter and reports every third-party
    import as missing.
    """
    candidates = [os.environ.get("VIRTUAL_ENV"), str(root / ".venv"), str(root / "venv")]
    for venv in candidates:
        if not venv:
            continue
        for relative in (Path("bin") / "python", Path("Scripts") / "python.exe"):
            interpreter = Path(venv) / relative
            if interpreter.is_file():
                return {"pythonPath": str(interpreter)}
    return {}


# ---------------------------------------------------------------------------
# the builtin catalog
# ---------------------------------------------------------------------------

#: Lockfiles that mark a JavaScript project root, in upstream's order.
_JS_ROOTS = (
    "package-lock.json",
    "bun.lockb",
    "bun.lock",
    "pnpm-lock.yaml",
    "yarn.lock",
)
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_DENO_ROOTS = ("deno.json", "deno.jsonc")

_ESLINT_CONFIGS = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)
_BIOME_CONFIGS = ("biome.json", "biome.jsonc")

BUILTIN: tuple[ServerInfo, ...] = (
    ServerInfo(
        id="typescript",
        extensions=_JS_EXTENSIONS,
        root_markers=_JS_ROOTS,
        exclude_markers=_DENO_ROOTS,
        candidates=(("typescript-language-server", ("--stdio",)),),
        install=Install(
            "npm",
            "typescript-language-server",
            "typescript-language-server",
            ("--stdio",),
            extra=("typescript",),
        ),
        initialization=tsserver_options,
        precondition=has_tsserver,
    ),
    ServerInfo(
        id="deno",
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs"),
        root_markers=_DENO_ROOTS,
        strict_root=True,
        candidates=(("deno", ("lsp",)),),
        needs="deno",
    ),
    ServerInfo(
        id="vue",
        extensions=(".vue",),
        root_markers=_JS_ROOTS,
        candidates=(("vue-language-server", ("--stdio",)),),
        install=Install("npm", "@vue/language-server", "vue-language-server"),
    ),
    ServerInfo(
        id="eslint",
        extensions=(*_JS_EXTENSIONS, ".vue"),
        root_markers=_JS_ROOTS,
        candidates=(("vscode-eslint-language-server", ("--stdio",)),),
        install=Install(
            "npm",
            "vscode-langservers-extracted",
            "vscode-eslint-language-server",
            ("--stdio",),
            when=_ESLINT_CONFIGS,
        ),
    ),
    ServerInfo(
        id="biome",
        extensions=(
            *_JS_EXTENSIONS,
            ".json",
            ".jsonc",
            ".vue",
            ".astro",
            ".svelte",
            ".css",
            ".graphql",
            ".gql",
            ".html",
        ),
        root_markers=(*_BIOME_CONFIGS, *_JS_ROOTS),
        strict_root=True,
        candidates=(("biome", ("lsp-proxy",)),),
        install=Install("npm", "@biomejs/biome", "biome", ("lsp-proxy",), when=_BIOME_CONFIGS),
    ),
    ServerInfo(
        id="oxlint",
        extensions=(*_JS_EXTENSIONS, ".vue", ".astro", ".svelte"),
        root_markers=(".oxlintrc.json", *_JS_ROOTS, "package.json"),
        strict_root=True,
        # oxlint 1.x serves LSP itself (``oxlint --lsp``) and ships no
        # oxc_language_server; older releases had only that.
        candidates=(("oxlint", ("--lsp",)), ("oxc_language_server", ())),
        install=Install("npm", "oxlint", "oxlint", ("--lsp",), when=(".oxlintrc.json",)),
    ),
    ServerInfo(
        id="gopls",
        extensions=(".go",),
        root_markers=("go.work", "go.mod", "go.sum"),
        candidates=(("gopls", ()),),
        install=Install("go", "golang.org/x/tools/gopls@latest", "gopls"),
    ),
    ServerInfo(
        id="ruby-lsp",
        extensions=(".rb", ".rake", ".gemspec", ".ru"),
        root_markers=("Gemfile",),
        candidates=(("ruby-lsp", ()), ("rubocop", ("--lsp",))),
        needs="gem install ruby-lsp",
    ),
    ServerInfo(
        id="pyright",
        extensions=(".py", ".pyi"),
        root_markers=(
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
            "pyrightconfig.json",
        ),
        candidates=(
            ("pyright-langserver", ("--stdio",)),
            ("basedpyright-langserver", ("--stdio",)),
        ),
        install=Install("npm", "pyright", "pyright-langserver", ("--stdio",)),
        initialization=python_path,
    ),
    ServerInfo(
        id="ty",
        extensions=(".py", ".pyi"),
        root_markers=(
            "pyproject.toml",
            "ty.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
            "pyrightconfig.json",
        ),
        candidates=(("ty", ("server",)),),
        install=Install("pip", "ty", "ty", ("server",)),
        initialization=python_path,
    ),
    ServerInfo(
        id="ruff",
        extensions=(".py", ".pyi"),
        root_markers=("pyproject.toml", "ruff.toml", ".ruff.toml"),
        candidates=(("ruff", ("server",)),),
        install=Install("pip", "ruff", "ruff", ("server",)),
    ),
    ServerInfo(
        id="elixir-ls",
        extensions=(".ex", ".exs"),
        root_markers=("mix.exs", "mix.lock"),
        candidates=(("elixir-ls", ()), ("language_server.sh", ())),
        needs="elixir-ls",
    ),
    ServerInfo(
        id="zls",
        extensions=(".zig", ".zon"),
        root_markers=("build.zig",),
        candidates=(("zls", ()),),
        needs="zls",
    ),
    ServerInfo(
        id="csharp",
        extensions=(".cs", ".csx"),
        root_markers=(".slnx", ".sln", "*.csproj", "global.json"),
        candidates=(
            ("Microsoft.CodeAnalysis.LanguageServer", ("--stdio", "--autoLoadProjects")),
            ("csharp-ls", ()),
        ),
        # Newer csharp-ls releases target a newer SDK and fail to install
        # under .NET 8 ("DotnetToolSettings.xml was not found").
        install=Install("dotnet", "csharp-ls", "csharp-ls", sdk_pins=((8, "0.16.0"),)),
    ),
    ServerInfo(
        id="fsharp",
        extensions=(".fs", ".fsi", ".fsx", ".fsscript"),
        root_markers=(".slnx", ".sln", "*.fsproj", "global.json"),
        candidates=(("fsautocomplete", ()),),
        install=Install("dotnet", "fsautocomplete", "fsautocomplete"),
    ),
    ServerInfo(
        id="sourcekit-lsp",
        extensions=(".swift", ".objc", ".objcpp"),
        root_markers=("Package.swift", "*.xcodeproj", "*.xcworkspace"),
        candidates=(("sourcekit-lsp", ()),),
        needs="a Swift toolchain (Xcode)",
    ),
    ServerInfo(
        id="rust",
        extensions=(".rs",),
        root_markers=("Cargo.toml", "Cargo.lock"),
        strict_root=True,
        candidates=(("rust-analyzer", ()),),
        install=Install("rustup", "rust-analyzer", "rust-analyzer"),
    ),
    ServerInfo(
        id="clangd",
        extensions=(
            ".c",
            ".cpp",
            ".cc",
            ".cxx",
            ".c++",
            ".h",
            ".hpp",
            ".hh",
            ".hxx",
            ".h++",
        ),
        root_markers=("compile_commands.json", "compile_flags.txt", ".clangd"),
        candidates=(("clangd", ("--background-index", "--clang-tidy")),),
        needs="clangd (LLVM)",
    ),
    ServerInfo(
        id="svelte",
        extensions=(".svelte",),
        root_markers=_JS_ROOTS,
        candidates=(("svelteserver", ("--stdio",)),),
        install=Install("npm", "svelte-language-server", "svelteserver", ("--stdio",)),
    ),
    ServerInfo(
        id="astro",
        extensions=(".astro",),
        root_markers=_JS_ROOTS,
        candidates=(("astro-ls", ("--stdio",)),),
        install=Install("npm", "@astrojs/language-server", "astro-ls", ("--stdio",)),
        initialization=tsdk_options,
        precondition=has_tsserver,
    ),
    ServerInfo(
        id="yaml-ls",
        extensions=(".yaml", ".yml"),
        root_markers=_JS_ROOTS,
        candidates=(("yaml-language-server", ("--stdio",)),),
        install=Install("npm", "yaml-language-server", "yaml-language-server", ("--stdio",)),
    ),
    ServerInfo(
        id="lua-ls",
        extensions=(".lua",),
        root_markers=(
            ".luarc.json",
            ".luarc.jsonc",
            ".luacheckrc",
            ".stylua.toml",
            "stylua.toml",
            "selene.toml",
            "selene.yml",
        ),
        candidates=(("lua-language-server", ()),),
        needs="lua-language-server",
    ),
    ServerInfo(
        id="bash",
        extensions=(".sh", ".bash", ".zsh", ".ksh"),
        candidates=(("bash-language-server", ("start",)),),
        install=Install("npm", "bash-language-server", "bash-language-server", ("start",)),
    ),
    ServerInfo(
        id="dockerfile",
        extensions=(".dockerfile", "Dockerfile"),
        candidates=(("docker-langserver", ("--stdio",)),),
        install=Install(
            "npm", "dockerfile-language-server-nodejs", "docker-langserver", ("--stdio",)
        ),
    ),
    ServerInfo(
        id="terraform",
        extensions=(".tf", ".tfvars"),
        root_markers=(".terraform.lock.hcl", "terraform.tfstate", "*.tf"),
        candidates=(("terraform-ls", ("serve",)),),
        needs="terraform-ls",
    ),
    ServerInfo(
        id="dart",
        extensions=(".dart",),
        root_markers=("pubspec.yaml", "analysis_options.yaml"),
        candidates=(("dart", ("language-server", "--lsp")),),
        needs="the Dart SDK",
    ),
    ServerInfo(
        id="ocaml-lsp",
        extensions=(".ml", ".mli"),
        root_markers=("dune-project", "dune-workspace", ".merlin", "opam"),
        candidates=(("ocamllsp", ()),),
        needs="opam install ocaml-lsp-server",
    ),
    ServerInfo(
        id="gleam",
        extensions=(".gleam",),
        root_markers=("gleam.toml",),
        candidates=(("gleam", ("lsp",)),),
        needs="gleam",
    ),
    ServerInfo(
        id="clojure-lsp",
        extensions=(".clj", ".cljs", ".cljc", ".edn"),
        root_markers=("deps.edn", "project.clj", "shadow-cljs.edn", "bb.edn", "build.boot"),
        candidates=(("clojure-lsp", ("listen",)),),
        needs="clojure-lsp",
    ),
    ServerInfo(
        id="nixd",
        extensions=(".nix",),
        root_markers=("flake.nix",),
        candidates=(("nixd", ()),),
        needs="nixd",
    ),
    ServerInfo(
        id="prisma",
        extensions=(".prisma",),
        root_markers=("schema.prisma",),
        # Prisma 7 dropped ``prisma language-server``; the server is its own package.
        candidates=(("prisma-language-server", ("--stdio",)),),
        install=Install(
            "npm", "@prisma/language-server", "prisma-language-server", ("--stdio",)
        ),
    ),
    ServerInfo(
        id="haskell-language-server",
        extensions=(".hs", ".lhs"),
        root_markers=("stack.yaml", "cabal.project", "hie.yaml", "*.cabal"),
        candidates=(("haskell-language-server-wrapper", ("--lsp",)),),
        needs="ghcup install hls",
    ),
    ServerInfo(
        id="julials",
        extensions=(".jl",),
        root_markers=("Project.toml", "Manifest.toml"),
        candidates=(
            (
                "julia",
                (
                    "--startup-file=no",
                    "--history-file=no",
                    "-e",
                    "using LanguageServer; runserver()",
                ),
            ),
        ),
        needs="julia with LanguageServer.jl",
    ),
)

#: ``pyright`` and ``ty`` both claim ``.py``; running two Python servers over
#: every edit doubles the latency for one set of diagnostics, so ``ty`` and
#: ``ruff`` are opt-in through ``lsp.enable`` (or an ``lsp.servers`` entry)
#: the way upstream gates ``ty`` behind an experimental flag.
DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"ty", "ruff"})


def _constant(value: dict[str, Any]) -> Callable[[Path], dict[str, Any]]:
    """An ``initialization`` hook that ignores the root and returns ``value``."""
    frozen = dict(value)

    def initialization(_root: Path) -> dict[str, Any]:
        return dict(frozen)

    return initialization


def from_settings(spec: dict[str, Any], server_id: str) -> ServerInfo | None:
    """Build a :class:`ServerInfo` from one ``lsp.servers`` entry.

    ``{"command": ["clangd"], "extensions": [".c"]}``; a missing or empty
    ``command`` is ignored rather than raising, because settings.json is
    hand-edited and a typo there must not stop the daemon.
    """
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) for item in command
    ):
        log.warning("lsp.servers.%s has no usable command; ignoring it", server_id)
        return None
    extensions = spec.get("extensions")
    env = spec.get("env")
    markers = spec.get("rootMarkers")
    initialization = spec.get("initialization")
    return ServerInfo(
        id=server_id,
        extensions=tuple(str(item) for item in extensions) if isinstance(extensions, list) else (),
        root_markers=tuple(str(item) for item in markers) if isinstance(markers, list) else (),
        candidates=((command[0], tuple(command[1:])),),
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        initialization=_constant(initialization) if isinstance(initialization, dict) else None,
    )


def install_hint(install: Install | None) -> str | None:
    """A human-readable command that would obtain ``install`` manually.

    Used by ``lsp.catalog`` to tell a user what to run when they would rather
    install a server themselves than flip on ``lsp.autoInstall``; ``None``
    when the server has no :class:`Install` (PATH-only, contract §4).
    """
    if install is None:
        return None
    if install.kind == "npm":
        return f"npm install -g {install.package}"
    if install.kind == "pip":
        return f"pip install {install.package}"
    if install.kind == "go":
        return f"go install {install.package}"
    if install.kind == "dotnet":
        return f"dotnet tool install -g {install.package}"
    if install.kind == "rustup":
        return f"rustup component add {install.package}"
    return None  # pragma: no cover - InstallKind is exhaustive


_BY_ID: dict[str, ServerInfo] = {server.id: server for server in BUILTIN}


def builtin(server_id: str) -> ServerInfo | None:
    return _BY_ID.get(server_id)


def catalog(
    *,
    disabled: Iterable[str] = (),
    overrides: dict[str, dict[str, Any]] | None = None,
    enable: Iterable[str] = (),
) -> dict[str, ServerInfo]:
    """The enabled servers, keyed by id (contract §2, §3).

    ``disabled`` removes builtins; ``enable`` (``lsp.enable``) turns on a
    default-off one (``ty``, ``ruff``) as it is, install and all.
    ``overrides`` (``lsp.servers``) both defines new servers and replaces a
    builtin's command, which is how a user points ``pyright`` at a wrapper
    script without touching the code; an entry with no ``command`` for a
    builtin id enables that builtin unchanged but for the fields it sets.
    """
    blocked = {str(item) for item in disabled}
    wanted = {str(item) for item in enable}
    servers = {
        server.id: server
        for server in BUILTIN
        if server.id not in blocked
        and (server.id not in DISABLED_BY_DEFAULT or server.id in wanted)
    }
    for server_id, spec in (overrides or {}).items():
        if server_id in blocked:
            servers.pop(server_id, None)
            continue
        if not isinstance(spec, dict):
            continue
        if spec.get("disabled") is True:
            servers.pop(server_id, None)
            continue
        base = _BY_ID.get(server_id)
        if base is not None and not spec.get("command"):
            servers[server_id] = _overlay(base, spec)
            continue
        built = from_settings(spec, server_id)
        if built is None:
            continue
        existing = servers.get(server_id)
        if existing is not None:
            built = ServerInfo(
                id=server_id,
                extensions=built.extensions or existing.extensions,
                root_markers=built.root_markers or existing.root_markers,
                exclude_markers=existing.exclude_markers,
                strict_root=existing.strict_root,
                candidates=built.candidates,
                install=None,
                initialization=built.initialization or existing.initialization,
                precondition=existing.precondition,
                env=built.env,
            )
        servers[server_id] = built
    return servers


def _overlay(base: ServerInfo, spec: dict[str, Any]) -> ServerInfo:
    """``base`` with the extensions, markers, env or options an entry sets."""
    extensions = spec.get("extensions")
    markers = spec.get("rootMarkers")
    env = spec.get("env")
    initialization = spec.get("initialization")
    return replace(
        base,
        extensions=(
            tuple(str(item) for item in extensions)
            if isinstance(extensions, list) and extensions
            else base.extensions
        ),
        root_markers=(
            tuple(str(item) for item in markers)
            if isinstance(markers, list) and markers
            else base.root_markers
        ),
        env=(
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else base.env
        ),
        initialization=(
            _constant(initialization)
            if isinstance(initialization, dict)
            else base.initialization
        ),
    )


__all__ = [
    "BUILTIN",
    "DISABLED_BY_DEFAULT",
    "INSTALL_TIMEOUT_SEC",
    "Candidate",
    "Install",
    "InstallKind",
    "ServerInfo",
    "auto_install",
    "builtin",
    "catalog",
    "find_root",
    "find_tsserver",
    "from_settings",
    "has_tsserver",
    "initialization_for",
    "install_args",
    "install_hint",
    "install_server",
    "is_managed",
    "is_rustup_proxy",
    "locate",
    "lsp_home",
    "needs",
    "precondition_holds",
    "python_path",
    "resolve_command",
    "rustup_which",
    "spawn",
    "start_server",
    "tsdk_options",
    "tsserver_options",
    "which",
]
