"""``read_file(paths=…)`` and ``patch(edits=…)``: several files in one round.

A local model makes about one tool call per round (1.21 on qwen38), and a call
per file was what made its coding turns long next to Codex and OpenCode, which
read every file of a task in one shell command.  Pinned here: the batched forms
work, keep the read-before-write record per file, stop at the first failing
edit and say what was applied, and every guard judges the call by its most
sensitive path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.session.session import Session
from snowpea_core.tools import config_guard, file_state, fs
from snowpea_core.tools.call_paths import MAX_BATCH, call_paths
from snowpea_core.tools.registry import ToolContext


class _Backend:
    kind = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    async def read_file(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def resolve(self, path: str) -> str:
        return str(self.root / path)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


def _ctx(root: Path) -> ToolContext:
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-batch", workdir=root)
    return ToolContext(session=session, core=core, backend=_Backend(root))  # type: ignore[arg-type]


def _files(root: Path) -> None:
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "b.py").write_text("B = 2\n", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")


async def test_read_file_reads_several_files_in_one_call(tmp_path: Path) -> None:
    _files(tmp_path)
    ctx = _ctx(tmp_path)
    result = await fs.read_file(ctx, {"paths": ["a.py", "b.py", "missing.py"]})
    assert result.ok is True
    assert "==> a.py <==\nA = 1" in result.output
    assert "==> b.py <==\nB = 2" in result.output
    assert "==> missing.py <==\n[error] no such file: missing.py" in result.output
    # Each file counts as read for the read-before-write rule.
    patched = await fs.patch(ctx, {"path": "b.py", "old_string": "B = 2", "new_string": "B = 3"})
    assert patched.ok and "note:" not in patched.output


async def test_a_batch_of_only_missing_files_fails_and_the_cap_holds(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    nothing = await fs.read_file(ctx, {"paths": ["x.py", "y.py"]})
    assert nothing.ok is False and "x.py" in (nothing.error or "")
    too_many = await fs.read_file(ctx, {"paths": [f"f{i}.py" for i in range(MAX_BATCH + 1)]})
    assert too_many.ok is False and str(MAX_BATCH) in (too_many.error or "")


async def test_a_single_path_read_is_unchanged(tmp_path: Path) -> None:
    _files(tmp_path)
    result = await fs.read_file(_ctx(tmp_path), {"path": "a.py"})
    assert result.ok and result.output == "A = 1\n"


async def test_patch_applies_several_edits_across_files_in_order(tmp_path: Path) -> None:
    _files(tmp_path)
    ctx = _ctx(tmp_path)
    await fs.read_file(ctx, {"paths": ["a.py", "b.py"]})
    result = await fs.patch(
        ctx,
        {
            "path": "a.py",
            "edits": [
                {"old_string": "A = 1", "new_string": "A = 10"},
                {"old_string": "A = 10", "new_string": "A = 11"},
                {"path": "b.py", "old_string": "B = 2", "new_string": "B = 20"},
            ],
        },
    )
    assert result.ok is True and result.output.startswith("applied 3 edit(s)")
    assert (tmp_path / "a.py").read_text() == "A = 11\n"
    assert (tmp_path / "b.py").read_text() == "B = 20\n"
    assert result.diff and "a.py" in result.diff and "b.py" in result.diff


async def test_a_failing_edit_stops_the_batch_and_says_what_was_applied(tmp_path: Path) -> None:
    _files(tmp_path)
    ctx = _ctx(tmp_path)
    await fs.read_file(ctx, {"paths": ["a.py", "b.py"]})
    result = await fs.patch(
        ctx,
        {
            "edits": [
                {"path": "a.py", "old_string": "A = 1", "new_string": "A = 5"},
                {"path": "b.py", "old_string": "NOT THERE", "new_string": "x"},
                {"path": "b.py", "old_string": "B = 2", "new_string": "B = 9"},
            ]
        },
    )
    assert result.ok is False
    assert "edit 2 (b.py)" in (result.error or "") and "1. a.py" in (result.error or "")
    assert (tmp_path / "a.py").read_text() == "A = 5\n"
    assert (tmp_path / "b.py").read_text() == "B = 2\n"  # the third never ran


def test_call_paths_collects_every_path_once() -> None:
    args = {
        "path": "a.py",
        "paths": ["b.py", "a.py"],
        "edits": [{"old_string": "x"}, {"path": "c.md"}],
    }
    assert call_paths(args) == ["a.py", "b.py", "c.md"]
    assert call_paths({}) == []


def test_a_batched_call_is_judged_by_its_most_sensitive_path(tmp_path: Path) -> None:
    session = Session(id="s-guard", workdir=tmp_path)
    session.backend = _Backend(tmp_path)  # type: ignore[assignment]
    write = {"edits": [{"path": "a.py"}, {"path": ".snowpea/settings.json"}]}
    assert config_guard.permission_for_write(write, session) == "config"
    assert config_guard.permission_for_write({"edits": [{"path": "a.py"}]}, session) == "write"
    read = {"paths": ["a.py", ".env"]}
    assert config_guard.permission_for_read(read, session) == "secret"
    assert config_guard.permission_for_read({"paths": ["a.py", "b.py"]}, session) == "read"


def test_plan_mode_allows_a_batched_patch_only_when_every_file_is_a_document(
    tmp_path: Path,
) -> None:
    tool = next(t for t in fs.TOOLS if t.name == "patch")
    policy = PermissionPolicy()
    session = Session(id="s-plan", workdir=tmp_path, mode="plan")
    docs = {"edits": [{"path": "plan.md"}, {"path": "notes.md"}]}
    mixed = {"edits": [{"path": "plan.md"}, {"path": "a.py"}]}
    assert policy.decide("plan", "write", tool, docs, session) == "allow"
    assert policy.decide("plan", "write", tool, mixed, session) == "deny"
