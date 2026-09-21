from __future__ import annotations

import hashlib
import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from snowpea_core import desktop_app
from snowpea_core.cli.main import build_parser
from snowpea_core.commands.registry import CommandRegistry, register_builtin_commands


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("linux", "x86_64", ("linux", "x64")),
        ("darwin", "arm64", ("mac", "arm64")),
        ("darwin", "x86_64", ("mac", "x64")),
        ("win32", "AMD64", ("win", "x64")),
    ],
)
def test_detect_target(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, expected: tuple[str, str]
) -> None:
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(desktop_app.platform_mod, "machine", lambda: machine)
    if system == "darwin" and machine == "x86_64":
        monkeypatch.setattr(
            desktop_app.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "0\n"})()
        )
    assert desktop_app.detect_target() == expected


def test_linux_arm64_is_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop_app.platform_mod, "machine", lambda: "aarch64")
    with pytest.raises(desktop_app.DesktopError, match="Linux arm64.*download"):
        desktop_app.detect_target()


def test_pick_asset_exact_fallback_direction_and_asset_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed = {
        "latest": {
            "version": "9.0.0",
            "assets": [
                {"platform": "mac", "arch": "x64", "version": "1.2.3", "url": "/x"},
                {"platform": "win", "arch": "x64", "version": "2.0.0", "url": "/w"},
            ],
        }
    }
    assert desktop_app.pick_asset(feed, "win", "x64")["version"] == "2.0.0"
    assert desktop_app.pick_asset(feed, "mac", "arm64")["version"] == "1.2.3"
    assert "Rosetta" in capsys.readouterr().out
    arm_only = {"latest": {"assets": [{"platform": "mac", "arch": "arm64"}]}}
    with pytest.raises(desktop_app.DesktopError):
        desktop_app.pick_asset(arm_only, "mac", "x64")


class _Server:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.asset_requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/releases":
                    payload = {
                        "latest": {
                            "version": "1.2.3",
                            "assets": [
                                {
                                    "platform": "linux",
                                    "arch": "x64",
                                    "kind": "AppImage",
                                    "url": "/asset",
                                    "size_bytes": len(owner.content),
                                    "sha256": hashlib.sha256(owner.content).hexdigest(),
                                    "version": "1.2.3",
                                }
                            ],
                        }
                    }
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/asset":
                    owner.asset_requests += 1
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(owner.content)))
                    self.end_headers()
                    self.wfile.write(owner.content)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            pytest.skip("sandbox forbids loopback sockets")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.httpd.shutdown()
        self.thread.join()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"


def _asset(content: bytes, *, sha: str | None = None, size: int | None = None) -> dict[str, Any]:
    return {
        "url": "/asset",
        "size_bytes": size or len(content),
        "sha256": sha or hashlib.sha256(content).hexdigest(),
    }


def test_download_relative_url_checksum_and_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"tiny fake appimage"
    with _Server(content) as server:
        monkeypatch.setenv("SNOWPEA_DESKTOP_FEED", server.base)
        destination = tmp_path / "app.AppImage"
        assert (
            desktop_app.download(_asset(content), destination, progress=lambda *_: None)
            == destination
        )
        assert destination.read_bytes() == content
        bad = tmp_path / "bad.AppImage"
        with pytest.raises(desktop_app.DesktopError, match="SHA-256"):
            desktop_app.download(_asset(content, sha="0" * 64), bad, progress=lambda *_: None)
        assert not bad.exists() and not bad.with_suffix(".part").exists()
        with pytest.raises(desktop_app.DesktopError, match="exceeded"):
            desktop_app.download(
                _asset(content, size=2), tmp_path / "large", progress=lambda *_: None
            )


def test_non_https_feed_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOWPEA_DESKTOP_FEED", "http://example.com")
    with pytest.raises(desktop_app.DesktopError, match="https"):
        desktop_app.feed_base()


def test_linux_install_record_and_desktop_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("SNOWPEA_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    source = tmp_path / "snowpea-1.2.3.AppImage"
    source.write_bytes(b"app")
    installed = desktop_app.install("linux", source)
    assert installed == home / "desktop" / "snowpea-desktop.AppImage"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    record = json.loads((home / "desktop" / "installed.json").read_text())
    assert record["version"] == "1.2.3" and record["path"] == str(installed)
    entry = (xdg / "applications" / "snowpea-desktop.desktop").read_text()
    assert "Name=snowpea desktop" in entry and f"Exec={installed} %U" in entry


def test_installed_launch_does_not_download_asset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"asset"
    with _Server(content) as server:
        home = tmp_path / "home"
        installed = home / "desktop" / "snowpea-desktop.AppImage"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"installed")
        (installed.parent / "installed.json").write_text(
            json.dumps({"version": "1.2.3", "path": str(installed), "sha256": "x"})
        )
        monkeypatch.setenv("SNOWPEA_HOME", str(home))
        monkeypatch.setenv("SNOWPEA_DESKTOP_FEED", server.base)
        monkeypatch.setattr(desktop_app, "detect_target", lambda: ("linux", "x64"))
        launched: list[Path] = []
        monkeypatch.setattr(desktop_app, "launch", launched.append)
        assert (
            desktop_app.run_cli(
                install_only=False, reinstall=False, check=False, yes=False, as_json=False
            )
            == 0
        )
        assert launched == [installed]
        assert server.asset_requests == 0


def test_check_and_confirmation_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _Server(b"asset") as server:
        monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SNOWPEA_DESKTOP_FEED", server.base)
        monkeypatch.setattr(desktop_app, "detect_target", lambda: ("linux", "x64"))
        assert (
            desktop_app.run_cli(
                install_only=False, reinstall=False, check=True, yes=False, as_json=False
            )
            == 0
        )
        output = capsys.readouterr().out
        assert "installed:" in output and "latest: 1.2.3" in output and server.asset_requests == 0
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert (
            desktop_app.run_cli(
                install_only=False, reinstall=False, check=False, yes=False, as_json=False
            )
            == 2
        )
        assert "--yes" in capsys.readouterr().err


def test_yes_proceeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _Server(b"asset") as server:
        monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SNOWPEA_DESKTOP_FEED", server.base)
        monkeypatch.setattr(desktop_app, "detect_target", lambda: ("linux", "x64"))
        installed = tmp_path / "installed.AppImage"
        monkeypatch.setattr(desktop_app, "install", lambda platform, file: installed)
        launched: list[Path] = []
        monkeypatch.setattr(desktop_app, "launch", launched.append)
        assert (
            desktop_app.run_cli(
                install_only=False, reinstall=False, check=False, yes=True, as_json=False
            )
            == 0
        )
        assert launched == [installed] and server.asset_requests == 1


def test_parser_and_desktop_command_registry() -> None:
    args = build_parser().parse_args(["desktop", "--check"])
    assert args.subcommand == "desktop" and args.check_only
    registry = register_builtin_commands(CommandRegistry())
    command = registry.get("desktop")
    assert command is not None and "Launch" in command.summary
