"""The exact option tables behind Jarvis mode's ``safe`` and read-like shell lines.

Security review 3 (2026-11-16) replaced the old "reader minus a deny-list"
check with this table.  A shell line is judged command by command; each one
is one of:

``safe``
    Every word is known: the program is a key of :data:`TABLE`, every option
    is in its exact allowlist (short options letter by letter, long options
    only by their full name), every value is validated, and every operand
    passes the caller's path check.  Only these may become ``safe`` (allowed
    by the engine alone), and only for a snowpea-native shell (see
    :mod:`snowpea_core.jarvis.classify`).
``readlike``
    Read-only by the table's knowledge, but not eligible for ``safe``: an
    option listed only as read-like (``grep -r``, ``ls -R``, ``find -L``), a
    program listed only as read-like (``rg``, ``jq``, ``seq``), a glob
    operand, an expansion in ``echo``/``printf``, or a read-only ``git`` form
    (:data:`GIT`).  Jarvis may allow these once; nobody allows them unasked.
``None``
    Anything else: an unknown program or option, an abbreviated or attached
    form the table does not list, a value that fails its check, an operand
    outside the project.

Nothing here is a deny-list: an option is accepted only when it is listed.
Options whose value is a file (``-f FILE``, ``--from-file``, ``--files0-from``,
magic files, order files, ``--rawfile``) are simply not listed.

The module is pure: it never reads the file system itself (the caller's
``path`` callback does) and never runs anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

Level = Literal["safe", "readlike"]

#: ``path(raw, cwd_word_is_dir_ok) -> "safe" | "readlike" | None``; supplied by the classifier.
PathCheck = Callable[[str, bool], "Level | None"]

#: A value check: a compiled pattern the whole value must match.
Value = re.Pattern[str]

INT = re.compile(r"[+-]?\d{1,12}[A-Za-z]{0,3}")
NUM = re.compile(r"[+-]?\d{1,12}(\.\d{1,6})?")
#: Any literal text (the caller has already refused expansions).
TEXT = re.compile(r"[^\x00]*", re.DOTALL)
#: A command or user name (``which ls``, ``id root``).
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}")


def _choices(*words: str) -> Value:
    return re.compile("|".join(re.escape(word) for word in words))


COLOR = _choices("never", "auto", "always")


@dataclass(frozen=True)
class Spec:
    """One program's exact option allowlist."""

    #: Short option letters that take no value.
    short: str = ""
    #: Short option letters that take a value (attached ``-n5`` or next word).
    short_valued: Mapping[str, Value] = field(default_factory=dict)
    #: Long options without a value, by their full name.
    long: frozenset[str] = frozenset()
    #: Long options with a required value (``--name=value`` or ``--name value``).
    long_valued: Mapping[str, Value] = field(default_factory=dict)
    #: Long options with an optional value: only ``--name`` or ``--name=value``.
    long_optional: Mapping[str, Value] = field(default_factory=dict)
    #: Options that make the command only read-like (same shapes as above).
    readlike_short: str = ""
    readlike_long: frozenset[str] = frozenset()
    #: What the operands are: ``files`` (paths read), ``meta`` (paths whose
    #: metadata is shown), ``text`` (literal text, never a file), ``names``,
    #: ``none``, ``pattern+files`` (grep), ``filter+files`` (jq), ``numbers``,
    #: ``date``.
    operands: str = "files"
    #: Most operands (``uniq IN OUT`` writes OUT, so uniq takes one).
    max_operands: int | None = None
    #: ``head -20`` / ``grep -3``: a bare ``-NUMBER`` option is allowed.
    numeric: bool = False
    #: With no operand, the command reads this (``tree`` -> ``.``).
    default_operand: str | None = None
    #: Every use is read-like at best (the program is not on the ``safe`` list).
    readlike_only: bool = False
    #: A directory operand makes it read-like (``diff a b`` compares the files in them).
    directory_readlike: bool = False


_HASH = Spec(short="btz", long=frozenset({"--binary", "--text", "--zero", "--tag"}))

_GREP = Spec(
    short="iynHhclLoqsvwxEFGPaIbZzTU",
    short_valued={"m": INT, "A": INT, "B": INT, "C": INT, "e": TEXT},
    long=frozenset(
        {"--ignore-case", "--no-ignore-case", "--line-number", "--with-filename",
         "--no-filename", "--count", "--files-with-matches", "--files-without-match",
         "--only-matching", "--quiet", "--silent", "--no-messages", "--invert-match",
         "--word-regexp", "--line-regexp", "--extended-regexp", "--fixed-strings",
         "--basic-regexp", "--perl-regexp", "--text", "--byte-offset", "--null",
         "--null-data", "--initial-tab", "--binary", "--line-buffered"}
    ),
    long_valued={
        "--max-count": INT, "--after-context": INT, "--before-context": INT, "--context": INT,
        "--regexp": TEXT, "--include": TEXT, "--exclude": TEXT, "--exclude-dir": TEXT,
        "--label": TEXT,
    },
    long_optional={"--color": COLOR, "--colour": COLOR},
    readlike_short="r",
    readlike_long=frozenset({"--recursive"}),
    operands="pattern+files",
    numeric=True,
)

#: Programs a ``safe`` / read-like line may run, with their exact options.
TABLE: dict[str, Spec] = {
    # -- no paths ------------------------------------------------------------
    "pwd": Spec(short="LP", operands="none"),
    "true": Spec(operands="none"),
    "false": Spec(operands="none"),
    "whoami": Spec(operands="none"),
    "arch": Spec(operands="none"),
    "id": Spec(short="ugGnrz", operands="names"),
    "groups": Spec(operands="names"),
    "uname": Spec(
        short="asnrvmpio",
        long=frozenset({"--all", "--kernel-name", "--nodename", "--kernel-release",
                        "--kernel-version", "--machine", "--processor", "--hardware-platform",
                        "--operating-system"}),
        operands="none",
    ),
    "nproc": Spec(long=frozenset({"--all"}), operands="none"),
    "uptime": Spec(short="ps", long=frozenset({"--pretty", "--since"}), operands="none"),
    "date": Spec(
        short="uR",
        short_valued={"d": TEXT},
        long=frozenset({"--utc", "--universal", "--rfc-email"}),
        long_valued={"--date": TEXT},
        operands="date",
        max_operands=1,
    ),
    "hostname": Spec(
        short="sfiIdA",
        long=frozenset({"--short", "--fqdn", "--long", "--ip-address", "--all-ip-addresses",
                        "--domain", "--all-fqdns"}),
        operands="none",
    ),
    "which": Spec(short="a", operands="names"),
    "type": Spec(short="atpP", operands="names"),
    "basename": Spec(short="az", short_valued={"s": TEXT},
                     long=frozenset({"--multiple", "--zero"}),
                     long_valued={"--suffix": TEXT}, operands="text"),
    "dirname": Spec(short="z", long=frozenset({"--zero"}), operands="text"),
    "tty": Spec(short="s", long=frozenset({"--silent", "--quiet"}), operands="none"),
    "locale": Spec(short="am", operands="none"),
    "getconf": Spec(short="a", operands="names"),
    "tr": Spec(short="dscCt",
               long=frozenset({"--delete", "--squeeze-repeats", "--complement",
                               "--truncate-set1"}),
               operands="text"),
    # -- metadata (names, sizes, kinds) ------------------------------------
    "ls": Spec(
        short="aAlh1dFptrSisngoGcuUXvCmxQNkB",
        long=frozenset({"--all", "--almost-all", "--human-readable", "--classify",
                        "--directory", "--reverse", "--size", "--inode", "--numeric-uid-gid",
                        "--no-group", "--full-time", "--group-directories-first", "--escape",
                        "--quote-name", "--literal", "--si", "--ignore-backups",
                        "--file-type"}),
        long_valued={
            "--sort": _choices("none", "size", "time", "version", "extension", "width"),
            "--time": _choices("atime", "access", "use", "ctime", "status", "birth",
                               "creation", "mtime", "modification"),
            "--format": _choices("across", "commas", "horizontal", "long", "single-column",
                                 "verbose", "vertical"),
            "--indicator-style": _choices("none", "slash", "file-type", "classify"),
        },
        long_optional={"--color": COLOR},
        readlike_short="R",
        readlike_long=frozenset({"--recursive"}),
        operands="meta",
        default_operand=".",
    ),
    "stat": Spec(short="tf", short_valued={"c": TEXT},
                 long=frozenset({"--terse", "--file-system"}),
                 long_valued={"--format": TEXT, "--printf": TEXT}, operands="meta"),
    "realpath": Spec(short="emsqz",
                     long=frozenset({"--canonicalize-existing", "--canonicalize-missing",
                                     "--no-symlinks", "--strip", "--quiet", "--zero"}),
                     operands="meta"),
    "readlink": Spec(short="femnqsvz",
                     long=frozenset({"--canonicalize", "--canonicalize-existing",
                                     "--canonicalize-missing", "--no-newline", "--quiet",
                                     "--silent", "--verbose", "--zero"}),
                     operands="meta"),
    "file": Spec(short="bikh",
                 long=frozenset({"--brief", "--mime", "--mime-type", "--mime-encoding",
                                 "--keep-going", "--no-dereference"}),
                 operands="files"),
    "du": Spec(short="shacbkmx", short_valued={"d": INT},
               long=frozenset({"--summarize", "--human-readable", "--all", "--total",
                               "--bytes", "--apparent-size", "--one-file-system", "--si"}),
               long_valued={"--max-depth": INT}, operands="meta", default_operand="."),
    "tree": Spec(short="adfisughDpFCnNQ", short_valued={"L": INT, "I": TEXT, "P": TEXT},
                 long=frozenset({"--noreport", "--dirsfirst"}), operands="meta",
                 default_operand="."),
    "df": Spec(short="hHkTilaP",
               long=frozenset({"--human-readable", "--si", "--print-type", "--inodes",
                               "--local", "--all", "--portability", "--total"}),
               operands="meta"),
    # -- contents ----------------------------------------------------------
    "cat": Spec(short="AbeEnstTuv",
                long=frozenset({"--show-all", "--number-nonblank", "--number",
                                "--squeeze-blank", "--show-ends", "--show-tabs",
                                "--show-nonprinting"})),
    "tac": Spec(short="b", long=frozenset({"--before"})),
    "head": Spec(short="qvz", short_valued={"n": INT, "c": INT},
                 long=frozenset({"--quiet", "--silent", "--verbose", "--zero-terminated"}),
                 long_valued={"--lines": INT, "--bytes": INT}, numeric=True),
    "tail": Spec(short="qvz", short_valued={"n": INT, "c": INT},
                 long=frozenset({"--quiet", "--silent", "--verbose", "--zero-terminated"}),
                 long_valued={"--lines": INT, "--bytes": INT}, numeric=True),
    "wc": Spec(short="cmlwL",
               long=frozenset({"--bytes", "--chars", "--lines", "--words",
                               "--max-line-length"})),
    "nl": Spec(short_valued={"b": _choices("a", "t", "n"), "n": _choices("ln", "rn", "rz"),
                             "w": INT, "v": INT, "i": INT}),
    "md5sum": _HASH, "sha1sum": _HASH, "sha224sum": _HASH, "sha256sum": _HASH,
    "sha384sum": _HASH, "sha512sum": _HASH, "b2sum": _HASH,
    "cksum": Spec(),
    "cmp": Spec(short="bls", short_valued={"n": INT},
                long=frozenset({"--print-bytes", "--verbose", "--quiet", "--silent"}),
                long_valued={"--bytes": INT}, max_operands=2, directory_readlike=True),
    "diff": Spec(
        short="uqsywbBiaNcEZtTp",
        short_valued={"U": INT, "C": INT, "W": INT},
        long=frozenset({"--brief", "--report-identical-files", "--side-by-side",
                        "--ignore-all-space", "--ignore-space-change", "--ignore-blank-lines",
                        "--ignore-case", "--text", "--new-file", "--unidirectional-new-file",
                        "--expand-tabs", "--initial-tab", "--strip-trailing-cr", "--minimal",
                        "--suppress-common-lines", "--ignore-tab-expansion",
                        "--ignore-trailing-space", "--show-c-function", "--left-column",
                        "--normal"}),
        long_valued={"--width": INT},
        long_optional={"--unified": INT, "--context": INT, "--color": COLOR},
        # No ``-r``: ``diff -r`` follows symlinks inside the trees it walks (N9).
        max_operands=2,
        directory_readlike=True,
    ),
    "cut": Spec(short="snz", short_valued={"b": TEXT, "c": TEXT, "f": TEXT, "d": TEXT},
                long=frozenset({"--only-delimited", "--complement", "--zero-terminated"}),
                long_valued={"--bytes": TEXT, "--characters": TEXT, "--fields": TEXT,
                             "--delimiter": TEXT, "--output-delimiter": TEXT}),
    "column": Spec(short="tx", short_valued={"s": TEXT, "c": INT}),
    "fold": Spec(short="sb", short_valued={"w": INT},
                 long=frozenset({"--spaces", "--bytes"}), long_valued={"--width": INT}),
    "rev": Spec(),
    "comm": Spec(short="123z",
                 long=frozenset({"--check-order", "--nocheck-order", "--total",
                                 "--zero-terminated"}),
                 long_valued={"--output-delimiter": TEXT}, max_operands=2),
    "paste": Spec(short="sz", short_valued={"d": TEXT},
                  long=frozenset({"--serial", "--zero-terminated"}),
                  long_valued={"--delimiters": TEXT}),
    "expand": Spec(short="i", short_valued={"t": TEXT}, long=frozenset({"--initial"}),
                   long_valued={"--tabs": TEXT}),
    "unexpand": Spec(short="a", short_valued={"t": TEXT},
                     long=frozenset({"--all", "--first-only"}), long_valued={"--tabs": TEXT}),
    "od": Spec(short="bcdfiloxsv",
               short_valued={"A": _choices("d", "o", "x", "n"),
                             "t": re.compile(r"[A-Za-z0-9]{1,8}"), "N": INT, "j": INT,
                             "w": INT},
               long=frozenset({"--output-duplicates"}),
               long_valued={"--address-radix": _choices("d", "o", "x", "n"),
                            "--format": re.compile(r"[A-Za-z0-9]{1,8}"),
                            "--read-bytes": INT, "--skip-bytes": INT},
               long_optional={"--width": INT}),
    "hexdump": Spec(short="bcCdoxv", short_valued={"n": INT, "s": INT}),
    "strings": Spec(short="a", short_valued={"n": INT, "t": _choices("o", "d", "x")},
                    long=frozenset({"--all"}),
                    long_valued={"--bytes": INT, "--radix": _choices("o", "d", "x")},
                    numeric=True),
    "grep": _GREP,
    "egrep": _GREP,
    "fgrep": _GREP,
    "sort": Spec(
        short="bdfgiMhnRrVsucmz",
        short_valued={"k": TEXT, "t": TEXT},
        long=frozenset({"--ignore-leading-blanks", "--dictionary-order", "--ignore-case",
                        "--general-numeric-sort", "--ignore-nonprinting", "--month-sort",
                        "--human-numeric-sort", "--numeric-sort", "--random-sort", "--reverse",
                        "--version-sort", "--check", "--merge", "--stable", "--unique",
                        "--zero-terminated"}),
        long_valued={"--key": TEXT, "--field-separator": TEXT},
    ),
    "uniq": Spec(short="cdDuiz", short_valued={"f": INT, "s": INT, "w": INT},
                 long=frozenset({"--count", "--repeated", "--unique", "--ignore-case",
                                 "--zero-terminated"}),
                 long_valued={"--skip-fields": INT, "--skip-chars": INT, "--check-chars": INT},
                 max_operands=1),
    # -- read-like only ------------------------------------------------------
    "seq": Spec(short="w", short_valued={"s": TEXT, "f": TEXT},
                long=frozenset({"--equal-width"}),
                long_valued={"--separator": TEXT, "--format": TEXT},
                operands="numbers", max_operands=3, readlike_only=True),
    "rg": Spec(
        short="inHcloqsvwxFPaUSN0",
        short_valued={"m": INT, "A": INT, "B": INT, "C": INT, "e": TEXT, "g": TEXT, "t": TEXT,
                      "T": TEXT, "M": INT, "d": INT},
        long=frozenset({"--files", "--files-with-matches", "--files-without-match", "--count",
                        "--count-matches", "--line-number", "--no-line-number",
                        "--ignore-case", "--smart-case", "--case-sensitive",
                        "--fixed-strings", "--word-regexp", "--line-regexp", "--invert-match",
                        "--only-matching", "--hidden", "--no-ignore", "--no-heading",
                        "--heading", "--with-filename", "--no-filename", "--vimgrep",
                        "--json", "--stats", "--null", "--quiet", "--multiline", "--trim",
                        "--column", "--byte-offset", "--no-config", "--no-messages"}),
        long_valued={"--glob": TEXT, "--iglob": TEXT, "--type": TEXT, "--type-not": TEXT,
                     "--max-count": INT, "--max-depth": INT, "--context": INT,
                     "--after-context": INT, "--before-context": INT, "--regexp": TEXT,
                     "--sort": _choices("none", "path", "modified", "accessed", "created"),
                     "--sortr": _choices("none", "path", "modified", "accessed", "created"),
                     "--max-filesize": TEXT, "--max-columns": INT},
        long_optional={"--color": COLOR},
        operands="pattern+files",
        readlike_only=True,
    ),
    "jq": Spec(
        short="rjcnesSaCM",
        long=frozenset({"--raw-output", "--join-output", "--compact-output", "--null-input",
                        "--exit-status", "--slurp", "--sort-keys", "--ascii-output", "--tab",
                        "--color-output", "--monochrome-output", "--raw-input"}),
        long_valued={"--indent": INT},
        operands="filter+files",
        readlike_only=True,
    ),
}

#: ``find`` primaries that take one value: what it may be (``file`` = a path in the project).
FIND_VALUED: dict[str, Value | str] = {
    "-name": TEXT, "-iname": TEXT, "-path": TEXT, "-ipath": TEXT, "-wholename": TEXT,
    "-iwholename": TEXT, "-regex": TEXT, "-iregex": TEXT, "-lname": TEXT, "-ilname": TEXT,
    "-type": re.compile(r"[bcdpflsD](,[bcdpflsD])*"),
    "-xtype": re.compile(r"[bcdpflsD](,[bcdpflsD])*"),
    "-maxdepth": INT, "-mindepth": INT, "-size": re.compile(r"[+-]?\d{1,12}[bcwkMG]?"),
    "-perm": re.compile(r"[-/]?[0-7ugoarwxXst,+=-]{1,32}"), "-user": NAME, "-group": NAME,
    "-uid": INT, "-gid": INT, "-mtime": INT, "-mmin": INT, "-atime": INT, "-amin": INT,
    "-ctime": INT, "-cmin": INT, "-used": INT, "-inum": INT, "-links": INT,
    "-newer": "file", "-anewer": "file", "-cnewer": "file", "-samefile": "file",
    "-printf": TEXT, "-regextype": NAME,
}
FIND_FLAGS: frozenset[str] = frozenset(
    {"-print", "-print0", "-ls", "-prune", "-quit", "-empty", "-true", "-false", "-not", "-a",
     "-and", "-o", "-or", "-depth", "-d", "-mount", "-xdev", "-noleaf", "-readable",
     "-writable", "-executable", "-nouser", "-nogroup", "-daystart",
     "-ignore_readdir_race", "-noignore_readdir_race", "(", ")", "!", ","}
)
#: ``find`` words that follow symlinks: the listing may leave the project (read-like).
FIND_READLIKE: frozenset[str] = frozenset({"-follow"})
FIND_READLIKE_LEADING: frozenset[str] = frozenset({"-L", "-H"})


# ---------------------------------------------------------------------------
# git: read-like only (repo-controlled config, attributes, pagers and diff
# drivers can make a read-only git command run code)
# ---------------------------------------------------------------------------

_GIT_DIFF_SHORT = "puswbRzW"
_GIT_DIFF_SHORT_VALUED: dict[str, Value] = {"n": INT, "U": INT}
_GIT_DIFF_LONG: frozenset[str] = frozenset(
    {"--oneline", "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
     "--graph", "--decorate", "--no-decorate", "--all", "--branches", "--tags", "--remotes",
     "--no-color", "--abbrev-commit", "--no-abbrev-commit", "--no-abbrev", "--patch",
     "--no-patch", "--cached", "--staged", "--reverse", "--first-parent", "--no-merges",
     "--merges", "--follow", "--summary", "--check", "--relative", "--no-relative",
     "--ignore-all-space", "--ignore-space-change", "--ignore-blank-lines",
     "--ignore-space-at-eol", "--ignore-cr-at-eol", "--minimal", "--patience", "--histogram",
     "--raw", "--full-index", "--binary", "--no-ext-diff", "--no-textconv", "--left-right",
     "--cherry-pick", "--cherry-mark", "--cherry", "--source", "--compact-summary",
     "--exit-code", "--quiet", "--no-renames", "--find-copies-harder", "--topo-order",
     "--date-order", "--author-date-order", "--boundary", "--simplify-by-decoration",
     "--all-match", "--invert-grep", "--regexp-ignore-case", "--basic-regexp",
     "--extended-regexp", "--fixed-strings", "--perl-regexp", "--full-history",
     "--ancestry-path", "--no-walk", "--walk-reflogs", "--parents", "--children",
     "--no-prefix", "--no-mailmap", "--use-mailmap", "--mailmap", "--no-notes",
     "--expand-tabs", "--no-expand-tabs", "--count", "--remerge-diff", "--cc",
     "--irreversible-delete", "--function-context", "--indent-heuristic",
     "--no-indent-heuristic", "--no-color-moved", "--merge-base", "--word-diff",
     "--color-words"}
)
_GIT_DIFF_LONG_VALUED: dict[str, Value] = {
    "--format": TEXT, "--pretty": TEXT, "--since": TEXT, "--until": TEXT, "--after": TEXT,
    "--before": TEXT, "--author": TEXT, "--committer": TEXT, "--grep": TEXT,
    "--max-count": INT, "--skip": INT, "--date": TEXT, "--diff-filter": TEXT,
    "--unified": INT, "--diff-algorithm": TEXT, "--word-diff-regex": TEXT,
    "--src-prefix": TEXT, "--dst-prefix": TEXT, "--line-prefix": TEXT,
    "--inter-hunk-context": INT, "--stat-width": INT, "--stat-name-width": INT,
    "--stat-count": INT, "--stat-graph-width": INT, "--encoding": TEXT,
    "--decorate-refs": TEXT, "--anchored": TEXT, "--diff-merges": TEXT,
}
_GIT_DIFF_LONG_OPTIONAL: dict[str, Value] = {
    "--color": COLOR, "--abbrev": INT, "--find-renames": INT, "--find-copies": INT,
    "--dirstat": TEXT, "--submodule": _choices("short", "log", "diff"),
    "--ignore-submodules": _choices("none", "untracked", "dirty", "all"),
    "--break-rewrites": TEXT, "--color-moved": TEXT, "--notes": TEXT, "--show-notes": TEXT,
    "--decorate": _choices("short", "full", "auto", "no"),
}

_GIT_DIFF = Spec(short=_GIT_DIFF_SHORT, short_valued=_GIT_DIFF_SHORT_VALUED,
                 long=_GIT_DIFF_LONG, long_valued=_GIT_DIFF_LONG_VALUED,
                 long_optional=_GIT_DIFF_LONG_OPTIONAL, operands="revs", numeric=True)

#: Read-only git subcommands and their exact options (all read-like at best).
GIT: dict[str, Spec] = {
    "status": Spec(
        short="sbzv",
        long=frozenset({"--short", "--branch", "--long", "--verbose", "--no-column",
                        "--ahead-behind", "--no-ahead-behind", "--renames", "--no-renames",
                        "--show-stash", "--null"}),
        long_optional={
            "--untracked-files": _choices("no", "normal", "all"),
            "--ignored": _choices("traditional", "no", "matching"),
            "--ignore-submodules": _choices("none", "untracked", "dirty", "all"),
            "--porcelain": _choices("v1", "v2"),
            "--column": TEXT, "--find-renames": INT,
        },
        operands="paths",
    ),
    "diff": _GIT_DIFF,
    "log": _GIT_DIFF,
    "show": _GIT_DIFF,
    "shortlog": Spec(short="nsec",
                     long=frozenset({"--numbered", "--summary", "--email", "--all",
                                     "--no-merges"}),
                     long_valued={"--since": TEXT, "--until": TEXT, "--format": TEXT},
                     operands="revs"),
    "blame": Spec(short="ltsenwfcp",
                  short_valued={"L": re.compile(r"[0-9]{1,9}(,[+-]?[0-9]{1,9})?")},
                  long=frozenset({"--porcelain", "--line-porcelain", "--incremental",
                                  "--show-name", "--show-number", "--show-email", "--root",
                                  "--show-stats"}),
                  long_valued={"--date": TEXT},
                  long_optional={"--abbrev": INT},
                  operands="revs"),
    "grep": Spec(
        short="nivwlLchHoqEFGPIaz",
        short_valued={"e": TEXT, "A": INT, "B": INT, "C": INT, "m": INT},
        long=frozenset({"--line-number", "--ignore-case", "--invert-match", "--word-regexp",
                        "--files-with-matches", "--files-without-match", "--count",
                        "--full-name", "--cached", "--extended-regexp", "--fixed-strings",
                        "--basic-regexp", "--perl-regexp", "--break", "--heading",
                        "--show-function", "--all-match", "--and", "--or", "--not",
                        "--only-matching", "--quiet", "--text", "--column", "--null"}),
        long_valued={"--max-depth": INT, "--regexp": TEXT, "--max-count": INT,
                     "--context": INT, "--after-context": INT, "--before-context": INT},
        operands="git-grep",
        numeric=True,
    ),
    "ls-files": Spec(short="cdmostuz",
                     long=frozenset({"--cached", "--deleted", "--modified", "--others",
                                     "--stage", "--unmerged", "--full-name",
                                     "--exclude-standard", "--directory",
                                     "--no-empty-directory", "--error-unmatch", "--eol"}),
                     operands="paths"),
    "ls-tree": Spec(short="rdtlz",
                    long=frozenset({"--name-only", "--name-status", "--full-name",
                                    "--full-tree", "--long", "--object-only"}),
                    long_optional={"--abbrev": INT}, operands="revs"),
    "rev-parse": Spec(
        short="q",
        long=frozenset({"--show-toplevel", "--show-prefix", "--show-cdup", "--git-dir",
                        "--absolute-git-dir", "--is-inside-work-tree", "--is-bare-repository",
                        "--is-inside-git-dir", "--verify", "--quiet", "--symbolic-full-name",
                        "--git-common-dir", "--show-superproject-working-tree"}),
        long_optional={"--abbrev-ref": _choices("strict", "loose"), "--short": INT},
        operands="revs",
    ),
    "rev-list": Spec(short_valued={"n": INT},
                     long=frozenset({"--count", "--all", "--reverse", "--first-parent",
                                     "--no-merges", "--merges", "--left-right",
                                     "--cherry-pick", "--topo-order"}),
                     long_valued={"--max-count": INT, "--since": TEXT, "--until": TEXT},
                     operands="revs"),
    "describe": Spec(long=frozenset({"--tags", "--always", "--long", "--exact-match",
                                     "--contains", "--all", "--first-parent"}),
                     long_valued={"--match": TEXT, "--exclude": TEXT, "--candidates": INT},
                     long_optional={"--abbrev": INT}, operands="revs"),
    "merge-base": Spec(long=frozenset({"--is-ancestor", "--all", "--octopus",
                                       "--independent"}), operands="revs"),
    "show-ref": Spec(short="qsd",
                     long=frozenset({"--heads", "--tags", "--head", "--verify", "--quiet",
                                     "--dereference", "--exists"}),
                     long_optional={"--hash": INT, "--abbrev": INT}, operands="revs"),
    "count-objects": Spec(short="vH", long=frozenset({"--verbose", "--human-readable"}),
                          operands="none"),
    "check-ignore": Spec(short="vnq",
                         long=frozenset({"--verbose", "--non-matching", "--quiet"}),
                         operands="paths"),
    "version": Spec(long=frozenset({"--build-options"}), operands="none"),
    "branch": Spec(
        short="arvli",
        long=frozenset({"--all", "--remotes", "--verbose", "--list", "--show-current",
                        "--ignore-case", "--omit-empty", "--no-color", "--no-column",
                        "--no-abbrev"}),
        long_valued={"--contains": TEXT, "--no-contains": TEXT, "--merged": TEXT,
                     "--no-merged": TEXT, "--sort": TEXT, "--format": TEXT,
                     "--points-at": TEXT},
        long_optional={"--color": COLOR, "--column": TEXT, "--abbrev": INT},
        operands="branch-list",
    ),
    "tag": Spec(
        short="li",
        long=frozenset({"--list", "--ignore-case", "--no-column"}),
        long_valued={"--contains": TEXT, "--no-contains": TEXT, "--merged": TEXT,
                     "--no-merged": TEXT, "--sort": TEXT, "--format": TEXT,
                     "--points-at": TEXT},
        long_optional={"--color": COLOR, "--column": TEXT},
        operands="tag-list",
    ),
    "for-each-ref": Spec(long_valued={"--format": TEXT, "--sort": TEXT, "--count": INT,
                                      "--points-at": TEXT, "--merged": TEXT,
                                      "--no-merged": TEXT, "--contains": TEXT},
                         operands="revs"),
    "cat-file": Spec(short="tspe", operands="revs", max_operands=1),
    "remote": Spec(short="v", long=frozenset({"--verbose"}), operands="none"),
    "worktree": Spec(short="v", long=frozenset({"--porcelain", "--verbose"}),
                     operands="worktree-list"),
    "stash": Spec(short="p", long=frozenset({"--stat", "--name-only", "--patch"}),
                  operands="stash-list"),
}
#: Global git options a read-like git line may carry (no ``-C``: the top level only).
GIT_GLOBAL: frozenset[str] = frozenset(
    {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects",
     "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"}
)


# ---------------------------------------------------------------------------
# the option walk
# ---------------------------------------------------------------------------


def _worse(first: Level | None, second: Level | None) -> Level | None:
    if first is None or second is None:
        return None
    return "readlike" if "readlike" in (first, second) else "safe"


@dataclass
class _Parsed:
    level: Level | None = "safe"
    operands: list[str] = field(default_factory=list)
    #: ``grep -e``: the pattern came from an option, so every operand is a file.
    pattern_given: bool = False
    #: ``rg --files``: no pattern at all.
    files_mode: bool = False
    recursive: bool = False


def parse_options(spec: Spec, words: list[str]) -> _Parsed:
    """Walk ``words`` against ``spec``; ``level`` is ``None`` at the first unknown word."""
    parsed = _Parsed()
    index = 0
    only_operands = False
    while index < len(words):
        word = words[index]
        index += 1
        if only_operands or word == "-" or not word.startswith("-"):
            parsed.operands.append(word)
            continue
        if word == "--":
            only_operands = True
            continue
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in spec.long or name in spec.readlike_long:
                if eq:
                    parsed.level = None
                    return parsed
                if name in spec.readlike_long:
                    parsed.level = _worse(parsed.level, "readlike")
                    parsed.recursive = True
                if name == "--files":
                    parsed.files_mode = True
                continue
            if name in spec.long_valued:
                if not eq:
                    if index >= len(words):
                        parsed.level = None
                        return parsed
                    value = words[index]
                    index += 1
                if not spec.long_valued[name].fullmatch(value):
                    parsed.level = None
                    return parsed
                if name == "--regexp":
                    parsed.pattern_given = True
                continue
            if name in spec.long_optional:
                if eq and not spec.long_optional[name].fullmatch(value):
                    parsed.level = None
                    return parsed
                continue
            parsed.level = None  # unknown, abbreviated or misspelt: not on the list
            return parsed
        if spec.numeric and re.fullmatch(r"-\d{1,9}", word):
            continue
        # A cluster of short options, checked letter by letter.
        letters = word[1:]
        position = 0
        while position < len(letters):
            letter = letters[position]
            position += 1
            if letter in spec.short:
                continue
            if letter in spec.readlike_short:
                parsed.level = _worse(parsed.level, "readlike")
                parsed.recursive = True
                continue
            if letter in spec.short_valued:
                value = letters[position:]
                if not value:
                    if index >= len(words):
                        parsed.level = None
                        return parsed
                    value = words[index]
                    index += 1
                if not spec.short_valued[letter].fullmatch(value):
                    parsed.level = None
                    return parsed
                if letter == "e":
                    parsed.pattern_given = True
                break
            parsed.level = None
            return parsed
    return parsed


def judge_command(program: str, rest: list[str], path: PathCheck) -> Level | None:
    """One simple command of a line, by :data:`TABLE` (git and find handled here too)."""
    if program == "find":
        return _find(rest, path)
    if program == "echo":
        return _echo(rest)
    if program == "printf":
        # No option at all (``printf -v NAME`` sets a variable, in any spelling).
        if not rest or rest[0].startswith("-"):
            return None
        return "safe"
    if program in ("test", "["):
        return _test(program, rest, path)
    if program == "sed":
        return _sed(rest, path)
    if program == "xxd":
        return _xxd(rest, path)
    if program == "expr":
        return "safe" if rest and not rest[0].startswith("--") else None
    if program == "git":
        return _git(rest, path)
    spec = TABLE.get(program)
    if spec is None:
        return None
    parsed = parse_options(spec, rest)
    level = parsed.level
    if level is None:
        return None
    if spec.readlike_only:
        level = "readlike"
    operands = list(parsed.operands)
    kind = spec.operands
    if kind == "pattern+files" and not parsed.pattern_given and not parsed.files_mode:
        if not operands:
            return None
        operands = operands[1:]  # the pattern
        kind = "files"
    elif kind == "pattern+files":
        kind = "files"
    if kind == "filter+files":
        if not operands or not _jq_filter_ok(operands[0]):
            return None
        operands = operands[1:]
        kind = "files"
    if spec.max_operands is not None and len(operands) > spec.max_operands:
        return None
    if kind == "none":
        return level if not operands else None
    if kind == "names":
        return level if all(NAME.fullmatch(w) for w in operands) else None
    if kind == "text":
        return level
    if kind == "numbers":
        return level if operands and all(NUM.fullmatch(w) for w in operands) else None
    if kind == "date":
        return level if all(w.startswith("+") for w in operands) else None
    if not operands:
        if spec.default_operand is not None:
            operands = [spec.default_operand]
        elif parsed.recursive and program in ("grep", "egrep", "fgrep", "rg", "diff"):
            operands = ["."]
    if program == "rg" and not operands:
        operands = ["."]  # rg searches the working directory by default
    for word in operands:
        level = _worse(level, path(word, not spec.directory_readlike))
        if level is None:
            return None
    return level


def _echo(rest: list[str]) -> Level | None:
    # Leading -n/-e/-E clusters are options; everything else is text.
    return "safe"


def _test(program: str, rest: list[str], path: PathCheck) -> Level | None:
    words = list(rest)
    if program == "[":
        if not words or words[-1] != "]":
            return None
        words = words[:-1]
    level: Level | None = "safe"
    operators = {"=", "==", "!=", "!", "(", ")", "-a", "-o"}
    for word in words:
        if word in operators or re.fullmatch(r"-[a-zA-Z]{1,2}", word):
            continue
        if "/" in word or word.startswith((".", "~")):
            level = _worse(level, path(word, True))
            if level is None:
                return None
    return level


def _sed(rest: list[str], path: PathCheck) -> Level | None:
    """``sed -n '1,20p' file``: printing line ranges, nothing else."""
    scripts: list[str] = []
    files: list[str] = []
    quiet = False
    index = 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if word in ("-n", "--quiet", "--silent"):
            quiet = True
        elif word in ("-E", "-r", "--regexp-extended"):
            continue
        elif word in ("-e", "--expression"):
            if index >= len(rest):
                return None
            scripts.append(rest[index])
            index += 1
        elif word.startswith("-") and word != "-":
            return None
        elif not scripts:
            scripts.append(word)
        else:
            files.append(word)
    line = r"(\d{1,9}|\$)"
    if not quiet or not scripts or not all(
        re.fullmatch(rf"{line}(,{line})?p", s.strip()) for s in scripts
    ):
        return None
    level: Level | None = "safe"
    for word in files:
        level = _worse(level, path(word, True))
    return level


def _xxd(rest: list[str], path: PathCheck) -> Level | None:
    """xxd matches its options by prefix, so only these exact words count."""
    valued = {"-l", "-c", "-g", "-s", "-o"}
    plain = {"-p", "-ps", "-u", "-b", "-e"}
    operands: list[str] = []
    index = 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if word in plain:
            continue
        if word in valued:
            if index >= len(rest) or not re.fullmatch(r"[+-]?(0x[0-9a-fA-F]{1,12}|\d{1,12})",
                                                      rest[index]):
                return None
            index += 1
            continue
        if word.startswith("-") and word != "-":
            return None
        operands.append(word)
    if len(operands) > 1:
        return None  # the second operand is the output file
    level: Level | None = "safe"
    for word in operands:
        level = _worse(level, path(word, True))
    return level


def _find(rest: list[str], path: PathCheck) -> Level | None:
    level: Level | None = "safe"
    index = 0
    while index < len(rest) and rest[index] in FIND_READLIKE_LEADING | {"-P"}:
        if rest[index] in FIND_READLIKE_LEADING:
            level = "readlike"
        index += 1
    roots: list[str] = []
    while index < len(rest) and not rest[index].startswith(("-", "(", "!", ")")):
        roots.append(rest[index])
        index += 1
    for root in roots or ["."]:
        level = _worse(level, path(root, True))
        if level is None:
            return None
    while index < len(rest):
        word = rest[index]
        index += 1
        if word in FIND_FLAGS:
            continue
        if word in FIND_READLIKE:
            level = _worse(level, "readlike")
            continue
        check = FIND_VALUED.get(word)
        if check is None or index >= len(rest):
            return None
        value = rest[index]
        index += 1
        if check == "file":
            level = _worse(level, path(value, True))
            if level is None:
                return None
        elif not check.fullmatch(value):  # type: ignore[union-attr]
            return None
    return level


#: Words in a jq filter that read the environment, files or modules.
_JQ_REACH = re.compile(
    r"\$?\benv\b|\$ENV|input_filename|\$__loc__|\$__prog|\bimport\b|\binclude\b|"
    r"\bmodulemeta\b|\bget_search_list\b|\binput_line_number\b|\bgetpath\b\s*\(\s*\[\s*\"ENV",
    re.IGNORECASE,
)


def _jq_filter_ok(text: str) -> bool:
    return not _JQ_REACH.search(text)


def _git(rest: list[str], path: PathCheck) -> Level | None:
    index = 0
    while index < len(rest) and rest[index] in GIT_GLOBAL:
        index += 1
    if index >= len(rest):
        return None
    sub = rest[index]
    spec = GIT.get(sub)
    if spec is None:
        return None
    args = rest[index + 1 :]
    if sub in ("worktree", "stash"):
        if not args or args[0] not in ("list", "show") or (sub == "worktree" and args[0] != "list"):
            return None
        tail = args[1:]
        parsed = parse_options(spec, tail)
        if parsed.level is None:
            return None
        if sub == "stash" and args[0] == "list" and (tail or parsed.operands):
            return None
        operands = parsed.operands
        if any(not re.fullmatch(r"stash@\{\d{1,4}\}|\d{1,4}", w) for w in operands):
            return None
        return "readlike"
    parsed = parse_options(spec, args)
    if parsed.level is None:
        return None
    operands = parsed.operands
    kind = spec.operands
    if spec.max_operands is not None and len(operands) > spec.max_operands:
        return None
    if kind == "none" and operands:
        return None
    if kind == "branch-list":
        listing = any(w in ("--list", "-l", "--contains", "--no-contains", "--merged",
                            "--no-merged", "--points-at", "--show-current")
                      or w.startswith(("--contains=", "--no-contains=", "--merged=",
                                       "--no-merged=", "--points-at="))
                      or (w.startswith("-") and not w.startswith("--") and "l" in w)
                      for w in args)
        if operands and not listing:
            return None  # ``git branch name`` creates a branch
    if kind == "tag-list":
        listing = any(w in ("--list", "-l") or (w.startswith("-") and not w.startswith("--")
                                                 and "l" in w) for w in args)
        if operands and not listing:
            return None  # ``git tag name`` creates a tag
    if kind == "git-grep" and not parsed.pattern_given:
        if not operands:
            return None
        operands = operands[1:]
    # Revisions and pathspecs alike: a word without ``:`` must be a path in the
    # project (``HEAD~3`` resolves to one that does not exist, which is fine);
    # ``HEAD:README.md`` names a blob in the repository's history.
    for word in operands:
        if ":" in word and not word.startswith(":("):
            continue
        if path(word, True) is None:
            return None
    return "readlike"


__all__ = ["FIND_FLAGS", "FIND_VALUED", "GIT", "TABLE", "Spec", "judge_command",
           "parse_options"]
