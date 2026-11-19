"""lsp.install reasons (lspInstallReasons) and the .NET SDK toolchain install
(lspToolchainInstall), 1.68.0.

No real download, no real npm or dotnet: fake scripts on PATH and an
``httpx.MockTransport`` serving a fake SDK archive.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.lsp import servers, toolchains
from snowpea_core.lsp import tools as lsp_tools
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.lsp_handlers import (
    lsp_catalog_handler,
    lsp_install_handler,
    lsp_toolchain_cancel_handler,
    lsp_toolchain_remove_handler,
)
from snowpea_core.server.protocol import (
    CAPABILITIES,
    Empty,
    LspInstallParams,
    LspToolchainParams,
)
from snowpea_core.server.rpc import RpcError
from snowpea_core.tools.registry import register_builtin_tools

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake tools are sh scripts")

# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_toolchains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "toolchains"
    monkeypatch.setenv("SNOWPEA_TOOLCHAINS_DIR", str(where))
    toolchains.forget_sdks()
    return where


class _Hub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, payload: dict[str, Any]) -> None:
        self.sent.append((method, payload))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.sent if name == method]


def _core(tmp_path: Path) -> Core:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(core.tools)
    lsp_tools.refresh_state(core)
    core.hub = _Hub()
    return core


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _person() -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind="ide", surface_id="ide:1")


def _browser(**extra: Any) -> Any:
    values = {
        "authenticated": True, "closed": False, "client_kind": "browser",
        "client_version": "0.5.0", "peer": "127.0.0.1", "surface_id": "browser:1",
        "client_id": "b1",
    }
    values.update(extra)
    return SimpleNamespace(**values)


# --- a fake .NET SDK on fake Microsoft hosts -------------------------------

RID = "linux-x64"
VERSION = "10.0.100"
ARCHIVE_URL = f"https://builds.dotnet.microsoft.com/dotnet/Sdk/{VERSION}/dotnet-sdk-{VERSION}-{RID}.tar.gz"
RELEASES_URL = "https://builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json"


def _archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, body, mode in (
            ("dotnet", b"#!/bin/sh\necho fake dotnet\n", 0o755),
            (f"sdk/{VERSION}/dotnet.dll", b"x" * 4096, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), mode
            bundle.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


ARCHIVE = _archive()
#: A fake ``dotnet tool install --tool-path <dir>`` body: drops csharp-ls in <dir>.
MAKE_CSHARP_LS = (
    '/bin/mkdir -p "$4"; printf "#!/bin/sh\\n" > "$4/csharp-ls"; '
    '/bin/chmod +x "$4/csharp-ls"\n'
)


class FakeMicrosoft:
    """releases-index.json → releases.json → the archive, with Range support."""

    def __init__(self, *, sha512: str | None = None, archive_url: str = ARCHIVE_URL) -> None:
        self.sha512 = sha512 or hashlib.sha512(ARCHIVE).hexdigest()
        self.archive_url = archive_url
        self.requests: list[tuple[str, str, str | None]] = []
        self.gate: asyncio.Event | None = None  # hold the body halfway until set
        self.started = asyncio.Event()

    def index(self) -> dict[str, Any]:
        return {"releases-index": [
            {"channel-version": "9.0", "releases.json": "https://builds.dotnet.microsoft.com/9"},
            {"channel-version": "10.0", "latest-sdk": VERSION, "releases.json": RELEASES_URL},
        ]}

    def releases(self) -> dict[str, Any]:
        files = [
            {"name": "dotnet-sdk-win-x64.zip", "rid": "win-x64", "url": "https://x/a.zip",
             "hash": "0" * 128},
            {"name": "dotnet-sdk-linux-x64.tar.gz", "rid": RID, "url": self.archive_url,
             "hash": self.sha512},
        ]
        return {"releases": [{"sdk": {"version": VERSION, "files": files}}]}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url, request.headers.get("range")))
        if url == toolchains.RELEASES_INDEX_URL:
            return httpx.Response(200, json=self.index())
        if url == RELEASES_URL:
            return httpx.Response(200, json=self.releases())
        if url == ARCHIVE_URL:
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(ARCHIVE))})
            start = 0
            ranged = request.headers.get("range")
            if ranged:
                start = int(ranged.split("=", 1)[1].rstrip("-"))
            body = ARCHIVE[start:]
            status = 206 if ranged else 200
            if self.gate is None:
                return httpx.Response(status, content=body,
                                      headers={"content-length": str(len(body))})
            gate, started = self.gate, self.started
            half = len(body) // 2

            async def stream() -> Any:
                yield body[:half]
                started.set()
                await gate.wait()
                yield body[half:]

            return httpx.Response(status, content=stream(),
                                  headers={"content-length": str(len(body))})
        if url == "https://builds.dotnet.microsoft.com/redirect-away":
            return httpx.Response(302, headers={"location": "https://evil.example/sdk.tar.gz"})
        return httpx.Response(404)


def _factory(fake: FakeMicrosoft) -> Any:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake))


async def _quiet(_phase: str, _percent: int | None, _line: str | None) -> None:
    return None


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> FakeMicrosoft:
    fake = FakeMicrosoft()
    monkeypatch.setattr(toolchains, "_client", _factory(fake))
    monkeypatch.setattr(toolchains, "runtime_id", lambda *a, **k: RID)
    return fake


# ---------------------------------------------------------------------------
# 1. the .NET SDK check
# ---------------------------------------------------------------------------


def test_parse_list_sdks() -> None:
    text = (
        "8.0.404 [C:\\Program Files\\dotnet\\sdk]\r\n"
        "9.0.100 [C:\\Program Files\\dotnet\\sdk]\r\n"
    )
    assert toolchains.parse_list_sdks(text) == ["8.0.404", "9.0.100"]
    assert toolchains.parse_list_sdks("") == []
    assert toolchains.parse_list_sdks("Welcome to .NET!\n") == []


@posix_only
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('echo "8.0.404 [/usr/share/dotnet/sdk]"\n', ["8.0.404"]),
        ("exit 0\n", []),  # runtime host only: no SDK lines
        ('echo "https://aka.ms/dotnet/sdk-not-found" >&2; exit 1\n', []),
    ],
)
async def test_dotnet_sdks_runs_list_sdks(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    log = tmp_path / "argv"
    dotnet = _script(tmp_path / "bin" / "dotnet", f'echo "$@" > {log}\n' + body)
    assert await toolchains.dotnet_sdks(str(dotnet)) == expected
    assert log.read_text().split() == ["--list-sdks"]


@posix_only
async def test_a_runtime_only_dotnet_needs_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path / "bin" / "dotnet", "exit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    csharp = servers.builtin("csharp")
    assert csharp is not None
    assert await servers.missing_toolchain(csharp) == "dotnet-sdk"
    details: dict[str, Any] = {}
    path, error = await servers.install_server(csharp.install, tmp_path / "home", details=details)  # type: ignore[arg-type]
    assert path is None and "only the runtime" in (error or "")
    assert details["reason"] == "needs_toolchain" and details["toolchain"] == "dotnet-sdk"
    assert details["url"] == "https://dotnet.microsoft.com/download"


@posix_only
async def test_a_dotnet_with_an_sdk_needs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path / "bin" / "dotnet", 'echo "9.0.100 [/x/sdk]"\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert await servers.missing_toolchain(servers.builtin("fsharp")) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# exit code 2147516571 / sdk-not-found
# ---------------------------------------------------------------------------


def test_sdk_missing_maps_the_exit_code_and_the_link() -> None:
    assert toolchains.sdk_missing(2147516571, "")
    assert toolchains.sdk_missing(-2147450725, "")  # the same code, signed
    assert toolchains.sdk_missing(1, "https://aka.ms/dotnet/sdk-not-found")
    assert toolchains.sdk_missing(1, "No .NET SDKs were found.")
    assert not toolchains.sdk_missing(1, "error NU1101: Unable to find package csharp-ls")


@posix_only
async def test_an_install_that_hits_sdk_not_found_says_needs_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "dotnet.log"
    _script(
        tmp_path / "bin" / "dotnet",
        '[ "$1" = --list-sdks ] && echo "8.0.100 [/x/sdk]" && exit 0\n'
        '[ "$1" = --version ] && echo 8.0.100 && exit 0\n'
        f'echo "$@" >> {log}\n'
        'echo "The command could not be loaded, possibly because:"\n'
        'echo "https://aka.ms/dotnet/sdk-not-found"\n'
        "exit 155\n",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    details: dict[str, Any] = {}
    path, error = await servers.install_server(
        servers.builtin("csharp").install, tmp_path / "home", details=details  # type: ignore[union-attr,arg-type]
    )
    assert path is None and error and error.startswith("needs the .NET SDK")
    assert details["reason"] == "needs_toolchain" and details["toolchain"] == "dotnet-sdk"
    # No pointless "tool update" after sdk-not-found.
    assert [line.split()[1] for line in log.read_text().splitlines()] == ["install"]


# ---------------------------------------------------------------------------
# 2. npm errors and the svelte install
# ---------------------------------------------------------------------------

ERESOLVE_OUTPUT = [
    "npm error code ERESOLVE",
    "npm error ERESOLVE unable to resolve dependency tree",
    "npm error",
    "npm error While resolving: snowpea-lsp@undefined",
    "npm error Found: typescript@7.0.2",
    "npm error node_modules/typescript",
    'npm error   typescript@"^7.0.2" from the root project',
    "npm error",
    "npm error Could not resolve dependency:",
    'npm error peer typescript@"^5.9.2 || ^6.0.2" from svelte-language-server@0.18.6',
    "npm error node_modules/svelte-language-server",
    'npm error   svelte-language-server@"*" from the root project',
    "npm error",
    "npm error Fix the upstream dependency conflict, or retry this command with --force or "
    "--legacy-peer-deps to accept an incorrect (and potentially broken) dependency resolution.",
    "npm error",
    "npm error",
    "npm error For a full report see:",
    "npm error C:\\Users\\me\\AppData\\Local\\npm-cache\\_logs\\"
    "2026-11-19T09_28_44_343Z-eresolve-report.txt",
    "npm error A complete log of this run can be found in: "
    "C:\\Users\\me\\AppData\\Local\\npm-cache\\_logs\\2026-11-19T09_28_44_343Z-debug-0.log",
]

EACCES_OUTPUT = [
    "npm WARN deprecated inflight@1.0.6: This module is not supported",
    "npm ERR! code EACCES",
    "npm ERR! syscall mkdir",
    "npm ERR! path /usr/lib/node_modules/svelte-language-server",
    "npm ERR! errno -13",
    "npm ERR! Error: EACCES: permission denied, "
    "mkdir '/usr/lib/node_modules/svelte-language-server'",
    "npm ERR! A complete log of this run can be found in:",
    "npm ERR!     /home/me/.npm/_logs/2026-11-19T00_00_00_000Z-debug-0.log",
]

ENOTFOUND_OUTPUT = [
    "npm error code ENOTFOUND",
    "npm error syscall getaddrinfo",
    "npm error errno ENOTFOUND",
    "npm error network request to https://registry.npmjs.org/svelte-language-server failed, "
    "reason: getaddrinfo ENOTFOUND registry.npmjs.org",
    "npm error A complete log of this run can be found in: C:\\x\\_logs\\a-debug-0.log",
]

E404_OUTPUT = [
    "npm error code E404",
    "npm error 404 Not Found - GET https://registry.npmjs.org/svelte-langauge-server - Not found",
    "npm error A complete log of this run can be found in: /x/_logs/a-debug-0.log",
]


def test_npm_failure_keeps_the_cause_not_the_log_line() -> None:
    found = servers.npm_failure(ERESOLVE_OUTPUT)
    assert found["reason"] == "npm_failed" and found["npmCode"] == "ERESOLVE"
    assert 'peer typescript@"^5.9.2 || ^6.0.2"' in found["detail"]
    assert "complete log" not in found["detail"] and "_logs" not in found["detail"]
    assert "For a full report" not in found["detail"]
    assert len(found["detail"]) <= servers.NPM_DETAIL_MAX


@pytest.mark.parametrize(
    ("lines", "code", "needle"),
    [
        (EACCES_OUTPUT, "EACCES", "permission denied"),
        (ENOTFOUND_OUTPUT, "ENOTFOUND", "getaddrinfo ENOTFOUND registry.npmjs.org"),
        (E404_OUTPUT, "E404", "404 Not Found"),
    ],
)
def test_npm_failure_codes(lines: list[str], code: str, needle: str) -> None:
    found = servers.npm_failure(lines)
    assert found["npmCode"] == code
    assert needle in found["detail"]
    assert "complete log" not in found["detail"] and "debug-0.log" not in found["detail"]
    assert "WARN" not in found["detail"]


def test_npm_failure_is_capped() -> None:
    lines = ["npm error code E500"] + [f"npm error {'x' * 300} {n}" for n in range(20)]
    found = servers.npm_failure(lines)
    assert len(found["detail"]) <= servers.NPM_DETAIL_MAX
    assert found["detail"].endswith(" 19")  # the last lines are kept
    assert servers.npm_failure([])["detail"] is None


@posix_only
async def test_a_failed_npm_install_reports_its_error_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = tmp_path / "eresolve.txt"
    sample.write_text("\n".join(ERESOLVE_OUTPUT) + "\n")
    _script(tmp_path / "bin" / "npm", f"/bin/cat {sample} >&2\nexit 1\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    details: dict[str, Any] = {}
    path, error = await servers.install_server(
        servers.builtin("svelte").install, tmp_path / "home", details=details  # type: ignore[union-attr,arg-type]
    )
    assert path is None
    assert error is not None and error.startswith("npm exited 1: ERESOLVE: ")
    assert "svelte-language-server@0.18.6" in error and "complete log" not in error
    assert details["npmCode"] == "ERESOLVE" and details["reason"] == "npm_failed"


@posix_only
async def test_svelte_installs_into_a_tree_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "npm.log"
    _script(
        tmp_path / "bin" / "npm",
        f'echo "$@" > {log}\n'
        'prefix="$3"; /bin/mkdir -p "$prefix/node_modules/.bin"\n'
        'printf "#!/bin/sh\\n" > "$prefix/node_modules/.bin/svelteserver"\n'
        '/bin/chmod +x "$prefix/node_modules/.bin/svelteserver"\n',
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    home = tmp_path / "home"
    # A typescript@7 saved for typescript-language-server must not be in its way.
    shared = home / "lsp"
    shared.mkdir(parents=True)
    (shared / "package.json").write_text('{"dependencies": {"typescript": "^7.0.2"}}')
    path, error = await servers.install_server(servers.builtin("svelte").install, home)  # type: ignore[union-attr,arg-type]
    prefix = home / "lsp" / "npm" / "svelte-language-server"
    assert error is None and path == str(prefix / "node_modules" / ".bin" / "svelteserver")
    assert log.read_text().split() == [
        "install", "--prefix", str(prefix), "--no-audit", "--no-fund", "--no-update-notifier",
        "svelte-language-server",
    ]
    assert servers.install_args(servers.builtin("svelte")) == ("--stdio",)  # type: ignore[arg-type]


def test_npm_prefix_names() -> None:
    home = Path("/h")
    npm = home / "lsp" / "npm"
    assert servers.npm_prefix(home, "@vue/language-server") == npm / "vue__language-server"
    assert servers.npm_prefix(home, "pyright") == home / "lsp" / "npm" / "pyright"


def test_the_managed_typescript_is_found_in_its_own_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    tsserver = (
        servers.npm_prefix(home, "typescript-language-server")
        / "node_modules" / "typescript" / "lib" / "tsserver.js"
    )
    tsserver.parent.mkdir(parents=True)
    tsserver.write_text("")
    project = tmp_path / "p"
    project.mkdir()
    assert servers.find_tsserver(project, home) == tsserver


def test_npm_argv_on_windows_runs_node_without_cmd(tmp_path: Path) -> None:
    nodejs = tmp_path / "nodejs"
    cli = nodejs / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    (nodejs / "node.exe").write_text("")
    npm_cmd = str(nodejs / "npm.CMD")
    assert servers.npm_argv(npm_cmd, windows=True) == [str(nodejs / "node.exe"), str(cli)]
    # Not the standard layout: the shim as it is (still an argv, never a shell string).
    other = tmp_path / "other" / "npm.cmd"
    assert servers.npm_argv(str(other), windows=True) == [str(other)]
    assert servers.npm_argv("/usr/bin/npm", windows=False) == ["/usr/bin/npm"]


# ---------------------------------------------------------------------------
# 3. reason fields in lsp.catalog and lsp.install
# ---------------------------------------------------------------------------


@posix_only
async def test_catalog_rows_carry_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path / "bin" / "dotnet", "exit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    result = await lsp_catalog_handler(None, Empty(), _core(tmp_path))  # type: ignore[arg-type]
    rows = {row.id: row for row in result.servers}
    for server_id in ("csharp", "fsharp"):
        row = rows[server_id]
        assert row.needs == ".NET SDK" and row.reason == "needs_toolchain"
        assert row.toolchain == "dotnet-sdk" and row.toolchainInstallable is True
        assert row.url == "https://dotnet.microsoft.com/download" and row.hint
    assert (rows["gopls"].needs, rows["gopls"].toolchain) == ("go", "go")
    assert rows["gopls"].toolchainInstallable is False
    assert (rows["rust"].needs, rows["rust"].toolchain) == ("rustup", "rustup")
    assert (rows["svelte"].needs, rows["svelte"].toolchain) == ("npm", "node")
    # A PATH-only server keeps its old needs and gets no reason.
    assert rows["zls"].reason is None and rows["zls"].needs == "zls"
    assert "lspInstallReasons" in CAPABILITIES and "lspToolchainInstall" in CAPABILITIES


@posix_only
async def test_install_without_an_sdk_offers_the_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    _script(tmp_path / "bin" / "dotnet", "exit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    result = await lsp_install_handler(
        _person(), LspInstallParams(ids=["csharp", "gopls"]), _core(tmp_path)
    )
    csharp, gopls = result.results
    assert not csharp.ok and csharp.reason == "needs_toolchain" and csharp.toolchain == "dotnet-sdk"
    assert csharp.needsToolchain is not None
    assert csharp.needsToolchain.name == "dotnet-sdk"
    assert csharp.needsToolchain.source == ARCHIVE_URL
    assert csharp.needsToolchain.version == VERSION
    assert csharp.needsToolchain.sizeMb == round(len(ARCHIVE) / 1_000_000)
    assert gopls.reason == "needs_toolchain" and gopls.toolchain == "go"
    assert gopls.needsToolchain is None and gopls.error == "needs go"
    assert not toolchains.dotnet_dir().exists(), "nothing is downloaded without withToolchain"


# ---------------------------------------------------------------------------
# 4. the toolchain install itself
# ---------------------------------------------------------------------------


async def test_installs_a_verified_sdk(fake_http: FakeMicrosoft) -> None:
    phases: list[tuple[str, int | None]] = []

    async def progress(phase: str, percent: int | None, _line: str | None) -> None:
        phases.append((phase, percent))

    path = await toolchains.install("dotnet-sdk", progress)
    assert path == str(toolchains.dotnet_exe()) and toolchains.dotnet_installed()
    assert (toolchains.dotnet_dir() / "sdk" / VERSION / "dotnet.dll").is_file()
    marker = json.loads((toolchains.dotnet_dir() / toolchains.MARKER).read_text())
    assert marker["version"] == VERSION and marker["sha512"] == fake_http.sha512
    assert [p for p, _ in phases][0] == "resolve" and ("download", 100) in phases
    assert phases[-1] == ("done", 100) and any(p == "verify" for p, _ in phases)
    assert not list((toolchains.toolchains_dir() / "downloads").iterdir())
    assert all(url.startswith("https://builds.dotnet.microsoft.com/") for _, url, _ in
               fake_http.requests)


async def test_a_wrong_sha512_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    fake_http.sha512 = hashlib.sha512(b"something else").hexdigest()
    with pytest.raises(toolchains.ToolchainError) as raised:
        await toolchains.install("dotnet-sdk", _quiet)
    assert raised.value.code == "checksum"
    assert not toolchains.dotnet_installed() and not toolchains.dotnet_dir().exists()
    assert not list((toolchains.toolchains_dir() / "downloads").iterdir())


async def test_too_little_disk_space_is_refused_first(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    monkeypatch.setattr(toolchains.shutil, "disk_usage", lambda _p: SimpleNamespace(free=10))
    with pytest.raises(toolchains.ToolchainError) as raised:
        await toolchains.install("dotnet-sdk", _quiet)
    assert raised.value.code == "disk_full" and "MB free" in str(raised.value)
    assert not any(method == "GET" and url == ARCHIVE_URL for method, url, _ in fake_http.requests)


async def test_a_full_disk_while_downloading_cleans_up(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    import errno

    real_open = Path.open

    def full(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(".part") and mode in ("wb", "ab"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full)
    with pytest.raises(toolchains.ToolchainError) as raised:
        await toolchains.install("dotnet-sdk", _quiet)
    assert raised.value.code == "disk_full"
    assert not list((toolchains.toolchains_dir() / "downloads").glob("*.part"))


async def test_cancel_keeps_the_partial_download_and_the_next_try_resumes(
    fake_http: FakeMicrosoft,
) -> None:
    fake_http.gate = asyncio.Event()
    task = asyncio.ensure_future(toolchains.install("dotnet-sdk", _quiet))
    await asyncio.wait_for(fake_http.started.wait(), 5)
    assert toolchains.running("dotnet-sdk")
    with pytest.raises(toolchains.ToolchainError) as busy:
        await toolchains.install("dotnet-sdk", _quiet)
    assert busy.value.code == "busy"
    for _ in range(50):  # the first half reaches the file
        parts = list((toolchains.toolchains_dir() / "downloads").glob("*.part"))
        if parts and parts[0].stat().st_size:
            break
        await asyncio.sleep(0.01)
    assert toolchains.cancel("dotnet-sdk") is True
    with pytest.raises(toolchains.ToolchainError) as raised:
        await task
    assert raised.value.code == "cancelled"
    assert not toolchains.running("dotnet-sdk") and not toolchains.dotnet_installed()
    part = next((toolchains.toolchains_dir() / "downloads").glob("*.part"))
    have = part.stat().st_size
    assert 0 < have < len(ARCHIVE)
    fake_http.gate = None
    await toolchains.install("dotnet-sdk", _quiet)
    assert toolchains.dotnet_installed()
    assert ("GET", ARCHIVE_URL, f"bytes={have}-") in fake_http.requests


async def test_only_official_hosts_over_https(fake_http: FakeMicrosoft) -> None:
    with pytest.raises(toolchains.ToolchainError) as raised:
        toolchains.check_url("http://builds.dotnet.microsoft.com/x")
    assert raised.value.code == "source"
    with pytest.raises(toolchains.ToolchainError):
        toolchains.check_url("https://builds.dotnet.microsoft.com.evil.example/x")
    fake_http.archive_url = "https://builds.dotnet.microsoft.com/redirect-away"
    with pytest.raises(toolchains.ToolchainError) as redirected:
        await toolchains.install("dotnet-sdk", _quiet)
    assert redirected.value.code == "source"
    assert not any("evil" in url for _, url, _ in fake_http.requests)


async def test_an_archive_entry_outside_the_target_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(buffer.getvalue())
    (tmp_path / "t").mkdir()
    with pytest.raises(toolchains.ToolchainError) as raised:
        toolchains._extract(archive, tmp_path / "t")
    assert raised.value.code == "archive" and not (tmp_path / "escape").exists()


async def test_remove_deletes_only_core_s_copy(fake_http: FakeMicrosoft) -> None:
    assert toolchains.remove("dotnet-sdk") is False
    await toolchains.install("dotnet-sdk", _quiet)
    assert toolchains.remove("dotnet-sdk") is True
    assert not toolchains.dotnet_dir().exists() and not toolchains.dotnet_installed()
    assert toolchains.toolchains_dir().is_dir()
    with pytest.raises(toolchains.ToolchainError):
        toolchains.remove("go")


# ---------------------------------------------------------------------------
# env scoping
# ---------------------------------------------------------------------------


@posix_only
async def test_the_dotnet_env_reaches_the_server_process_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    await toolchains.install("dotnet-sdk", _quiet)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # not a real csharp-ls on PATH
    before = dict(os.environ)
    home = tmp_path / "home"
    out = tmp_path / "env.txt"
    _script(
        home / "lsp" / "bin" / "csharp-ls",
        f'printf "%s|%s|%s|%s" "$DOTNET_ROOT" "$DOTNET_CLI_HOME" '
        f'"$DOTNET_CLI_TELEMETRY_OPTOUT" "$PATH" > {out}\n',
    )
    csharp = servers.ServerInfo(
        id="csharp-test", extensions=(".cs",), candidates=(("csharp-ls", ()),),
        install=servers.Install("dotnet", "csharp-ls", "csharp-ls"),
    )
    process, reason = await servers.start_server(csharp, tmp_path, home=home, allow_install=False)
    assert reason is None and process is not None
    await asyncio.wait_for(process.wait(), 10)
    root, cli_home, optout, path = out.read_text().split("|", 3)
    assert root == str(toolchains.dotnet_dir()) and optout == "1"
    assert cli_home == str(toolchains.toolchains_dir() / "dotnet-cli-home")
    assert path.split(os.pathsep)[0] == str(toolchains.dotnet_dir())
    assert dict(os.environ) == before and "DOTNET_ROOT" not in os.environ
    # A PATH-installed .NET server (not core's) keeps its own environment.
    assert servers.server_env(csharp, "/usr/bin/csharp-ls", home) == {}


@posix_only
async def test_a_tool_install_uses_core_s_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    await toolchains.install("dotnet-sdk", _quiet)
    log = tmp_path / "dotnet.log"
    _script(
        toolchains.dotnet_exe(),
        '[ "$1" = --version ] && echo 10.0.100 && exit 0\n'
        f'echo "$DOTNET_ROOT $@" >> {log}\n'
        + MAKE_CSHARP_LS,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no system dotnet at all
    assert await servers.missing_toolchain(servers.builtin("csharp")) is None  # type: ignore[arg-type]
    home = tmp_path / "home"
    path, error = await servers.install_server(servers.builtin("csharp").install, home)  # type: ignore[union-attr,arg-type]
    assert error is None and path == str(home / "lsp" / "bin" / "csharp-ls")
    words = log.read_text().split()
    assert words[0] == str(toolchains.dotnet_dir())
    assert words[1:4] == ["tool", "install", "--tool-path"] and "--version" not in words


# ---------------------------------------------------------------------------
# lsp.install {withToolchain} end to end, and who may
# ---------------------------------------------------------------------------


@posix_only
async def test_with_toolchain_installs_the_sdk_then_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    real_install = toolchains._install_dotnet

    async def install_then_fake_dotnet(*args: Any) -> str:
        path = await real_install(*args)
        _script(
            Path(path),
            '[ "$1" = --version ] && echo 10.0.100 && exit 0\n'
            + MAKE_CSHARP_LS,
        )
        return path

    monkeypatch.setattr(toolchains, "_install_dotnet", install_then_fake_dotnet)
    core = _core(tmp_path)
    result = await lsp_install_handler(
        _browser(), LspInstallParams(ids=["csharp"], withToolchain=True, actor="person",
                                     personConfirm="window"), core,
    )
    item = result.results[0]
    assert item.ok, item.error
    assert item.path == str(core.paths.home / "lsp" / "bin" / "csharp-ls")
    progress = core.hub.of("lsp.install.progress")  # type: ignore[attr-defined]
    phases = [p.get("phase") for p in progress]
    assert "download" in phases and "toolchain" in phases
    assert any(p.get("percent") == 100 and p.get("toolchain") == "dotnet-sdk" for p in progress)
    assert progress[-1]["stage"] == "done" and progress[-1]["ok"] is True


@posix_only
async def test_a_failed_toolchain_is_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_http: FakeMicrosoft
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    fake_http.sha512 = "f" * 128
    result = await lsp_install_handler(
        _person(), LspInstallParams(ids=["fsharp"], withToolchain=True), _core(tmp_path)
    )
    item = result.results[0]
    assert not item.ok and item.reason == "toolchain_failed"
    assert item.toolchainError == "checksum" and item.toolchain == "dotnet-sdk"


REFUSED: list[tuple[str, Any, dict[str, Any]]] = [
    ("remote web login", SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                                          remote_login=True, surface_id="web:1"), {}),
    ("connector", SimpleNamespace(authenticated=True, closed=False, client_kind="connector",
                                  surface_id="c:1"), {}),
    ("jarvis", SimpleNamespace(authenticated=True, closed=False, client_kind="jarvis",
                               surface_id="jarvis:1"), {}),
    ("in-process caller (a model's tool)", None, {}),
    ("closed connection", SimpleNamespace(authenticated=False, closed=True, client_kind="ide",
                                          surface_id="ide:2"), {}),
    ("browser without the window confirm", _browser(), {"actor": "person"}),
    ("browser without the actor", _browser(), {"personConfirm": "window"}),
    ("legacy browser", _browser(client_version="0.3.1"),
     {"actor": "person", "personConfirm": "window"}),
    ("remote browser", _browser(peer="203.0.113.9"),
     {"actor": "person", "personConfirm": "window"}),
]


@pytest.mark.parametrize(("label", "conn", "extra"), REFUSED, ids=[r[0] for r in REFUSED])
async def test_who_may_not_download_or_remove_a_toolchain(
    tmp_path: Path, fake_http: FakeMicrosoft, label: str, conn: Any, extra: dict[str, Any]
) -> None:
    core = _core(tmp_path)
    with pytest.raises(RpcError) as raised:
        await lsp_install_handler(
            conn, LspInstallParams(ids=["csharp"], withToolchain=True, **extra), core
        )
    assert raised.value.code == errors.JARVIS_PERSON_ONLY, label
    with pytest.raises(RpcError):
        await lsp_toolchain_remove_handler(
            conn, LspToolchainParams(name="dotnet-sdk", **extra), core
        )
    assert not fake_http.requests, "a refused call never reaches the network"


async def test_a_person_may_remove_and_cancel(tmp_path: Path, fake_http: FakeMicrosoft) -> None:
    core = _core(tmp_path)
    await toolchains.install("dotnet-sdk", _quiet)
    removed = await lsp_toolchain_remove_handler(
        _browser(), LspToolchainParams(name="dotnet-sdk", actor="person", personConfirm="window"),
        core,
    )
    assert removed.removed and not toolchains.dotnet_installed()
    again = await lsp_toolchain_remove_handler(_person(), LspToolchainParams(name="dotnet-sdk"),
                                               core)
    assert again.removed is False
    cancelled = await lsp_toolchain_cancel_handler(
        _browser(), LspToolchainParams(name="dotnet-sdk", actor="person"), core
    )
    assert cancelled.cancelled is False
    with pytest.raises(RpcError):
        await lsp_toolchain_cancel_handler(None, LspToolchainParams(name="dotnet-sdk"), core)  # type: ignore[arg-type]
    with pytest.raises(RpcError) as unknown:
        await lsp_toolchain_remove_handler(_person(), LspToolchainParams(name="java"), core)
    assert unknown.value.code == errors.INVALID_PARAMS


def test_the_daemon_dirs_are_per_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SNOWPEA_TOOLCHAINS_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    if os.name != "nt":
        assert toolchains.toolchains_dir() == tmp_path / "data" / "snowpea" / "toolchains"
    assert toolchains.runtime_id("Windows", "AMD64") == "win-x64"
    assert toolchains.runtime_id("Darwin", "arm64") == "osx-arm64"
    assert toolchains.runtime_id("Linux", "aarch64") == "linux-arm64"
    assert sys.platform  # (the real machine's id is whatever it is)
