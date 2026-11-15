"""Tests for the hello extension; run with `python -m pytest tests` in its folder.

Needs pytest and pytest-asyncio next to snowpea in the same Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.extensions.testing import load_extension

EXTENSION = Path(__file__).resolve().parents[1]  # the folder holding plugin.py


@pytest.mark.asyncio
async def test_greets_by_name(tmp_path: Path) -> None:
    # A private daemon in tmp_path; the extension is loaded as the real one would be.
    async with load_extension(EXTENSION, home=tmp_path) as ext:
        assert ext.registrations == ["tool:hello_greet"]
        result = await ext.call_tool("hello_greet", {"name": "Ada"})
        assert result.ok and result.output == "Hello, Ada!"
        too_long = await ext.call_tool("hello_greet", {"name": "x" * 101})
        assert not too_long.ok and "longer than 100" in too_long.error
    # Leaving the block unloads it and fails the test if anything stayed registered.
