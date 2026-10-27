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
from dataclasses import dataclass, field
from typing import Any

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


# ---------------------------------------------------------------------------
# is_mutating: the plan-then-auto widening (1.32.0)
# ---------------------------------------------------------------------------
#
# "Allow Everything" while planning lets the planner's inspection run unasked,
# but plan mode still changes nothing: a command that writes, or that runs code
# of anyone's choosing, keeps asking.  This is an **allowlist of shapes**: a
# segment runs unasked only when its program is in :data:`SPECS` (or is one of
# the special shapes below) and *every* flag it carries is one that program's
# spec names.  An unknown program or an unknown flag asks — a false "safe"
# costs the user a file, a false "mutating" costs one approval prompt.


@dataclass(frozen=True)
class Spec:
    """What one program may be given and still only read.

    ``valued`` maps a flag to the kind of value it takes: ``any``, ``int``,
    ``project`` (a path inside the project that plan mode cannot write),
    ``pair`` (two words, jq's ``--arg name value``) or ``choice:a,b`` .
    ``positional`` is ``any``, ``project``, ``url``, ``plus`` (``+FORMAT``)
    or ``none``.  ``single_dash`` reads ``-run`` as one flag (go, find).
    """

    bare: frozenset[str] = frozenset()
    valued: dict[str, str] = field(default_factory=dict)
    positional: str = "any"
    numeric: bool = False
    single_dash: bool = False
    max_positional: int | None = None
    dashdash: bool = True


def _spec(
    bare: str = "",
    valued: dict[str, str] | None = None,
    **kwargs: Any,
) -> Spec:
    return Spec(bare=frozenset(bare.split()), valued=dict(valued or {}), **kwargs)


_ANY = "any"
_INT = "int"

#: Programs that only read or print, with every flag they may carry.
SPECS: dict[str, Spec] = {
    "ls": _spec(
        "-l -a -A -h -R -t -r -S -1 -d -F -i -s -n -G -p -c -u -X -v --all --almost-all "
        "--human-readable --recursive --group-directories-first",
        {"--color": _ANY, "--sort": _ANY},
    ),
    "cat": _spec("-n -b -A -e -E -s -T -v"),
    "head": _spec(
        "-q -v", {"-n": _ANY, "-c": _ANY, "--lines": _ANY, "--bytes": _ANY}, numeric=True
    ),
    "tail": _spec(
        "-q -v", {"-n": _ANY, "-c": _ANY, "--lines": _ANY, "--bytes": _ANY}, numeric=True
    ),
    "grep": _spec(
        "-r -R -n -i -I -l -L -c -v -w -x -h -H -o -q -s -E -F -P -z -Z -a --recursive "
        "--line-number --ignore-case --files-with-matches --files-without-match --count "
        "--invert-match --word-regexp --line-regexp --no-filename --with-filename "
        "--only-matching --quiet --silent --extended-regexp --fixed-strings --perl-regexp",
        {
            "-e": _ANY,
            "-f": _ANY,
            "-A": _INT,
            "-B": _INT,
            "-C": _INT,
            "-m": _INT,
            "--include": _ANY,
            "--exclude": _ANY,
            "--exclude-dir": _ANY,
            "--color": _ANY,
            "--colour": _ANY,
            "--max-count": _INT,
            "--context": _INT,
            "--regexp": _ANY,
            "--binary-files": _ANY,
        },
        numeric=True,
    ),
    "rg": _spec(
        "-n -i -S -s -l -c -w -v -u -uu -F -x -o -L -N -U -H -I -0 --hidden --files "
        "--no-ignore --count --files-with-matches --files-without-match --no-heading "
        "--heading --json --vimgrep --line-number --ignore-case --smart-case "
        "--case-sensitive --fixed-strings --multiline --follow --no-filename "
        "--with-filename --stats --type-list --word-regexp --invert-match",
        {
            "-g": _ANY,
            "--glob": _ANY,
            "-t": _ANY,
            "--type": _ANY,
            "-T": _ANY,
            "--type-not": _ANY,
            "-e": _ANY,
            "--regexp": _ANY,
            "-A": _INT,
            "-B": _INT,
            "-C": _INT,
            "-m": _INT,
            "--max-count": _INT,
            "-d": _INT,
            "--max-depth": _INT,
            "--color": _ANY,
            "-j": _INT,
            "--threads": _INT,
            "-M": _INT,
            "--max-columns": _INT,
            "--sort": _ANY,
            "--sortr": _ANY,
            "--context": _INT,
        },
    ),
    "find": _spec(
        "-print -print0 -prune -o -or -a -and -not -empty -ls -readable -writable -L -H "
        "-P -follow -depth -xdev -mount -true -false",
        {
            name: _ANY
            for name in (
                "-name -iname -path -ipath -wholename -iwholename -type -xtype -maxdepth "
                "-mindepth -size -mtime -mmin -atime -amin -ctime -cmin -newer -user "
                "-group -perm -regex -iregex -regextype -links -uid -gid -samefile -inum"
            ).split()
        },
        single_dash=True,
    ),
    "wc": _spec("-l -w -c -m -L --lines --words --bytes --chars --max-line-length"),
    "tree": _spec(
        "-a -d -f -i -s -h -p -u -g -D -F -C -n -Q --noreport --dirsfirst",
        {"-L": _INT, "-I": _ANY, "-P": _ANY, "--filelimit": _INT},
    ),
    "sort": _spec(
        "-n -r -u -f -h -V -b -g -M -s -c -C -d -i -z", {"-k": _ANY, "-t": _ANY, "--key": _ANY}
    ),
    "uniq": _spec("-c -d -u -i -D -z", {"-f": _INT, "-s": _INT, "-w": _INT}, max_positional=1),
    "cut": _spec("-s -n --complement", {"-d": _ANY, "-f": _ANY, "-c": _ANY, "-b": _ANY}),
    "tr": _spec("-d -s -c -C -t"),
    "nl": _spec("", {"-b": _ANY, "-n": _ANY, "-w": _INT, "-s": _ANY}),
    "column": _spec("-t -x -n -e", {"-s": _ANY, "-c": _INT, "-o": _ANY}),
    "fold": _spec("-s -b", {"-w": _INT}),
    "comm": _spec("-1 -2 -3 --check-order --nocheck-order"),
    "diff": _spec(
        "-u -r -N -q -y -w -b -B -i -a -t -s --brief --recursive --side-by-side "
        "--ignore-all-space --new-file --text --report-identical-files",
        {"-U": _INT, "--unified": _INT, "--color": _ANY, "-x": _ANY, "--exclude": _ANY},
    ),
    "cmp": _spec("-l -s -b --silent --verbose"),
    "echo": _spec("-n -e -E"),
    "printf": _spec(""),
    "pwd": _spec("-L -P", positional="none"),
    "whoami": _spec("", positional="none"),
    "id": _spec("-u -g -G -n -r"),
    "nproc": _spec("--all", positional="none"),
    "uptime": _spec("-p -s", positional="none"),
    "true": _spec(""),
    "false": _spec(""),
    "printenv": _spec(""),
    "uname": _spec("-a -s -n -r -v -m -p -i -o", positional="none"),
    "free": _spec("-h -m -g -k -b -t -w --human", positional="none"),
    "df": _spec("-h -H -T -i -a -l -k -P", {"-t": _ANY, "-x": _ANY}),
    "du": _spec(
        "-s -h -c -a -k -m -x -L --summarize --human-readable",
        {"-d": _INT, "--max-depth": _INT, "--exclude": _ANY},
    ),
    "which": _spec("-a"),
    "file": _spec("-b -i -L -z --mime --brief --mime-type"),
    "basename": _spec("-a -z", {"-s": _ANY}),
    "dirname": _spec("-z"),
    "realpath": _spec("-e -m -s -q -z", {"--relative-to": _ANY, "--relative-base": _ANY}),
    "readlink": _spec("-f -e -m -n -q -s -v -z"),
    "stat": _spec("-L -f -t", {"-c": _ANY, "--format": _ANY, "--printf": _ANY}),
    "od": _spec("-b -c -d -o -x -v", {"-A": _ANY, "-t": _ANY, "-N": _INT, "-j": _INT, "-w": _INT}),
    "strings": _spec("-a", {"-n": _INT, "-t": _ANY}),
    "jq": _spec(
        "-r -c -n -e -s -S -j -a -C -M -R --raw-output --compact-output --null-input "
        "--exit-status --slurp --sort-keys --join-output --ascii-output --tab --raw-input",
        {"--indent": _INT, "--arg": "pair", "--argjson": "pair"},
    ),
    "seq": _spec("-w", {"-s": _ANY, "-f": _ANY}, numeric=True),
    "date": _spec(
        "-u -R -I --utc --rfc-2822",
        {"-d": _ANY, "--date": _ANY, "-r": _ANY, "--iso-8601": _ANY},
        positional="plus",
    ),
    "hostname": _spec("-f -s -i -I -d -A --fqdn --short", positional="none"),
    "pgrep": _spec("-f -l -a -x -n -o -c -i -v", {"-u": _ANY, "-P": _INT, "-g": _ANY}),
    "md5sum": _spec("-c -b -t --check --quiet --status --tag -z"),
    "sha1sum": _spec("-c -b -t --check --quiet --status --tag -z"),
    "sha256sum": _spec("-c -b -t --check --quiet --status --tag -z"),
    "sha512sum": _spec("-c -b -t --check --quiet --status --tag -z"),
    "cksum": _spec(""),
}
SPECS["egrep"] = SPECS["grep"]
SPECS["fgrep"] = SPECS["grep"]

#: ``git`` subcommands that only read.
GIT_READ: frozenset[str] = frozenset(
    {
        "blame",
        "cat-file",
        "check-ignore",
        "count-objects",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "whatchanged",
        "branch",
        "tag",
        "remote",
        "stash",
        "config",
        "worktree",
    }
)

_GIT_BARE = (
    "--oneline --stat --shortstat --numstat --name-only --name-status --porcelain -s -b "
    "--short --branch --graph --decorate --all -p --patch --cached --staged -w "
    "--ignore-all-space --no-color --abbrev-commit --follow --word-diff --reverse "
    "--merges --no-merges --first-parent --tags --always --dirty --long -a -r -v -vv -l "
    "--list -i -t -q --quiet --show-current --remotes --get --get-all -e --exit-code "
    "--untracked-files --ignored --verbose --heads --abbrev-ref --show-toplevel "
    "--is-inside-work-tree --verify --symbolic-full-name --count -c --cc --raw"
)
_GIT_VALUED = {
    "--format": _ANY,
    "--pretty": _ANY,
    "--since": _ANY,
    "--until": _ANY,
    "--after": _ANY,
    "--before": _ANY,
    "--author": _ANY,
    "--grep": _ANY,
    "--max-count": _INT,
    "-U": _INT,
    "--unified": _INT,
    "--diff-filter": _ANY,
    "--color": _ANY,
    "-L": _ANY,
    "--date": _ANY,
    "--abbrev": _INT,
    "--untracked-files": _ANY,
    "--sort": _ANY,
    "--contains": _ANY,
    "--merged": _ANY,
    "--no-merged": _ANY,
    "--points-at": _ANY,
}
#: ``-n`` counts commits in a log, and numbers lines in a grep.
GIT_SPECS: dict[str, Spec] = {
    "log": _spec(_GIT_BARE, {**_GIT_VALUED, "-n": _INT}, numeric=True),
    "shortlog": _spec(_GIT_BARE + " -n -s -e", _GIT_VALUED, numeric=True),
    "rev-list": _spec(_GIT_BARE, {**_GIT_VALUED, "-n": _INT}, numeric=True),
    "whatchanged": _spec(_GIT_BARE, {**_GIT_VALUED, "-n": _INT}, numeric=True),
    "grep": _spec(_GIT_BARE + " -n -E -F -P -h -H -o", {**_GIT_VALUED, "-e": _ANY}),
}
_GIT_DEFAULT = _spec(_GIT_BARE, _GIT_VALUED, numeric=True)

#: Package-manager scripts a planner runs to learn the state of the code.
CHECK_SCRIPT = re.compile(r"^(test|tests|lint|typecheck|type-check|check)(:[\w:-]+)?$")

_PYTEST = _spec(
    "-x -q -qq -v -vv -s -l --lf --ff --co --collect-only --sw --stepwise --no-header "
    "--exitfirst --quiet --verbose --last-failed --failed-first --showlocals --strict-markers",
    {
        "-k": _ANY,
        "-m": _ANY,
        "--tb": "choice:auto,long,short,line,native,no",
        "-r": _ANY,
        "--maxfail": _INT,
        "--durations": _INT,
        "-p": "choice:no:cacheprovider",
    },
    positional="project",
)
_UNITTEST = _spec("-v -q -f -b", {"-k": _ANY}, positional="project")
_MYPY = _spec(
    "--strict --ignore-missing-imports --no-incremental --pretty --show-error-codes "
    "--no-error-summary --show-column-numbers --no-color-output --warn-unused-ignores "
    "--check-untyped-defs --disallow-untyped-defs",
    {"--python-version": _ANY, "-p": _ANY, "-m": _ANY},
    positional="project",
)
_RUFF_CHECK = _spec(
    "--statistics -q --quiet --no-cache --show-fixes -e --exit-zero -s --silent",
    {
        "--select": _ANY,
        "--ignore": _ANY,
        "--extend-select": _ANY,
        "--extend-ignore": _ANY,
        "--output-format": "choice:concise,full,json,json-lines,junit,grouped,github,gitlab,"
        "pylint,rdjson,azure,sarif",
        "--target-version": _ANY,
        "--line-length": _INT,
    },
)
_RUFF_FORMAT = _spec("--check --diff -q --quiet --no-cache")

#: Checkers a planner runs, and the one flag some of them require.
CHECKERS: dict[str, tuple[Spec, tuple[str, ...]]] = {
    "tsc": (
        _spec(
            "--noEmit --pretty --strict",
            {"-p": "project", "--project": "project"},
            positional="project",
        ),
        ("--noEmit",),
    ),
    "vitest": (
        _spec(
            "--run --silent --passWithNoTests --no-color",
            {
                "-t": _ANY,
                "--testNamePattern": _ANY,
                "--reporter": "choice:default,basic,verbose,dot,json,tap,tap-flat,junit,"
                "hanging-process,github-actions,tree",
            },
            positional="project",
        ),
        (),
    ),
    "jest": (
        _spec(
            "--silent --ci --runInBand -i --passWithNoTests --verbose --listTests",
            {"-t": _ANY, "--testNamePattern": _ANY},
            positional="project",
        ),
        (),
    ),
    "mocha": (_spec("--recursive", {"-g": _ANY, "--grep": _ANY}, positional="project"), ()),
    "eslint": (
        _spec("--quiet", {"--max-warnings": _INT, "--ext": _ANY}, positional="project"),
        (),
    ),
    "prettier": (
        _spec("--check -l --list-different", positional="project"),
        ("--check", "-l", "--list-different"),
    ),
}

_CARGO = _spec(
    "--lib --bins --tests --workspace --all-targets --no-run -q --quiet --release "
    "--all-features --benches --examples --doc",
    {
        "-p": _ANY,
        "--package": _ANY,
        "--features": _ANY,
        "-F": _ANY,
        "--test": _ANY,
        "--bin": _ANY,
        "--example": _ANY,
    },
    positional="any",
    dashdash=False,
)
_CARGO_TEST_ARGS = _spec(
    "--nocapture --ignored --include-ignored --exact -q --quiet --show-output",
    {"--test-threads": _INT},
)
_CARGO_CLIPPY_ARGS = _spec("", {"-D": _ANY, "-W": _ANY, "-A": _ANY}, positional="none")
_GO = {
    "test": _spec(
        "-v -race -short -cover -failfast",
        {"-run": _ANY, "-count": _INT, "-timeout": _ANY},
        positional="project",
        single_dash=True,
    ),
    "vet": _spec("", positional="project", single_dash=True),
}
_CURL = _spec(
    "-s -S -L -I -i -f --fail --compressed --silent --show-error --location --head --include",
    {
        "-H": _ANY,
        "--header": _ANY,
        "-m": _INT,
        "--max-time": _INT,
        "--connect-timeout": _INT,
        "-A": _ANY,
        "--user-agent": _ANY,
    },
    positional="url",
)

#: ``>`` / ``>>`` / ``&>`` and the word after them.
REDIRECT = re.compile(r"(?:\d|&)?>>?\|?\s*(&\d+|[^\s;&|<>]+)?")

#: A ``timeout`` duration: ``5``, ``2.5``, ``10s``, ``1m``.
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")

#: The longest ``sleep`` that counts as waiting rather than hanging the turn.
MAX_SLEEP_SECONDS = 60


class _Paths:
    """The project a command's path arguments are judged against."""

    def __init__(self, workdir: object, home: object, settings: object) -> None:
        self.workdir = workdir
        self.home = home
        self.settings = settings

    def harmless_target(self, path: str) -> bool:
        """A redirect target that changes nothing of the project's."""
        import posixpath

        from snowpea_core.permissions.plan_paths import is_plan_writable

        target = path.strip("'\"")
        if target.startswith("&") and target[1:].isdigit():
            return True
        if not target or any(m in target for m in ("$", "~", "`")) or ".." in target.split("/"):
            return False
        if target == "/dev/null":
            return True
        normal = posixpath.normpath(target)
        if normal.startswith("/tmp/"):
            return True
        return is_plan_writable(
            normal, workdir=self.workdir, home=self.home, settings=self.settings
        )

    def project(self, word: str) -> bool:
        """A path a checker may be pointed at: inside the project, not a plan file.

        A test runner executes the file it is given, so a file plan mode may
        write (``docs/``, a markdown) or anything in /tmp would be code of the
        planner's own making.
        """
        import posixpath

        from snowpea_core.permissions.plan_paths import is_plan_writable

        if not word or any(m in word for m in ("$", "~", "`")) or ".." in word.split("/"):
            return False
        path = word.split("::", 1)[0]  # pytest node ids: tests/x.py::test_y
        if path.startswith("/"):
            if self.workdir is None:
                return False
            root = posixpath.normpath(str(self.workdir))
            if not (posixpath.normpath(path) + "/").startswith(root + "/"):
                return False
        return not is_plan_writable(
            path, workdir=self.workdir, home=self.home, settings=self.settings
        )

    def git_dir(self, word: str) -> bool:
        """``git -C`` may read another folder, but not one plan mode can fill."""
        import posixpath

        from snowpea_core.permissions.plan_paths import is_plan_writable

        if not word or any(m in word for m in ("$", "~", "`")):
            return False
        base = str(self.workdir or "")
        normal = posixpath.normpath(posixpath.join(base, word))
        if normal == "/tmp" or normal.startswith("/tmp/"):
            return False
        return not is_plan_writable(
            normal, workdir=self.workdir, home=self.home, settings=self.settings
        )


def _value_ok(kind: str, value: str, paths: _Paths) -> bool:
    if kind == _ANY:
        return True
    if kind == _INT:
        return value.isdigit()
    if kind == "project":
        return paths.project(value)
    if kind.startswith("choice:"):
        return value in kind[len("choice:") :].split(",")
    return False


def _args_ok(spec: Spec, args: list[str], paths: _Paths) -> bool:
    """Every flag of ``args`` is one ``spec`` names, every value and operand passes."""
    positionals: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            if not spec.dashdash:
                return False
            # What follows `--` is an operand only to a parser that honours
            # it: `find -- . -delete` still deletes and `go test -- -exec`
            # hands the flag on, so a dashed word after it is not judged.
            tail = args[index + 1 :]
            if any(item.startswith("-") for item in tail):
                return False
            positionals.extend(tail)
            break
        if word == "-" or not word.startswith("-"):
            positionals.append(word)
            index += 1
            continue
        if word.startswith("--") or spec.single_dash:
            name, eq, value = word.partition("=")
            if name in spec.bare and not eq:
                index += 1
                continue
            kind = spec.valued.get(name)
            if kind is None:
                return False
            if kind == "pair":
                if eq or index + 2 >= len(args):
                    return False
                index += 3
                continue
            if not eq:
                if index + 1 >= len(args):
                    return False
                index += 1
                value = args[index]
            if not _value_ok(kind, value, paths):
                return False
            index += 1
            continue
        if spec.numeric and word[1:].isdigit():
            index += 1
            continue
        if word in spec.bare:
            index += 1
            continue
        # A cluster of short flags: -la, -rn, -A3, -n5.
        for at in range(1, len(word)):
            flag = "-" + word[at]
            kind = spec.valued.get(flag)
            if kind is not None and kind != "pair":
                value = word[at + 1 :].removeprefix("=")
                if not value:
                    if index + 1 >= len(args):
                        return False
                    index += 1
                    value = args[index]
                if not _value_ok(kind, value, paths):
                    return False
                break
            if flag not in spec.bare:
                return False
        index += 1
    if spec.max_positional is not None and len(positionals) > spec.max_positional:
        return False
    if spec.positional == "none":
        return not positionals
    if spec.positional == "project":
        return all(paths.project(word) for word in positionals)
    if spec.positional == "url":
        return bool(positionals) and all(
            word.startswith(("http://", "https://")) for word in positionals
        )
    if spec.positional == "plus":
        return all(word.startswith("+") for word in positionals)
    return True


def _git_ok(rest: list[str], paths: _Paths) -> bool:
    words = list(rest)
    while words and words[0].startswith("-"):
        if words[0] in ("--no-pager", "-P", "--no-optional-locks"):
            words = words[1:]
        elif words[0] == "-C" and len(words) > 1 and paths.git_dir(words[1]):
            words = words[2:]
        else:
            return False  # -c, --git-dir, --work-tree, --exec-path …
    if not words or words[0] not in GIT_READ:
        return False
    sub, args = words[0], words[1:]
    positional = [word for word in args if not word.startswith("-")]
    if sub == "branch":
        listing = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--show-current"}
        return not positional and all(word in listing for word in args)
    if sub == "tag":
        return not args or (args[0] in ("-l", "--list") and _args_ok(_GIT_DEFAULT, args, paths))
    if sub == "remote":
        return not args or args == ["-v"]
    if sub == "stash":
        return args[:1] == ["list"] and len(args) == 1
    if sub == "config":
        return bool(args) and args[0] in ("--get", "--get-all", "--list", "-l") and len(args) <= 2
    if sub == "worktree":
        return args == ["list"]
    return _args_ok(GIT_SPECS.get(sub, _GIT_DEFAULT), args, paths)


def _package_ok(head: str, rest: list[str]) -> bool:
    """``npm test``, ``pnpm lint``, ``yarn run typecheck``, ``npm ls``: no flags but --silent."""
    if "--" in rest:
        return False
    if any(word.startswith("-") and word not in ("--silent", "-s") for word in rest):
        return False
    sub = list(rest)
    if not sub:
        return False
    if sub[0] in ("ls", "list", "outdated", "view", "info", "why"):
        return True
    if sub[0] in ("test", "t"):
        return len(sub) == 1
    if sub[0] == "run":
        return len(sub) == 2 and bool(CHECK_SCRIPT.match(sub[1]))
    return head != "npm" and len(sub) == 1 and bool(CHECK_SCRIPT.match(sub[0]))


def _checker_ok(tool: str, rest: list[str], paths: _Paths) -> bool:
    entry = CHECKERS.get(tool)
    if entry is None:
        return False
    spec, required = entry
    if tool == "vitest":
        if not rest or rest[0] not in ("run", "list"):
            return False  # a bare `vitest` watches forever
        rest = rest[1:]
    if required and not any(flag in rest for flag in required):
        return False
    return _args_ok(spec, rest, paths)


def _python_ok(rest: list[str], paths: _Paths) -> bool:
    if rest in (["--version"], ["-V"]):
        return True
    if len(rest) < 2 or rest[0] != "-m":
        return False  # -c, a script, or interpreter flags: arbitrary code
    module, args = rest[1], rest[2:]
    if module == "pytest":
        return _args_ok(_PYTEST, args, paths)
    if module == "unittest":
        return _args_ok(_UNITTEST, args, paths)
    if module == "mypy":
        return _args_ok(_MYPY, args, paths)
    if module == "ruff":
        return _ruff_ok(args, paths)
    return False


def _ruff_ok(args: list[str], paths: _Paths) -> bool:
    if args == ["--version"]:
        return True
    if not args:
        return False
    if args[0] == "check":
        return _args_ok(_RUFF_CHECK, args[1:], paths)
    if args[0] == "format":
        rest = args[1:]
        return ("--check" in rest or "--diff" in rest) and _args_ok(_RUFF_FORMAT, rest, paths)
    return False


def _cargo_ok(rest: list[str], paths: _Paths) -> bool:
    if not rest or rest[0] not in ("test", "check", "clippy"):
        return False
    sub, args = rest[0], rest[1:]
    tail: list[str] = []
    if "--" in args:
        cut = args.index("--")
        args, tail = args[:cut], args[cut + 1 :]
    if not _args_ok(_CARGO, args, paths):
        return False
    if not tail:
        return True
    if sub == "test":
        return _args_ok(_CARGO_TEST_ARGS, tail, paths)
    if sub == "clippy":
        return _args_ok(_CARGO_CLIPPY_ARGS, tail, paths)
    return False


def _normal_head(word: str) -> str | None:
    """The program name, or ``None`` when the head is not a plain name or /usr/bin/X."""
    import posixpath

    if "/" not in word:
        return word
    normal = posixpath.normpath(word)
    if normal != word or posixpath.dirname(normal) not in ("/usr/bin", "/bin"):
        return None
    return posixpath.basename(normal)


def _segment_ok(words: list[str], paths: _Paths) -> bool:
    """True when one command (already split) only reads."""
    words = _strip_runner(words)
    if not words or "=" in words[0]:
        return False  # FOO=1 cmd: LD_PRELOAD and NODE_OPTIONS are code too
    head = _normal_head(words[0])
    if head is None:
        return False
    rest = words[1:]
    if head in ("time", "command"):
        # Unwrapped only with no flags of their own (`command -p` changes PATH).
        return bool(rest) and not rest[0].startswith("-") and _segment_ok(rest, paths)
    if head == "timeout":
        return len(rest) > 1 and bool(_DURATION.match(rest[0])) and _segment_ok(rest[1:], paths)
    if head == "sleep":
        return (
            len(rest) == 1
            and bool(_DURATION.match(rest[0]))
            and rest[0][-1] in "0123456789s"
            and float(rest[0].rstrip("s")) <= MAX_SLEEP_SECONDS
        )
    if head == "ps":
        return all(re.fullmatch(r"-?[A-Za-z]+|[\w,=%:-]+", word) for word in rest)
    spec = SPECS.get(head)
    if spec is not None:
        return _args_ok(spec, rest, paths)
    if head == "git":
        return _git_ok(rest, paths)
    if head in ("npm", "pnpm", "yarn", "bun"):
        if head in ("pnpm", "bun") and rest[:1] == ["exec"] and len(rest) > 1:
            return _checker_ok(rest[1], rest[2:], paths)
        return _package_ok(head, rest)
    if head in ("npx", "bunx"):
        rest = [word for word in rest if word not in ("-y", "--yes")]
        return bool(rest) and _checker_ok(rest[0], rest[1:], paths)
    if head in CHECKERS:
        return _checker_ok(head, rest, paths)
    if head in ("python", "python3"):
        return _python_ok(rest, paths)
    if head == "pytest":
        return _args_ok(_PYTEST, rest, paths)
    if head == "mypy":
        return _args_ok(_MYPY, rest, paths)
    if head == "ruff":
        return _ruff_ok(rest, paths)
    if head == "cargo":
        return _cargo_ok(rest, paths)
    if head == "go":
        return bool(rest) and rest[0] in _GO and _args_ok(_GO[rest[0]], rest[1:], paths)
    if head == "curl":
        return _args_ok(_CURL, rest, paths)
    if head == "node":
        return rest in (["--version"], ["-v"])
    return False


def is_mutating(
    command: str, *, workdir: object = None, home: object = None, settings: object = None
) -> bool:
    """True unless every part of ``command`` is a known inspection (1.32.0).

    Used only by plan mode's "Allow Everything": a command for which this is
    ``False`` runs unasked, everything else still asks.  Any ``$``, backtick,
    heredoc, process substitution or backgrounding makes the line
    unjudgeable; a redirection is fine only into /dev/null, /tmp or a file
    plan mode may write.
    """
    text = (command or "").strip()
    if not text:
        return True
    if any(mark in text for mark in ("`", "$", "<<", "\n", "<(", ">(")):
        return True
    if "&" in SEPARATORS.sub(" ", REDIRECT.sub(" ", text)):
        return True
    paths = _Paths(workdir, home, settings)
    for match in REDIRECT.finditer(text):
        if not paths.harmless_target(match.group(1) or ""):
            return True
    for segment in (part.strip() for part in SEPARATORS.split(REDIRECT.sub(" ", text))):
        if not segment:
            return True
        try:
            words = shlex.split(segment)
        except ValueError:
            return True
        if not _segment_ok(words, paths):
            return True
    return False


__all__ = [
    "BANNED_ARGS",
    "FORBIDDEN_SUBSTRINGS",
    "GIT_READ",
    "SPECS",
    "Spec",
    "NEVER_SAFE",
    "RUNNER_PREFIXES",
    "SAFE_COMMANDS",
    "SAFE_PREFIXES",
    "is_mutating",
    "is_read_only",
]
