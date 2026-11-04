"""An MCP result is capped once on arrival; the whole text stays on disk."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from snowpea_core.agent.loop import _spill_long_result
from snowpea_core.tools.registry import ToolResult


def _core(tmp_path: Path, limit: int | None = None) -> SimpleNamespace:
    tools = SimpleNamespace() if limit is None else SimpleNamespace(mcpResultMaxChars=limit)
    return SimpleNamespace(
        settings=SimpleNamespace(tools=tools), paths=SimpleNamespace(home=tmp_path)
    )


def test_a_big_mcp_result_is_cut_with_a_pointer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path))
    text = "x" * 90_000
    result = ToolResult(ok=True, output=text)
    out = _spill_long_result(_core(tmp_path), "mcp__studio__get_project", result)
    assert len(out.output) < 26_000
    assert "characters omitted" in out.output


def test_a_small_mcp_result_and_other_tools_are_untouched(tmp_path: Path) -> None:
    small = ToolResult(ok=True, output="y" * 5000)
    assert _spill_long_result(_core(tmp_path), "mcp__a__b", small).output == "y" * 5000
    big = ToolResult(ok=True, output="z" * 90_000)
    assert len(_spill_long_result(_core(tmp_path), "read_file", big).output) == 90_000


def test_the_limit_is_a_setting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path))
    result = ToolResult(ok=True, output="q" * 20_000)
    out = _spill_long_result(_core(tmp_path, 4000), "mcp__a__b", result)
    assert len(out.output) < 5_000


def test_an_mcp_error_is_never_cut(tmp_path: Path) -> None:
    result = ToolResult(ok=False, error="e" * 50_000)
    assert _spill_long_result(_core(tmp_path), "mcp__a__b", result).error == "e" * 50_000
