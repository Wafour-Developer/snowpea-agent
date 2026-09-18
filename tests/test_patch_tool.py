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
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bye\n"
