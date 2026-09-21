"""Install and launch the platform-specific snowpea desktop application."""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_mod
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import urljoin, urlparse

import httpx

from snowpea_core import __version__
from snowpea_core.config.paths import resolve_home
from snowpea_core.update import HTTP_TIMEOUT_SEC, is_newer

DESKTOP_FEED = "https://agent.snowpea.ai"
DOWNLOAD_PAGE = "https://agent.snowpea.ai/download"
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
PRODUCT_NAME = "snowpea"

Platform = Literal["linux", "mac", "win"]
Arch = Literal["x64", "arm64"]
Progress = Callable[[int, int], None]


class DesktopError(RuntimeError):
    """A user-facing desktop discovery, download, install, or launch error."""


def _home() -> Path:
    return Path(resolve_home(None))


def _desktop_dir() -> Path:
    return _home() / "desktop"


def _loopback(host: str | None) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


def _secure_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" or (parsed.scheme == "http" and _loopback(parsed.hostname))


def feed_base() -> str:
    """Return and validate the configured release-feed origin."""
    value = os.environ.get("SNOWPEA_DESKTOP_FEED", DESKTOP_FEED).rstrip("/")
    parsed = urlparse(value)
    if not parsed.netloc or not _secure_url(value):
        raise DesktopError(
            "SNOWPEA_DESKTOP_FEED must use https (http is allowed only for localhost)"
        )
    return value


def detect_target() -> tuple[Platform, Arch]:
    """Map the running OS and CPU to release-feed identifiers."""
    system = sys.platform
    machine = platform_mod.machine().lower()
    if system.startswith("linux"):
        target: Platform = "linux"
    elif system == "darwin":
        target = "mac"
        if machine in {"x86_64", "amd64"}:
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.optional.arm64"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.stdout.strip() == "1":
                    machine = "arm64"
            except OSError:
                pass
    elif system == "win32":
        target = "win"
    else:
        raise DesktopError(f"unsupported desktop platform: {system}")
    if machine in {"x86_64", "amd64"}:
        arch: Arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise DesktopError(f"unsupported desktop CPU: {machine}")
    if target == "linux" and arch == "arm64":
        raise DesktopError(f"snowpea desktop has no Linux arm64 build; see {DOWNLOAD_PAGE}")
    return target, arch


def pick_asset(feed: Mapping[str, Any], platform: Platform, arch: Arch) -> dict[str, Any]:
    """Choose the exact asset, with only mac arm64-to-x64 fallback permitted."""
    latest = feed.get("latest")
    assets = latest.get("assets", []) if isinstance(latest, Mapping) else []
    candidates = [a for a in assets if isinstance(a, Mapping) and a.get("platform") == platform]
    for item in candidates:
        if item.get("arch") == arch:
            return dict(item)
    if platform == "mac" and arch == "arm64":
        for item in candidates:
            if item.get("arch") == "x64":
                print("No macOS arm64 build is available; using the x64 build with Rosetta.")
                return dict(item)
    if platform == "linux" and arch == "arm64":
        raise DesktopError(f"snowpea desktop has no Linux arm64 build; see {DOWNLOAD_PAGE}")
    raise DesktopError(f"no snowpea desktop build for {platform}/{arch}; see {DOWNLOAD_PAGE}")


def _read_record() -> dict[str, Any]:
    try:
        value = json.loads((_desktop_dir() / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def find_installed() -> Path | None:
    """Find a recorded install first, then fixed platform-specific locations."""
    record = _read_record()
    recorded = record.get("path")
    if isinstance(recorded, str) and Path(recorded).exists():
        return Path(recorded)
    try:
        target, _ = detect_target()
    except DesktopError:
        return None
    if target == "linux":
        managed = _desktop_dir() / "snowpea-desktop.AppImage"
        if managed.exists():
            return managed
        for directory in (Path.home() / "Applications", Path.home() / ".local" / "bin"):
            matches = sorted(directory.glob("snowpea-ide-*.AppImage")) if directory.is_dir() else []
            if matches:
                return matches[-1]
    elif target == "mac":
        for candidate in (
            Path("/Applications") / f"{PRODUCT_NAME}.app",
            Path.home() / "Applications" / f"{PRODUCT_NAME}.app",
        ):
            if candidate.exists():
                return candidate
    else:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidate = Path(local) / "Programs" / PRODUCT_NAME / f"{PRODUCT_NAME}.exe"
            if candidate.exists():
                return candidate
    return None


def fetch_feed() -> dict[str, Any]:
    """Fetch the desktop release feed with the core updater's HTTP conventions."""
    base = feed_base()
    url = f"{base}/api/releases"
    try:
        response = httpx.get(
            url,
            timeout=HTTP_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": f"snowpea-agent/{__version__}"},
        )
        response.raise_for_status()
        _validate_redirects(response)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DesktopError(f"could not fetch desktop releases: {exc}") from exc
    if not isinstance(payload, dict):
        raise DesktopError("desktop release feed is not a JSON object")
    return payload


def _asset_url(asset: Mapping[str, Any]) -> str:
    raw = str(asset.get("url") or "")
    url = urljoin(f"{feed_base()}/", raw)
    if not _secure_url(url):
        raise DesktopError("desktop download URL must use https")
    return url


def _validate_redirects(response: httpx.Response) -> None:
    for hop in response.history:
        location = hop.headers.get("location")
        if location and not _secure_url(urljoin(str(hop.url), location)):
            raise DesktopError("desktop request redirected to an insecure URL")
    if not _secure_url(str(response.url)):
        raise DesktopError("desktop request redirected to an insecure URL")


def download(asset: Mapping[str, Any], dest: Path, *, progress: Progress) -> Path:
    """Stream, bound, and verify one release asset before atomically publishing it."""
    expected_size = int(asset.get("size_bytes") or 0)
    expected_sha = str(asset.get("sha256") or "").lower()
    if expected_size <= 0 or expected_size > MAX_DOWNLOAD_BYTES:
        raise DesktopError("desktop download size is invalid or exceeds the 1 GiB limit")
    if len(expected_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise DesktopError("desktop release has an invalid SHA-256")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    part.unlink(missing_ok=True)
    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET",
            _asset_url(asset),
            timeout=HTTP_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": f"snowpea-agent/{__version__}"},
        ) as response:
            response.raise_for_status()
            _validate_redirects(response)
            with part.open("wb") as handle:
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > expected_size or received > MAX_DOWNLOAD_BYTES:
                        raise DesktopError("desktop download exceeded its declared size")
                    digest.update(chunk)
                    handle.write(chunk)
                    progress(received, expected_size)
        if received != expected_size:
            raise DesktopError(
                f"desktop download size mismatch: expected {expected_size}, got {received}"
            )
        if digest.hexdigest().lower() != expected_sha:
            raise DesktopError("desktop download SHA-256 mismatch")
        part.replace(dest)
        return dest
    except (httpx.HTTPError, OSError) as exc:
        raise DesktopError(f"desktop download failed: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)


def _write_record(path: Path, *, version: str, sha256: str) -> None:
    directory = _desktop_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "installed.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": version, "path": str(path), "sha256": sha256}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _record_from_file(path: Path, source: Path) -> None:
    match = re.search(r"(\d+\.\d+\.\d+)", source.name)
    digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else ""
    _write_record(path, version=match.group(1) if match else "unknown", sha256=digest)


def install(platform: Platform, file: Path) -> Path:
    """Install a verified asset and return the fixed executable/application path."""
    if platform == "linux":
        source = file
        target = _desktop_dir() / "snowpea-desktop.AppImage"
        target.parent.mkdir(parents=True, exist_ok=True)
        if file != target:
            shutil.move(str(file), target)
        target.chmod(0o755)
        applications = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"
        )
        try:
            applications.mkdir(parents=True, exist_ok=True)
            (applications / "snowpea-desktop.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=snowpea desktop\n"
                f"Exec={target} %U\nTerminal=false\nCategories=Development;\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        _record_from_file(target, source)
        return target
    if platform == "mac":
        with tempfile.TemporaryDirectory(prefix="snowpea-desktop-") as mount:
            mountpoint = Path(mount)
            subprocess.run(
                [
                    "hdiutil",
                    "attach",
                    "-nobrowse",
                    "-readonly",
                    "-mountpoint",
                    str(mountpoint),
                    str(file),
                ],
                check=True,
            )
            try:
                bundles = list(mountpoint.glob("*.app"))
                if not bundles:
                    raise DesktopError("the desktop DMG contains no application bundle")
                system_target = Path("/Applications") / f"{PRODUCT_NAME}.app"
                user_target = Path.home() / "Applications" / f"{PRODUCT_NAME}.app"
                target = system_target if os.access("/Applications", os.W_OK) else user_target
                target.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ditto", str(bundles[0]), str(target)], check=True)
            finally:
                subprocess.run(["hdiutil", "detach", str(mountpoint)], check=False)
        _record_from_file(target, file)
        return target
    subprocess.run([str(file), "/S"], check=True)
    windows_target = find_installed()
    if windows_target is None:
        raise DesktopError("the Windows installer finished but snowpea.exe was not found")
    _record_from_file(windows_target, file)
    return windows_target


def launch(path: Path) -> None:
    """Launch a discovered fixed-location desktop executable without waiting."""
    target, _ = detect_target()
    if target == "linux":
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise DesktopError(
                "no graphical display is available; snowpea desktop was not launched"
            )
        try:
            subprocess.Popen(
                [str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            if "fuse" not in str(exc).lower():
                raise DesktopError(f"could not launch snowpea desktop: {exc}") from exc
            subprocess.Popen(
                [str(path), "--appimage-extract-and-run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    elif target == "mac":
        try:
            subprocess.Popen(
                ["open", "-a", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise DesktopError(
                "macOS blocked the first launch; right-click snowpea.app and choose Open"
            ) from exc
    elif hasattr(os, "startfile"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(
            [str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        )


def installed_info(path: Path | None = None) -> dict[str, str]:
    """Return recorded metadata when it still describes the discovered path."""
    found = path or find_installed()
    if found is None:
        return {}
    record = _read_record()
    return {
        "version": str(record.get("version") or "unknown"),
        "path": str(found),
        "sha256": str(record.get("sha256") or ""),
    }


def fail(message: str) -> NoReturn:
    raise DesktopError(message)


def run_cli(*, install_only: bool, reinstall: bool, check: bool, yes: bool, as_json: bool) -> int:
    """Implement ``snowpea desktop`` with deterministic CLI exit codes."""
    try:
        installed = find_installed()
        info = installed_info(installed)
        target, arch = detect_target()
        try:
            feed = fetch_feed()
        except DesktopError:
            if installed is None or check or reinstall:
                raise
            feed = None
        asset = pick_asset(feed, target, arch) if feed is not None else None
        if check:
            assert asset is not None
            payload = {"installed": info or None, "latest": asset}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                installed_version = info.get("version", "not installed")
                print(f"installed: {installed_version} {info.get('path', '')}".rstrip())
                print(f"latest: {asset.get('version', 'unknown')} ({target}/{arch})")
            return 0
        if installed is not None and not reinstall:
            latest = str(asset.get("version") or "") if asset else ""
            current = info.get("version", "unknown")
            newer = is_newer(latest, current)
            if newer and not as_json:
                print(f"newer desktop {latest} is available: snowpea desktop --reinstall")
            if not install_only:
                launch(installed)
            if as_json:
                print(
                    json.dumps(
                        {
                            "version": current,
                            "path": str(installed),
                            "launched": not install_only,
                            "newer": latest if newer else None,
                        }
                    )
                )
            else:
                action = "installed" if install_only else "launched"
                print(f"snowpea desktop {current} {action}")
            return 0
        assert asset is not None
        size = int(asset["size_bytes"])
        source = urlparse(_asset_url(asset)).hostname or "unknown"
        if not as_json:
            print(
                f"snowpea desktop {asset.get('version')} ({size / 1_000_000:.1f} MB) from {source}"
            )
        if not yes:
            if not sys.stdin.isatty():
                print("snowpea: confirmation requires a terminal; pass --yes", file=sys.stderr)
                return 2
            answer = input("Download and install? [Y/n] ").strip().lower()
            if answer not in {"", "y", "yes"}:
                return 2
        suffix = {"linux": ".AppImage", "mac": ".dmg", "win": ".exe"}[target]
        staging = _desktop_dir() / f"snowpea-desktop-{asset.get('version')}{suffix}"

        # One redrawn line on a terminal, a step every 10% in a pipe or a log:
        # a line per 64 KiB chunk was three thousand lines for one download.
        live = sys.stdout.isatty()
        shown = {"step": -1}

        def progress(done: int, total: int) -> None:
            if as_json or total <= 0:
                return
            step = done * (100 if live else 10) // total
            if step == shown["step"]:
                return
            shown["step"] = step
            line = f"Downloading {done / 1_000_000:.1f}/{total / 1_000_000:.1f} MB"
            print(f"\r{line}" if live else line, end="" if live else "\n", flush=True)

        downloaded = download(asset, staging, progress=progress)
        if live and not as_json:
            print()
        installed = install(target, downloaded)
        _write_record(
            installed,
            version=str(asset.get("version") or "unknown"),
            sha256=str(asset.get("sha256") or ""),
        )
        if not install_only:
            launch(installed)
        if as_json:
            print(
                json.dumps(
                    {
                        "version": asset.get("version"),
                        "path": str(installed),
                        "launched": not install_only,
                    }
                )
            )
        else:
            action = "installed" if install_only else "launched"
            print(f"snowpea desktop {asset.get('version')} {action}")
        return 0
    except (DesktopError, OSError, subprocess.SubprocessError) as exc:
        print(f"snowpea: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "DESKTOP_FEED",
    "DesktopError",
    "detect_target",
    "download",
    "fetch_feed",
    "find_installed",
    "install",
    "installed_info",
    "launch",
    "pick_asset",
    "run_cli",
]
