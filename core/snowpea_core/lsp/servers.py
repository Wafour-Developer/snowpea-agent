"""The language-server registry: what to launch, and where its root is.

Ported from opencode's ``packages/opencode/src/lsp/server.ts`` and
``lsp/launch.ts`` (MIT, commit 95daf90).  Upstream writes one ``spawn()``
closure per server, each with its own bespoke download logic; here a server is
*data* — the binaries to look for, the arguments they take, the markers that
locate their project root — and one :func:`spawn` drives all of them.  That is
what makes ``lsp.servers`` in settings.json able to add a server without any
code, and it is what keeps AC-46 checkable: there is exactly one place that
starts a process, and it takes an argv list.

Auto-install (``lsp.autoInstall``, default off) is limited to the three package
managers opencode uses without fetching an archive itself — npm, pip and go —
into ``$SNOWPEA_HOME/lsp/``.  Servers upstream only knows how to obtain by
downloading and unzipping a release (eslint, csharp, zls, elixir-ls, terraform,
…) are PATH-only here; see ``docs/design/deviations/CORE-lsp.md``.

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
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("snowpea.lsp.servers")

#: How long an auto-install may take before it is given up on.
INSTALL_TIMEOUT_SEC = 300.0

#: A binary on PATH (or in the managed bin directory) plus its stdio arguments.
Candidate = tuple[str, tuple[str, ...]]

#: Package managers :func:`spawn` will drive when ``lsp.autoInstall`` is on.
InstallKind = Literal["npm", "pip", "go"]


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
    return (root / "bin", root / "node_modules" / ".bin", root / "py" / "bin")


def which(binary: str, home: Path | None = None) -> str | None:
    """``shutil.which`` widened to the managed install directories."""
    found = shutil.which(binary)
    if found:
        return found
    if home is None:
        return None
    for prefix in _managed_prefixes(home):
        candidate = prefix / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


async def _run(argv: list[str], *, env: dict[str, str] | None = None) -> bool:
    """Run ``argv`` to completion; True on exit 0.  Never raises, never shells out."""
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
        return False
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=INSTALL_TIMEOUT_SEC)
    except TimeoutError:
        process.kill()
        log.warning("%s timed out after %ss", argv[0], INSTALL_TIMEOUT_SEC)
        return False
    if process.returncode != 0:
        log.warning("%s failed (%s): %s", argv[0], process.returncode, output.decode()[-400:])
        return False
    return True


async def auto_install(spec: Install, home: Path) -> str | None:
    """Install ``spec`` under ``$SNOWPEA_HOME/lsp`` and return the binary path.

    Only called when ``lsp.autoInstall`` is true (AC-46).  Any failure —
    missing package manager, network, non-zero exit — returns ``None``; a
    language server that cannot be installed is never an error.
    """
    root = lsp_home(home)
    root.mkdir(parents=True, exist_ok=True)
    if spec.kind == "npm":
        if not shutil.which("npm"):
            return None
        ok = await _run(
            ["npm", "install", "--no-save", "--prefix", str(root), spec.package]
        )
    elif spec.kind == "pip":
        venv = root / "py"
        if not venv.exists() and not await _run([sys.executable, "-m", "venv", str(venv)]):
            return None
        pip = venv / "bin" / "pip"
        if not pip.is_file():
            return None
        ok = await _run([str(pip), "install", "--upgrade", spec.package])
    elif spec.kind == "go":
        if not shutil.which("go"):
            return None
        ok = await _run(
            ["go", "install", spec.package], env={"GOBIN": str(root / "bin")}
        )
    else:  # pragma: no cover - InstallKind is exhaustive
        return None
    if not ok:
        return None
    return which(spec.binary, home)


async def resolve_command(
    server: ServerInfo, *, home: Path, allow_install: bool
) -> list[str] | None:
    """The argv that starts ``server``, or ``None`` when it is unavailable.

    PATH is always consulted first.  ``allow_install`` is the ``lsp.autoInstall``
    setting; when it is false nothing is downloaded, ever.
    """
    for binary, args in server.candidates:
        found = which(binary, home)
        if found:
            return [found, *args]
    if not allow_install or server.install is None:
        return None
    log.info("installing language server %s (%s)", server.id, server.install.package)
    path = await auto_install(server.install, home)
    if path is None:
        return None
    args = server.install.args or (server.candidates[0][1] if server.candidates else ())
    return [path, *args]


async def spawn(
    server: ServerInfo,
    root: Path,
    *,
    home: Path,
    allow_install: bool,
) -> asyncio.subprocess.Process | None:
    """Start ``server`` in ``root``, or return ``None`` when it is not installed.

    The single process-creating call in the LSP layer.  It is
    ``create_subprocess_exec`` with an argv list and never a shell string
    (AC-46); a missing server is ``None``, never an exception (contract §2).
    """
    if server.precondition is not None and not server.precondition(root):
        log.debug("%s declines %s: its precondition does not hold", server.id, root)
        return None
    argv = await resolve_command(server, home=home, allow_install=allow_install)
    if argv is None:
        log.debug("no binary for language server %s", server.id)
        return None
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **server.env},
        )
    except (OSError, ValueError) as exc:
        log.warning("could not start language server %s: %s", server.id, exc)
        return None


# ---------------------------------------------------------------------------
# initialization options
# ---------------------------------------------------------------------------


def find_tsserver(root: Path) -> Path | None:
    """The workspace's own ``typescript/lib/tsserver.js``, searching upwards.

    typescript-language-server refuses to initialize without it, and astro-ls
    wants the same directory as its ``typescript.tsdk``.
    """
    for directory in _walk_up(root, Path(root.anchor)):
        candidate = directory / "node_modules" / "typescript" / "lib" / "tsserver.js"
        if candidate.is_file():
            return candidate
    return None


def has_tsserver(root: Path) -> bool:
    """Precondition for the TypeScript-backed servers."""
    return find_tsserver(root) is not None


def tsserver_options(root: Path) -> dict[str, Any]:
    """``initializationOptions`` pointing at the workspace's tsserver."""
    found = find_tsserver(root)
    return {"tsserver": {"path": str(found)}} if found else {}


def tsdk_options(root: Path) -> dict[str, Any]:
    """astro-ls wants the *directory* holding tsserver.js, as ``typescript.tsdk``."""
    found = find_tsserver(root)
    return {"typescript": {"tsdk": str(found.parent)}} if found else {}


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

BUILTIN: tuple[ServerInfo, ...] = (
    ServerInfo(
        id="typescript",
        extensions=_JS_EXTENSIONS,
        root_markers=_JS_ROOTS,
        exclude_markers=_DENO_ROOTS,
        candidates=(("typescript-language-server", ("--stdio",)),),
        install=Install(
            "npm", "typescript-language-server", "typescript-language-server", ("--stdio",)
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
        root_markers=("biome.json", "biome.jsonc", *_JS_ROOTS),
        strict_root=True,
        candidates=(("biome", ("lsp-proxy",)),),
    ),
    ServerInfo(
        id="oxlint",
        extensions=(*_JS_EXTENSIONS, ".vue", ".astro", ".svelte"),
        root_markers=(".oxlintrc.json", *_JS_ROOTS, "package.json"),
        strict_root=True,
        candidates=(("oxc_language_server", ()),),
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
    ),
    ServerInfo(
        id="zls",
        extensions=(".zig", ".zon"),
        root_markers=("build.zig",),
        candidates=(("zls", ()),),
    ),
    ServerInfo(
        id="csharp",
        extensions=(".cs", ".csx"),
        root_markers=(".slnx", ".sln", "*.csproj", "global.json"),
        candidates=(
            ("Microsoft.CodeAnalysis.LanguageServer", ("--stdio", "--autoLoadProjects")),
            ("csharp-ls", ()),
        ),
    ),
    ServerInfo(
        id="fsharp",
        extensions=(".fs", ".fsi", ".fsx", ".fsscript"),
        root_markers=(".slnx", ".sln", "*.fsproj", "global.json"),
        candidates=(("fsautocomplete", ()),),
    ),
    ServerInfo(
        id="sourcekit-lsp",
        extensions=(".swift", ".objc", ".objcpp"),
        root_markers=("Package.swift", "*.xcodeproj", "*.xcworkspace"),
        candidates=(("sourcekit-lsp", ()),),
    ),
    ServerInfo(
        id="rust",
        extensions=(".rs",),
        root_markers=("Cargo.toml", "Cargo.lock"),
        strict_root=True,
        candidates=(("rust-analyzer", ()),),
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
    ),
    ServerInfo(
        id="dart",
        extensions=(".dart",),
        root_markers=("pubspec.yaml", "analysis_options.yaml"),
        candidates=(("dart", ("language-server", "--lsp")),),
    ),
    ServerInfo(
        id="ocaml-lsp",
        extensions=(".ml", ".mli"),
        root_markers=("dune-project", "dune-workspace", ".merlin", "opam"),
        candidates=(("ocamllsp", ()),),
    ),
    ServerInfo(
        id="gleam",
        extensions=(".gleam",),
        root_markers=("gleam.toml",),
        candidates=(("gleam", ("lsp",)),),
    ),
    ServerInfo(
        id="clojure-lsp",
        extensions=(".clj", ".cljs", ".cljc", ".edn"),
        root_markers=("deps.edn", "project.clj", "shadow-cljs.edn", "bb.edn", "build.boot"),
        candidates=(("clojure-lsp", ("listen",)),),
    ),
    ServerInfo(
        id="nixd",
        extensions=(".nix",),
        root_markers=("flake.nix",),
        candidates=(("nixd", ()),),
    ),
    ServerInfo(
        id="prisma",
        extensions=(".prisma",),
        root_markers=("schema.prisma",),
        candidates=(("prisma", ("language-server",)),),
    ),
    ServerInfo(
        id="haskell-language-server",
        extensions=(".hs", ".lhs"),
        root_markers=("stack.yaml", "cabal.project", "hie.yaml", "*.cabal"),
        candidates=(("haskell-language-server-wrapper", ("--lsp",)),),
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
    ),
)

#: ``pyright`` and ``ty`` both claim ``.py``; running two Python servers over
#: every edit doubles the latency for one set of diagnostics, so ``ty`` and
#: ``ruff`` are opt-in through ``lsp.disabled`` / ``lsp.servers`` the way
#: upstream gates ``ty`` behind an experimental flag.
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


def catalog(
    *,
    disabled: Iterable[str] = (),
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, ServerInfo]:
    """The enabled servers, keyed by id (contract §2, §3).

    ``disabled`` removes builtins; ``overrides`` (``lsp.servers``) both defines
    new servers and replaces a builtin's command, which is how a user points
    ``pyright`` at a wrapper script without touching the code.
    """
    blocked = {str(item) for item in disabled}
    servers = {
        server.id: server
        for server in BUILTIN
        if server.id not in blocked and server.id not in DISABLED_BY_DEFAULT
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


__all__ = [
    "BUILTIN",
    "DISABLED_BY_DEFAULT",
    "INSTALL_TIMEOUT_SEC",
    "Candidate",
    "Install",
    "InstallKind",
    "ServerInfo",
    "auto_install",
    "catalog",
    "find_root",
    "find_tsserver",
    "from_settings",
    "has_tsserver",
    "lsp_home",
    "python_path",
    "resolve_command",
    "spawn",
    "tsdk_options",
    "tsserver_options",
    "which",
]
