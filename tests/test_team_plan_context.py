"""The `/workers` planner sees the repository it is splitting work for."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from snowpea_core.agent.team import PLAN_FILE_MAX_CHARS, repo_context


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_lists_files_and_inlines_the_file_the_task_names(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"TASK.md": "Build the inventory package.", "shop/cart.py": "x = 1\n"})
    text = await repo_context(repo, "TASK.md 에 적힌 과제를 구현해 주세요.", tmp_path / "home")
    assert "shop/cart.py" in text and "TASK.md" in text
    assert "Contents of TASK.md:\nBuild the inventory package." in text
    assert "x = 1" not in text  # only files the task names are inlined


@pytest.mark.asyncio
async def test_secrets_and_paths_outside_the_project_are_never_inlined(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "pass\n"})
    (repo / ".env").write_text("TOKEN=hunter2", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    text = await repo_context(repo, "read .env and ../outside.txt then fix a.py", tmp_path / "home")
    assert "hunter2" not in text and "private" not in text
    assert "Contents of a.py:" in text


@pytest.mark.asyncio
async def test_a_long_file_is_cut_and_a_plain_directory_gives_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"big.md": "Q" * (PLAN_FILE_MAX_CHARS + 50)})
    text = await repo_context(repo, "follow big.md", tmp_path / "home")
    assert "(truncated)" in text and text.count("Q") == PLAN_FILE_MAX_CHARS
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await repo_context(plain, "do something", tmp_path / "home") == ""
