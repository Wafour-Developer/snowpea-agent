"""The repository orientation block of the environment prompt (1.35.0)."""

from __future__ import annotations

import json
from pathlib import Path

from snowpea_core.prompts import environment as prompt_env
from snowpea_core.prompts.repo_orientation import orientation


def _project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export {}\n")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "app.test.ts").write_text("")
    (root / "pnpm-lock.yaml").write_text("")
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "lint": "eslint .", "typecheck": "tsc -b"}})
    )
    (root / "Makefile").write_text("build:\n\techo\ncheck:\n\techo\n")


def test_the_block_names_layout_scripts_and_check_commands(tmp_path: Path) -> None:
    _project(tmp_path)
    text = orientation(tmp_path)
    assert "src/ app.ts" in text and "tests/ app.test.ts" in text
    assert "node_modules" not in text
    assert "package.json (pnpm)" in text and "test: vitest run" in text
    assert "Makefile — build, check" in text
    assert "test `pnpm test` or `make check`" in text
    assert "lint `pnpm run lint`" in text and "typecheck `pnpm run typecheck`" in text


def test_a_directory_that_is_not_a_project_is_not_walked(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x")
    assert orientation(tmp_path) == ""
    assert orientation(tmp_path / "missing") == ""


def test_the_block_is_stable_and_clipped(tmp_path: Path) -> None:
    _project(tmp_path)
    for index in range(200):
        (tmp_path / "src" / f"file{index:03}.ts").write_text("")
    first = orientation(tmp_path)
    assert first == orientation(tmp_path)
    assert "(+" in first and len(first) < 3000


def test_it_rides_the_environment_block_only_when_asked(tmp_path: Path) -> None:
    _project(tmp_path)
    plain = prompt_env.collect(tmp_path, read_context=False, read_git=True)
    assert plain.orientation == ""
    oriented = prompt_env.collect(tmp_path, read_context=False, read_git=True, orient=True)
    assert "Repository (a snapshot" in prompt_env.build_environment_block(oriented)
