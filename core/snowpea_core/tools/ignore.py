"""What the Python search walk leaves out: ``.gitignore`` rules and lock files.

ripgrep reads ignore files itself.  When the backend has no ``rg`` the walk in
:mod:`snowpea_core.tools.glob` runs instead, and without these rules it read
``target/classes`` and ``package-lock.json`` before any source file.  The
matcher covers the common ``.gitignore`` subset: blank lines and ``#``
comments, ``!`` negation, a trailing ``/`` for directories, a leading or
inner ``/`` anchoring the pattern to its file's directory, ``*``, ``?``,
``[...]`` and ``**``.  As in git, a later rule wins over an earlier one and a
file inside an ignored directory cannot be re-included.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

#: Lock files hold thousands of lines that match almost any identifier; a
#: search skips them unless its glob or path names them.
LOCK_FILES: frozenset[str] = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "go.sum"}
)

#: Build output a manifest produces, pruned only where no ``.gitignore``
#: governs the directory: ``target/`` next to ``pom.xml`` is Maven's, but a
#: ``target`` directory elsewhere may be source.  (``build`` and ``dist`` are
#: in ``glob.PRUNED_DIRS`` already.)
BUILD_OUTPUT: dict[str, frozenset[str]] = {
    "target": frozenset({"pom.xml", "build.sbt", "Cargo.toml", "project.clj"}),
}


def is_lock_file(name: str) -> bool:
    """True for a dependency lock file name (``*.lock`` included)."""
    base = name.rsplit("/", 1)[-1]
    return base in LOCK_FILES or base.endswith(".lock")


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """A regex for ``pattern`` where ``**`` crosses directory separators."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
            else:
                out.append(fnmatch.translate(pattern[i : close + 1])[4:-3])
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class Rule:
    """One ``.gitignore`` line, held by the directory ``base`` ("" for the top)."""

    base: str
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool
    anchored: bool


def parse(text: str, base: str) -> list[Rule]:
    """The rules of one ``.gitignore`` whose directory is ``base``."""
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        elif line.startswith("\\"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        rules.append(Rule(base, compile_pattern(line), negate, dir_only, anchored))
    return rules


def ignored(rules: list[Rule], path: str, *, is_dir: bool) -> bool:
    """True when the last rule that matches ``path`` ignores it."""
    verdict = False
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        if rule.base:
            if not path.startswith(rule.base + "/"):
                continue
            sub = path[len(rule.base) + 1 :]
        else:
            sub = path
        if rule.regex.match(sub if rule.anchored else sub.rsplit("/", 1)[-1]):
            verdict = not rule.negate
    return verdict


__all__ = [
    "BUILD_OUTPUT",
    "LOCK_FILES",
    "Rule",
    "compile_pattern",
    "ignored",
    "is_lock_file",
    "parse",
]
