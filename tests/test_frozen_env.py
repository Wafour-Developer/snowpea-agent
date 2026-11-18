"""A frozen core's children start without its PyInstaller state.

On a mac arm64 frozen core (0.2.91) the snowpea-browser guard hook —
``${SNOWPEA_PYTHON} hooks/browser_guard.py``, i.e. ``snowpea-core --run-hook``
— died with SIGSEGV (exit -11) when the daemon spawned it, and ran fine by
hand: the child inherited the parent's ``_PYI_*``/``_MEIPASS2`` and library
paths into the bundle.  Children now get :func:`frozen_child_env`, and the
``--run-hook`` entry no longer imports the daemon.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.exec import local
from snowpea_core.skills import hooks
from snowpea_core.util.frozen_env import RESET_VAR, frozen_child_env

CORE = Path(__file__).resolve().parents[1] / "core"


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to be a frozen onedir core whose bundle is ``tmp_path/_internal``."""
    root = tmp_path / "_internal"
    root.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    return root


def test_frozen_scrubs_pyinstaller_variables_and_bundle_paths(bundle: Path) -> None:
    sep = os.pathsep
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "_MEIPASS2": str(bundle),
        "_PYI_APPLICATION_HOME_DIR": str(bundle),
        "_PYI_ARCHIVE_FILE": str(bundle / "base_library.zip"),
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "DYLD_LIBRARY_PATH": str(bundle),
        "DYLD_FRAMEWORK_PATH": f"{bundle / 'fw'}{sep}/opt/user/fw",
        "PYTHONHOME": str(bundle),
        "PYTHONPATH": f"{bundle / 'lib'}{sep}/home/me/pylib",
        "LD_LIBRARY_PATH": f"{bundle}{sep}/opt/user/lib",
    }
    env = frozen_child_env(base)
    assert not [name for name in env if name.startswith("_PYI_") or name == "_MEIPASS2"]
    assert "DYLD_LIBRARY_PATH" not in env
    assert env["DYLD_FRAMEWORK_PATH"] == "/opt/user/fw"
    assert "PYTHONHOME" not in env
    assert env["PYTHONPATH"] == "/home/me/pylib"
    assert env["LD_LIBRARY_PATH"] == "/opt/user/lib"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/me"
    assert env[RESET_VAR] == "1"
    assert base["_PYI_ARCHIVE_FILE"]  # the caller's mapping is not changed


def test_frozen_restores_ld_library_path_from_orig(bundle: Path) -> None:
    env = frozen_child_env(
        {"LD_LIBRARY_PATH": f"{bundle}{os.pathsep}/opt/a", "LD_LIBRARY_PATH_ORIG": "/opt/a:/opt/b"}
    )
    assert env["LD_LIBRARY_PATH"] == "/opt/a:/opt/b"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_frozen_keeps_values_the_user_set(bundle: Path) -> None:
    base = {
        "PYTHONHOME": "/opt/python3.12",
        "PYTHONPATH": "/home/me/pylib",
        "LD_LIBRARY_PATH": "/opt/cuda/lib64",
        "DYLD_LIBRARY_PATH": "/opt/homebrew/lib",
    }
    env = frozen_child_env(base)
    for name, value in base.items():
        assert env[name] == value


def test_frozen_without_meipass_still_drops_pyinstaller_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    env = frozen_child_env({"_PYI_ARCHIVE_FILE": "x", "LD_LIBRARY_PATH": "/opt/a"})
    assert env == {"LD_LIBRARY_PATH": "/opt/a", RESET_VAR: "1"}


def test_not_frozen_passes_the_environment_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    base = {"_PYI_ARCHIVE_FILE": "x", "_MEIPASS2": "y", "LD_LIBRARY_PATH": "/z", "PYTHONPATH": "/p"}
    assert frozen_child_env(base) == base
    monkeypatch.setenv("SNOWPEA_FROZEN_ENV_PROBE", "1")
    assert frozen_child_env() == dict(os.environ)


def test_the_tool_child_env_is_scrubbed_too(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("LD_LIBRARY_PATH", str(bundle))
    env = local.child_env({"PYTHONUNBUFFERED": "1"})
    assert "_PYI_PARENT_PROCESS_LEVEL" not in env and "LD_LIBRARY_PATH" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


class _Done:
    returncode = 0

    async def communicate(self, _body: bytes) -> tuple[bytes, bytes]:
        return b"", b""


async def test_the_hook_spawner_passes_the_scrubbed_env(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def create(command: str, **kwargs: Any) -> _Done:
        seen["command"], seen["env"] = command, kwargs["env"]
        return _Done()

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", create)
    monkeypatch.setattr(sys, "executable", "/Applications/Snowpea.app/core/snowpea-core")
    monkeypatch.setenv("_MEIPASS2", str(bundle))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(bundle))
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", str(bundle))
    monkeypatch.setenv("PYTHONPATH", str(bundle))
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="${SNOWPEA_PYTHON} hooks/g.py")
    code, _out, _err = await hooks.run_hook_output(hook, {"tool_name": "shell"}, home=tmp_path)
    assert code == 0
    assert seen["command"].endswith("snowpea-core --run-hook hooks/g.py")
    env = seen["env"]
    assert not [n for n in env if n.startswith("_PYI_") or n == "_MEIPASS2"]
    assert "DYLD_LIBRARY_PATH" not in env and "PYTHONPATH" not in env
    assert env[RESET_VAR] == "1"
    assert env["SNOWPEA_TOOL_NAME"] == "shell"
    assert env["SNOWPEA_PYTHON"].endswith("--run-hook")


@pytest.mark.parametrize(
    "entry",
    [
        # The PyInstaller entry script build-core.sh writes.
        "import sys; from snowpea_core.__main__ import main; sys.exit(main())",
        # ``python -m snowpea_core``.
        "import runpy; runpy.run_module('snowpea_core', run_name='__main__')",
    ],
)
def test_run_hook_dispatches_before_the_daemon_is_imported(tmp_path: Path, entry: str) -> None:
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "loaded = [m for m in ('snowpea_core.server.app_server', 'snowpea_core.server.protocol')"
        " if m in sys.modules]\n"
        "print(','.join(loaded) or 'clean')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(CORE)}
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", entry, "--run-hook", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.returncode == 3, done.stderr
    assert done.stdout.strip() == "clean"
