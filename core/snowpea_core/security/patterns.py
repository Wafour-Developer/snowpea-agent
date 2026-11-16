"""The command pattern language of the blocked-command list.

A pattern is what a person types into "Blocked commands" (and what the preset
catalog is written in).  It is matched against every simple command a line
would run — after ``sudo``/``env``/``nice``/``timeout``/``xargs``/``find
-exec`` and the other wrappers are taken off, inside ``sh -c``/``bash -c``
bodies, both sides of pipes and ``&&``/``;``, inside command substitutions and
here-strings (see :mod:`snowpea_core.security.shell`).

Syntax (words are split like a shell line: quote a word that has spaces)::

    rm -rf                  program, then options
    git push --force        program, words in order, then options
    docker * prune          ``*`` is any one word
    mkfs*                   a program name may use * ? [..] wildcards
    rm -r /                 a word starting with / or ~ is a path, compared resolved
    git push --force=*      a long option with a value (wildcards allowed in it)
    re:^curl .*\\| *sh      ``re:`` then a Python regular expression, searched in
                            each command's text (its words joined by spaces)

The first word is the program, matched by its file name (``/usr/bin/rm`` is
``rm``) or its family (``python3.12`` is ``python``).  Words that start with
``-`` are options; every one must be present, anywhere in the command, in any
order:

* ``-rf`` means the short options ``r`` and ``f``, whether written ``-rf``,
  ``-fr``, ``-r -f`` or inside a larger cluster (``-rfv``).  For ``find`` an
  option word is whole (``-delete``).
* ``--force`` also matches every abbreviation git and getopt accept
  (``--forc``, ``--fo``) and ``--force=value``; ``--force-with-lease`` is a
  different option.
* Spellings a program treats as the same option count as one: for ``rm`` and
  ``cp`` ``-r`` = ``-R`` = ``--recursive``, ``-f`` = ``--force``; for
  ``chmod``/``chown``/``chgrp`` ``-R`` = ``--recursive``; for ``git push``
  ``-f`` = ``--force``, ``-d`` = ``--delete``; for ``git branch`` ``-D`` =
  ``--delete --force``.

Other words must appear among the command's non-option words in the same
order, not necessarily next to each other (``git push origin`` matches ``git
-C x push origin main``).
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

#: Longest pattern accepted.
MAX_PATTERN_CHARS = 500
#: Most custom patterns per list.
MAX_PATTERNS = 500

#: Programs whose options are whole words with one dash (``find -delete``).
WHOLE_WORD_OPTIONS: frozenset[str] = frozenset({"find", "bfind", "gfind"})

#: Spellings that mean the same option, per program (and ``program sub``).
#: Every value is a set of equivalent spellings; the key is the canonical one.
ALIASES: dict[str, dict[str, frozenset[str]]] = {
    "rm": {"-r": frozenset({"-r", "-R", "--recursive"}), "-f": frozenset({"-f", "--force"})},
    "cp": {"-r": frozenset({"-r", "-R", "--recursive"}), "-f": frozenset({"-f", "--force"})},
    "srm": {"-r": frozenset({"-r", "-R", "--recursive"})},
    "chmod": {"-R": frozenset({"-R", "--recursive"})},
    "chown": {"-R": frozenset({"-R", "--recursive"})},
    "chgrp": {"-R": frozenset({"-R", "--recursive"})},
    "setfacl": {"-R": frozenset({"-R", "--recursive"})},
    "git push": {"-f": frozenset({"-f", "--force"}), "-d": frozenset({"-d", "--delete"})},
    "git clean": {"-f": frozenset({"-f", "--force"})},
    "docker rm": {"-f": frozenset({"-f", "--force"})},
    "docker rmi": {"-f": frozenset({"-f", "--force"})},
    "podman rm": {"-f": frozenset({"-f", "--force"})},
    "docker compose down": {"-v": frozenset({"-v", "--volumes"})},
    "docker-compose down": {"-v": frozenset({"-v", "--volumes"})},
}
#: Options that stand for several others (``git branch -D`` = ``--delete --force``).
COMPOUND: dict[str, dict[str, tuple[str, ...]]] = {
    "git branch": {"-D": ("--delete", "--force")},
}


class PatternError(ValueError):
    """A pattern the language does not accept; the message says why and how to fix it."""


@dataclass(frozen=True)
class Pattern:
    """One compiled pattern."""

    source: str
    regex: re.Pattern[str] | None = None
    program: str = ""
    #: Positional words in order (``*`` any one word); paths start with / or ~.
    words: tuple[str, ...] = ()
    #: Short option letters that must all be present.
    shorts: tuple[str, ...] = ()
    #: Long options (``--force``) or ``(--name, value-glob)`` pairs.
    longs: tuple[tuple[str, str | None], ...] = ()
    #: Whole single-dash option words (``-delete``) for :data:`WHOLE_WORD_OPTIONS`.
    whole: tuple[str, ...] = ()


@dataclass
class Subject:
    """A simple command, as the matcher sees it."""

    words: list[str]
    cwd: Path | None = None
    user_home: Path | None = None
    extra_paths: dict[str, str] = field(default_factory=dict)

    @property
    def program(self) -> str:
        return program_name(self.words[0]) if self.words else ""


def program_name(word: str) -> str:
    """``/usr/bin/Rm.EXE`` -> ``rm``."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[: -len(".exe")] if name.endswith(".exe") else name


def program_family(program: str) -> str:
    if re.fullmatch(r"python[0-9.]*", program):
        return "python"
    if re.fullmatch(r"pip[0-9.]*", program):
        return "pip"
    if re.fullmatch(r"(node|ruby|perl|php|lua)[0-9.]*", program):
        return re.sub(r"[0-9.]+$", "", program)
    return program


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def compile_pattern(source: str) -> Pattern:
    """Compile one pattern; :class:`PatternError` with a helpful message when it is not valid."""
    if not isinstance(source, str):
        raise PatternError("a pattern must be text")
    text = source.strip()
    if not text:
        raise PatternError("the pattern is empty")
    if len(text) > MAX_PATTERN_CHARS:
        raise PatternError(f"the pattern is longer than {MAX_PATTERN_CHARS} characters")
    if "\n" in text or _CONTROL.search(text):
        raise PatternError("a pattern is one line without control characters")
    if text.startswith("re:"):
        body = text[3:].strip()
        if not body:
            raise PatternError("'re:' needs a regular expression after it, e.g. re:^curl .*\\| *sh")
        try:
            regex = re.compile(body)
        except re.error as exc:
            raise PatternError(f"the regular expression does not compile: {exc}") from exc
        if regex.search("") is not None:
            raise PatternError(
                "the regular expression matches an empty command, so it would block every "
                "command; make it more specific"
            )
        return Pattern(source=text, regex=regex)
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise PatternError(f"the pattern has an unbalanced quote ({exc})") from exc
    if not tokens:
        raise PatternError("the pattern is empty")
    head = tokens[0]
    if head.startswith("-"):
        raise PatternError("start with the program name, e.g. 'rm -rf', not with an option")
    if "/" in head and not head.startswith("/"):
        raise PatternError("give the program by its name (rm), not a relative path")
    program = program_name(head)
    if not program or not program.strip("*?[]!"):
        raise PatternError(
            "the program cannot be only a wildcard: that would block every command; "
            "name the program, e.g. 'docker * prune'"
        )
    words: list[str] = []
    shorts: list[str] = []
    longs: list[tuple[str, str | None]] = []
    whole: list[str] = []
    for token in tokens[1:]:
        if token in ("-", "--"):
            raise PatternError(f"'{token}' alone is not an option; remove it")
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            if not re.fullmatch(r"--[A-Za-z0-9][A-Za-z0-9_.:-]*", name):
                raise PatternError(f"'{token}' is not a long option (--name or --name=value)")
            longs.append((name, value if eq else None))
        elif token.startswith("-"):
            body = token[1:]
            if program in WHOLE_WORD_OPTIONS and len(body) > 1:
                whole.append(token)
            elif not re.fullmatch(r"[A-Za-z0-9?@#]+", body):
                raise PatternError(f"'{token}' is not a short option cluster (-r, -rf)")
            else:
                shorts.extend(body)
        else:
            words.append(token)
    return Pattern(source=text, program=program, words=tuple(words),
                   shorts=tuple(dict.fromkeys(shorts)), longs=tuple(longs), whole=tuple(whole))


def validate(source: str) -> str | None:
    """Why ``source`` is not a valid pattern, or ``None``."""
    try:
        compile_pattern(source)
    except PatternError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _split_options(program: str, args: list[str]) -> tuple[list[str], list[str], list[str]]:
    """``(short letters, long/whole option words, positional words)`` of a command."""
    letters: list[str] = []
    options: list[str] = []
    positional: list[str] = []
    only = False
    for word in args:
        if only:
            positional.append(word)
            continue
        if word == "--":
            only = True
            continue
        if word.startswith("--") and len(word) > 2:
            options.append(word)
        elif word.startswith("-") and len(word) > 1 and not _numberish(word):
            if program in WHOLE_WORD_OPTIONS:
                options.append(word)
                if len(word) == 2:
                    letters.append(word[1])
            else:
                letters.extend(word[1:].split("=", 1)[0])
        else:
            positional.append(word)
    return letters, options, positional


def _numberish(word: str) -> bool:
    """``-1`` / ``-9`` are operands of ``kill`` and friends as often as options."""
    return False if len(word) < 2 else word[1:].isdigit() and len(word) > 2


def _long_present(name: str, value: str | None, options: list[str]) -> bool:
    for word in options:
        spelled, eq, given = word.partition("=")
        if spelled == name or (len(spelled) > 2 and name.startswith(spelled)):
            if value is None:
                return True
            if eq and fnmatch.fnmatchcase(given, value):
                return True
    return False


def _alias_key(program: str, positional: list[str]) -> list[str]:
    keys = [program]
    if positional:
        keys.insert(0, f"{program} {positional[0]}")
        if len(positional) > 1:
            keys.insert(0, f"{program} {positional[0]} {positional[1]}")
    return keys


def _option_present(spelling: str, program: str, positional: list[str], letters: list[str],
                     options: list[str]) -> bool:
    """One pattern option (``-f`` / ``--force``) is in the command, any equivalent spelling."""
    spellings = {spelling}
    for key in _alias_key(program, positional):
        table = ALIASES.get(key, {})
        for group in table.values():
            if spelling in group:
                spellings |= set(group)
    for key in _alias_key(program, positional):
        for short, longs in COMPOUND.get(key, {}).items():
            if spelling in longs and short[1:] in letters:
                return True
    for item in spellings:
        if item.startswith("--"):
            if _long_present(item, None, options):
                return True
        elif len(item) == 2 and item[1] in letters:
            return True
    return False


def _resolve_word(word: str, subject: Subject) -> str:
    """A path word, made absolute (``~``, ``$HOME``, relative to the command's folder)."""
    text = word
    home = str(subject.user_home) if subject.user_home else os.path.expanduser("~")
    for name, value in (("${HOME}", home), ("$HOME", home), *subject.extra_paths.items()):
        if text.startswith(name):
            text = value + text[len(name):]
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    if not text.startswith("/"):
        if subject.cwd is None:
            return word
        text = str(subject.cwd / text)
    normal = posixpath.normpath(text)
    return "/" if normal.startswith("//") and normal.strip("/") == "" else normal


def _word_matches(want: str, got: str, subject: Subject) -> bool:
    if want == "*":
        return True
    if want.startswith(("/", "~")):
        target = _resolve_word(want, subject)
        actual = _resolve_word(got, subject)
        if any(c in want for c in "*?["):
            return fnmatch.fnmatchcase(actual, target)
        return actual == target
    return fnmatch.fnmatchcase(got, want)


def _ordered(want: tuple[str, ...], got: list[str], subject: Subject) -> bool:
    position = 0
    for item in want:
        while position < len(got) and not _word_matches(item, got[position], subject):
            position += 1
        if position >= len(got):
            return False
        position += 1
    return True


def matches(pattern: Pattern, subject: Subject) -> bool:
    """``pattern`` matches the simple command ``subject``."""
    if not subject.words:
        return False
    if pattern.regex is not None:
        return pattern.regex.search(" ".join(subject.words)) is not None
    program = subject.program
    if not (
        fnmatch.fnmatchcase(program, pattern.program)
        or fnmatch.fnmatchcase(program_family(program), pattern.program)
    ):
        return False
    letters, options, positional = _split_options(program, subject.words[1:])
    for option in pattern.whole:
        if option not in options:
            return False
    for name, value in pattern.longs:
        if value is not None:
            if not _long_present(name, value, options):
                return False
        elif not _option_present(name, program, positional, letters, options):
            return False
    for letter in pattern.shorts:
        if not _option_present(f"-{letter}", program, positional, letters, options):
            return False
    return _ordered(pattern.words, positional, subject)


__all__ = [
    "ALIASES",
    "MAX_PATTERNS",
    "MAX_PATTERN_CHARS",
    "Pattern",
    "PatternError",
    "Subject",
    "compile_pattern",
    "matches",
    "program_family",
    "program_name",
    "validate",
]
