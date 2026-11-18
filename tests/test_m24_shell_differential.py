"""m24 differential check (security review 2 of the shell reader): no line is
judged more weakly than the reader before m24 (main at 00b916e4) judged it.

``fixtures/m24_shell_differential.json`` holds the corpus with the old
verdicts frozen as data: ``[line, under "ask", under "auto"]``, each ``b``
(blocked), ``o`` (asked) or ``a`` (let through), in a session in auto mode.
The corpus is every line of the security test tables (review 3's generated
table sampled), the lines of both m24 security reviews, and generated variants
(quotes, ``$'…'``, here-documents, ``case``, ``$((``, ``${``, nested
substitutions, ``cd`` forms, pipes into shells).  It was judged by the old
modules exported read-only with ``git archive 00b916e4``.

Every line is DATA handed to the pure checker; nothing here runs any of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.agent.loop import _asks_unseen
from snowpea_core.security import written
from snowpea_core.security.check import Context, check_command
from snowpea_core.security.lists import from_raw

CORPUS = Path(__file__).parent / "fixtures" / "m24_shell_differential.json"
RANK = {"a": 0, "o": 1, "b": 2}

#: Lines judged more weakly on purpose, each with why.  Every one is a plain
#: process-substitution read, or a line the old reader misread, whose commands
#: are judged exactly like the same commands written out plainly.
EXCEPTIONS: dict[str, str] = {
    "LC_ALL=C comm -23 <(LC_ALL=C sort /tmp/dirs.txt) <(awk '{print $2}' /tmp/probe.txt "
    "| LC_ALL=C sort) | head -5":
        "the owner's line: process substitution is read now (it was parse:syntax)",
    'echo "done: $(wc -l < /tmp/probe.txt)/102 total: $(awk \'{s+=$1} END {printf "%.1f", '
    's/1048576}\' /tmp/probe.txt) GB"; LC_ALL=C comm -23 <(LC_ALL=C sort /tmp/dirs.txt) '
    "<(awk '{print $2}' /tmp/probe.txt | LC_ALL=C sort) | head -5; "
    'echo "--- still running ---"; pgrep -f "du -sk" | wc -l; df -h /home/u/ext | tail -1':
        "the owner's full line: process substitution is read now",
    "paste <(cut -f1 a.tsv) <(cut -f2 b.tsv) | head":
        "a plain <( ) read: the bodies are read as commands, the pipes are no files",
    "cat < <(cat ~/.snowpea/trust/projects.json)":
        "a plain <( ) read of a file any line may read (`cat ~/.snowpea/trust/"
        "projects.json` alone was and is let through)",
    'echo "$(echo $\'\\\'\' )" ; cat ~/.snowpea/trust/projects.json ; echo "\' )"':
        "$'\\'' is read as the shell reads it now; what runs is `echo`, the same "
        "allowed read as above, and `echo`",
}


def _outcome(line: str, root: Path, value: str) -> str:
    written.reset()
    ctx = Context(workdir=root, user_home=Path("/home/u"), home=Path("/home/u/.snowpea"),
                  projects=(str(root),), mounts=(), env_venv=False, unknown_code=value,
                  dry_run=True)
    verdict = check_command(line, from_raw({}), ctx, cwd=root)
    if verdict.blocked:
        return "b"
    return "o" if _asks_unseen(verdict, SimpleNamespace(mode="auto")) else "a"


def test_the_corpus_is_large_and_holds_the_review_lines() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    lines = {entry[0] for entry in corpus}
    assert len(corpus) >= 300
    assert set(EXCEPTIONS) <= lines
    for line in ('echo "\\"" >(rm -rf ~)', "cd ~ && cat <(rm -rf .ssh)",
                 'echo "$(case a in a) rm -rf ~/.ssh;; esac)"',
                 "IFS=,; X=rm,-rf,/home/u/.ssh; $X"):
        assert line in lines, line


def test_no_line_is_judged_more_weakly_than_before_m24(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    root = tmp_path / "proj"
    root.mkdir()
    weaker: list[tuple[str, list[str], list[str]]] = []
    relaxed: set[str] = set()
    for line, old_ask, old_auto in json.loads(CORPUS.read_text(encoding="utf-8")):
        now = [_outcome(line, root, "ask"), _outcome(line, root, "auto")]
        if RANK[now[0]] < RANK[old_ask] or RANK[now[1]] < RANK[old_auto]:
            if line in EXCEPTIONS:
                relaxed.add(line)
                continue
            weaker.append((line, [old_ask, old_auto], now))
    assert not weaker, "\n".join(f"{line!r}: {old} -> {now}" for line, old, now in weaker)
    # Every exception is still needed (a stale one would hide nothing, but says
    # something untrue about the reader).
    assert relaxed == set(EXCEPTIONS), set(EXCEPTIONS) - relaxed
