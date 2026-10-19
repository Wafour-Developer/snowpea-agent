"""Tests for parallel tool-call batch planning."""

from __future__ import annotations

from pathlib import Path

from snowpea_core.agent.tool_batch import plan_tool_batch_segments
from snowpea_core.providers.base import ToolCall


def _call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"id-{name}", name=name, arguments=arguments)


def test_independent_reads_batch_parallel(tmp_path: Path) -> None:
    calls = [
        _call("read_file", path="a.ts"),
        _call("read_file", path="b.ts"),
    ]
    segments = plan_tool_batch_segments(calls, workdir=tmp_path)
    assert segments == [("parallel", calls)]


def test_read_then_edit_same_path_is_sequential(tmp_path: Path) -> None:
    calls = [
        _call("read_file", path="a.ts"),
        _call("patch", path="a.ts", old_string="x", new_string="y"),
    ]
    segments = plan_tool_batch_segments(calls, workdir=tmp_path)
    assert segments == [("sequential", calls)]


def test_two_writes_to_different_paths_parallel(tmp_path: Path) -> None:
    calls = [
        _call("write_file", path="a.ts", content="a"),
        _call("write_file", path="b.ts", content="b"),
    ]
    segments = plan_tool_batch_segments(calls, workdir=tmp_path)
    assert segments == [("parallel", calls)]


def test_shell_is_always_sequential_barrier(tmp_path: Path) -> None:
    calls = [
        _call("glob", pattern="*"),
        _call("shell", command="pwd"),
        _call("read_file", path="a.ts"),
    ]
    segments = plan_tool_batch_segments(calls, workdir=tmp_path)
    assert all(kind == "sequential" for kind, _ in segments)
    assert [call.name for _, batch in segments for call in batch] == [
        "glob",
        "shell",
        "read_file",
    ]


def test_single_call_demoted_to_sequential(tmp_path: Path) -> None:
    calls = [_call("read_file", path="only.ts")]
    segments = plan_tool_batch_segments(calls, workdir=tmp_path)
    assert segments == [("sequential", calls)]


def test_a_batched_patch_reserves_every_file_it_edits(tmp_path: Path) -> None:
    """``edits`` may reach past ``path``: a read of that other file must wait."""
    calls = [
        _call(
            "patch",
            path="a.ts",
            edits=[
                {"old_string": "x", "new_string": "y"},
                {"path": "b.ts", "old_string": "x", "new_string": "y"},
            ],
        ),
        _call("read_file", path="b.ts"),
    ]
    assert plan_tool_batch_segments(calls, workdir=tmp_path) == [("sequential", calls)]


def test_a_multi_file_read_still_runs_beside_unrelated_reads(tmp_path: Path) -> None:
    calls = [_call("read_file", paths=["a.ts", "b.ts"]), _call("read_file", path="c.ts")]
    assert plan_tool_batch_segments(calls, workdir=tmp_path) == [("parallel", calls)]
    clash = [
        _call("read_file", paths=["a.ts", "b.ts"]),
        _call("patch", path="b.ts", old_string="x", new_string="y"),
    ]
    assert plan_tool_batch_segments(clash, workdir=tmp_path) == [("sequential", clash)]
