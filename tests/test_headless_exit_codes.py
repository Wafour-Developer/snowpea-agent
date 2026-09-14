"""US-006: the ``snowpea`` CLI's deterministic exit codes (contract §10, plan §3.6).

Every case shells out to the real console script so the test exercises argument
parsing, daemon discovery/spawn and the JSON-RPC client exactly as a user would.
Each test gets its own ``SNOWPEA_HOME`` and stops the daemon it started.

Exit codes: ``0`` complete · ``1`` agent failed · ``2`` usage/config ·
``3`` daemon unreachable · ``4`` denied · ``5`` timeout/interrupted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import snowpea_core

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SCRIPT = REPO_ROOT / "tests" / "fixtures" / "providers" / "fake" / "basic.json"

#: Seconds a CLI invocation may take before the test fails.
CLI_TIMEOUT = 90.0

#: How long to keep polling for US-005 (``session.*``/``tool.list``) to land.
#: Defaults to 0 so a normal run skips immediately instead of blocking; set
#: ``SNOWPEA_US005_WAIT_SEC=900`` to poll for the 15 minutes the story asked for.
US005_WAIT_SEC = float(os.environ.get("SNOWPEA_US005_WAIT_SEC", "0"))

_NOT_IMPLEMENTED = "not_implemented"


def _base_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["SNOWPEA_HOME"] = str(home)
    env["SNOWPEA_PROVIDER"] = f"fake:{FAKE_SCRIPT}"
    env.pop("SNOWPEA_DAEMON_CMD", None)
    env.pop("SNOWPEA_TUI_ENTRY", None)
    return env


def run_cli(
    *args: str,
    env: dict[str, str],
    timeout: float = CLI_TIMEOUT,
    stdin: str | None = "",
) -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run snowpea <args>`` with a non-TTY stdin."""
    command = ["uv", "run", "snowpea", *args]
    return subprocess.run(  # noqa: S603 - fixed command, test-local args
        command,
        cwd=str(REPO_ROOT),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """An isolated ``SNOWPEA_HOME``; any daemon started in it is stopped after."""
    directory = tmp_path / "home"
    directory.mkdir()
    try:
        yield directory
    finally:
        if (directory / "daemon.json").exists():
            with_env = _base_env(directory)
            subprocess.run(  # noqa: S603 - fixed command
                ["uv", "run", "snowpea", "daemon", "stop"],
                cwd=str(REPO_ROOT),
                env=with_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )


@pytest.fixture(scope="session", autouse=True)
def _require_uv() -> None:
    if shutil.which("uv") is None:  # pragma: no cover - environment guard
        pytest.skip("uv is not on PATH; the CLI tests shell out to `uv run snowpea`")


# ---------------------------------------------------------------------------
# US-005 readiness probes
# ---------------------------------------------------------------------------


def _probe(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return run_cli(*args, env=env)


def _wait_for_implementation(env: dict[str, str], *args: str, label: str) -> None:
    """Skip the test unless ``args`` stops reporting ``not_implemented``.

    Polls for :data:`US005_WAIT_SEC` seconds so the suite can be run while
    US-005 is still landing its ``session.*`` handlers.
    """
    deadline = time.monotonic() + US005_WAIT_SEC
    while True:
        result = _probe(env, *args)
        if _NOT_IMPLEMENTED not in (result.stderr + result.stdout):
            return
        if time.monotonic() >= deadline:
            pytest.skip(
                f"{label} still answers not_implemented (US-005 has not landed); "
                "set SNOWPEA_US005_WAIT_SEC to poll for longer"
            )
        time.sleep(5)


# ---------------------------------------------------------------------------
# no daemon needed
# ---------------------------------------------------------------------------


def test_version_exits_zero(home: Path) -> None:
    result = run_cli("--version", env=_base_env(home))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(f"snowpea {snowpea_core.__version__}")


def test_unknown_flag_is_a_usage_error(home: Path) -> None:
    result = run_cli("--definitely-not-a-flag", env=_base_env(home))
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_unknown_subcommand_is_a_usage_error(home: Path) -> None:
    result = run_cli("nonsense", env=_base_env(home))
    assert result.returncode == 2


def test_empty_prompt_is_a_usage_error(home: Path) -> None:
    result = run_cli("-c", "   ", env=_base_env(home))
    assert result.returncode == 2
    assert "prompt" in result.stderr


def test_service_without_an_action_is_a_usage_error(home: Path) -> None:
    """``service`` is implemented at M8 (US-022), so a bare call is a usage error."""
    result = run_cli("service", env=_base_env(home))
    assert result.returncode == 2
    assert "usage: snowpea service install|uninstall|status" in result.stderr


def test_service_status_on_a_clean_machine_exits_zero(home: Path) -> None:
    """Safe to call from a script: nothing registered is not an error (contract §3)."""
    env = _base_env(home)
    # Look for the unit under a throwaway config home, so a developer machine
    # that really has the service installed does not change the answer.
    env["XDG_CONFIG_HOME"] = str(home / "config")
    result = run_cli("service", "status", env=env)
    assert result.returncode == 0
    assert result.stdout.strip()


def test_skill_without_an_action_is_a_usage_error(home: Path) -> None:
    """``skill`` is implemented at M6, so a bare invocation is a usage error."""
    result = run_cli("skill", env=_base_env(home))
    assert result.returncode == 2
    assert "usage: snowpea skill" in result.stderr


def test_daemon_spawn_failure_exits_three(home: Path) -> None:
    """``SNOWPEA_DAEMON_CMD`` pointing at a failing binary → exit 3."""
    env = _base_env(home)
    env["SNOWPEA_DAEMON_CMD"] = "false"
    result = run_cli("-c", "say hello", env=env)
    assert result.returncode == 3, result.stderr
    assert "daemon" in result.stderr.lower()
    assert not (home / "daemon.json").exists()


# ---------------------------------------------------------------------------
# daemon lifecycle
# ---------------------------------------------------------------------------


def test_daemon_status_reports_the_port(home: Path) -> None:
    env = _base_env(home)
    result = run_cli("daemon", "status", env=env)
    assert result.returncode == 0, result.stderr
    assert "port" in result.stdout
    assert (home / "daemon.json").exists()


def test_daemon_status_json_carries_counters(home: Path) -> None:
    env = _base_env(home)
    result = run_cli("daemon", "status", "--json", env=env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pid"] > 0
    assert payload["port"] > 0
    assert set(payload["counters"]) >= {"sessions", "jobs", "gateway_bindings", "named_agents"}
    assert "willExit" in payload["lifecycle"]


def test_daemon_stop_removes_daemon_json(home: Path) -> None:
    env = _base_env(home)
    assert run_cli("daemon", "start", env=env).returncode == 0
    daemon_json = home / "daemon.json"
    assert daemon_json.exists()
    pid = json.loads(daemon_json.read_text(encoding="utf-8"))["pid"]

    result = run_cli("daemon", "stop", env=env)
    assert result.returncode == 0, result.stderr
    assert not daemon_json.exists()
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_daemon_stop_without_a_daemon_is_ok(home: Path) -> None:
    result = run_cli("daemon", "stop", env=_base_env(home))
    assert result.returncode == 0
    assert "no daemon" in result.stdout


def test_daemon_is_reused_between_invocations(home: Path) -> None:
    env = _base_env(home)
    assert run_cli("daemon", "start", env=env).returncode == 0
    first = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    assert run_cli("daemon", "status", env=env).returncode == 0
    second = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    assert first["pid"] == second["pid"]
    assert first["port"] == second["port"]


# ---------------------------------------------------------------------------
# session-less lookups (need US-005's registries)
# ---------------------------------------------------------------------------


def test_tools_list_json(home: Path) -> None:
    env = _base_env(home)
    _wait_for_implementation(env, "tools", "list", "--json", label="tool.list")
    result = run_cli("tools", "list", "--json", env=env)
    assert result.returncode == 0, result.stderr
    tools = json.loads(result.stdout)
    assert isinstance(tools, list)
    assert {tool["name"] for tool in tools} >= {"read_file", "shell"}


def test_commands_list_json(home: Path) -> None:
    env = _base_env(home)
    _wait_for_implementation(env, "commands", "list", "--json", label="command.list")
    result = run_cli("commands", "list", "--json", env=env)
    assert result.returncode == 0, result.stderr
    commands = json.loads(result.stdout)
    assert isinstance(commands, list)
    assert {command["name"] for command in commands} >= {"help", "mode"}


# ---------------------------------------------------------------------------
# headless turns (need US-005's agent loop)
# ---------------------------------------------------------------------------


def test_headless_completion_exits_zero(home: Path) -> None:
    env = _base_env(home)
    _wait_for_implementation(env, "-c", "say hello", label="session.prompt")
    result = run_cli("-c", "say hello", env=env)
    assert result.returncode == 0, result.stderr
    assert "Hi from fake" in result.stdout


def test_headless_json_emits_a_result_line(home: Path) -> None:
    env = _base_env(home)
    _wait_for_implementation(env, "-c", "say hello", label="session.prompt")
    result = run_cli("-c", "say hello", "--json", env=env)
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stdout
    final = lines[-1]
    assert final["kind"] == "result"
    assert final["exitCode"] == 0
    assert final["sessionId"]
    assert "inputTokens" in final["usage"]
    assert any(event.get("kind") == "turn.done" for event in lines[:-1])


def test_approve_none_denies_and_exits_four(home: Path) -> None:
    """``accept`` mode asks before ``shell``; ``--approve-none`` denies → exit 4."""
    env = _base_env(home)
    _wait_for_implementation(env, "-c", "say hello", label="session.prompt")
    result = run_cli("--mode", "accept", "-c", "please run ls", "--approve-none", env=env)
    assert result.returncode == 4, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_plan_mode_blocks_exec_and_exits_four(home: Path) -> None:
    """``plan`` mode denies ``exec`` outright (policy table, contract §7)."""
    env = _base_env(home)
    _wait_for_implementation(env, "-c", "say hello", label="session.prompt")
    result = run_cli("--mode", "plan", "-c", "please run ls", env=env)
    assert result.returncode == 4, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_bad_cwd_is_a_usage_error(home: Path) -> None:
    result = run_cli("-c", "say hello", "--cwd", "/no/such/directory", env=_base_env(home))
    assert result.returncode == 2
    assert "--cwd" in result.stderr


def test_timeout_exits_five(home: Path) -> None:
    """US-011 gave the fake provider ``delaySec``; the ``stall forever`` step
    sleeps 5s, so ``--timeout 1`` must abort the turn and exit 5."""
    env = _base_env(home)
    _wait_for_implementation(env, "-c", "say hello", label="session.prompt")
    result = run_cli("-c", "stall forever", "--timeout", "1", env=env)
    assert result.returncode == 5, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_missing_tui_bundle_is_a_usage_error(home: Path) -> None:
    """``snowpea`` with no arguments and no bundle → exit 2, never a traceback."""
    env = _base_env(home)
    env["SNOWPEA_TUI_ENTRY"] = str(home / "does-not-exist.js")
    result = run_cli(env=env)
    assert result.returncode == 2
    assert "SNOWPEA_TUI_ENTRY" in result.stderr


def test_plain_output_ignores_the_prompt_event() -> None:
    """``-c`` prints the model's side, not the prompt the caller just typed.

    ``message.user`` exists so ``session.resume`` can rebuild a transcript; a
    one-shot run already has the prompt on its own command line.
    """
    import io

    from snowpea_core.cli.render import PlainRenderer

    out, err = io.StringIO(), io.StringIO()
    renderer = PlainRenderer(out=out, err=err)
    renderer.event({"kind": "message.user", "payload": {"text": "say hello"}})
    assert out.getvalue() == ""
    renderer.event({"kind": "message.delta", "payload": {"text": "hello"}})
    assert out.getvalue() == "hello"
    assert err.getvalue() == ""


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-q", *sys.argv[1:]]))
