"""Verify-on-stop (agent/verify_gate.py), ported from Hermes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from snowpea_core.agent import verify_gate
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolResult


def _core(value: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop=value)))


def _session(tmp_path: Path) -> Session:
    session = Session(id="s-gate", workdir=tmp_path)
    verify_gate.begin_turn(session)
    return session


def _edit(session: Session, path: str) -> None:
    verify_gate.observe(session, "patch", {"path": path}, ToolResult(ok=True, path=path))


def test_an_edit_with_no_check_is_sent_back_twice_at_most(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "app/main.py")
    first = verify_gate.nudge(_core(), session)
    assert first and "app/main.py" in first and "No test" in first
    assert verify_gate.nudge(_core(), session)
    assert verify_gate.nudge(_core(), session) is None


def test_a_passing_check_after_the_edit_lets_the_turn_end(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "app/main.py")
    verify_gate.observe(session, "shell", {"command": "uv run pytest -q"}, ToolResult(ok=True))
    assert verify_gate.nudge(_core(), session) is None


def test_a_check_before_the_last_edit_is_stale_and_a_failing_one_counts_as_failed(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    verify_gate.observe(session, "shell", {"command": "npm test"}, ToolResult(ok=True))
    _edit(session, "src/a.ts")
    assert "stale" in (verify_gate.nudge(_core(), session) or "")
    verify_gate.observe(session, "shell", {"command": "npm run build"}, ToolResult(ok=False))
    assert "failed" in (verify_gate.nudge(_core(), session) or "")


def test_docs_only_and_plain_commands_do_not_count(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "docs/PLAN.md")
    assert verify_gate.nudge(_core(), session) is None
    assert not verify_gate.is_check_command("ls -la && cat README.md")
    assert verify_gate.is_check_command("cd web && npx vitest run")
    assert verify_gate.is_check_command("cargo test --all")


def test_a_ui_edit_also_needs_a_look_in_the_browser(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "frontend/src/App.vue")
    verify_gate.observe(session, "shell", {"command": "npm run build"}, ToolResult(ok=True))
    assert "browser" in (verify_gate.nudge(_core(), session) or "")
    verify_gate.observe(
        session, "browser_navigate", {"url": "http://127.0.0.1:8899"}, ToolResult(ok=True)
    )
    assert verify_gate.nudge(_core(), session), "navigating is not a look"
    verify_gate.observe(session, "browser_screenshot", {}, ToolResult(ok=True))
    assert verify_gate.nudge(_core(), session) is None


def test_a_verifier_pass_counts_and_the_setting_turns_it_off(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "frontend/src/App.vue")
    verify_gate.observe(
        session,
        "delegate_task",
        {"agent": "verifier"},
        ToolResult(ok=True, output="PASS: all good"),
    )
    assert verify_gate.nudge(_core(), session) is None
    other = _session(tmp_path)
    _edit(other, "app/main.py")
    assert verify_gate.nudge(_core("off"), other) is None
    other.origin_surface = "gateway:telegram:1"
    assert verify_gate.nudge(_core("auto"), other) is None


def test_open_todos_get_one_reminder(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.todos = [
        {"id": "1", "content": "build the world", "status": "completed"},
        {"id": "2", "content": "add missions", "status": "pending"},
    ]
    reminder = verify_gate.nudge(_core(), session)
    assert reminder and "add missions" in reminder and "build the world" not in reminder
    assert verify_gate.nudge(_core(), session) is None


def test_only_a_verifiers_clean_pass_counts(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _edit(session, "app/main.py")
    verify_gate.observe(
        session, "delegate_task", {"agent": "executor"}, ToolResult(ok=True, output="7/7 PASS")
    )
    assert verify_gate.nudge(_core(), session)
    verify_gate.observe(
        session,
        "delegate_task",
        {"agent": "verifier"},
        ToolResult(ok=True, output="API PASS; UI UNVERIFIED"),
    )
    assert verify_gate.nudge(_core(), session)


def test_scripts_of_a_web_project_are_ui(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<canvas></canvas>")
    (tmp_path / "tools").mkdir()
    session = _session(tmp_path)
    _edit(session, "web/game.js")
    _edit(session, "tools/build.ts")
    assert session.verify_turn.ui_edits.keys() == {"web/game.js"}
    assert session.verify_turn.code_edits.keys() == {"tools/build.ts"}


def test_a_ui_only_turn_is_proved_by_a_screenshot_and_node_counts_as_a_check(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    _edit(session, "index.html")
    verify_gate.observe(session, "browser_screenshot", {}, ToolResult(ok=True))
    assert verify_gate.nudge(_core(), session) is None
    assert verify_gate.is_check_command("node scripts/smoke.mjs")
    assert not verify_gate.is_check_command("node --version")
