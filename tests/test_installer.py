"""US-022: the installer scripts, the TUI bundle resolution and `snowpea service`.

Static checks on the shell installers (they have no unit-testable seams, so
what is testable is that they parse, that `--dry-run` changes nothing, and that
shellcheck is happy where it is available), plus real unit tests for the two
pieces of Python M8 adds: `cli/main.resolve_tui_command` and `cli/service`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from snowpea_core.cli import service
from snowpea_core.cli.main import (
    TUI_BUNDLE,
    TuiNotFound,
    packaged_tui_bundle,
    repo_tui_bundle,
    resolve_tui_command,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "installer" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "installer" / "install.ps1"
SMOKE_SH = REPO_ROOT / "tests" / "e2e" / "v01_smoke.sh"
NPM_BIN = REPO_ROOT / "installer" / "npm" / "bin" / "snowpea.js"

#: The install URL of record for v0.1 (AC-01, plan §7.9 step 1).
INSTALL_URL = (
    "https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh"
)


def _run(*argv: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, test-local
        list(argv), capture_output=True, text=True, check=False, **kwargs
    )


# ---------------------------------------------------------------------------
# shell scripts: they exist, they parse, they are executable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [INSTALL_SH, SMOKE_SH], ids=["install.sh", "v01_smoke.sh"])
def test_shell_scripts_parse(script: Path) -> None:
    """``bash -n``: a broken installer must not reach a release."""
    assert script.is_file(), f"{script} is missing"
    assert os.access(script, os.X_OK), f"{script} is not executable"
    result = _run("bash", "-n", str(script))
    assert result.returncode == 0, result.stderr


def test_install_sh_is_posix_sh_compatible() -> None:
    """The published one-liner pipes into ``sh``, not bash."""
    result = _run("sh", "-n", str(INSTALL_SH))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [INSTALL_SH, SMOKE_SH], ids=["install.sh", "v01_smoke.sh"])
def test_shellcheck_is_clean(script: Path) -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    result = _run("shellcheck", "--severity=warning", str(script))
    assert result.returncode == 0, result.stdout or result.stderr


def test_install_sh_defaults_to_the_git_source_until_pypi() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'DEFAULT_SOURCE="git+${REPO_URL}"' in text
    assert "SNOWPEA_INSTALL_SOURCE" in text
    assert "SNOWPEA_WHEEL_URL" in text


def test_the_install_url_of_record_is_the_one_in_the_plan() -> None:
    """AC-01: the raw GitHub URL is the v0.1 install path of record."""
    assert INSTALL_URL in SMOKE_SH.read_text(encoding="utf-8")
    assert INSTALL_URL in INSTALL_SH.read_text(encoding="utf-8")


def test_install_ps1_uses_winget_and_the_windows_home() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "astral-sh.uv" in text
    assert "OpenJS.NodeJS.LTS" in text
    # Contract §2: Windows state lives under %LOCALAPPDATA%\snowpea.
    assert "LOCALAPPDATA" in text
    assert "SNOWPEA_HOME" in text


def test_npm_shim_runs_the_installer_then_execs_snowpea() -> None:
    assert NPM_BIN.is_file()
    text = NPM_BIN.read_text(encoding="utf-8")
    assert "install.sh" in text and "install.ps1" in text
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    result = _run("node", "--check", str(NPM_BIN))
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# --dry-run prints a plan and changes nothing
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_planned_steps_and_exits_zero(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["SNOWPEA_BIN_DIR"] = str(tmp_path / "bin")
    env["SNOWPEA_NODE_DIR"] = str(tmp_path / "node")
    (tmp_path / "home").mkdir()

    result = _run("bash", str(INSTALL_SH), "--dry-run", env=env)

    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    assert "uv tool install" in result.stdout
    assert "snowpea --version" in result.stdout
    # Nothing was created: no bin directory, no node directory, no rc file.
    assert not (tmp_path / "bin").exists()
    assert not (tmp_path / "node").exists()
    assert list((tmp_path / "home").iterdir()) == []


def test_dry_run_from_checkout_plans_the_local_install(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    result = _run("bash", str(INSTALL_SH), "--dry-run", "--from-checkout", env=env)
    assert result.returncode == 0, result.stderr
    assert str(REPO_ROOT) in result.stdout


def test_an_unknown_flag_fails_with_a_pointer_to_help(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    result = _run("bash", str(INSTALL_SH), "--nope", env=env)
    assert result.returncode != 0
    assert "--help" in result.stderr


# ---------------------------------------------------------------------------
# TUI bundle resolution order (contract §1)
# ---------------------------------------------------------------------------


def _make_bundle(root: Path) -> Path:
    bundle = root / "tui" / "dist" / TUI_BUNDLE
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("// bundle\n", encoding="utf-8")
    return bundle


def test_tui_entry_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_bundle(tmp_path / "packaged")
    override = tmp_path / "custom.js"
    override.write_text("// custom\n", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_TUI_ENTRY", str(override))

    command = resolve_tui_command(package_root=tmp_path / "packaged", repo_root=tmp_path / "repo")

    assert command == ["node", str(override)]


def test_a_tsx_override_runs_through_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "index.tsx"
    override.write_text("// dev entry\n", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_TUI_ENTRY", str(override))

    assert resolve_tui_command() == ["npx", "tsx", str(override)]


def test_a_missing_override_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOWPEA_TUI_ENTRY", str(tmp_path / "gone.js"))
    with pytest.raises(TuiNotFound):
        resolve_tui_command()


def test_the_packaged_bundle_beats_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SNOWPEA_TUI_ENTRY", raising=False)
    packaged = _make_bundle(tmp_path / "packaged")
    repo = _make_bundle(tmp_path / "repo")

    command = resolve_tui_command(package_root=tmp_path / "packaged", repo_root=tmp_path / "repo")

    assert command == ["node", str(packaged)]
    assert command != ["node", str(repo)]


def test_the_checkout_is_the_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOWPEA_TUI_ENTRY", raising=False)
    repo = _make_bundle(tmp_path / "repo")

    command = resolve_tui_command(package_root=tmp_path / "empty", repo_root=tmp_path / "repo")

    assert command == ["node", str(repo)]


def test_no_bundle_anywhere_names_the_build_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SNOWPEA_TUI_ENTRY", raising=False)
    with pytest.raises(TuiNotFound) as excinfo:
        resolve_tui_command(package_root=tmp_path / "a", repo_root=tmp_path / "b")
    assert "npm run build" in str(excinfo.value)


def test_the_default_roots_are_the_package_and_the_checkout() -> None:
    """The defaults are what the wheel and a dev checkout actually use."""
    assert packaged_tui_bundle().parts[-3:] == ("tui", "dist", TUI_BUNDLE)
    assert repo_tui_bundle() == REPO_ROOT / "tui" / "dist" / TUI_BUNDLE


# ---------------------------------------------------------------------------
# the build hook (contract §1)
# ---------------------------------------------------------------------------


def test_the_build_hook_is_wired_into_pyproject() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.hatch.build.hooks.custom]" in text
    assert 'path = "hatch_build.py"' in text
    # The version is single-sourced from the package, not typed twice.
    assert 'dynamic = ["version"]' in text
    assert 'path = "core/snowpea_core/__init__.py"' in text
    assert "core/snowpea_core/tui/dist/snowpea-tui.js" in text


def _load_build_hook_module() -> object:
    """Import ``hatch_build`` from the repo root (it is not on sys.path)."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import hatch_build

        return hatch_build
    finally:
        sys.path.pop(0)


def test_the_build_hook_refuses_to_build_without_a_bundle(tmp_path: Path) -> None:
    """A wheel with no bundle installs a `snowpea` that cannot start."""
    hatch_build = _load_build_hook_module()

    with pytest.raises(hatch_build.MissingBundle) as excinfo:  # type: ignore[attr-defined]
        hatch_build.copy_bundle(tmp_path, skip=False)  # type: ignore[attr-defined]

    assert "npm" in str(excinfo.value)


def test_skip_tui_builds_without_the_bundle(tmp_path: Path) -> None:
    hatch_build = _load_build_hook_module()
    assert hatch_build.copy_bundle(tmp_path, skip=True) is None  # type: ignore[attr-defined]


def test_the_build_hook_copies_the_bundle_into_the_package(tmp_path: Path) -> None:
    hatch_build = _load_build_hook_module()
    source = tmp_path / hatch_build.SOURCE_BUNDLE  # type: ignore[attr-defined]
    source.parent.mkdir(parents=True)
    source.write_text("// bundle\n", encoding="utf-8")
    source.with_name("snowpea-tui.js.map").write_text("{}", encoding="utf-8")

    target = hatch_build.copy_bundle(tmp_path, skip=False)  # type: ignore[attr-defined]

    assert target == tmp_path / hatch_build.TARGET_DIR / "snowpea-tui.js"  # type: ignore[attr-defined]
    assert target.read_text(encoding="utf-8") == "// bundle\n"
    assert (target.parent / "snowpea-tui.js.map").is_file()


def test_a_wheel_built_from_an_sdist_reuses_the_copy(tmp_path: Path) -> None:
    """`uv build` builds the wheel out of the sdist, where tui/ is gone."""
    hatch_build = _load_build_hook_module()
    target = tmp_path / hatch_build.TARGET_DIR / "snowpea-tui.js"  # type: ignore[attr-defined]
    target.parent.mkdir(parents=True)
    target.write_text("// from the sdist\n", encoding="utf-8")

    assert hatch_build.copy_bundle(tmp_path, skip=False) == target  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# snowpea service (contract §3)
# ---------------------------------------------------------------------------


def test_status_on_a_clean_machine_says_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe to call from a script: no service, no error."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "_system", lambda: "Linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    state = service.status()

    assert not state.installed
    assert "not installed" in state.render()


def test_service_status_exits_zero_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "_system", lambda: "Linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    code = service.service_command("status")

    assert code == 0
    assert "not installed" in capsys.readouterr().out


def test_an_unknown_action_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert service.service_command("restart") == 2
    assert "install|uninstall|status" in capsys.readouterr().err


def test_the_systemd_unit_starts_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "_system", lambda: "Linux")
    # No systemctl on this machine: the unit is still written, nothing started.
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    message = service.install(tmp_path / "home")

    unit = service.systemd_unit_path()
    assert unit.is_file()
    body = unit.read_text(encoding="utf-8")
    assert "ExecStart=" in body
    assert "--port 0" in body
    assert "WantedBy=default.target" in body
    assert str(unit) in message


def test_install_then_uninstall_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "_system", lambda: "Linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    service.install(tmp_path / "home")
    service.install(tmp_path / "home")  # writing it twice is fine
    assert service.status().installed

    first = service.uninstall()
    second = service.uninstall()

    assert "removed" in first
    assert "nothing to remove" in second
    assert not service.status().installed


def test_the_launchd_plist_is_a_valid_login_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib

    payload = plistlib.loads(service.launchd_plist_bytes(tmp_path / "home"))

    assert payload["Label"] == service.LAUNCHD_LABEL
    assert payload["RunAtLoad"] is True
    assert "--port" in payload["ProgramArguments"]


def test_the_daemon_command_carries_the_home_override(tmp_path: Path) -> None:
    command = service.daemon_command(tmp_path / "home")
    assert command[-4:-2] == ["--port", "0"]
    assert command[-2] == "--home"
    assert command[-1] == str((tmp_path / "home").resolve())
