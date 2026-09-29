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
