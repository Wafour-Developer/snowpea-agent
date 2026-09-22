"""Tests for the Hermes-compatible patch tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.session.session import Session
from snowpea_core.tools import file_state, fs
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


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


async def test_patch_replace_mode_edits_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    settings = Settings()
    core = type("Core", (), {"settings": settings})()
    session = Session(id="s-1", workdir=tmp_path)
    ctx = ToolContext(session=session, core=core, backend=_Backend(tmp_path))  # type: ignore[arg-type]

    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True
    result = await fs.patch(
        ctx,
        {"path": "a.txt", "old_string": "hello", "new_string": "bye"},
    )
    assert result.ok is True
    assert result.output == "replaced 1 occurrence(s) in a.txt"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bye\n"


async def test_fuzzy_patch_reports_strategy(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("  hello\n  world\n", encoding="utf-8")
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-2", workdir=tmp_path)
    ctx = ToolContext(session=session, core=core, backend=_Backend(tmp_path))  # type: ignore[arg-type]
    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True

    result = await fs.patch(
        ctx, {"path": "a.txt", "old_string": "hello\nworld", "new_string": "bye"}
    )

    assert result.ok is True
    assert result.output == (
        "matched approximately (strategy: line_trimmed); replaced 1 occurrence — "
        "re-read the region if the change looks wrong"
    )
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "  bye\n"


async def test_oversized_fuzzy_patch_is_refused_without_writing(tmp_path: Path) -> None:
    before = "start\na\nb\nc\nd\nend\n"
    (tmp_path / "a.txt").write_text(before, encoding="utf-8")
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-3", workdir=tmp_path)
    ctx = ToolContext(session=session, core=core, backend=_Backend(tmp_path))  # type: ignore[arg-type]
    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True

    result = await fs.patch(
        ctx,
        {
            "path": "a.txt",
            "old_string": r"start\na\nb\nc\nd\nend",
            "new_string": "replacement",
        },
    )

    assert result.ok is False
    assert result.error == (
        "old_string matched approximately but the match spans 6 lines where old_string has 1; "
        "re-read the file and pass the exact current text"
    )
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == before
