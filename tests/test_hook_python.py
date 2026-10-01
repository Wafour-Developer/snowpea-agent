"""Plugin hooks that need Python, and guard hooks that must fail closed (1.6.0).

A frozen core has no ``python3`` of its own, so ``${SNOWPEA_PYTHON}`` names the
core's interpreter (``snowpea-core --run-hook`` when frozen).  A PreToolUse
guard declared ``failClosed`` blocks the call when it cannot run, instead of
silently letting it through.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core import __main__ as core_main
from snowpea_core.skills import hooks


def _core(tmp_path: Path, registry: hooks.HookRegistry) -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(home=tmp_path),
        skills=SimpleNamespace(hooks=registry),
    )


def _session(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(id="s-hook", workdir=tmp_path)


def test_snowpea_python_expands_to_this_interpreter() -> None:
    assert hooks.expand("${SNOWPEA_PYTHON} hooks/x.py", None).startswith(sys.executable)


def test_a_frozen_core_runs_hooks_through_its_run_hook_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert hooks.expand("${SNOWPEA_PYTHON} x.py", None).endswith("--run-hook x.py")


def test_run_hook_entry_runs_a_script_and_keeps_its_exit_status(tmp_path: Path) -> None:
    script = tmp_path / "guard.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    argv = list(sys.argv)
    try:
        assert core_main.main(["--run-hook", str(script)]) == 2
    finally:
        sys.argv = argv


async def test_a_python_hook_blocks_through_snowpea_python(tmp_path: Path) -> None:
    script = tmp_path / "deny.py"
    script.write_text(
        "import sys\nprint('no payments', file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8"
    )
    registry = hooks.HookRegistry()
    registry.add(
        hooks.Hook(event="PreToolUse", matcher="*", command=f"${{SNOWPEA_PYTHON}} {script}")
    )
    message = await hooks.pre_tool_use(_core(tmp_path, registry), _session(tmp_path), "repl", {})
    assert message == "no payments"


async def test_a_guard_that_cannot_run_blocks_only_when_it_fails_closed(tmp_path: Path) -> None:
    missing = "definitely-not-a-command-snowpea-hook"
    open_registry = hooks.HookRegistry()
    open_registry.add(hooks.Hook(event="PreToolUse", matcher="*", command=missing))
    assert (
        await hooks.pre_tool_use(_core(tmp_path, open_registry), _session(tmp_path), "repl", {})
        is None
    )

    closed_registry = hooks.HookRegistry()
    closed_registry.add(
        hooks.Hook(event="PreToolUse", matcher="*", command=missing, fail_closed=True)
    )
    message = await hooks.pre_tool_use(
        _core(tmp_path, closed_registry), _session(tmp_path), "repl", {}
    )
    assert message is not None and "guard hook failed" in message


def test_fail_closed_is_read_from_hooks_json(tmp_path: Path) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        '{"hooks": {"PreToolUse": [{"matcher": "repl", "hooks": '
        '[{"type": "command", "command": "x", "failClosed": true}]}]}}',
        encoding="utf-8",
    )
    registry = hooks.HookRegistry()
    assert registry.load_file(config, plugin="browser") == 1
    assert registry.for_tool("PreToolUse", "repl")[0].fail_closed is True


# ---------------------------------------------------------------------------
# Stop hooks that keep the agent going (Claude Code's contract)
# ---------------------------------------------------------------------------


def _stop_registry(command: str) -> hooks.HookRegistry:
    registry = hooks.HookRegistry()
    registry.add(hooks.Hook(event="Stop", matcher="", command=command))
    return registry


async def test_a_stop_hook_blocks_with_exit_2_and_its_stderr(tmp_path: Path) -> None:
    script = tmp_path / "keep_going.py"
    seen = tmp_path / "payload.json"
    script.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())\n"
        "print('finish the M1 scaffold', file=sys.stderr)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    core = _core(tmp_path, _stop_registry(f"${{SNOWPEA_PYTHON}} {script}"))
    decision = await hooks.stop(
        core, _session(tmp_path), last_message="Next I'll build it.", active=False
    )
    assert decision.block and decision.reason == "finish the M1 scaffold"
    import json

    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == "Stop"
    assert payload["stop_hook_active"] is False
    assert payload["last_assistant_message"] == "Next I'll build it."
    # Who the session belongs to rides along, so a guard need not read state.db.
    for key in ("origin_surface", "host_tools_from", "agent", "parent_session_id"):
        assert key in payload


async def test_a_stop_hook_blocks_with_a_json_decision(tmp_path: Path) -> None:
    script = tmp_path / "json_block.py"
    script.write_text(
        "import json\nprint(json.dumps({'decision': 'block', 'reason': 'run the tests'}))\n",
        encoding="utf-8",
    )
    core = _core(tmp_path, _stop_registry(f"${{SNOWPEA_PYTHON}} {script}"))
    decision = await hooks.stop(core, _session(tmp_path))
    assert decision == hooks.StopDecision(block=True, reason="run the tests")


async def test_a_quiet_or_continue_false_stop_hook_lets_the_turn_end(tmp_path: Path) -> None:
    quiet = tmp_path / "quiet.py"
    quiet.write_text("pass\n", encoding="utf-8")
    stop_all = tmp_path / "stop_all.py"
    stop_all.write_text(
        "import json\nprint(json.dumps({'continue': False, 'decision': 'block'}))\n",
        encoding="utf-8",
    )
    for script in (quiet, stop_all):
        core = _core(tmp_path, _stop_registry(f"${{SNOWPEA_PYTHON}} {script}"))
        assert (await hooks.stop(core, _session(tmp_path))).block is False
