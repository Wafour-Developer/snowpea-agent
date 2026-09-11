"""``snowpea service install|uninstall|status`` (M8 contract §3).

Registers the core daemon with the platform's per-user service manager so it
comes back after a reboot: a systemd **user** unit on Linux, a launchd
LaunchAgent on macOS, a logon Scheduled Task on Windows.  Never a system-wide
service — nothing here needs root, and the daemon reads the user's
``SNOWPEA_HOME``.

Off by default: nothing installs a service unless the user asks for it.
``status`` on a machine that never ran ``install`` prints ``not installed`` and
exits 0, so it is safe to call from scripts.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from snowpea_core.cli.render import EXIT_OK, EXIT_USAGE
from snowpea_core.config.paths import resolve_home

#: systemd unit / launchd label / scheduled-task name.
UNIT_NAME = "snowpea.service"
LAUNCHD_LABEL = "ai.snowpea.daemon"
TASK_NAME = "Snowpea"

ACTIONS = ("install", "uninstall", "status")


class ServiceError(RuntimeError):
    """Something the user has to fix (→ exit 2), with a precise message."""


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def daemon_command(home: Path | str | None = None) -> list[str]:
    """The argv a service unit runs: ``snowpea-core --port 0`` (contract §3).

    Prefers the installed console script; falls back to ``python -m
    snowpea_core`` so a checkout or a venv without the script still works.
    """
    executable = shutil.which("snowpea-core")
    command = [executable] if executable else [sys.executable, "-m", "snowpea_core"]
    command += ["--port", "0"]
    resolved = home if home is not None else os.environ.get("SNOWPEA_HOME")
    if resolved:
        command += ["--home", str(resolve_home(resolved))]
    return command


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is built here, never from user text
        argv, capture_output=True, text=True, check=False
    )


def _quote(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


@dataclass(frozen=True)
class ServiceStatus:
    """What ``snowpea service status`` reports."""

    manager: str
    unit: str
    installed: bool
    running: bool
    detail: str = ""

    def render(self) -> str:
        if not self.installed:
            return f"not installed ({self.manager})"
        state = "running" if self.running else "installed, not running"
        line = f"{state} ({self.manager}: {self.unit})"
        return f"{line} — {self.detail}" if self.detail else line


# ---------------------------------------------------------------------------
# linux — systemd user unit
# ---------------------------------------------------------------------------


def systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "systemd" / "user" / UNIT_NAME


def systemd_unit_text(home: Path | str | None = None) -> str:
    return f"""[Unit]
Description=Snowpea agent daemon
Documentation=https://github.com/Wafour-Developer/snowpea-agent
After=network.target

[Service]
Type=simple
ExecStart={_quote(daemon_command(home))}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which("systemctl"):
        return None
    return _run(["systemctl", "--user", *args])


def _install_systemd(home: Path | str | None) -> str:
    path = systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(systemd_unit_text(home), encoding="utf-8")
    result = _systemctl("daemon-reload")
    if result is None:
        return f"wrote {path}; systemctl is not available, so nothing was started"
    started = _systemctl("enable", "--now", UNIT_NAME)
    if started is not None and started.returncode != 0:
        raise ServiceError(
            f"wrote {path} but `systemctl --user enable --now {UNIT_NAME}` failed:\n"
            f"{(started.stderr or started.stdout).strip()}"
        )
    return f"installed and started {UNIT_NAME} ({path})"


def _uninstall_systemd() -> str:
    path = systemd_unit_path()
    _systemctl("disable", "--now", UNIT_NAME)
    if path.exists():
        path.unlink()
        _systemctl("daemon-reload")
        return f"removed {path}"
    return f"nothing to remove ({path} does not exist)"


def _status_systemd() -> ServiceStatus:
    path = systemd_unit_path()
    installed = path.exists()
    running = False
    detail = ""
    result = _systemctl("is-active", UNIT_NAME)
    if result is None:
        detail = "systemctl is not available"
    else:
        running = result.stdout.strip() == "active"
        detail = result.stdout.strip() or result.stderr.strip()
    return ServiceStatus("systemd --user", str(path), installed, running, detail)


# ---------------------------------------------------------------------------
# macOS — launchd LaunchAgent
# ---------------------------------------------------------------------------


def launchd_plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"


def launchd_plist_bytes(home: Path | str | None = None) -> bytes:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": daemon_command(home),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


def _install_launchd(home: Path | str | None) -> str:
    path = launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launchd_plist_bytes(home))
    if not shutil.which("launchctl"):
        return f"wrote {path}; launchctl is not available, so nothing was started"
    _run(["launchctl", "unload", str(path)])
    loaded = _run(["launchctl", "load", "-w", str(path)])
    if loaded.returncode != 0:
        raise ServiceError(
            f"wrote {path} but `launchctl load -w {path}` failed:\n"
            f"{(loaded.stderr or loaded.stdout).strip()}"
        )
    return f"installed and loaded {LAUNCHD_LABEL} ({path})"


def _uninstall_launchd() -> str:
    path = launchd_plist_path()
    if shutil.which("launchctl"):
        _run(["launchctl", "unload", "-w", str(path)])
    if path.exists():
        path.unlink()
        return f"removed {path}"
    return f"nothing to remove ({path} does not exist)"


def _status_launchd() -> ServiceStatus:
    path = launchd_plist_path()
    running = False
    detail = ""
    if shutil.which("launchctl"):
        result = _run(["launchctl", "list", LAUNCHD_LABEL])
        running = result.returncode == 0
    else:
        detail = "launchctl is not available"
    return ServiceStatus("launchd", str(path), path.exists(), running, detail)


# ---------------------------------------------------------------------------
# Windows — logon Scheduled Task
# ---------------------------------------------------------------------------


def _schtasks(*args: str) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which("schtasks"):
        return None
    return _run(["schtasks", *args])


def _install_schtasks(home: Path | str | None) -> str:
    command = _quote(daemon_command(home))
    result = _schtasks("/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", command)
    if result is None:
        raise ServiceError("schtasks is not available; Windows service install needs it")
    if result.returncode != 0:
        raise ServiceError(
            f"schtasks could not create {TASK_NAME}:\n{(result.stderr or result.stdout).strip()}"
        )
    return f"installed the logon task {TASK_NAME} ({command})"


def _uninstall_schtasks() -> str:
    result = _schtasks("/Delete", "/F", "/TN", TASK_NAME)
    if result is None:
        raise ServiceError("schtasks is not available")
    if result.returncode != 0:
        return f"nothing to remove ({TASK_NAME} is not registered)"
    return f"removed the logon task {TASK_NAME}"


def _status_schtasks() -> ServiceStatus:
    result = _schtasks("/Query", "/TN", TASK_NAME)
    if result is None:
        return ServiceStatus("schtasks", TASK_NAME, False, False, "schtasks is not available")
    installed = result.returncode == 0
    running = installed and "Running" in result.stdout
    return ServiceStatus("schtasks", TASK_NAME, installed, running)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _system() -> str:
    return platform.system()


def install(home: Path | str | None = None) -> str:
    system = _system()
    if system == "Linux":
        return _install_systemd(home)
    if system == "Darwin":
        return _install_launchd(home)
    if system == "Windows":
        return _install_schtasks(home)
    raise ServiceError(f"no service manager is known for {system}")


def uninstall() -> str:
    system = _system()
    if system == "Linux":
        return _uninstall_systemd()
    if system == "Darwin":
        return _uninstall_launchd()
    if system == "Windows":
        return _uninstall_schtasks()
    raise ServiceError(f"no service manager is known for {system}")


def status() -> ServiceStatus:
    system = _system()
    if system == "Linux":
        return _status_systemd()
    if system == "Darwin":
        return _status_launchd()
    if system == "Windows":
        return _status_schtasks()
    return ServiceStatus(system, "-", False, False, "no service manager is known")


def service_command(action: str, home: Path | str | None = None) -> int:
    """``snowpea service <action>``; returns the process exit code."""
    if action not in ACTIONS:
        print("usage: snowpea service install|uninstall|status", file=sys.stderr)
        return EXIT_USAGE
    try:
        if action == "status":
            print(status().render())
            return EXIT_OK
        print(install(home) if action == "install" else uninstall())
    except ServiceError as exc:
        print(f"snowpea service {action}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"snowpea service {action}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


__all__ = [
    "ACTIONS",
    "LAUNCHD_LABEL",
    "TASK_NAME",
    "UNIT_NAME",
    "ServiceError",
    "ServiceStatus",
    "daemon_command",
    "install",
    "launchd_plist_bytes",
    "launchd_plist_path",
    "service_command",
    "status",
    "systemd_unit_path",
    "systemd_unit_text",
    "uninstall",
]
