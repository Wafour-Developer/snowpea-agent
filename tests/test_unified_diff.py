"""Unified diffs split on "\\n" only and mark a missing last newline (browser peer, 2026-11-16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.session.checkpoints import _patch
from snowpea_core.tools.fs import unified_diff
from snowpea_core.util.udiff import NO_NEWLINE, lines, unified


def _hunk_counts(diff: str) -> list[tuple[int, int, int, int]]:
    import re

    out = []
    for match in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M):
        out.append(tuple(int(g) if g is not None else 1 for g in match.groups()))
    return out  # type: ignore[return-value]


def _body_counts(diff: str) -> tuple[int, int]:
    old = new = 0
    for line in diff.split("\n"):
        if line.startswith(("---", "+++", "@@", "\\")):
            continue
        if line.startswith("-"):
            old += 1
        elif line.startswith("+"):
            new += 1
        elif line.startswith(" "):
            old += 1
            new += 1
    return old, new


def test_lines_split_on_newline_only() -> None:
    assert lines("a\fb c\rd\x1ce\x85f\ng") == ["a\fb c\rd\x1ce\x85f\n", "g"]
    assert lines("") == []
    assert lines("a\n") == ["a\n"]


def test_the_fused_last_line_case() -> None:
    diff = unified("a\nb", "a\nc", "a/x", "b/x")
    assert "-b\n" + NO_NEWLINE in diff and "+c\n" + NO_NEWLINE in diff
    assert "-b+c" not in diff


@pytest.mark.parametrize("odd", ["\f", " ", "\r", "\x1c", "\x85", "\v"])
def test_hunk_counts_match_with_odd_separators(odd: str) -> None:
    before = f"one{odd}two\nthree\nfour\n"
    after = f"one{odd}two\nTHREE\nfour\n"
    for diff in (unified(before, after, "a/x", "b/x"), unified_diff("x", before, after),
                 _patch("x", before.encode(), after.encode())):
        counts = _hunk_counts(diff)
        assert counts, diff
        _start_old, old_len, _start_new, new_len = counts[0]
        assert _body_counts(diff) == (old_len, new_len), diff


def test_checkpoint_patch_marks_the_missing_newline() -> None:
    patch = _patch("f.txt", b"keep\nold", b"keep\nnew")
    assert NO_NEWLINE in patch and "-old\n" in patch and "+new\n" in patch


def test_agy_and_claude_previews(tmp_path: Path) -> None:
    from snowpea_core.external.agy import _unified

    diff = _unified("/p/x.txt", "a\nb", "a\nc")
    assert NO_NEWLINE in diff
    from snowpea_core.external import claude

    (tmp_path / "x.txt").write_text("a\nb")
    preview = claude.preview_diff("Write", {"file_path": str(tmp_path / "x.txt"),
                                            "content": "a z\nc"}, str(tmp_path))
    assert preview is not None and "\\ No newline at end of file" in preview
    assert "+a z" in preview
