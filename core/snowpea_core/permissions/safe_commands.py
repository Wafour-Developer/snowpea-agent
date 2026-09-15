"""Which shell commands are read-only enough to run unasked in plan mode.

Plan mode is for understanding the code, and a planner that has to ask before
every ``git status`` either stops asking questions or stops planning.  So the
``exec`` row stays ``ask`` in general and this module carves out the commands
that cannot change anything: a listing, a search, a diff, a test run.

The classifier is deliberately **conservative and table-driven**.  It answers
``True`` only for shapes it recognises in full; everything it has not been
taught — an unknown program, an unparseable line, a redirection, a substitution
— falls through to ``False`` and is asked about as before.  That asymmetry is
the whole design: a false ``False`` costs one approval prompt, a false ``True``
costs the user a file.

It is a pure function of the command string so it can be tested as a table,
and so nothing about it depends on what the filesystem happens to contain.
"""

from __future__ import annotations

import re
import shlex

#: Programs that cannot write anything, whatever arguments they are given.
#: ``python`` and ``node`` are deliberately absent: ``python -c`` and
#: ``node -e`` run arbitrary code, and no flag check makes them safe.
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "df",
        "du",
        "echo",
        "file",
        "find",
        "grep",
        "head",
        "ls",
        "pwd",
        "pytest",
        "rg",
        "stat",
        "tail",
        "tree",
        "wc",
        "which",
    }
)

#: Multi-word invocations that are safe as a whole.  A bare ``git`` or ``npm``
#: is not in :data:`SAFE_COMMANDS` precisely because most of what follows it
#: writes; only these openings are recognised.
SAFE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "blame"),
    ("git", "branch"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "ls-files"),
    ("git", "show"),
    ("git", "status"),
    ("npm", "ls"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("pnpm", "ls"),
    ("pnpm", "test"),
    ("pnpm", "run", "test"),
    ("yarn", "list"),
    ("yarn", "test"),
    ("yarn", "run", "test"),
)

#: Prefixes that only wrap another command: strip them and judge what is left.
#: ``uv run pytest`` is the spelling this project's own tests use.
RUNNER_PREFIXES: tuple[tuple[str, ...], ...] = (("uv", "run"),)

#: Programs that are never safe, listed so a reader can see the intent rather
#: than infer it from the absence of an entry above.
NEVER_SAFE: frozenset[str] = frozenset(
    {
        "chgrp",
        "chmod",
        "chown",
        "cp",
        "curl",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "node",
        "patch",
        "perl",
        "python",
        "python3",
        "rm",
        "rmdir",
        "ruby",
        "sed",
        "sh",
        "bash",
        "tee",
        "touch",
        "truncate",
        "wget",
        "zsh",
    }
)

#: Arguments that turn a recognised safe opening into a writing one.  ``git
#: branch`` lists branches; ``git branch -D x`` deletes one.
BANNED_ARGS: dict[tuple[str, ...], frozenset[str]] = {
    ("git", "branch"): frozenset(
        {
            "-c",
            "-C",
            "-d",
            "-D",
            "-m",
            "-M",
            "-u",
            "--copy",
            "--delete",
            "--edit-description",
            "--force",
            "--move",
            "--set-upstream-to",
            "--unset-upstream",
        }
    ),
    ("find",): frozenset(
        {"-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprint0", "-ok", "-okdir"}
    ),
}

#: Characters and openings that put the line beyond this classifier: a
#: redirection writes, a substitution runs a command this code never sees.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (">", "<", "`", "$(", "${", "\n")

#: Where one command line ends and the next begins.  Every segment has to be
#: safe on its own — ``ls && rm -rf .`` is not made safe by its first half.
SEPARATORS = re.compile(r"\|\||&&|[;|]")


def _strip_runner(words: list[str]) -> list[str]:
    """Drop a wrapper like ``uv run`` so the real program is judged."""
    for prefix in RUNNER_PREFIXES:
        length = len(prefix)
        if len(words) > length and tuple(words[:length]) == prefix:
            return words[length:]
    return words


def _banned(words: list[str]) -> bool:
    """True when a recognised opening carries an argument that writes."""
    for prefix, flags in BANNED_ARGS.items():
        if tuple(words[: len(prefix)]) != prefix:
            continue
        rest = words[len(prefix) :]
        if any(word in flags or word.split("=", 1)[0] in flags for word in rest):
            return True
    return False


def _segment_is_safe(segment: str) -> bool:
    """One command line, already split off at a separator."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return False
    words = _strip_runner(words)
    if not words:
        return False
    head = words[0]
    # An environment assignment in front of a command (`FOO=1 ls`) is not a
    # program this table knows, and `env FOO=1 cmd` sets state for it.
    if "=" in head or head in NEVER_SAFE:
        return False
    if head == "env":
        # `env` alone prints the environment; `env FOO=1 cmd` changes what the
        # command sees, which is a write by another name.
        rest = words[1:]
        if not rest:
            return True
        return "=" not in rest[0] and _segment_is_safe(shlex.join(rest))
    if _banned(words):
        return False
    if any(tuple(words[: len(prefix)]) == prefix for prefix in SAFE_PREFIXES):
        return True
    return head in SAFE_COMMANDS


def is_read_only(command: str) -> bool:
    """True when ``command`` only inspects, so plan mode may run it unasked.

    Every segment of a ``;``/``&&``/``||``/pipe chain must be safe on its own,
    and any redirection or command substitution disqualifies the whole line.
    """
    text = (command or "").strip()
    if not text or any(mark in text for mark in FORBIDDEN_SUBSTRINGS):
        return False
    # `&&` is a separator; a lone `&` backgrounds the command, which this
    # classifier has no way to keep watching.
    if "&" in SEPARATORS.sub(" ", text):
        return False
    segments = [part.strip() for part in SEPARATORS.split(text)]
    return bool(segments) and all(segment and _segment_is_safe(segment) for segment in segments)


__all__ = [
    "BANNED_ARGS",
    "FORBIDDEN_SUBSTRINGS",
    "NEVER_SAFE",
    "RUNNER_PREFIXES",
    "SAFE_COMMANDS",
    "SAFE_PREFIXES",
    "is_read_only",
]
