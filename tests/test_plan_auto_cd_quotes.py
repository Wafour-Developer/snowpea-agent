"""Plan mode's "Allow Everything": a ``cd`` prefix and quoted separators (owner report).

Lines are judged only, never run.
"""

from __future__ import annotations

import pytest

from snowpea_core.permissions.safe_commands import is_mutating

INSPECTIONS = [
    'cd /mnt/data/flux2_fast && echo "=== 8787.log tail ===" && tail -60 logs/8787.log; echo; '
    'echo "=== grep timing ==="; '
    'grep -n -iE "s/it|it/s|elapsed|sec|took|step" logs/8787.log | tail -40',
    'cd /mnt/data/flux2_fast && ls config/ && echo "===" && cat config/x.json && '
    'echo "=== tree ===" && find wan2gp -maxdepth 2 -type d | head -40 && du -sh wan2gp',
    "cd /tmp && ls",
    'grep -iE "a|b" f',
    'echo "x; rm -rf a"',
]

STILL_ASKED = [
    "cd /tmp && rm a",
    "cd /tmp && echo x > PLAN.md",  # a relative redirect after cd is not judged
    "cd /tmp && ls > /dev/null",  # any redirect turns the cd exemption off
    "cd a b",
    "cd -P /tmp && ls",
    'ls "a" ; rm b',
    "grep x f | sh",
    'echo "unclosed',
    "cat a | tee b",
    # security review (m29): a quoted ">" must not hide a real ";"
    "cat \"a>/tmp/b\" ; touch P ; grep '\"'\\' f",
    "cat 'a>/tmp/b' ; touch P",
    # security review (m29): after cd, code-running checkers judge the wrong folder
    "cd docs && python -m pytest",
    "cd docs && npm test",
    "cd /tmp && python -m pytest test_p.py",
    "cd docs/r && git status",
    "cd docs && cargo test",
    "cd docs && go test ./...",
]


@pytest.mark.parametrize("line", INSPECTIONS)
def test_read_only_lines_run_unasked(line: str) -> None:
    assert is_mutating(line) is False


@pytest.mark.parametrize("line", STILL_ASKED)
def test_changing_or_unclear_lines_still_ask(line: str) -> None:
    assert is_mutating(line) is True
