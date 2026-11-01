"""A shell command that writes a file is an edit (1.35.0)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from snowpea_core.agent import shell_writes, verify_gate
from snowpea_core.tools.registry import ToolResult


def test_a_file_the_command_names_and_changed_is_reported(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    new = tmp_path / "src" / "app.py"
    new.write_text("x = 1\n")
    old = tmp_path / "old.py"
    old.write_text("y = 2\n")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    started = time.time() - 0.2

    command = (
        "cat > src/app.py <<'EOF'\nx = 1\nEOF\ncat old.py; "
        f"sed -i s/a/b/ {tmp_path}/src/app.py"
    )
    assert shell_writes.changed_files(command, tmp_path, started) == ["src/app.py"]
    # Outside the workdir, missing, or untouched files are not edits.
    assert shell_writes.changed_files("cat /etc/hostname ../x.py nope.py", tmp_path, started) == []


def test_node_modules_and_git_are_never_reported(tmp_path: Path) -> None:
    for name in ("node_modules/pkg/index.js", ".git/config"):
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text("x")
    assert shell_writes.changed_files(
        "npm install; cat node_modules/pkg/index.js .git/config", tmp_path, time.time() - 5
    ) == []


def test_a_shell_edit_arms_the_verify_gate(tmp_path: Path) -> None:
    session = SimpleNamespace(workdir=str(tmp_path))
    verify_gate.begin_turn(session, "fix it")
    result = ToolResult(ok=True, output="", meta={"wrote": ["src/app.py"]})
    verify_gate.observe(session, "shell", {"command": "cat > src/app.py"}, result)
    assert "src/app.py" in session.verify_turn.code_edits

    plain = ToolResult(ok=True, output="")
    verify_gate.begin_turn(session, "look")
    verify_gate.observe(session, "shell", {"command": "ls"}, plain)
    assert not session.verify_turn.code_edits
