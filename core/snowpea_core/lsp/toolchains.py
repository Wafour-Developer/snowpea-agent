"""Toolchains a language server's installer needs: what is missing, and the .NET SDK.

``lsp.install`` drives package managers (npm, go, ``dotnet tool``, rustup).
When the package manager itself is missing, a catalog row and an install
result say so with ``reason: "needs_toolchain"`` and a :data:`TOOLCHAINS`
entry (1.68.0, capability ``lspInstallReasons``).

One toolchain core can obtain by itself, at the person's request
(``lsp.install {withToolchain: true}``, capability ``lspToolchainInstall``):
the .NET SDK.  It comes straight from Microsoft's release metadata
(``releases-index.json`` → the channel's ``releases.json``, which carries a
sha512 for every archive), is downloaded over https from Microsoft's own
hosts only, verified against that sha512 and unpacked into a per-user,
core-owned directory — never the system PATH, never a system-wide dotnet.
``DOTNET_ROOT``, ``PATH`` and ``DOTNET_CLI_HOME`` point at it only in the
environment of the ``dotnet tool`` install and of the servers it installed
(:func:`dotnet_env`); the daemon's own environment never changes.

Go and rustup are reported as needed but not installed yet; :data:`TOOLCHAINS`
marks which entries :func:`install` can handle (``auto``).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import platform
import shutil
import tarfile
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from snowpea_core.exec import winproc

log = logging.getLogger("snowpea.lsp.toolchains")

REASON_NEEDS_TOOLCHAIN = "needs_toolchain"
REASON_NPM_FAILED = "npm_failed"
REASON_TOOLCHAIN_FAILED = "toolchain_failed"


@dataclass(frozen=True)
class Toolchain:
    name: str
    #: What ``lsp.catalog``'s ``needs`` has always said for it (kept for old clients).
    needs: str
    hint: str
    url: str
    #: True when :func:`install` can obtain it (``withToolchain``).
    auto: bool = False


TOOLCHAINS: dict[str, Toolchain] = {
    "dotnet-sdk": Toolchain(
        "dotnet-sdk",
        ".NET SDK",
        "Install the .NET SDK: the .NET runtime alone cannot install language servers",
        "https://dotnet.microsoft.com/download",
        auto=True,
    ),
    "go": Toolchain("go", "go", "Install Go and make sure go is on PATH", "https://go.dev/dl/"),
    "rustup": Toolchain(
        "rustup", "rustup", "Install Rust with rustup and make sure it is on PATH",
        "https://rustup.rs",
    ),
    "node": Toolchain(
        "node", "npm", "Install Node.js (it brings npm) and make sure npm is on PATH",
        "https://nodejs.org/en/download",
    ),
    "python": Toolchain(
        "python", "python3", "Install Python 3 (or uv) and make sure it is on PATH",
        "https://www.python.org/downloads/",
    ),
}

#: The toolchain each ``Install.kind`` runs.
TOOLCHAIN_FOR_KIND: dict[str, str] = {
    "dotnet": "dotnet-sdk",
    "go": "go",
    "rustup": "rustup",
    "npm": "node",
    "pip": "python",
}


def reason_fields(name: str) -> dict[str, Any]:
    """``{reason, toolchain, hint, url, toolchainInstallable}`` for a missing toolchain."""
    info = TOOLCHAINS[name]
    return {
        "reason": REASON_NEEDS_TOOLCHAIN,
        "toolchain": name,
        "hint": info.hint,
        "url": info.url,
        "toolchainInstallable": info.auto,
    }


class ToolchainError(Exception):
    """A toolchain install that did not happen; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# where
# ---------------------------------------------------------------------------


def toolchains_dir() -> Path:
    """The per-user directory core owns for toolchains (no admin rights needed).

    ``%LOCALAPPDATA%\\snowpea\\toolchains`` on Windows,
    ``$XDG_DATA_HOME/snowpea/toolchains`` (``~/.local/share``) elsewhere;
    ``SNOWPEA_TOOLCHAINS_DIR`` overrides both.
    """
    override = os.environ.get("SNOWPEA_TOOLCHAINS_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "snowpea" / "toolchains"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "snowpea" / "toolchains"


MARKER = ".snowpea-toolchain.json"


def dotnet_dir() -> Path:
    return toolchains_dir() / "dotnet"


def dotnet_exe() -> Path:
    return dotnet_dir() / ("dotnet.exe" if os.name == "nt" else "dotnet")


def dotnet_installed() -> bool:
    """True when core's own .NET SDK is complete (its marker is written last)."""
    return (dotnet_dir() / MARKER).is_file() and dotnet_exe().is_file()


def dotnet_env() -> dict[str, str]:
    """The environment a ``dotnet tool`` install or a .NET server gets, or ``{}``.

    Only for child processes: merged over ``os.environ`` by the caller.
    """
    if not dotnet_installed():
        return {}
    root = str(dotnet_dir())
    return {
        "DOTNET_ROOT": root,
        "PATH": root + os.pathsep + os.environ.get("PATH", ""),
        "DOTNET_CLI_HOME": str(toolchains_dir() / "dotnet-cli-home"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_MULTILEVEL_LOOKUP": "0",
    }


# ---------------------------------------------------------------------------
# is there an SDK
# ---------------------------------------------------------------------------

#: ``dotnet`` without an SDK exits with this (0x8000809B), "sdk-not-found".
SDK_NOT_FOUND_EXIT = 2147516571
_SDK_NOT_FOUND_CODES = frozenset({SDK_NOT_FOUND_EXIT, SDK_NOT_FOUND_EXIT - 2**32})
SDK_CHECK_TIMEOUT_SEC = 15.0
_SDK_CACHE_SEC = 30.0
_SDK_CACHE: dict[str, tuple[float, list[str]]] = {}


def parse_list_sdks(text: str) -> list[str]:
    """The versions in ``dotnet --list-sdks`` output (``8.0.404 [C:\\…\\sdk]`` lines)."""
    versions = []
    for line in text.splitlines():
        head = line.strip().split(" ", 1)[0]
        if head[:1].isdigit() and "." in head:
            versions.append(head)
    return versions


def sdk_missing(returncode: int | None, text: str) -> bool:
    """True when a ``dotnet`` command failed only because there is no SDK."""
    lowered = text.lower()
    return (
        returncode in _SDK_NOT_FOUND_CODES
        or "sdk-not-found" in lowered
        or "no .net sdks were found" in lowered
    )


async def dotnet_sdks(dotnet: str, *, env: dict[str, str] | None = None) -> list[str]:
    """``dotnet --list-sdks``: the SDK versions, ``[]`` for none or any failure."""
    now = time.monotonic()
    cached = _SDK_CACHE.get(dotnet)
    if cached is not None and now - cached[0] < _SDK_CACHE_SEC:
        return cached[1]
    versions: list[str] = []
    try:
        process = await asyncio.create_subprocess_exec(
            dotnet,
            "--list-sdks",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            **winproc.spawn_kwargs(),
        )
    except (OSError, ValueError) as exc:
        log.debug("could not run %s --list-sdks: %s", dotnet, exc)
    else:
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=SDK_CHECK_TIMEOUT_SEC
            )
        except TimeoutError:
            await winproc.kill(process)
            with contextlib.suppress(Exception):
                await process.wait()
            log.warning("%s --list-sdks timed out", dotnet)
        else:
            if process.returncode == 0:
                versions = parse_list_sdks(output.decode(errors="replace"))
    _SDK_CACHE[dotnet] = (now, versions)
    return versions


def forget_sdks() -> None:
    """Drop the cached ``--list-sdks`` answers (after an install or a removal)."""
    _SDK_CACHE.clear()


# ---------------------------------------------------------------------------
# the .NET SDK from Microsoft's release metadata
# ---------------------------------------------------------------------------

#: The SDK channel core installs: the current LTS, which csharp-ls and
#: fsautocomplete both install and run under.
DOTNET_CHANNEL = "10.0"
RELEASES_INDEX_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json"
)
#: Every URL fetched (metadata, archive, each redirect hop) must be https on one of these.
ALLOWED_HOSTS: frozenset[str] = frozenset({
    "builds.dotnet.microsoft.com",
    "dotnetcli.blob.core.windows.net",
    "dotnetcli.azureedge.net",
    "download.visualstudio.microsoft.com",
})
#: Download size shown when the archive's size cannot be learnt (MB).
ESTIMATE_MB = 300
#: Free space wanted per byte downloaded: the archive plus what it unpacks to.
DISK_FACTOR = 4
METADATA_TIMEOUT_SEC = 15.0
_MAX_REDIRECTS = 5
_CHUNK = 1 << 20

ClientFactory = Callable[[], Any]
#: ``progress(phase, percent, line)``: phase is resolve, download, verify, extract.
Progress = Callable[[str, int | None, str | None], Awaitable[None]]


@dataclass(frozen=True)
class DotnetRelease:
    version: str
    url: str
    sha512: str
    rid: str
    size: int | None = None


def check_url(url: str) -> None:
    """Refuse anything but https on :data:`ALLOWED_HOSTS`."""
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ToolchainError("source", f"refused to fetch {url}: not an official .NET host")


def runtime_id(system: str | None = None, machine: str | None = None) -> str:
    """The .NET runtime id of this machine: ``win-x64``, ``osx-arm64``, ``linux-x64``…"""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_part = {"windows": "win", "darwin": "osx"}.get(system, "linux")
    arch = {
        "amd64": "x64", "x86_64": "x64", "x64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
        "x86": "x86", "i386": "x86", "i686": "x86",
    }.get(machine, machine)
    return f"{os_part}-{arch}"


def _client() -> Any:
    import httpx

    # Redirects are followed by hand, each hop checked against ALLOWED_HOSTS.
    return httpx.AsyncClient(timeout=60.0, follow_redirects=False)


async def _open(client: Any, method: str, url: str, headers: dict[str, str] | None = None) -> Any:
    """``method url`` streamed, redirects followed only within :data:`ALLOWED_HOSTS`."""
    for _ in range(_MAX_REDIRECTS + 1):
        check_url(url)
        request = client.build_request(method, url, headers=headers or {})
        response = await client.send(request, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise ToolchainError("network", f"redirect without a location from {url}")
            url = urljoin(url, location)
            continue
        return response
    raise ToolchainError("network", f"too many redirects from {url}")


async def _json(client: Any, url: str) -> Any:
    response = await _open(client, "GET", url)
    try:
        if response.status_code != 200:
            raise ToolchainError("network", f"HTTP {response.status_code} for {url}")
        body = await response.aread()
    finally:
        await response.aclose()
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ToolchainError("metadata", f"{url} is not JSON: {exc}") from exc


def _pick_file(release: dict[str, Any], rid: str) -> tuple[str, str, str]:
    """``(sdk version, url, sha512)`` of the SDK archive for ``rid``."""
    sdk = release.get("sdk") or {}
    version = str(sdk.get("version") or "")
    suffix = ".zip" if rid.startswith("win-") else ".tar.gz"
    for item in sdk.get("files") or ():
        name = str(item.get("name") or "")
        if item.get("rid") == rid and name.startswith("dotnet-sdk") and name.endswith(suffix):
            url, digest = str(item.get("url") or ""), str(item.get("hash") or "")
            if url and len(digest) == 128:
                return version, url, digest.lower()
    raise ToolchainError("metadata", f"no .NET SDK {version or DOTNET_CHANNEL} archive for {rid}")


async def resolve_dotnet(
    *, client_factory: ClientFactory | None = None, rid: str | None = None
) -> DotnetRelease:
    """The newest SDK of :data:`DOTNET_CHANNEL` for this machine, with its sha512."""
    rid = rid or runtime_id()
    try:
        async with (client_factory or _client)() as client:
            index = await _json(client, RELEASES_INDEX_URL)
            entry = next(
                (item for item in index.get("releases-index") or ()
                 if item.get("channel-version") == DOTNET_CHANNEL),
                None,
            )
            if entry is None or not entry.get("releases.json"):
                raise ToolchainError("metadata", f"no .NET {DOTNET_CHANNEL} channel in the index")
            channel = await _json(client, str(entry["releases.json"]))
            wanted = str(entry.get("latest-sdk") or "")
            releases = channel.get("releases") or []
            release = next(
                (item for item in releases if (item.get("sdk") or {}).get("version") == wanted),
                releases[0] if releases else {},
            )
            version, url, digest = _pick_file(release, rid)
            check_url(url)
            size: int | None = None
            with contextlib.suppress(Exception):
                head = await _open(client, "HEAD", url)
                await head.aclose()
                if head.status_code == 200:
                    size = int(head.headers.get("content-length") or 0) or None
            return DotnetRelease(version, url, digest, rid, size)
    except ToolchainError:
        raise
    except Exception as exc:  # noqa: BLE001 - every network failure is one clear error
        raise ToolchainError("network", f"could not read the .NET release metadata: {exc}") from exc


async def offer(name: str, *, client_factory: ClientFactory | None = None) -> dict[str, Any]:
    """``{name, sizeMb, source, version}`` for the confirm a person sees before an install."""
    if name != "dotnet-sdk":
        return {"name": name, "sizeMb": None, "source": None, "version": None}
    try:
        release = await asyncio.wait_for(
            resolve_dotnet(client_factory=client_factory), timeout=METADATA_TIMEOUT_SEC
        )
    except (ToolchainError, TimeoutError) as exc:
        log.info("no .NET release metadata for the offer: %s", exc)
        return {"name": name, "sizeMb": ESTIMATE_MB, "source": RELEASES_INDEX_URL,
                "version": None}
    size_mb = round(release.size / 1_000_000) if release.size else ESTIMATE_MB
    return {"name": name, "sizeMb": size_mb, "source": release.url, "version": release.version}


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


async def _download(
    client: Any, release: DotnetRelease, part: Path, progress: Progress
) -> None:
    """Fetch ``release`` into ``part`` (resuming a partial one) and verify its sha512."""
    for _attempt in range(2):
        have = part.stat().st_size if part.is_file() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        response = await _open(client, "GET", release.url, headers)
        try:
            if response.status_code == 416 and have:
                part.unlink()  # the partial file is not a prefix of this archive
                continue
            if response.status_code == 206 and have:
                mode = "ab"
            elif response.status_code == 200:
                have, mode = 0, "wb"
            else:
                raise ToolchainError(
                    "network", f"HTTP {response.status_code} for {release.url}"
                )
            length = int(response.headers.get("content-length") or 0)
            total = (have + length) if length else (release.size or 0)
            digest = hashlib.sha512()
            if have:
                with part.open("rb") as handle:
                    for block in iter(lambda: handle.read(_CHUNK), b""):
                        digest.update(block)
            done, shown = have, -1
            try:
                with part.open(mode) as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        percent = int(done * 100 / total) if total else None
                        if percent is not None and percent != shown:
                            shown = percent
                            await progress("download", min(percent, 100), None)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    with contextlib.suppress(OSError):
                        part.unlink()
                    raise ToolchainError("disk_full", f"the disk filled up: {exc}") from exc
                raise ToolchainError("io", f"could not write {part}: {exc}") from exc
        finally:
            await response.aclose()
        await progress("verify", None, f"sha512 {digest.hexdigest()[:16]}…")
        if digest.hexdigest() != release.sha512:
            with contextlib.suppress(OSError):
                part.unlink()
            raise ToolchainError(
                "checksum",
                "the downloaded .NET SDK does not match Microsoft's sha512; "
                "nothing was installed",
            )
        return
    raise ToolchainError("network", f"could not resume the download of {release.url}")


def _inside(base: Path, name: str) -> Path:
    target = (base / name).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ToolchainError("archive", f"refused archive entry {name!r}: outside the target")
    return target


def _extract(archive: Path, target: Path) -> None:
    """Unpack ``archive`` into ``target``; entries that leave it are refused."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                _inside(target, member.filename)
            bundle.extractall(target)
            if os.name != "nt":
                for member in bundle.infolist():
                    mode = (member.external_attr >> 16) & 0o777
                    if mode:
                        os.chmod(_inside(target, member.filename), mode)
        return
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            _inside(target, member.name)
            if member.issym() or member.islnk():
                link = member.linkname
                base = (target / member.name).parent if member.issym() else target
                if os.path.isabs(link) or not (base / link).resolve().is_relative_to(
                    target.resolve()
                ):
                    raise ToolchainError("archive", f"refused link {member.name!r} -> {link!r}")
            elif not (member.isfile() or member.isdir()):
                raise ToolchainError("archive", f"refused special entry {member.name!r}")
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(target, members=members, filter="data")
        else:  # pragma: no cover - Python before 3.11.4
            bundle.extractall(target, members=members)


def _rmtree(path: Path) -> None:
    """Remove a directory core created under :func:`toolchains_dir`, and nothing else."""
    root = toolchains_dir().resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ToolchainError("io", f"refused to remove {path}: not a toolchain directory")
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


async def _install_dotnet(
    progress: Progress, client_factory: ClientFactory | None, rid: str | None
) -> str:
    base = toolchains_dir()
    downloads = base / "downloads"
    staging = base / "dotnet.staging"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolchainError("io", f"cannot write {base}: {exc}") from exc
    await progress("resolve", None, RELEASES_INDEX_URL)
    release = await resolve_dotnet(client_factory=client_factory, rid=rid)
    archive_name = urlsplit(release.url).path.rsplit("/", 1)[-1] or f"dotnet-sdk-{rid}"
    part = downloads / (archive_name + ".part")
    for old in downloads.glob("*.part"):
        if old != part:  # an older version's partial download
            with contextlib.suppress(OSError):
                old.unlink()
    have = part.stat().st_size if part.is_file() else 0
    wanted = (release.size or ESTIMATE_MB * 1_000_000) * DISK_FACTOR - have
    free = _free_bytes(base)
    if free < wanted:
        raise ToolchainError(
            "disk_full",
            f"the .NET SDK needs about {wanted // 1_000_000} MB free in {base}; "
            f"{free // 1_000_000} MB are",
        )
    await progress("download", 0, f"{release.url} (.NET SDK {release.version})")
    try:
        async with (client_factory or _client)() as client:
            await _download(client, release, part, progress)
    except ToolchainError:
        raise
    except asyncio.CancelledError:
        raise  # the partial download stays for the next try
    except Exception as exc:  # noqa: BLE001 - network failures are one clear error
        raise ToolchainError("network", f"the download failed: {exc}") from exc
    await progress("extract", None, str(dotnet_dir()))
    _rmtree(staging)
    archive = part.with_name(archive_name)
    part.replace(archive)
    marker = {
        "name": "dotnet-sdk",
        "version": release.version,
        "rid": release.rid,
        "source": release.url,
        "sha512": release.sha512,
        "installedAt": int(time.time()),
    }

    def unpack() -> None:
        staging.mkdir(parents=True)
        _extract(archive, staging)
        (staging / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    job = asyncio.ensure_future(asyncio.to_thread(unpack))
    try:
        await asyncio.shield(job)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await job  # the thread finishes; then its half-made tree goes
        with contextlib.suppress(Exception):
            _rmtree(staging)
            archive.unlink()
        raise
    except ToolchainError:
        with contextlib.suppress(Exception):
            _rmtree(staging)
            archive.unlink()
        raise
    except OSError as exc:
        with contextlib.suppress(Exception):
            _rmtree(staging)
            archive.unlink()
        code = "disk_full" if exc.errno == errno.ENOSPC else "io"
        raise ToolchainError(code, f"could not unpack the .NET SDK: {exc}") from exc
    try:
        _rmtree(dotnet_dir())
        staging.replace(dotnet_dir())
    except OSError as exc:
        raise ToolchainError(
            "io", f"could not replace {dotnet_dir()} (a .NET server may still run): {exc}"
        ) from exc
    with contextlib.suppress(OSError):
        archive.unlink()
    forget_sdks()
    await progress("done", 100, f".NET SDK {release.version} in {dotnet_dir()}")
    return str(dotnet_exe())


_RUNNING: dict[str, asyncio.Task[str]] = {}


async def install(
    name: str,
    progress: Progress,
    *,
    client_factory: ClientFactory | None = None,
    rid: str | None = None,
) -> str:
    """Install toolchain ``name`` and return its main binary; one at a time.

    Raises :class:`ToolchainError` (``busy``, ``unsupported``, ``source``,
    ``network``, ``metadata``, ``checksum``, ``disk_full``, ``archive``, ``io``,
    ``cancelled``).  :func:`cancel` stops it; a partial download is resumed
    by the next install, a partial unpack is removed.
    """
    info = TOOLCHAINS.get(name)
    if info is None or not info.auto:
        raise ToolchainError("unsupported", f"snowpea cannot install {name} by itself yet")
    if any(not task.done() for task in _RUNNING.values()):
        raise ToolchainError("busy", "a toolchain install is already running")
    task = asyncio.ensure_future(_install_dotnet(progress, client_factory, rid))
    _RUNNING[name] = task
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and not (current and current.cancelling()):
            raise ToolchainError("cancelled", "the .NET SDK install was cancelled") from None
        task.cancel()
        raise
    finally:
        if _RUNNING.get(name) is task:
            del _RUNNING[name]


def running(name: str) -> bool:
    task = _RUNNING.get(name)
    return task is not None and not task.done()


def cancel(name: str) -> bool:
    """Stop a running install of ``name``; True when one was running."""
    task = _RUNNING.get(name)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def remove(name: str) -> bool:
    """Delete core's own copy of ``name`` (and its downloads); True when there was one."""
    if name != "dotnet-sdk":
        raise ToolchainError("unsupported", f"snowpea did not install {name}")
    if running(name):
        raise ToolchainError("busy", "the .NET SDK is being installed; cancel it first")
    base = toolchains_dir()
    found = False
    for path in (dotnet_dir(), base / "dotnet.staging", base / "downloads",
                 base / "dotnet-cli-home"):
        if path.exists() or path.is_symlink():
            found = found or path == dotnet_dir()
            _rmtree(path)
    forget_sdks()
    return found


__all__ = [
    "ALLOWED_HOSTS",
    "DOTNET_CHANNEL",
    "REASON_NEEDS_TOOLCHAIN",
    "REASON_NPM_FAILED",
    "REASON_TOOLCHAIN_FAILED",
    "RELEASES_INDEX_URL",
    "SDK_NOT_FOUND_EXIT",
    "TOOLCHAINS",
    "TOOLCHAIN_FOR_KIND",
    "DotnetRelease",
    "Toolchain",
    "ToolchainError",
    "cancel",
    "check_url",
    "dotnet_dir",
    "dotnet_env",
    "dotnet_exe",
    "dotnet_installed",
    "dotnet_sdks",
    "forget_sdks",
    "install",
    "offer",
    "parse_list_sdks",
    "reason_fields",
    "remove",
    "resolve_dotnet",
    "running",
    "runtime_id",
    "sdk_missing",
    "toolchains_dir",
]
