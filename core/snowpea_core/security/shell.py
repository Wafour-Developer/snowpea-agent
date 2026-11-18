"""What a shell line runs and touches, for the security lists.

The classifier's parser (:mod:`snowpea_core.jarvis.classify`) already agrees
with the shell on words, operators, redirections, here-documents and the
wrappers that run another command (``sudo``, ``env``, ``nice``, ``timeout``,
``xargs``, ``find -exec``, package runners).  :class:`_Probe` drives it and,
instead of a verdict, collects:

* every simple command that would run (:class:`Command`), each wrapper and
  each command it wraps, the bodies of ``sh -c``/``bash -c``, ``eval``,
  ``su -c``, ``watch``, ``flock -c``, ``env -S``, here-strings and literal
  here-documents fed to a shell, and every command inside a command
  substitution;
* every path the line writes, deletes, moves or reads (:class:`Effect`);
* why a part of it cannot be seen (:attr:`ShellView.opaque`): a program only
  the shell knows (``$X args``, ``$(echo rm) …``), ``eval`` of a variable, a
  ``-c`` body or a here-document built from variables, a shell reading a pipe
  (``… | sh``, an encoded payload), a line the parser cannot split.

Inline interpreter code (``python -c``, ``node -e``, ``perl -e``, ``ruby -e``,
``php -r``) is analysed by :mod:`snowpea_core.security.code` (security review
5, H4): its destructive and spawning primitives become the commands and
effects they stand for, a non-literal one makes the line opaque; its string
literals that read as command lines are judged too.  A script FILE a line runs
(``bash x.sh``, ``python tool.py``, ``./x``, ``source x``) is read (size-capped):
a shell script through this same walk, other languages through the code
analysis; what cannot be read counts as opaque when the file is recent (the
agent's own), and a script's own opaque parts count only then too.  Every
script read is recorded with its digest (:attr:`ShellView.scripts`), so a
swap between a question and its answer is caught.  Nothing here runs anything.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.jarvis import classify as _c
from snowpea_core.security import code as _code
from snowpea_core.security import envvars as _env
from snowpea_core.security import written

#: Programs whose path operands this module judges itself (the classifier's
#: generic write handling does not say what is deleted or moved).
_OWN_WRITE_PROGRAMS: frozenset[str] = frozenset(
    {"rm", "rmdir", "unlink", "mv", "cp", "ln", "install", "rename", "mmv", "shred", "srm",
     "trash", "trash-put", "gio", "chmod", "chown", "chgrp", "chattr", "setfacl", "truncate",
     "touch", "mkdir", "tee", "rsync"}
)
#: Programs whose writes reach below the folder they name (extraction, ``find -delete``).
_TREE_WRITERS: frozenset[str] = frozenset(
    {"tar", "bsdtar", "gtar", "unzip", "7z", "7za", "unrar", "find", "cpio", "bsdcpio"}
)
#: git subcommands that rewrite the working tree, and those that destroy in it.
_GIT_TREE: frozenset[str] = frozenset(
    {"checkout", "switch", "reset", "clean", "restore", "stash", "merge", "rebase", "pull",
     "cherry-pick", "revert", "am", "apply", "rm", "mv", "read-tree", "checkout-index"}
)
_GIT_DESTROY: frozenset[str] = frozenset({"clean", "reset", "rm", "checkout", "restore"})
#: Options of the write programs that take the NEXT word as their value, per
#: program.  One shared table used to list ``-r`` (``touch -r ref``), so the
#: source of ``cp -r src dst`` and the target of ``rm -r dir`` were swallowed as
#: an option's value and never judged (found in security review 6).
_COPY_VALUED: frozenset[str] = frozenset({"-t", "--target-directory", "-S", "--suffix"})
_VALUED_BY: dict[str, frozenset[str]] = {
    "cp": _COPY_VALUED, "mv": _COPY_VALUED, "ln": _COPY_VALUED,
    "install": _COPY_VALUED | {"-m", "--mode", "-o", "--owner", "-g", "--group"},
    "rsync": frozenset({"-e", "--rsh", "--files-from", "--include", "--exclude", "--filter",
                        "--exclude-from", "--include-from", "-f", "-B", "--block-size",
                        "--max-size", "--min-size", "--timeout", "--port", "--log-file",
                        "--password-file", "--chown", "--chmod", "--compare-dest", "--copy-dest",
                        "--link-dest", "-T", "--temp-dir", "--partial-dir", "--bwlimit",
                        "--out-format", "--backup-dir", "--suffix", "--rsync-path", "-M"}),
    "mkdir": frozenset({"-m", "--mode"}),
    "touch": frozenset({"-r", "--reference", "-d", "--date", "-t"}),
    "truncate": frozenset({"-s", "--size", "-r", "--reference"}),
    "shred": frozenset({"-n", "--iterations", "-s", "--size"}),
    "chmod": frozenset({"--reference"}), "chown": frozenset({"--reference", "--from"}),
    "chgrp": frozenset({"--reference"}),
    "chattr": frozenset({"-v", "-p"}),
    "setfacl": frozenset({"-m", "-x", "-M", "-X", "--modify", "--remove", "--modify-file",
                          "--remove-file", "--set", "--set-file"}),
}
#: Unseen parts that can change the shell's own state: a builtin named by a
#: variable can be ``export HOME=…`` or ``cd``; ``eval`` and ``source`` run
#: text in this very shell (m21, :attr:`ShellView.stateful_shells`).
STATEFUL_REASONS: frozenset[str] = frozenset({"dynamic-program", "eval", "brace-program",
                                              "source", "script-dynamic", "printf-v",
                                              "function"})
#: Script operands that name the shell's stdin or another descriptor it was
#: given (``python3 /dev/fd/3 3< <(curl …)``, security review 4).
_STDIN_FILE = re.compile(r"/dev/stdin|/dev/fd/\d+|/proc/self/fd/\d+")
#: Ids of the shells a walk sees (:attr:`Effect.shell`).
_SHELL_IDS = itertools.count(1)
#: Ids of the simple commands a walk sees (:attr:`Effect.cmd`).
_CMD_IDS = itertools.count(1)
#: Write programs that change a file's mode, owner or times, never what it holds.
_ATTRIBUTE_ONLY: frozenset[str] = frozenset({"chmod", "chown", "chgrp", "chattr", "setfacl",
                                             "touch"})
#: Most nested bodies (``sh -c "bash -c '…'"``) followed before the line is opaque.
MAX_DEPTH = 6
#: Most lines (the line, each body, each script) one view walks; past it the
#: rest is opaque (``nesting``): a nest of substitutions cannot make the walk
#: run for ever.
MAX_LINES = 512
#: String literals of inline code searched for command lines.
MAX_LITERALS = 64
_LITERAL = re.compile(r"\"((?:[^\"\\\n]|\\.){2,400})\"|'((?:[^'\\\n]|\\.){2,400})'")


@dataclass
class Command:
    """One simple command that would run."""

    words: list[str]
    dynamic: list[bool]
    cwd: Path | None
    #: Fed by a pipe from the command before it.
    piped_in: bool = False
    #: Takes its arguments from ``xargs`` / ``find -exec``.
    stdin_args: bool = False
    #: Which parsed line (a body counts as its own line) it belongs to.
    line: int = 0
    #: Its position in that line.
    index: int = 0

    @property
    def program(self) -> str:
        return _c._program(self.words[0]) if self.words else ""

    @property
    def family(self) -> str:
        return _c._family(self.program)

    @property
    def rest(self) -> list[str]:
        return self.words[1:]

    @property
    def text(self) -> str:
        return " ".join(self.words)


@dataclass
class Effect:
    """A path a command writes (``write``) or reads (``read``)."""

    raw: str
    cwd: Path | None
    kind: str
    #: Deletes, moves or changes everything below the path (``rm -r``, ``mv``,
    #: ``chmod -R``, an extraction into it): reaching a protected folder through
    #: one of its ancestors counts.
    destructive: bool = False
    #: A read that walks everything below the path (``grep -r``, ``tar c``).
    recursive: bool = False
    #: The path holds an expansion only the shell knows.
    dynamic: bool = False
    program: str = ""
    #: Which shell runs it (m21): the agent's own line is one, each script file
    #: it runs another.  An unseen part can change only its own shell's state.
    shell: int = 0
    #: Where it was written (m21, for the person): ``{"part", "file"?, "line"?}``.
    origin: dict[str, Any] | None = None
    #: What the path holds lives on under another name (``mv`` / hard ``ln`` /
    #: ``rename`` sources, ``tar --remove-files``): for a secret, a read.
    relocates: bool = False
    #: Which simple command emitted it: a read is excused by a write of the
    #: same word only from the same command (``rm x`` reads and writes ``x``).
    cmd: int = 0


@dataclass
class ShellView:
    """Everything :func:`view` saw on one line."""

    commands: list[Command] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    #: Why something could not be seen (``dynamic-program``, ``shell-stdin``, …).
    opaque: list[str] = field(default_factory=list)
    #: Every line text parsed (the line itself, then each body), for raw checks.
    texts: list[str] = field(default_factory=list)
    #: Commands found in string literals of inline code (best effort, never opaque).
    literal_commands: list[Command] = field(default_factory=list)
    #: ``(path, digest)`` of every script file the line runs, as read now.
    scripts: list[tuple[str, str]] = field(default_factory=list)
    #: Why the line always asks a person, whatever the lists and settings say
    #: (``snowpea-cli``: it runs snowpea's own CLI; security review 6, H-1).
    control: list[str] = field(default_factory=list)
    #: ``(path-like literal, cwd, read only)`` inline code names, for the
    #: control-plane check.  ``read only``: EVERY use of the literal in that code
    #: is a recognised read of exactly that expression (security review 7, H7-3).
    mentions: list[tuple[str, Path | None, bool]] = field(default_factory=list)
    #: Commands whose program only the shell knows (``$X -rf ~``), kept for what
    #: they are given (m21: ``check._worst_case``).
    unknown_programs: list[Command] = field(default_factory=list)
    #: Shells (:attr:`Effect.shell`) where an unseen part may change the shell's
    #: own state (a builtin named by a variable, ``eval``, ``source`` of an
    #: unknown file): their paths that depend on it are not what was judged.
    stateful_shells: set[int] = field(default_factory=set)
    #: Runs of snowpea's own CLI that only read (m21, :func:`cli_read_only`):
    #: judged like any read-only command unless :func:`_settle` finds a reason not to.
    cli_reads: list[str] = field(default_factory=list)
    #: ``PATH`` values the agent's own line gives (each a list of folders, with
    #: ``"$PATH"`` for the inherited value); ``None`` once one cannot be modelled.
    path_values: list[list[str]] | None = field(default_factory=list)
    #: m21 script grants: the :attr:`opaque` reasons raised by the line itself,
    #: not by the content of a script file it runs (:attr:`scripts`).  A reason in
    #: both counts as the line's own.
    own_opaque: list[str] = field(default_factory=list)
    #: ... and those raised by the content of a script file it runs.
    script_opaque: list[str] = field(default_factory=list)
    #: ``[start, end)`` spans of :attr:`effects` that a script file's content made.
    script_spans: list[tuple[int, int]] = field(default_factory=list)
    #: Where each opaque or control reason was first met (m21, ``uncheckedWhy``):
    #: reason id -> ``{"part", "file"?, "line"?}``.
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


#: Positive analysis or ask (security review 7, P1).  The classifier marks what
#: it cannot judge with ``exec:opaque:<why>``.  Each such mark becomes a reason
#: in :attr:`ShellView.opaque` unless THIS module analysed that very element of
#: that very command: :meth:`_Probe.words` keeps, per command, the marks it has
#: read itself (``_claims``), and the classifier's mark is dropped only against
#: that set.  There is no list of marks dropped by name any more.
#:
#: What a command's claims can hold, and what "analysed" means for each:
#:
#: ``env-code``            every assignment of the command went through
#:                         :func:`snowpea_core.security.envvars.judge` (which is
#:                         opaque itself for a value it cannot call harmless);
#: ``encoded-payload``     a decoder runs nothing: its output is data, judged
#:                         where it lands (a redirection, a pipe into a shell or
#:                         an interpreter, a script file that is run);
#: ``inline-code`` / ``interpreter-stdin`` / ``preload``
#:                         the interpreter's code, module, script, preload or
#:                         stdin went through :meth:`_Probe._interp` (a language
#:                         without a table comes back ``code:language``);
#: ``source`` / ``trap``   the file or the body was read as a line;
#: ``awk-program-file`` / ``sed-program-file``
#:                         the program file was read;
#: ``git-alias``           git's configuration was read and the alias, if any,
#:                         walked as the command it stands for.
_ALWAYS_CLAIMED: frozenset[str] = frozenset({"env-code", "encoded-payload"})

#: Programs that only read the files they are given.  A path that names one of
#: snowpea's write-level control files as an operand of any OTHER program asks a
#: person (security review 7, H7-5): what that program does with the file is not
#: known here (``sqlite3 <store> "INSERT …"``, ``ln -s <store> x``).  The writes
#: of the programs listed here that do write (``cp``'s destination, ``find
#: -delete``, ``sort -o``) are effects of their own.
READ_ONLY_PROGRAMS: frozenset[str] = frozenset(
    {"cat", "tac", "head", "tail", "less", "more", "bat", "batcat", "nl", "ls", "ll", "dir",
     "stat", "file", "wc", "du", "tree", "grep", "egrep", "fgrep", "rg", "ag", "ack", "diff",
     "cmp", "comm", "md5sum", "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum",
     "shasum", "b2sum", "cksum", "jq", "od", "xxd", "hexdump", "strings", "cut", "sort",
     "column", "realpath", "readlink", "basename", "dirname", "test", "[", "[[", "echo",
     "printf", "cd", "pushd", "find", "cp", "zcat", "bzcat", "xzcat", "date",
     # not programs: a ``<`` redirection, a read the code analysis recognised,
     # a file tool's own read.
     "redirect", "code", "tool"}
)
#: ``Effect.program`` of the new name a link gets (``ln``'s destination, ``cp
#: -s``/``-l``, ``os.symlink``'s second argument): one inside snowpea's home asks
#: a person in every mode (security review 8, H8-2).
LINK = "link"
#: Hosts ``ssh`` reaches on this machine: the remote line runs here (M7-5).
_LOCAL_HOSTS: frozenset[str] = frozenset(
    {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "0",
     "ip6-localhost", "ip6-loopback"}
)
_SSH_VALUED: frozenset[str] = frozenset(
    {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o",
     "-p", "-Q", "-R", "-S", "-W", "-w"}
)
_SSH_RUNS = re.compile(r"(?i)proxycommand|localcommand|knownhostscommand|remotecommand")
#: Programs that hand a command to something that runs it later, outside this
#: line (M7-5): what will run is not judged by anything.
_DEFERRED: frozenset[str] = frozenset({"at", "batch", "crontab", "launchctl", "schtasks"})
_DEFERRED_READS: dict[str, frozenset[str]] = {
    "crontab": frozenset({"-l", "-r", "-h", "--help", "-V"}),
    "at": frozenset({"-l", "-r", "-d", "-c", "-V", "-q"}),
    "launchctl": frozenset({"list", "print", "print-disabled", "blame", "version", "help",
                            "stop", "unload", "bootout", "disable", "remove"}),
    "schtasks": frozenset({"/query", "/end", "/delete"}),
}
#: Shell names whose meaning the walk depends on: a function of that name
#: changes what later words do.
_SHADOWED: frozenset[str] = frozenset(
    {"cd", "pushd", "popd", "chdir", "builtin", "command", "export", "unset", "declare",
     "local", "readonly", "typeset", "eval", "source", ".", "alias", "set", "exec", "env",
     "read", "trap", "test", "[", "echo", "printf"}
)

#: ``python -m <module>``: modules that take no code of their own and run
#: nothing but themselves or the project's own tests/app (security review 6,
#: H-2).  A module found inside the project is read as a script instead; any
#: other module is opaque (``code:module``).  ``http.server`` is not here.
SAFE_MODULES: frozenset[str] = frozenset(
    {
        # the standard library's command-line tools
        "json.tool", "venv", "ensurepip", "compileall", "py_compile", "zipfile", "tarfile",
        "gzip", "base64", "calendar", "platform", "sysconfig", "site", "tokenize", "tabnanny",
        "ast", "dis", "pickletools", "zipapp", "unittest", "doctest", "uuid",
        "mimetypes", "filecmp", "pprint", "this", "locale", "quopri", "symtable",
        "keyword", "token", "random", "secrets",
        # test runners, linters, formatters, type checkers, builders
        "pytest", "ruff", "black", "mypy", "flake8", "pylint", "isort", "pyflakes",
        "pycodestyle", "pydocstyle", "bandit", "pyright", "pytype", "autopep8", "yapf",
        "vulture", "radon", "build", "wheel", "tox", "nox", "pre_commit", "sphinx", "mkdocs",
        "pipdeptree", "pip_audit", "playwright",
    }
)
#: Servers and runners of the project's own app: the module (``main:app``) or
#: the file they are pointed at is read as a script; the rest runs as before.
_APP_MODULES: frozenset[str] = frozenset(
    {"uvicorn", "gunicorn", "hypercorn", "flask", "django", "streamlit", "celery", "fastapi"}
)
#: Modules that are a command runner of the same name (``python -m uv run x``):
#: judged as that command.
_COMMAND_MODULES: frozenset[str] = frozenset({"uv", "poetry", "hatch", "pdm", "pipenv", "pipx"})
#: Modules that run a script they are given: that script is read.
_RUNNER_MODULES: frozenset[str] = frozenset(
    {"cProfile", "profile", "pdb", "trace", "runpy", "coverage", "kernprof", "memory_profiler"}
)
#: Most files one line's code may pull in by import before it counts as unread.
MAX_IMPORTED = 48


@dataclass(frozen=True)
class _Spec:
    """How one interpreter family takes its options (for :func:`_invocation`)."""

    #: Short letters whose value is code (the rest of the word, else the next word).
    code: str = ""
    #: Whole words whose next word is code (``--eval``); ``--eval=code`` counts too.
    word_code: tuple[str, ...] = ()
    #: Short letters with a value (the rest of the word, else the next word).
    valued: str = ""
    #: Short letters with an optional value attached (never the next word).
    attached: str = ""
    #: Short letters followed by optional digits, then more letters (perl ``-0``, ``-l``).
    digits: str = ""
    long_valued: tuple[str, ...] = ()
    #: Options that load a file or module before the script (``node -r``).
    preload: tuple[str, ...] = ()
    #: The letter that names a module to run (python ``-m``).
    module: str = ""
    #: Words after which nothing is run (``--version``).
    info: tuple[str, ...] = ()
    #: Words that make the interpreter find its own work (``node --test``).
    self_run: tuple[str, ...] = ()
    #: The letter that edits the operand files in place (``perl -i``).
    inplace: str = ""
    #: Options end at the code (python ``-c``): what follows are its arguments.
    code_ends: bool = False


_NODE_SPEC = _Spec(
    word_code=("-e", "--eval", "-p", "--print", "-pe", "-ep"),
    valued="C", long_valued=("--env-file", "--conditions", "--inspect-port", "--input-type",
                             "--title", "--watch-path", "--cpu-prof-dir", "--redirect-warnings",
                             "--report-dir", "--diagnostic-dir", "--test-name-pattern",
                             "--test-reporter", "--test-reporter-destination"),
    preload=("-r", "--require", "--import", "--loader", "--experimental-loader"),
    info=("-v", "--version", "-h", "--help", "--v8-options", "--completion-bash", "-c",
          "--check"),
    self_run=("--test", "--watch"),
)
_SPECS: dict[str, _Spec] = {
    "python": _Spec(code="c", module="m", valued="WXQ",
                    long_valued=("--check-hash-based-pycs",), code_ends=True,
                    info=("-V", "-VV", "--version", "-h", "--help", "-?", "--help-all",
                          "--help-env", "--help-xoptions")),
    "node": _NODE_SPEC, "nodejs": _NODE_SPEC,
    "perl": _Spec(code="eE", valued="IF", attached="xiCDMm", digits="l0", inplace="i",
                  info=("-v", "-V", "--version", "-h", "--help")),
    "ruby": _Spec(code="e", valued="IrCEF", attached="KTxiW", digits="0", inplace="i",
                  info=("--version", "-h", "--help", "--copyright")),
    "php": _Spec(code="rBRE", valued="dczftF", self_run=("-S",),
                 info=("-v", "-i", "-m", "-h", "--version", "--ini", "--help")),
    "lua": _Spec(code="e", valued="l", info=("-v",)),
    "luajit": _Spec(code="e", valued="lbjO", info=("-v",)),
    "rscript": _Spec(word_code=("-e", "--expr"), info=("--version", "--help")),
    "osascript": _Spec(code="e", valued="l"),
    "julia": _Spec(word_code=("-e", "--eval", "-E", "--print"), valued="pLJCOgt",
                   long_valued=("--project", "--sysimage", "--threads", "--procs"),
                   info=("-v", "--version", "-h", "--help")),
    "groovy": _Spec(code="e", info=("-v", "--version", "-h", "--help")),
    "scala": _Spec(code="e", info=("-version", "-help")),
    "swift": _Spec(code="e", info=("--version", "-version", "--help")),
}
_SPECS["pypy"] = _SPECS["python"]
#: ``bun``: a package manager and a runtime; only its inline code is read here.
_BUN_SPEC = _Spec(word_code=("-e", "--eval", "-p", "--print"),
                  preload=("-r", "--preload", "--require", "--import"),
                  info=("-v", "--version", "-h", "--help", "--revision"))


@dataclass
class _Invocation:
    """What an interpreter's words ask it to run."""

    code: list[tuple[str, bool]] = field(default_factory=list)
    preload: list[tuple[str, bool]] = field(default_factory=list)
    module: tuple[str, bool] | None = None
    #: Index (in ``rest``) of the first word after the options, the code or the module.
    after: int = 0
    info: bool = False
    self_run: bool = False
    inplace: bool = False


def _invocation(spec: _Spec, rest: list[str], rest_dyn: list[bool]) -> _Invocation:  # noqa: C901
    """Read an interpreter's options: its inline code, module, preloads, first operand.

    A cluster of short letters is read letter by letter (security review 6,
    H-2): ``-pe CODE``, ``-ne CODE``, ``-uc CODE`` carry code in the NEXT word,
    ``-MFile::Path`` is a module name however many ``e`` it holds.
    """
    found = _Invocation()
    index = 0

    def value_at(position: int) -> tuple[str, bool]:
        if position < len(rest):
            return rest[position], rest_dyn[position]
        return "", True

    while index < len(rest):
        word = rest[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or word == "-":
            break
        if word in spec.word_code:
            found.code.append(value_at(index + 1))
            index += 2
            continue
        attached_code = next((name for name in spec.word_code
                              if name.startswith("--") and word.startswith(name + "=")), None)
        if attached_code is not None:
            found.code.append((word.split("=", 1)[1], rest_dyn[index]))
            index += 1
            continue
        if word in spec.preload:
            found.preload.append(value_at(index + 1))
            index += 2
            continue
        attached_preload = next((name for name in spec.preload
                                 if name.startswith("--") and word.startswith(name + "=")), None)
        if attached_preload is not None:
            found.preload.append((word.split("=", 1)[1], rest_dyn[index]))
            index += 1
            continue
        if word in spec.info:
            found.info = True
            index += 1
            continue
        if word in spec.self_run:
            found.self_run = True
            index += 1
            continue
        if word.startswith("--"):
            index += 2 if word in spec.long_valued else 1
            continue
        letters = word[1:]
        position = 0
        took_next = False
        stop = False
        while position < len(letters):
            letter = letters[position]
            tail = letters[position + 1:]
            if letter in spec.code:
                if tail:
                    found.code.append((tail, rest_dyn[index]))
                else:
                    found.code.append(value_at(index + 1))
                    took_next = True
                stop = spec.code_ends
                break
            if spec.module and letter == spec.module:
                if tail:
                    found.module = (tail, rest_dyn[index])
                else:
                    found.module = value_at(index + 1)
                    took_next = True
                stop = True
                break
            if letter in spec.valued:
                took_next = not tail
                break
            if letter in spec.attached:
                if letter == spec.inplace:
                    found.inplace = True
                break
            if letter in spec.digits:
                position += 1
                while position < len(letters) and (letters[position].isdigit()
                                                   or letters[position] in "xX"):
                    position += 1
                continue
            position += 1
        index += 2 if took_next else 1
        if stop:
            break
    found.after = min(index, len(rest))
    return found


# -- reader precision (m21): literal values a script or a line gives a name ------------
#
# ``bash bin/shot.sh /tmp/a.png`` where the script says ``OUT=${1:-/tmp/b.png}``,
# ``ADB=/opt/sdk/adb`` and ``$ADB exec-out screencap -p > "$OUT"``: the program is
# ``/opt/sdk/adb`` and the file written is ``/tmp/a.png`` or ``/tmp/b.png`` — both
# are judged, each exactly as if the text spelled it.  A name qualifies only when
# nothing else in the text can give it another value (:func:`_scope_for`); a value
# that is computed, read, inherited or would split into several words keeps the
# expansion unknown, as before.

#: Names the shell sets or reads itself: never substituted.
_SPECIAL_NAMES: frozenset[str] = frozenset(
    {"RANDOM", "SECONDS", "LINENO", "PPID", "PWD", "OLDPWD", "IFS", "OPTARG", "OPTIND",
     "REPLY", "HOSTNAME", "HOSTTYPE", "UID", "EUID", "GROUPS", "FUNCNAME", "PIPESTATUS",
     "MAPFILE", "COLUMNS", "LINES", "SHLVL", "HISTFILE", "SHELLOPTS", "BASHOPTS",
     "EPOCHSECONDS", "EPOCHREALTIME", "SRANDOM", "BASHPID", "COPROC", "DIRSTACK", "PS4",
     "CDPATH", "ENV", "BASH_ENV", "HOME", "TMPDIR", "SNOWPEA_HOME", "PATH"}
)
#: Constructs that can give any name a value the text does not spell: nothing of
#: such a text is substituted (``eval``, ``source``/``.``, ``printf -v``, a
#: nameref, ``set -a``/allexport, ``alias``, ``let``, ``${!x}``, ``IFS``, a
#: builtin given a name only the shell knows).
_NO_BINDING = re.compile(
    r"(?:^|[\s;&|(){}`])(?:eval|source|alias|let|enable|builtin|command)(?=[\s;&|)]|$)"
    r"|(?:^|[;&|\n({]|\bthen|\bdo|\belse)\s*\.\s+\S"
    r"|\bprintf\s+(?:-\S+\s+)*-v\b"
    r"|\b(?:declare|local|typeset|readonly)\s+(?:-\w+\s+)*-\w*n"
    r"|\bset\s+(?:-\w*a|-o\s+allexport|[-+]o\s+a\b)|\ballexport\b"
    r"|\$\{!|\bIFS\b"
    r"|\b(?:read|mapfile|readarray|getopts|declare|typeset|local|export|readonly|unset)"
    r"(?:\s+-\w+)*\s+[\"']?\$",
    re.MULTILINE,
)
#: Constructs that change the positional parameters (or give a function its own).
_NO_POSITIONAL = re.compile(
    r"\bshift\b|\bset\s+(?:--|[^-\s])|[A-Za-z_][A-Za-z0-9_-]*\s*\(\s*\)|\bfunction\s",
)
#: ``$NAME``, ``${NAME}``, ``${NAME:-lit}``, ``${NAME-lit}``, ``${NAME:=lit}``,
#: ``$1``, ``${12:-lit}``, ``$#``.  The default must be a plain literal.
_EXPANSION = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*|[0-9]+|#)"
    r"(?:(?P<op>:?[-=])(?P<default>[^{}$`'\"\\]*))?\}"
    r"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*|[0-9#]))"
)
#: A value that would split into several words, expand as a pattern, or expand
#: again when used unquoted (whether a use is quoted is not known here).
_SPLITS = re.compile(r"[\s*?\[\]$`\\'\"{}]")
#: Most candidate command lines one simple command is judged as.
MAX_VARIANTS = 16
#: Most candidate command lines one line (with every script it runs) is judged as.
MAX_WALKS = 256


@dataclass
class _Binding:
    """The literal values a name's one assignment gives it (:func:`_scope_for`)."""

    values: tuple[str, ...]
    #: The assignment's position among the text's parsed commands.
    index: int
    #: A top-level, unconditional statement of its own: a use parsed after it
    #: cannot see the name unset.
    sure: bool


@dataclass
class _Scope:
    """What a text (a script, a ``sh -c`` body, the agent's own line) gives names."""

    bound: dict[str, _Binding] = field(default_factory=dict)
    #: Names only ever assigned by standalone statements and never exported: no
    #: program the text runs can read them.
    local: set[str] = field(default_factory=set)
    #: ``$1``… of a script run with arguments (an item ``None``: not literal);
    #: ``None`` when they are not known.
    positional: list[str | None] | None = None
    #: Uses can be ordered against assignments (no ``trap`` body can run first).
    ordered: bool = True
    #: The line id (:attr:`_Probe._line`) of the text itself: only its own
    #: commands are read with this scope, never a body walked inside it.
    line_id: int = 0


def _bare(text: str, name: str) -> list[int]:
    """Offsets of every occurrence of ``name`` in ``text`` other than ``$name``/``${name``."""
    out: list[int] = []
    for match in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text):
        start = match.start()
        if start >= 1 and text[start - 1] == "$":
            continue
        if start >= 2 and text[start - 2:start] == "${":
            if text[match.end():match.end() + 2].startswith((":=", "=")):
                out.append(start)  # ``${NAME:=v}`` assigns: counts as another place
            continue
        out.append(start)
    return out


def _skip_quoted(text: str, i: int, limit: int) -> int:
    """Index after the quoted span, ``$(…)``, ``${…}`` or backtick span opening at ``i``;
    ``-1`` when it does not close before ``limit``."""
    char = text[i]
    if char == "'":
        end = text.find("'", i + 1)
        return end + 1 if 0 <= end < limit else -1
    if char in '"`':
        j = i + 1
        while j < limit:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == char:
                return j + 1
            j += 1
        return -1
    opener = text[i + 1]
    closer = ")" if opener == "(" else "}"
    depth = 0
    j = i + 1
    while j < limit:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] in "'\"" and opener == "(":
            k = _skip_quoted(text, j, limit)
            if k < 0:
                return -1
            j = k
            continue
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


_KEYWORDS_OPEN: frozenset[str] = frozenset({"if", "while", "until", "for", "select", "{"})
_KEYWORDS_CLOSE: frozenset[str] = frozenset({"fi", "done", "}"})
_KEYWORDS_NEXT: frozenset[str] = frozenset({"then", "do", "else", "elif", "!", "time"})


def _top_level(text: str, offset: int) -> bool:  # noqa: C901 - one small scanner
    """The statement at ``offset`` runs unconditionally in the shell itself: not in a
    block, a loop, a subshell, a pipeline or after ``&&``/``||``.  When in doubt
    (a here-document, ``case``) ``False``."""
    head = text[:offset]
    if "<<" in head or re.search(r"(?:^|[\s;&|(])case\s", head):
        return False
    depth = 0
    last = ";"
    at_command = True
    i = 0
    while i < offset:
        char = text[i]
        if char == "\\":
            i += 2
            at_command = False
            continue
        if char in "'\"`" or (char == "$" and text[i + 1:i + 2] in ("(", "{")):
            end = _skip_quoted(text, i, offset)
            if end < 0:
                return False
            i = end
            at_command = False
            continue
        if char == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            i = offset if end < 0 else end
            continue
        if char in "\n;":
            last, at_command = ";", True
            i += 1
            continue
        if char in "&|":
            double = text[i + 1:i + 2] == char
            last = char * 2 if double else char
            at_command = True
            i += 2 if double else 1
            continue
        if char == "(":
            depth += 1
            last, at_command = "(", True
            i += 1
            continue
        if char == ")":
            depth -= 1
            at_command = False
            i += 1
            continue
        if char in " \t":
            i += 1
            continue
        j = i
        while j < offset and text[j] not in " \t\n;&|()<>'\"`$\\":
            j += 1
        if j == i:
            i += 1
            at_command = False
            continue
        word = text[i:j]
        if at_command:
            if word in _KEYWORDS_OPEN:
                depth += 1
                last = word
            elif word in _KEYWORDS_CLOSE:
                depth -= 1
                at_command = False
            elif word in _KEYWORDS_NEXT:
                last = word
            else:
                at_command = False
        i = j
    if depth != 0 or last != ";":
        return False
    # Not the head of a pipeline or a background job either.
    tail = text[offset:]
    end = 0
    while end < len(tail) and tail[end] not in " \t\n;&|":
        if tail[end] in "'\"`" or (tail[end] == "$" and tail[end + 1:end + 2] in ("(", "{")):
            skipped = _skip_quoted(tail, end, len(tail))
            if skipped < 0:
                return False
            end = skipped
            continue
        end += 2 if tail[end] == "\\" else 1
    follow = tail[end:].lstrip(" \t")
    return not (follow.startswith("|") and not follow.startswith("||")) and not (
        follow.startswith("&") and not follow.startswith("&&"))


def _ends_statement(text: str, offset: int) -> bool:
    """The assignment at ``offset`` (and any assignments right after it) is the whole
    statement: no program follows on it (the parser splits ``X=$(…) prog`` in two,
    so this is read from the text itself)."""
    i = offset
    while True:
        while i < len(text) and text[i] not in " \t\n;&|":
            if text[i] in "'\"`" or (text[i] == "$" and text[i + 1:i + 2] in ("(", "{")):
                skipped = _skip_quoted(text, i, len(text))
                if skipped < 0:
                    return False
                i = skipped
                continue
            i += 2 if text[i] == "\\" else 1
        while i < len(text) and text[i] in " \t":
            i += 1
        rest = text[i:]
        if not rest or rest.startswith(("\n", ";", "&&", "||", "#")):
            return True
        if not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", rest):
            return False


def _quoting_unclear(text: str) -> bool:
    """The parser drops quotes, so ``'$X'$Y`` reads as ``$X$Y``: a ``$`` written in
    single quotes next to other text, an escaped ``\\$`` or ``$'…'`` makes it
    unclear which ``$`` of a word expands (nothing of the text is substituted)."""
    if "\\$" in text or "$'" in text:
        return True
    i = 0
    double = False
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == '"':
            double = not double
        elif char == "'" and not double:
            end = text.find("'", i + 1)
            if end < 0:
                return True
            if "$" in text[i + 1:end]:
                before = text[i - 1] if i else " "
                after = text[end + 1] if end + 1 < len(text) else " "
                if before not in " \t\n;&|(<>" or after not in " \t\n;&|)<>":
                    return True
            i = end
        i += 1
    return False


def _plain(value: str, *, whole: bool) -> bool:
    """``value`` stands for itself as one unquoted word (``~`` only as a whole word,
    where the reader treats it as the shell does)."""
    if _SPLITS.search(value) or not value:
        return False
    return whole or "~" not in value


def _scope_for(text: str, *, positional: list[tuple[str, bool]] | None,
               outer: str) -> _Scope | None:
    """The literal values ``text`` gives its names, or ``None`` when nothing in it
    can be substituted.  ``outer``: the texts of what runs it (a name they mention
    may come exported from there, so it is never substituted nor called local)."""
    if _NO_BINDING.search(text) or _quoting_unclear(text):
        return None
    try:
        parsed = _c.parse_line(text)
    except Exception:  # noqa: BLE001 - an unreadable text binds nothing
        return None
    if parsed.unsure:
        return None
    scope = _Scope(ordered=not re.search(r"(?:^|[\s;&|(])trap\s", text))
    if positional is not None and not _NO_POSITIONAL.search(text):
        scope.positional = [word if not dyn and _plain(word, whole=True) else None
                            for word, dyn in positional]
    # The standalone assignments (statements of nothing but assignments), by name.
    standalone: dict[str, list[tuple[int, str, bool]]] = {}
    for index, cmd in enumerate(parsed.commands):
        words = list(cmd.words)
        while words and words[0] in _c.RESERVED_WORDS:
            words = words[1:]
        if not words or cmd.redirects or not all(_c._ASSIGNMENT.match(w) for w in words):
            continue
        dyn_all = list(cmd.dynamic)[len(cmd.words) - len(words):]
        for word, dyn in zip(words, dyn_all, strict=False):
            name, _, value = word.partition("=")
            standalone.setdefault(name, []).append((index, value, dyn))
    ordered_names = sorted(standalone, key=lambda n: standalone[n][0][0])
    for name in ordered_names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in _SPECIAL_NAMES \
                or _env.is_watched(name) or name in os.environ \
                or name.upper().startswith(("SNOWPEA", "BASH", "XDG_")):
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", outer):
            continue  # what runs the text may export it (``export NAME=…; bash x.sh``)
        places = _bare(text, name)
        if len(places) != len(standalone[name]) or not all(
                _ends_statement(text, place) for place in places):
            continue  # ``export NAME``, ``read NAME``, ``NAME=$(…) prog``, ``NAME+=``…
        scope.local.add(name)
        if len(places) != 1:
            continue  # assigned twice: which value a use sees is not known
        index, raw, dyn = standalone[name][0]
        if dyn:
            values = _expand(raw, scope, index)
        else:
            values = [raw] if _plain(raw, whole=True) else None
        if values is None or not values or any(not v for v in values):
            continue
        scope.bound[name] = _Binding(tuple(dict.fromkeys(values)), index,
                                     _top_level(text, places[0]))
    return scope


def _expand(word: str, scope: _Scope, index: int) -> list[str] | None:
    """Every text ``word`` (a word of the scope's own text, the ``index``-th
    command) can expand to, or ``None`` when one part of it is not known."""
    if "$(" in word or "`" in word or "$'" in word:
        return None
    matches = list(_EXPANSION.finditer(word))
    if not matches:
        return None
    whole = len(matches) == 1 and matches[0].span() == (0, len(word))
    pieces: list[list[str]] = []
    position = 0
    for match in matches:
        literal = word[position:match.start()]
        if "$" in literal:
            return None
        pieces.append([literal])
        name = match.group("braced") or match.group("plain")
        op = match.group("op")
        default = match.group("default")
        if op and op.endswith("="):
            return None  # ``${X:=v}`` assigns
        if op and default is not None and not _plain(default, whole=whole):
            return None
        values: list[str]
        if name == "#":
            if scope.positional is None:
                return None
            values = [str(len(scope.positional))]
        elif name[0].isdigit():
            number = int(name)
            if scope.positional is None or number == 0:
                return None
            if number > len(scope.positional):
                if not op or not default:
                    return None  # empty: the word may vanish or be ``""``
                values = [default]
            else:
                given = scope.positional[number - 1]
                if given is None:
                    return None
                # The argument, or the default: both are judged.
                values = [given, default] if op and default else [given]
        elif name in scope.bound:
            binding = scope.bound[name]
            if not (binding.sure and scope.ordered and index > binding.index):
                return None  # the use may see the name unset or not yet set
            values = list(binding.values)
            if op and default:
                values.append(default)
        else:
            return None
        if not whole and any(v.startswith("~") for v in values):
            return None
        pieces.append(values)
        position = match.end()
    if "$" in word[position:]:
        return None
    pieces.append([word[position:]])
    results = [""]
    for options in pieces:
        results = [head + option for head in results for option in dict.fromkeys(options)]
        if len(results) > MAX_VARIANTS:
            return None
    if any(not r for r in results):
        return None
    return results


def _locate(text: str, words: list[str]) -> int | None:
    """The 1-based line of ``text`` a simple command of ``words`` is on (best effort:
    the first line holding its first two words; ``None`` when none does)."""
    probes = [w for w in words[:8] if w and w.strip()]
    if not probes or not text:
        return None
    best, found = 0, None
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") or probes[0] not in line:
            continue
        score = sum(1 for w in probes if w in line)
        if score > best:
            best, found = score, number
    return found


def _echo_text(words: list[str]) -> str | None:
    """What ``echo …`` / ``printf …`` (literal words) writes; ``None`` when not known."""
    program = _c._program(words[0]) if words else ""
    args = words[1:]
    if program == "echo":
        escapes = False
        newline = True
        while args and re.fullmatch(r"-[neE]+", args[0]):
            escapes = ("e" in args[0] or escapes) and "E" not in args[0]
            newline = newline and "n" not in args[0]
            args = args[1:]
        text = " ".join(args)
        if escapes:
            text = _unescape(text)
        return text + ("\n" if newline else "")
    if program == "printf":
        if args[:1] == ["--"]:
            args = args[1:]
        if not args or args[0].startswith("-"):
            return None
        template, values = _unescape(args[0]), list(args[1:])
        out: list[str] = []
        position = 0
        while position < len(template):
            char = template[position]
            if char != "%":
                out.append(char)
                position += 1
                continue
            directive = template[position + 1:position + 2]
            if directive == "%":
                out.append("%")
            elif directive in ("s", "b") and values:
                value = values.pop(0)
                out.append(_unescape(value) if directive == "b" else value)
            else:
                return None
            position += 2
        return None if values else "".join(out)
    return None


_ESCAPE = re.compile(r"\\(n|t|r|\\|'|\"|a|b|e|f|v|x[0-9a-fA-F]{1,2}|0?[0-7]{1,3})")


def _unescape(text: str) -> str:
    table = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a",
             "b": "\b", "e": "\x1b", "f": "\f", "v": "\v"}

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body in table:
            return table[body]
        if body.startswith("x"):
            return chr(int(body[1:], 16))
        return chr(int(body, 8) & 0xFF)

    return _ESCAPE.sub(replace, text)


class _Probe(_c._Judge):
    """The classifier's walk, collecting commands and effects instead of a verdict."""

    def __init__(self, request: _c.ClassifyRequest, view: ShellView, *, literals: bool = True,
                 positional: list[tuple[str, bool]] | None = None, outer: str = "",
                 file: str | None = None, budget: list[int] | None = None):
        super().__init__(request)
        #: Candidate command lines walked so far by this probe and every script
        #: probe under it (m21): past :data:`MAX_WALKS` nothing more is substituted.
        #: ``[1]``: lines walked so far (:data:`MAX_LINES`), shared the same way.
        self._budget = budget if budget is not None else [0, 0]
        self.view = view
        self.literals = literals
        #: Reader precision (m21): the arguments of the script this probe reads,
        #: the texts of what runs it, the file it is, and the scopes of the texts
        #: being walked (the probe's own text, a ``sh -c`` body run with arguments).
        self._positional = positional
        self._outer = outer
        self._file = file
        self._text: str | None = None
        self._scopes: list[_Scope] = []
        self._pending: list[tuple[str, bool]] | None = None
        #: The next line is a ``>(…)`` body: its first command reads stdin.
        self._fed = False
        self._resolving: list[int] = []
        #: The probe of the agent's own line (set by :func:`view`): only there is a
        #: run of snowpea's CLI vetted as read-only (m21) and ``PATH`` followed.
        self.root = False
        #: This probe's shell (m21, :attr:`Effect.shell`).
        self._shell_id = next(_SHELL_IDS)
        self._line = -1
        self._index = 0
        self._cmd: _c._Cmd | None = None
        #: The command whose output the current one reads through a pipe.
        self._feeder: _c._Cmd | None = None
        #: The program whose writes this module judges itself (super's are dropped).
        self._own: list[bool] = []
        self._context: list[str] = []
        #: The id of each command being walked (:attr:`Effect.cmd`).
        self._cmd_ids: list[int] = []
        #: Per command being walked: the classifier's opaque marks this module
        #: analysed itself for that command (security review 7, P1).
        self._claims: list[set[str]] = []
        #: Inside the content of a script file (m21 script grants: ``own_opaque``).
        self._in_script = 0
        guard_all = self.hits.guard_all

        def tracked(rule: str) -> None:
            guard_all(rule)
            if rule.startswith("exec:opaque:"):
                self._unseen(rule[len("exec:opaque:"):])

        self.hits.guard_all = tracked  # type: ignore[method-assign]

    # -- recording ---------------------------------------------------------
    def _where(self) -> dict[str, Any]:
        """The simple command being walked, and its script file and line (m21)."""
        cmd = self._cmd
        if cmd is None:
            part = (self._text or "").strip().splitlines()[0] if (self._text or "").strip() \
                else ""
            words: list[str] = []
        else:
            words = list(cmd.words)
            part = " ".join(words + [f"{op}{target}" for op, target, _ in cmd.redirects])
        part = " ".join(part.split())
        out: dict[str, Any] = {"part": part if len(part) <= 160 else part[:159] + "…"}
        if self._file is not None:
            out["file"] = self._file
            redirects = [target for _, target, _ in cmd.redirects] if cmd is not None else []
            line = _locate(self._text or "", words + redirects)
            if line is not None:
                out["line"] = line
                written_as = " ".join((self._text or "").splitlines()[line - 1].split())
                if written_as and len(written_as) <= 160:
                    out["part"] = written_as  # as the script spells it (``SIZE=$(…)``)
        return out

    def _detail(self, why: str) -> None:
        if why not in self.view.details:
            try:
                self.view.details[why] = self._where()
            except Exception:  # noqa: BLE001 - an explanation never breaks the judgement
                self.view.details[why] = {"part": ""}

    def _opaque(self, why: str) -> None:
        self._detail(why)
        if why in STATEFUL_REASONS:
            self.view.stateful_shells.add(self._shell_id)
        if why not in self.view.opaque:
            self.view.opaque.append(why)
        self._attribute(why)

    def _attribute(self, why: str) -> None:
        """m21 script grants: note whether ``why`` came from a script's content."""
        kept = self.view.script_opaque if self._in_script else self.view.own_opaque
        if why not in kept:
            kept.append(why)

    def _unseen(self, why: str) -> None:
        """The classifier could not judge something: opaque, unless the command
        being walked had exactly that analysed here."""
        claimed = self._claims[-1] if self._claims else frozenset()
        if why.split(":", 1)[0] not in claimed:
            self._opaque(why)

    def _control(self, why: str) -> None:
        self._detail(why)
        if why not in self.view.control:
            self.view.control.append(why)

    def finish(self) -> None:
        """Nothing is left to copy at the end: every opaque mark of the classifier
        is taken (or dropped as analysed) when it is made (:meth:`_unseen`)."""

    # -- reader precision (m21) ---------------------------------------------
    def _scope_here(self) -> _Scope | None:
        """The scope of the text whose command is being walked; ``None`` inside any
        body (a substitution, a nested shell) the scope does not belong to."""
        if self._scopes and self._scopes[-1].line_id == self._line:
            return self._scopes[-1]
        return None

    def _resolved_path(self, raw: str) -> list[str] | None:
        scope = self._scope_here()
        if scope is None or "$" not in raw:
            return None
        return _expand(raw, scope, self._index)

    def _variants(self, words: list[str],
                  dynamic: list[bool]) -> list[tuple[list[str], list[bool]]] | None:
        """``words`` with every expansion the scope knows replaced by its values
        (one candidate line per combination); ``None`` when nothing changes."""
        scope = self._scope_here()
        if scope is None or not any(dynamic) or self._line in self._resolving:
            return None
        start = 0
        while start < len(words) and (
            (not dynamic[start] and words[start] in _c.RESERVED_WORDS)
            or _c._ASSIGNMENT.match(words[start])
        ):
            start += 1
        options: list[list[list[tuple[str, bool]]]] = []
        changed = False
        for position, (word, dyn) in enumerate(zip(words, dynamic, strict=False)):
            if position < start or not dyn:
                options.append([[(word, dyn)]])
                continue
            if word in ("$@", "${@}") and scope.positional is not None \
                    and all(v is not None for v in scope.positional):
                options.append([[(str(v), False) for v in scope.positional]])
                changed = True
                continue
            values = _expand(word, scope, self._index)
            if values is None:
                options.append([[(word, dyn)]])
                continue
            options.append([[(value, False)] for value in values])
            changed = True
        if not changed:
            return None
        lines: list[list[tuple[str, bool]]] = [[]]
        for choice in options:
            lines = [head + option for head in lines for option in choice]
            if len(lines) > MAX_VARIANTS:
                return None
        out = [([w for w, _ in line], [d for _, d in line]) for line in lines]
        out = [item for item in out if item[0]]
        if not out or self._budget[0] + len(out) > MAX_WALKS:
            return None
        self._budget[0] += len(out)
        return out

    def _effect(self, raw: str, cwd: Path | None, kind: str, *, destructive: bool = False,
                recursive: bool = False, dynamic: bool = False, program: str = "",
                relocates: bool = False) -> None:
        if isinstance(raw, _c.ProcessWord):
            return  # a pipe's name (``/dev/fd/63``); its body is read as commands
        dyn = dynamic or "$" in raw or "`" in raw
        origin = None
        if dyn:
            try:
                origin = self._where()
            except Exception:  # noqa: BLE001 - an explanation never breaks the judgement
                origin = None
        self.view.effects.append(Effect(raw, cwd, kind, destructive, recursive, dyn,
                                        program or (self._context[-1] if self._context else ""),
                                        shell=self._shell_id, origin=origin,
                                        relocates=relocates,
                                        cmd=self._cmd_ids[-1] if self._cmd_ids else 0))

    # -- the classifier's hooks -------------------------------------------
    def check_write_path(self, raw: str, cwd: Path | None, rule: str, dynamic: bool) -> None:
        if self._own and self._own[-1] and rule != "redirect":
            return
        if dynamic:
            resolved = self._resolved_path(raw)
            if resolved is not None:
                for value in resolved:
                    self.check_write_path(value, cwd, rule, False)
                return
        program = self._context[-1] if self._context else ""
        tree = rule != "redirect" and program in _TREE_WRITERS
        # ``find -delete`` deletes below its root; an extraction only writes below.
        self._effect(raw, cwd, "write", destructive=tree and program == "find",
                     recursive=tree, dynamic=dynamic,
                     program="redirect" if rule == "redirect" else program)

    def check_read_path(
        self,
        raw: str,
        cwd: Path | None,
        rule: str,
        shell: bool = False,
        *,
        recursive: bool = False,
        dynamic: bool = False,
    ) -> None:
        del shell
        if dynamic:
            resolved = self._resolved_path(raw)
            if resolved is not None:
                for value in resolved:
                    self.check_read_path(value, cwd, rule, recursive=recursive)
                return
        # ``< file`` only ever reads; any other word is an operand of the program
        # being walked (which decides what happens to the file it names).
        self._effect(raw, cwd, "read", recursive=recursive, dynamic=dynamic,
                     program="redirect" if rule == "redirect" else "")

    def dynamic_read(self, raw: str, cwd: Path | None, rule: str) -> None:
        del rule
        self._effect(raw or "$?", cwd, "read", dynamic=True)

    # -- the walk ------------------------------------------------------------
    def line(self, command: str, cwd: Path | None, depth: int) -> None:
        pending, self._pending = self._pending, None
        fed, self._fed = self._fed, False
        # Kept before the depth check: a text too deep to walk is still judged
        # by its words (check._lexical_worst, and the snowpea-name rule).
        self.view.texts.append(command)
        self._budget[1] += 1
        if depth > MAX_DEPTH or self._budget[1] > MAX_LINES:
            self._opaque("nesting")
            return
        try:
            parsed = _c.parse_line(command)
        except _c._Opaque as exc:
            self._opaque(f"parse:{exc}")
            return
        except Exception:  # noqa: BLE001 - a parser bug is a line nobody can see
            self._opaque("parse:error")
            return
        for why in parsed.unsure:
            # Read as found, but where it ends may be misread (security review 4).
            self._opaque(f"parse:{why}")
        if fed:
            # A ``>(…)`` body, or a substitution in a piped command: every
            # command of it may read that pipe (``>(true; bash)``).
            for cmd in parsed.commands:
                cmd.fed = True
        self._line += 1
        line_id = self._line
        scope: _Scope | None = None
        if self._text is None:
            # The probe's own text (the agent's line, or the script it reads).
            self._text = command
            scope = _scope_for(command, positional=self._positional, outer=self._outer)
        elif pending is not None:
            # ``sh -c '…' name A``: a new shell with its own arguments.
            scope = _scope_for(command, positional=pending,
                               outer=self._outer + "\n" + self._text)
        if scope is not None:
            scope.line_id = line_id
            self._scopes.append(scope)
            try:
                self._walk_line(command, parsed, cwd, depth, line_id)
            finally:
                if self._scopes and self._scopes[-1] is scope and pending is not None:
                    self._scopes.pop()
            return
        self._walk_line(command, parsed, cwd, depth, line_id)

    def _walk_line(self, command: str, parsed: Any, cwd: Path | None, depth: int,
                   line_id: int) -> None:
        for name in _env.references(command):
            # ``${HOME:=/x}``: an assignment inside an expansion.
            self._opaque(f"env-code:{name}")
        for name in parsed.functions:
            # ``cd() { :; }``: later words of the line no longer mean what they say.
            # (The body itself is walked: the parser makes it commands of its own.)
            if "$" in name or "`" in name or name in _SHADOWED or _c._program(name) in _SHADOWED:
                self._opaque("function")
        self._commands(parsed, command, cwd, depth, line_id)

    def _commands(self, parsed: Any, command: str, cwd: Path | None, depth: int,
                  line_id: int) -> None:
        # Every substitution's body, exactly as the parser met it (``"$(…)"``,
        # backticks, ``<(…)``/``>(…)``, here-documents, ``${X:-$(…)}``), is a line
        # of its own, read where the command it belongs to runs (its folder, its
        # stdin).  A ``>(…)`` body reads what the outer command writes: stdin.
        def bodies(position: int, here: Path | None, last: bool = False) -> None:
            cmd = parsed.commands[position] if position < len(parsed.commands) else None
            for opener, body, index in parsed.bodies:
                if index == position or (last and index >= len(parsed.commands)):
                    self._fed = opener == ">" or bool(cmd and (cmd.piped_in or cmd.fed))
                    self.line(body, here, depth + 1)

        for position, cmd in enumerate(parsed.commands):
            bodies(position, cwd)
            self.fed_by_lister = self.lister_ok and cmd.piped_in
            self.lister_ok = False
            saved = (self._line, self._index, self._cmd, self._feeder)
            self._line, self._index, self._cmd = line_id, position, cmd
            self._feeder = parsed.commands[position - 1] if cmd.piped_in and position else None
            try:
                cwd = self.simple(cmd, cwd, depth)
            finally:
                self._line, self._index, self._cmd, self._feeder = saved
                self._line = max(self._line, line_id)
        bodies(len(parsed.commands), cwd, last=True)

    def simple(self, cmd: _c._Cmd, cwd: Path | None, depth: int) -> Path | None:
        words = list(cmd.words)
        braced = list(cmd.braced) or [False] * len(words)
        start = 0
        while start < len(words) and (
            words[start] in _c.RESERVED_WORDS or _c._ASSIGNMENT.match(words[start])
        ):
            start += 1
        if start < len(words) and start < len(braced) and braced[start]:
            self._opaque("brace-program")
        return super().simple(cmd, cwd, depth)

    def words(  # noqa: C901 - one dispatch over the commands that hide a command
        self,
        words: list[str],
        dynamic: list[bool],
        cwd: Path | None,
        depth: int,
        *,
        piped_in: bool,
        heredoc: bool,
        stdin_args: bool,
    ) -> Path | None:
        words, dynamic = list(words), list(dynamic)
        variants = self._variants(words, dynamic)
        if variants is not None:
            # m21: each value the text gives is judged as if it were spelled out.
            self._resolving.append(self._line)
            try:
                ends = [self.words(w, d, cwd, depth, piped_in=piped_in, heredoc=heredoc,
                                   stdin_args=stdin_args) for w, d in variants]
            finally:
                self._resolving.pop()
            return ends[0] if all(end == ends[0] for end in ends) else None
        named_function = False
        while words and not dynamic[0] and words[0] in _c.RESERVED_WORDS:
            if words[0] in ("for", "select"):
                # ``for NAME in …`` assigns NAME on every turn.
                if len(words) > 1 and _env.is_watched(words[1]):
                    self._opaque(f"env-code:{words[1].upper()}")
                return cwd
            if words[0] == "case":
                # ``case WORD in PATTERN)``: no command; each branch is one of its own.
                return cwd
            named_function = named_function or words[0] == "function"
            words, dynamic = words[1:], dynamic[1:]
        # A function definition: its body is walked as the commands it holds
        # (it used to be read as arguments of a program called like the function).
        if named_function and words:
            if dynamic[0] or words[0] in _SHADOWED or _c._program(words[0]) in _SHADOWED:
                # ``cd() { :; }``: later words of the line no longer mean what they say.
                self._opaque("function")
            words, dynamic = words[1:], dynamic[1:]
            while words and not dynamic[0] and words[0] in _c.RESERVED_WORDS \
                    and words[0] != "for":
                words, dynamic = words[1:], dynamic[1:]
        standalone = all(_c._ASSIGNMENT.match(word) for word in words)
        while words and _c._ASSIGNMENT.match(words[0]):
            self._assignment(words[0], dynamic[0], cwd, standalone=standalone)
            words, dynamic = words[1:], dynamic[1:]
        if not words:
            return cwd
        if dynamic[0]:
            # ``$X args`` / ``$(echo rm) -rf``: what runs is not in the line.
            self._opaque("dynamic-program")
            # m21: what it is given is still seen (check._worst_case).
            self.view.unknown_programs.append(Command(
                words=list(words), dynamic=list(dynamic), cwd=cwd, piped_in=piped_in,
                stdin_args=stdin_args, line=max(self._line, 0), index=self._index))
            return cwd
        if depth > MAX_DEPTH + 4:
            self._opaque("nesting")
            return cwd
        self.view.commands.append(Command(
            words=list(words), dynamic=list(dynamic), cwd=cwd, piped_in=piped_in,
            stdin_args=stdin_args, line=max(self._line, 0), index=self._index,
        ))
        program = _c._program(words[0])
        family = _c._family(program)
        rest, rest_dyn = words[1:], dynamic[1:]
        if program == "toybox" and rest:
            # ``toybox rm -rf /``: like busybox, the applet named next runs (review 5, H4 c).
            return self.words(rest, rest_dyn, cwd, depth + 1, piped_in=piped_in,
                              heredoc=heredoc, stdin_args=stdin_args)
        own = program in _OWN_WRITE_PROGRAMS
        claims: set[str] = set(_ALWAYS_CLAIMED)
        self._claims.append(claims)
        self._own.append(own)
        self._context.append(program)
        self._cmd_ids.append(next(_CMD_IDS))
        try:
            if self._hidden_command(program, family, rest, rest_dyn, cwd, depth, piped_in,
                                    heredoc):
                return cwd
            if program in ("tar", "bsdtar", "gtar", "zip"):
                self._archive_removal(program, rest, rest_dyn, cwd)
            if own:
                self._own_writes(program, rest, rest_dyn, cwd)
            if program in ("git",) or family == "git":
                self._git_tree(rest, rest_dyn, cwd)
                # Review 8, H8-3 (b): git's settings are read here (``git-program``
                # marks the ones that run something), so the classifier's coarser
                # ``git-config`` mark (any mention, reads too) is this analysis's.
                if self._git_runs(rest, rest_dyn, cwd):
                    claims.add("git-config")
                if self._git_alias(rest, rest_dyn, cwd, depth):
                    claims.add("git-alias")
            if program in _c.CONTENT_READERS:
                # A file named without a folder (``cat token`` after ``cd``) is read
                # like any other: the classifier's own scan only takes path-like words.
                for word, dyn in _c._reader_operands(program, rest, rest_dyn):
                    if not dyn and word and "/" not in word and not word.startswith(("-", "~")):
                        self._effect(word, cwd, "read", program=program)
            if program == "snowpea" or program.startswith("snowpea-"):
                # snowpea's own CLI talks to the daemon as the local person (H-1);
                # a vetted read-only run of it is an ordinary read (m21).
                vetted = self._cli_vetted(words, dynamic, cwd)
                if vetted:
                    self.view.cli_reads.append(" ".join(words))
                else:
                    self._control("snowpea-cli")
            else:
                vetted = False
            self._names(program, rest, rest_dyn, cwd)
            if program in ("ssh", "autossh", "mosh"):
                self._ssh(rest, rest_dyn, depth)
            if program in _DEFERRED:
                self._deferred(program, rest)
            if family in _c.INTERPRETERS and self.literals:
                self._interp(family, rest, rest_dyn, cwd, depth, piped_in, heredoc)
                claims.update(("inline-code", "interpreter-stdin", "preload"))
            elif program in ("bun", "deno") and self.literals:
                if self._js_runtime(program, rest, rest_dyn, cwd, depth, piped_in, heredoc):
                    claims.add("inline-code")
            elif vetted:
                pass  # the installed snowpea program itself (m21): not a script to read
            elif self.literals and "/" in words[0].replace("\\", "/") and not dynamic[0]:
                # ``./x``, ``scripts/x``, ``/opt/tool/run``: the file itself runs.
                self._script(words[0], cwd, depth, "shell",
                             args=list(zip(rest, rest_dyn, strict=False)))
            elif self.literals and not dynamic[0]:
                # Security review 8, H8-3 (c): a bare name that PATH resolves to a
                # file the agent wrote (or one in a temporary folder) is its script.
                found = path_program(words[0], cwd)
                if found is not None:
                    self._script(str(found), cwd, depth, "shell", strict=True,
                                 args=list(zip(rest, rest_dyn, strict=False)))
            if self.literals:
                self._project_runner(program, rest, rest_dyn, cwd, depth)
                if program in ("awk", "gawk", "mawk", "nawk", "sed", "gsed"):
                    # ``awk -f x.awk`` / ``sed -f x``: the program file is read (what it
                    # holds counts when it is the agent's own; review 6, H-2).
                    value = _option_value(rest, rest_dyn, ("-f", "--file"))
                    if value is not None:
                        if value[1] or not value[0]:
                            self._opaque("script-dynamic")
                        else:
                            self._script(value[0], cwd, depth,
                                         "sed" if "sed" in program else "awk")
                        claims.update(("awk-program-file", "sed-program-file"))
            return super().words(words, dynamic, cwd, depth, piped_in=piped_in,
                                 heredoc=heredoc, stdin_args=stdin_args)
        finally:
            self._claims.pop()
            self._own.pop()
            self._context.pop()
            self._cmd_ids.pop()

    def _names(self, program: str, rest: list[str], rest_dyn: list[bool],
               cwd: Path | None) -> None:
        """Builtins that give variables a value (security review 7, H7-1)."""
        if program in ("export", "declare", "typeset", "readonly", "local"):
            for word, dyn in zip(rest, rest_dyn, strict=False):
                if _c._ASSIGNMENT.match(word):
                    self._assignment(word, dyn, cwd)
                elif dyn and "=" in word:
                    self._opaque("env-code:dynamic")  # ``export $NAME=value``
        elif program == "unset":
            for word, dyn in zip(rest, rest_dyn, strict=False):
                if dyn:
                    self._opaque("env-code:dynamic")
                elif not word.startswith("-") and _env.is_resolve(word.split("[", 1)[0]):
                    self._opaque(f"env-code:{word.upper()}")
        elif program in ("read", "mapfile", "readarray", "getopts"):
            # They assign the names they are given, with a value nobody sees.
            for word, dyn in zip(rest, rest_dyn, strict=False):
                name = word.split("[", 1)[0]
                if dyn:
                    self._opaque("env-code:dynamic")
                elif not word.startswith("-") and _env.is_watched(name):
                    self._opaque(f"env-code:{name.upper()}")

    def _ssh(self, rest: list[str], rest_dyn: list[bool], depth: int) -> None:
        """``ssh host '<line>'``: on this machine the line is read like any other;
        on another host nothing here can read what it does (review 7, M7-5)."""
        index = 0
        host: tuple[str, bool] | None = None
        while index < len(rest):
            word = rest[index]
            if word.startswith("-") and len(word) > 1 and word != "--":
                flag = word[:2]
                attached = word[2:]
                value = attached or (rest[index + 1] if index + 1 < len(rest) else "")
                if flag == "-o" and _SSH_RUNS.search(value):
                    self._opaque("ssh-option")  # a command the client itself runs
                index += 2 if flag in _SSH_VALUED and not attached else 1
                continue
            if word == "--":
                index += 1
                continue
            host = (word, rest_dyn[index])
            index += 1
            break
        command, command_dyn = rest[index:], rest_dyn[index:]
        if command[:1] == ["--"]:
            command, command_dyn = command[1:], command_dyn[1:]
        if host is None or not command:
            return
        name = host[0].rsplit("@", 1)[-1].lower()
        local = set(_LOCAL_HOSTS)
        try:
            import socket

            local.add(socket.gethostname().lower())
        except OSError:
            pass
        if not host[1] and (name in local or name.startswith("127.")):
            self._body(" ".join(command), any(command_dyn), self.ctx.user_home, depth,
                       "shell-body")
        else:
            self._opaque("remote-shell")

    def _deferred(self, program: str, rest: list[str]) -> None:
        """``crontab file`` / ``at`` / ``batch`` / ``launchctl load``: a command handed
        over to run later, outside this line (review 7, M7-5)."""
        reads = _DEFERRED_READS.get(program, frozenset())
        lowered = [word.lower() for word in rest]
        if program in ("launchctl", "schtasks"):
            first = next((w for w in lowered if program == "schtasks" or not w.startswith("-")),
                         "")
            if not first or first in reads:
                return
        elif any(word in reads for word in rest):
            return
        self._opaque("deferred-run")

    def _git_runs(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> bool:
        """Security review 8, H8-3 (b): git settings that make git run a program
        (``-c core.pager=…``, ``git config core.hooksPath …``) and a repository
        whose hooks or configuration the agent wrote are opaque.  True when no
        ``-c``/``--config-env`` is on the line (then ``git config`` was read here
        in full and the classifier's own mark is this analysis's)."""
        root = cwd
        index = 0
        whole = True
        while index < len(rest):
            word = rest[index]
            if word == "-C" and index + 1 < len(rest):
                root = None if rest_dyn[index + 1] or root is None else _c._resolve(
                    rest[index + 1], root, self.ctx)
                index += 2
                continue
            if word.startswith(("-c", "--config-env")) and word != "-C":
                whole = False
            if word in ("-c", "--config-env") and index + 1 < len(rest):
                key, _, value = rest[index + 1].partition("=")
                # ``--config-env=key=ENVVAR``: the value comes from the environment.
                if rest_dyn[index + 1] or git_risky(key, None if word != "-c" else value):
                    self._opaque("git-program")
                index += 2
                continue
            if word.startswith("--config-env="):
                if git_risky(word.split("=", 1)[1].partition("=")[0]):
                    self._opaque("git-program")
            if word in ("--git-dir", "--work-tree", "--namespace") or word in _c._GIT_VALUED:
                if word == "--git-dir":
                    root = None
                index += 2
                continue
            if word.startswith("--git-dir="):
                root = None
            if word.startswith("-"):
                index += 1
                continue
            break
        if index < len(rest) and rest[index] == "config" and not rest_dyn[index]:
            tail = rest[index + 1:]
            if any(rest_dyn[index + 1:]) or any(git_risky(key, value)
                                                for key, value in git_config_sets(tail)):
                self._opaque("git-program")
        why = git_session_risk(root, self.ctx.user_home)
        if why is not None:
            self._opaque(why)
        return whole

    def _git_alias(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None,
                   depth: int) -> bool:
        """True when the subcommand is git's own, a program of its own (``git-x``),
        or an alias that was read from git's configuration and walked here."""
        root = cwd
        index = 0
        while index < len(rest):
            word = rest[index]
            if word == "-C" and index + 1 < len(rest):
                root = None if rest_dyn[index + 1] or root is None else _c._resolve(
                    rest[index + 1], root, self.ctx)
                index += 2
                continue
            if word in ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env") \
                    or word in _c._GIT_VALUED:
                if word == "--git-dir":
                    root = None
                index += 2
                continue
            if word.startswith("--git-dir="):
                root = None
            if word.startswith("-"):
                index += 1
                continue
            break
        if index >= len(rest):
            return True
        sub = rest[index]
        if rest_dyn[index]:
            return False
        if sub in _c.GIT_SUBCOMMANDS:
            return True
        alias = git_alias(sub, root, self.ctx.user_home)
        if alias is None:
            return False
        if not alias:
            return True
        if alias.startswith("!"):
            self._body(alias[1:].strip(), False, root, depth, "shell-body")
            return True
        try:
            expansion = shlex.split(alias)
        except ValueError:
            return False
        if not expansion or depth > MAX_DEPTH:
            return False
        self.words(["git", *expansion, *rest[index + 1:]],
                   [False] * (len(expansion) + 1) + list(rest_dyn[index + 1:]), cwd, depth + 1,
                   piped_in=False, heredoc=False, stdin_args=False)
        return True

    def _js_runtime(self, program: str, rest: list[str], rest_dyn: list[bool],
                    cwd: Path | None, depth: int, piped_in: bool, heredoc: bool) -> bool:
        """``bun -e`` / ``deno eval`` / ``bun run -`` / ``deno run -``: JavaScript read
        with the node table (review 7, H7-2).  True when the code was read here."""
        if program == "bun":
            found = _invocation(_BUN_SPEC, rest, rest_dyn)
            for name, dyn in found.preload:
                self._preload(name, dyn, cwd, depth, "node")
            if found.code:
                for text, dyn in found.code:
                    self._code_word(text, dyn, "node", cwd, depth)
                return True
            operands = list(zip(rest[found.after:], rest_dyn[found.after:], strict=False))
            if operands[:1] and operands[0][0] == "run":
                operands = [(w, d) for w, d in operands[1:] if not w.startswith("--")]
        else:
            first = next((i for i, w in enumerate(rest) if not w.startswith("-")), None)
            if first is None:
                return False
            sub = rest[first]
            operands = []
            skip = False
            for word, dyn in zip(rest[first + 1:], rest_dyn[first + 1:], strict=False):
                if skip:
                    skip = False
                elif word in ("-c", "--config", "--import-map", "--ext", "--lock", "-L",
                              "--log-level", "--cert", "--location", "--seed"):
                    skip = True
                elif not word.startswith("-") or word == "-":
                    operands.append((word, dyn))
            if sub == "eval":
                if not operands:
                    return False
                self._code_word(operands[0][0], operands[0][1], "node", cwd, depth)
                return True
            if sub != "run":
                return False
        if not operands:
            return False
        word, dyn = operands[0]
        if word == "-":
            self._stdin_code("node", cwd, depth, piped_in, heredoc)
            return True
        if dyn:
            self._opaque("script-dynamic")
        elif re.match(r"(?:https?|jsr|npm|data|blob):", word):
            if program == "deno" or word.startswith(("http", "data", "blob")):
                self._opaque("code:remote")  # code fetched when it runs
        elif re.search(r"\.(?:[cm]?[jt]s|[jt]sx)$", word):
            self._script(word, cwd, depth, "node")
        return False
    # -- commands hidden inside a command ---------------------------------------
    def _body(self, body: str, dyn: bool, cwd: Path | None, depth: int, why: str) -> None:
        if dyn:
            self._opaque(why)
            return
        self.line(body, cwd, depth + 1)

    def _hidden_command(
        self,
        program: str,
        family: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
    ) -> bool:
        """Handle a command that runs a command line it is given; False otherwise."""
        handled = True
        if program == "eval":
            self._body(" ".join(rest), any(rest_dyn), cwd, depth, "eval")
            return handled
        if program == "trap":
            # ``trap '<line>' EXIT``: the line runs later; read it now (review 6, H-2).
            operands = [(w, d) for w, d in zip(rest, rest_dyn, strict=False)
                        if not (w.startswith("-") and len(w) > 1)]
            if len(operands) >= 2 and operands[0][0] not in ("", "-"):
                self._body(operands[0][0], operands[0][1], cwd, depth, "trap")
            # Walked above (or opaque there); the other forms list or reset traps.
            self._claims[-1].add("trap")
            return False
        if program in ("source", "."):
            if rest and self.literals:
                if isinstance(rest[0], _c.ProcessWord) or _STDIN_FILE.fullmatch(rest[0]):
                    # ``source <(curl …)``: code from a pipe, run in this shell.
                    self._opaque("shell-stdin")
                    self.view.stateful_shells.add(self._shell_id)
                elif rest_dyn[0]:
                    self._opaque("script-dynamic")
                else:
                    self._script(rest[0], cwd, depth, "shell", same_shell=True)
                self._claims[-1].add("source")
            return False
        if family in _c.SHELLS and family != "busybox":
            self._shell(family, rest, rest_dyn, cwd, depth, piped_in, heredoc)
            return handled
        if program in ("su", "runuser", "script"):
            body = _option_value(rest, rest_dyn, ("-c", "--command"))
            if body is not None:
                self._body(body[0], body[1], cwd, depth, "shell-body")
            return handled
        if program == "watch":
            inner = _c._skip_options(rest, frozenset({"-n", "--interval", "-q", "--equexit"}))
            if inner:
                offset = len(rest) - len(inner)
                self._body(" ".join(inner), any(rest_dyn[offset:]), cwd, depth, "shell-body")
            return handled
        if program in ("parallel",):
            self._opaque("shell-string")
            return handled
        if program == "env":
            for index, word in enumerate(rest):
                if word in ("-S", "--split-string") and index + 1 < len(rest):
                    self._body(" ".join(rest[index + 1 :]), any(rest_dyn[index + 1 :]), cwd,
                               depth, "shell-body")
                    return handled
                if word.startswith("--split-string="):
                    self._body(word.split("=", 1)[1], rest_dyn[index], cwd, depth, "shell-body")
                    return handled
                if word.startswith("-S") and len(word) > 2:
                    self._body(word[2:], rest_dyn[index], cwd, depth, "shell-body")
                    return handled
                if not word.startswith("-") and "=" not in word:
                    break
                if _c._ASSIGNMENT.match(word):
                    self._assignment(word, rest_dyn[index], cwd)
            return False
        if program == "flock":
            body = _option_value(rest, rest_dyn, ("-c", "--command"))
            if body is not None:
                self._body(body[0], body[1], cwd, depth, "shell-body")
                return handled
            return False
        if program in ("npx", "npm"):
            body = _option_value(rest, rest_dyn, ("-c", "--call"))
            if body is not None:
                self._body(body[0], body[1], cwd, depth, "shell-body")
                return handled
        return False

    def _shell(
        self,
        family: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
    ) -> None:
        if family in ("pwsh", "powershell", "cmd"):
            self._opaque("foreign-shell")
            return
        options = _c._leading_options(rest)
        for index, word in enumerate(options):
            inline = word in ("-c", "--command") or (
                word.startswith("-") and not word.startswith("--") and "c" in word[1:]
            )
            if not inline:
                continue
            after = len(options)
            if after < len(rest):
                # ``sh -c '…' name A B``: ``$1``… of the body are A, B (m21).
                self._pending = list(zip(rest[after + 2:], rest_dyn[after + 2:],
                                         strict=False))
                try:
                    self._body(rest[after], rest_dyn[after], cwd, depth, "shell-body")
                finally:
                    self._pending = None
            else:
                self._opaque("shell-body")
            del index
            return
        script = rest[len(options)] if len(rest) > len(options) else None
        if script is not None and not any(w in ("-s", "--stdin") for w in options):
            # Runs a script file: read it (security review 5, H4).
            if isinstance(script, _c.ProcessWord) or _STDIN_FILE.fullmatch(script):
                # ``bash <(curl …)`` / ``bash /dev/stdin``: code from a pipe.
                self._opaque("shell-stdin")
            elif rest_dyn[len(options)]:
                self._opaque("script-dynamic")
            elif self.literals:
                at = len(options) + 1
                self._script(script, cwd, depth, "shell",
                             args=list(zip(rest[at:], rest_dyn[at:], strict=False)))
            return
        cmd = self._cmd
        if heredoc and cmd is not None and cmd.stdin_text is not None:
            self._body(cmd.stdin_text, cmd.stdin_dynamic, cwd, depth, "shell-stdin")
            return
        if piped_in or heredoc or (cmd is not None and any(
            op in ("<", "<&", "<>") for op, _, _ in cmd.redirects
        )):
            self._opaque("shell-stdin")

    # -- interpreters (security review 5, H4; review 6, H-2) ----------------------
    def _assignment(self, word: str, dyn: bool, cwd: Path | None, *,
                    standalone: bool = False) -> None:
        """``NAME=value`` — before a command, alone, after ``env`` or ``export``.

        The classifier says which variables carry code or redirect configuration;
        :func:`snowpea_core.security.envvars.judge` lets a value through only when
        it can say why it is harmless (security review 7, H7-1).  What the value
        names is judged like an operand of a program nobody knows: ``X=<a control
        file>; echo … > $X`` names that file."""
        name, _, value = word.partition("=")
        appended = name.endswith("+")
        name = name.rstrip("+").split("[", 1)[0]
        if name.upper() == "PATH" and self.view.path_values is not None:
            folders = None if appended or not (self.root and self._line == 0) else \
                path_folders(value, self.ctx.user_home)
            if folders is None:
                self.view.path_values = None
            else:
                self.view.path_values.append(folders)
        why = _env.judge(name, value, dyn, cwd=cwd, user_home=self.ctx.user_home,
                         home=self.ctx.home)
        scope = self._scope_here()
        if why == f"env-code:{name.upper()}" and standalone and not appended \
                and scope is not None and name in scope.local and not _env.is_watched(name):
            # m21: a name only ever assigned on its own and never exported is the
            # shell's own: nothing the line runs can read it (review 8, M8-2 is
            # about what a program reads from its environment).
            why = None
        if why is not None:
            self._opaque(why)
            if _env.is_code(name) and value.strip() and self.literals:
                # m21: ``EDITOR='rm -rf ~' git commit`` — a variable that names a
                # command line: what it plainly says is judged against the block
                # list (its unseen part stays one), whatever security.unknownCode says.
                sub = ShellView()
                try:
                    _Probe(self.request, sub, literals=False).line(value, cwd, MAX_DEPTH - 1)
                except Exception:  # noqa: BLE001 - a best effort never breaks the judgement
                    sub = ShellView()
                self.view.literal_commands.extend(sub.commands)
                self.view.literal_commands.extend(sub.literal_commands)
        for part in value.split(":"):
            if part.startswith(("/", "~", "./", "../", "$HOME", "${HOME}", "$SNOWPEA_HOME",
                                "${SNOWPEA_HOME}", "$PWD", "${PWD}", "$TMPDIR", "${TMPDIR}")):
                self._effect(part, cwd, "read", program="assign")

    def _inline(self, text: str, language: str, cwd: Path | None, depth: int) -> None:
        """Inline code: its primitives and its string literals that read as command lines."""
        literal_commands(text, self.request, self.view)
        self.apply_code(_code.analyse(text, language), cwd, depth, count_opaque=True)

    def _code_word(self, text: str, dyn: bool, language: str, cwd: Path | None,
                   depth: int) -> None:
        """Code given as one word of the line.  A word that is nothing but an
        expansion (``python -c "$X"``) is code nobody can read; an expansion inside
        the code is text the shell fills in before the interpreter parses it."""
        if dyn:
            if re.fullmatch(r"\s*(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(.*\)|`.*`)\s*", text,
                            re.DOTALL):
                self._opaque("inline-dynamic")
                return
            self._opaque("code:interpolated")
        self._inline(text, language, cwd, depth)

    def _interp(self, family: str, rest: list[str], rest_dyn: list[bool],
                     cwd: Path | None, depth: int, piped_in: bool, heredoc: bool) -> None:
        """What an interpreter is asked to run: inline code, a module, a script, its stdin."""
        flags = _c.INTERPRETERS.get(family, frozenset())
        spec = _SPECS.get(family, _Spec(
            code="".join(flag[1] for flag in flags if len(flag) == 2),
            word_code=tuple(sorted(flag for flag in flags if len(flag) > 2))))
        found = _invocation(spec, rest, rest_dyn)
        language = _code.LANGUAGES.get(family, family)
        for name, dyn in found.preload:
            self._preload(name, dyn, cwd, depth, language)
        operands = list(zip(rest[found.after:], rest_dyn[found.after:], strict=False))
        if found.code:
            for text, code_dyn in found.code:
                self._code_word(text, code_dyn, language, cwd, depth)
            if found.inplace:
                # ``perl -i -pe …  files``: the files are rewritten in place.
                for word, dyn in operands:
                    self._effect(word, cwd, "write", dynamic=dyn)
            return
        if found.module is not None:
            self._module(found.module, operands, cwd, depth)
            return
        if found.info or (found.self_run and not operands):
            # ``--version``; ``node --test`` with nothing named.  (``node --watch
            # x.js`` and ``node --test x.js`` still run x.js: it is read below.)
            return
        if found.self_run and family == "php":
            operands = operands[1:]  # ``php -S host:port [router.php]``
            if not operands:
                return
        if family == "php" and "-f" in rest[:found.after]:
            index = rest.index("-f")
            operands = list(zip(rest[index + 1:index + 2], rest_dyn[index + 1:index + 2],
                                strict=False)) or operands
        if operands and operands[0][0] != "-":
            if isinstance(operands[0][0], _c.ProcessWord) or _STDIN_FILE.fullmatch(operands[0][0]):
                # ``python3 <(curl …)`` / ``python3 /dev/stdin``: code from a pipe.
                self._opaque("interpreter-stdin")
            elif operands[0][1]:
                self._opaque("script-dynamic")
            else:
                self._script(operands[0][0], cwd, depth, language)
            return
        self._stdin_code(language, cwd, depth, piped_in, heredoc)

    def _stdin_code(self, language: str, cwd: Path | None, depth: int, piped_in: bool,
                    heredoc: bool) -> None:
        """An interpreter with no script reads its code from stdin: a here-document or
        here-string is analysed like ``-c`` code, a literal ``printf``/``echo`` feeding
        the pipe and a readable ``< file`` too; anything else cannot be read."""
        cmd = self._cmd
        if heredoc and cmd is not None and cmd.stdin_text is not None:
            if cmd.stdin_dynamic:
                self._opaque("code:stdin")
            else:
                self._inline(cmd.stdin_text, language, cwd, depth)
            return
        redirect = next(((target, dyn) for op, target, dyn in (cmd.redirects if cmd else [])
                         if op == "<"), None)
        if redirect is not None:
            if redirect[1]:
                self._opaque("code:stdin")
            else:
                self._script(redirect[0], cwd, depth, language)
            return
        feeder = self._feeder if piped_in else None
        if feeder is not None and feeder.words and not any(feeder.dynamic) \
                and not feeder.piped_in and not feeder.heredoc:
            program = _c._program(feeder.words[0])
            text = _echo_text(list(feeder.words)) if program in ("echo", "printf") else None
            if text is not None:
                self._inline(text, language, cwd, depth)
                return
            if program == "cat" and len(feeder.words) == 2 and not feeder.words[1].startswith("-"):
                self._script(feeder.words[1], cwd, depth, language)
                return
        self._opaque("code:stdin")

    def _preload(self, name: str, dyn: bool, cwd: Path | None, depth: int,
                 language: str) -> None:
        """``node -r x`` / ``--import x`` / ``--loader x``: code that runs before the script."""
        if dyn or not name:
            self._opaque("preload")
            return
        if name.startswith(("./", "../", "/", "~")) or name.endswith((".js", ".cjs", ".mjs",
                                                                      ".ts")):
            self._script(name, cwd, depth, language)
            return
        # A package of node_modules: the project's own, unless the agent wrote it.
        package = "/".join(name.split("/")[:2] if name.startswith("@") else name.split("/")[:1])
        if cwd is None or _code.written.under(cwd / "node_modules" / package):
            self._opaque("preload")

    def _module_file(self, name: str, cwd: Path | None, *, imported: bool = False,
                     beside: Path | None = None) -> str | None:
        """The file ``python -m name`` would run (``imported``: the file ``import
        name`` runs) when the project holds it; ``beside``: the importing script's
        folder, searched first like Python does."""
        if not re.fullmatch(r"[A-Za-z_][\w.]*", name):
            return None
        relative = Path(*name.split("."))
        tail = "__init__.py" if imported else "__main__.py"
        bases = [base for base in (beside, cwd, cwd / "src" if cwd is not None else None)
                 if base is not None]
        for base in bases:
            for candidate in (base / relative.with_suffix(".py"), base / relative / tail):
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    continue
        return None

    def _imported(self, finding: _code.Finding, cwd: Path | None, depth: int,
                  beside: Path | None) -> None:
        """Project files the code pulls in — ``import helper``, ``require('./x')`` —
        are read like the script itself: importing a file runs it."""
        files: list[tuple[str, str]] = []
        for name in finding.imports:
            parts = name.split(".")
            for width in range(1, len(parts) + 1):
                path = self._module_file(".".join(parts[:width]), cwd, imported=True,
                                         beside=beside)
                if path is not None:
                    files.append((path, "python"))
        for raw in finding.requires:
            base = beside if raw.startswith(".") and beside is not None else cwd
            if base is None and not raw.startswith("/"):
                continue
            start = Path(raw) if raw.startswith("/") else base / raw  # type: ignore[operator]
            for candidate in (start, Path(f"{start}.js"), Path(f"{start}.cjs"),
                              Path(f"{start}.mjs"), start / "index.js"):
                try:
                    if candidate.is_file():
                        files.append((str(candidate), "node"))
                        break
                except OSError:
                    continue
        seen = {path for path, _ in self.view.scripts}
        for path, language in files:
            if path in seen:
                continue
            if len(seen) >= MAX_IMPORTED or depth >= MAX_DEPTH:
                # Too many, or too deep, to read.  The project's own files are let
                # be; one the agent wrote that stays unread is not.
                if _code.is_recent(path):
                    self._opaque("code:imports")
                    return
                continue
            seen.add(path)
            self._script(path, cwd, depth + 1, language,
                         root=beside if language == "python" else None)

    def _module(self, module: tuple[str, bool], operands: list[tuple[str, bool]],
                cwd: Path | None, depth: int) -> None:
        """``python -m <module>`` (security review 6, H-2)."""
        name, dyn = module
        root = name.split(".", 1)[0]
        if dyn or not name:
            self._opaque("code:module")
            return
        if root in ("snowpea_core", "snowpea", "snowpea_sdk"):
            self._control("snowpea-cli")
            return
        if root == "pip":
            return  # judged by the classifier's own pip rules
        local = self._module_file(name, cwd)
        if local is not None:
            self._script(local, cwd, depth, "python")
            return
        if name == "timeit":
            # Its operands (and ``-s`` values) are statements: the setup, then the rest,
            # run as one piece of code.
            skip = False
            statements: list[str] = []
            for word, _dyn in operands:
                if skip:
                    skip = False
                    continue
                if word in ("-n", "-r", "-u", "--number", "--repeat", "--unit"):
                    skip = True
                elif word in ("-s", "--setup"):
                    continue  # its value is the next word: a statement like the others
                elif not word.startswith("-") or " " in word:
                    statements.append(word)
            if statements:
                self._inline("\n".join(statements), "python", cwd, depth)
            return
        if root in _COMMAND_MODULES:
            # ``python -m uv run python x.py`` is ``uv run python x.py``.
            self.words([root, *[word for word, _ in operands]],
                       [False, *[dyn for _, dyn in operands]], cwd, depth + 1,
                       piped_in=False, heredoc=False, stdin_args=False)
            return
        if root in _APP_MODULES:
            # ``uvicorn main:app``, ``streamlit run app.py``, ``flask --app main run``:
            # the project module or file it is pointed at runs.
            for word, word_dyn in operands:
                if word_dyn or word.startswith("-"):
                    continue
                target = word.split(":", 1)[0]
                if word.endswith(".py"):
                    self._script(word, cwd, depth, "python")
                elif re.fullmatch(r"[A-Za-z_][\w.]*", target):
                    path = self._module_file(target, cwd, imported=True)
                    if path is not None:
                        self._script(path, cwd, depth, "python")
            return
        if root == "coverage" and operands and operands[0][0] != "run":
            return  # report, html, xml, combine, erase: nothing of the project runs
        if root in _RUNNER_MODULES:
            words = [word for word, _ in operands]
            if "-m" in words and words.index("-m") + 1 < len(operands):
                at = words.index("-m") + 1
                self._module(operands[at], operands[at + 1:], cwd, depth)
                return
            script = next(((w, d) for w, d in operands if w.endswith(".py")), None)
            if script is None or script[1]:
                self._opaque("code:module")
            else:
                self._script(script[0], cwd, depth, "python")
            return
        if name in SAFE_MODULES or root in SAFE_MODULES:
            if name in ("zipfile", "tarfile"):
                # ``-e archive [dest]`` extracts: everything below ``dest`` is written.
                words = [word for word, _ in operands]
                for flag in ("-e", "--extract"):
                    if flag in words:
                        dest = operands[words.index(flag) + 2:words.index(flag) + 3]
                        raw, dest_dyn = dest[0] if dest else (".", False)
                        self._effect(raw, cwd, "write", recursive=True, dynamic=dest_dyn,
                                     program="code")
            return
        self._opaque("code:module")

    # -- make / npm run: the project's own command lines ---------------------------
    def _project_runner(self, *args: Any) -> None:
        """:meth:`_project_lines`, with what the project file raises kept apart (m21)."""
        self._in_file(self._project_lines, *args)

    def _project_lines(self, program: str, rest: list[str], rest_dyn: list[bool],
                       cwd: Path | None, depth: int) -> None:
        """``make target`` / ``npm run name``: the line the project's file gives is read.

        A project file that was there before the session is the project's own
        (that is what project trust is about): its literal lines are judged, what
        they compute is not held against them.  One the agent wrote this session
        counts everything, and what cannot be read of it is opaque.
        """
        if cwd is None or depth > MAX_DEPTH:
            return
        if program in ("make", "gmake"):
            self._make(rest, rest_dyn, cwd, depth)
            return
        if program == "deno":
            self._deno_task(rest, rest_dyn, cwd, depth)
            return
        if program not in ("npm", "yarn", "pnpm", "bun"):
            return
        operands = [w for w, d in zip(rest, rest_dyn, strict=False)
                    if not w.startswith("-") and not d]
        if not operands:
            return
        name: str | None = None
        if operands[0] in ("run", "run-script", "rum", "urn") and program != "bun" or (
                program == "bun" and operands[0] == "run"):
            name = operands[1] if len(operands) > 1 else None
        elif program == "npm" and operands[0] in ("test", "t", "tst", "start", "stop",
                                                  "restart"):
            name = {"t": "test", "tst": "test"}.get(operands[0], operands[0])
        elif program in ("yarn", "pnpm"):
            name = operands[0]
        if name is None:
            return
        script = _code.read_script("package.json", cwd, default="json")
        if script is None or script.digest == "missing":
            return
        self.view.scripts.append((script.path, script.digest))
        try:
            data = json.loads(script.text) if script.text is not None else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            if script.recent:
                self._opaque("npm-script")
            return
        table = data.get("scripts")
        lines = [table[key] for key in (f"pre{name}", name, f"post{name}")
                 if isinstance(table.get(key), str)] if isinstance(table, dict) else []
        for line in lines:
            sub = ShellView()
            probe = _Probe(self.request, sub)
            probe.line(line, cwd, depth + 1)
            probe.finish()
            self._merge(sub, count_opaque=script.recent)

    def _deno_task(self, rest: list[str], rest_dyn: list[bool], cwd: Path,
                   depth: int) -> None:
        """``deno task <name>``: the line the project's ``deno.json`` gives is read,
        like an npm script's (security review 7, H7-2)."""
        operands = [w for w, d in zip(rest, rest_dyn, strict=False)
                    if not w.startswith("-") and not d]
        if operands[:1] != ["task"] or len(operands) < 2:
            return
        name = operands[1]
        for candidate in ("deno.json", "deno.jsonc"):
            script = _code.read_script(candidate, cwd, default="json")
            if script is None or script.digest == "missing":
                continue
            self.view.scripts.append((script.path, script.digest))
            try:
                data = json.loads(script.text) if script.text is not None else None
            except (ValueError, RecursionError):
                data = None  # comments (``.jsonc``) or a broken file
            tasks = data.get("tasks") if isinstance(data, dict) else None
            entry = tasks.get(name) if isinstance(tasks, dict) else None
            line = entry.get("command") if isinstance(entry, dict) else entry
            if not isinstance(line, str):
                if script.recent:
                    self._opaque("deno-task")
                return
            sub = ShellView()
            probe = _Probe(self.request, sub)
            probe.line(line, cwd, depth + 1)
            probe.finish()
            self._merge(sub, count_opaque=script.recent)
            return

    def _make(self, rest: list[str], rest_dyn: list[bool], cwd: Path, depth: int) -> None:
        folder: Path | None = cwd
        makefile: str | None = None
        targets: list[str] = []
        index = 0
        while index < len(rest):
            word, dyn = rest[index], rest_dyn[index]
            if word in ("-C", "--directory", "-f", "--file", "--makefile"):
                value = rest[index + 1] if index + 1 < len(rest) else ""
                value_dyn = rest_dyn[index + 1] if index + 1 < len(rest) else True
                if word in ("-C", "--directory"):
                    folder = None if value_dyn or folder is None else folder / value
                else:
                    makefile = None if value_dyn else value
                    if value_dyn:
                        folder = None
                index += 2
                continue
            if word in ("-j", "-l", "-o", "-W", "-I", "--jobs", "--load-average"):
                index += 2 if index + 1 < len(rest) and rest[index + 1].isdigit() else 1
                continue
            if not word.startswith("-") and "=" not in word and not dyn:
                targets.append(word)
            index += 1
        if folder is None:
            return
        script = None
        for candidate in ([makefile] if makefile else ["GNUmakefile", "makefile", "Makefile"]):
            script = _code.read_script(candidate, folder, default="make")
            if script is not None and script.digest != "missing":
                break
        if script is None or script.digest == "missing":
            return
        self.view.scripts.append((script.path, script.digest))
        if script.text is None:
            if script.recent:
                self._opaque("make-file")
            return
        recipes, complete = _make_recipes(script.text, targets)
        if not complete and script.recent:
            self._opaque("make-recipe")
        for line in recipes:
            if "$" in line or "`" in line:
                # make's own variables and functions: not a line a shell parser reads.
                if script.recent:
                    self._opaque("make-recipe")
                continue
            sub = ShellView()
            probe = _Probe(self.request, sub)
            probe.line(line, folder, depth + 1)
            probe.finish()
            self._merge(sub, count_opaque=script.recent)

    # -- code and scripts (security review 5, H4) --------------------------------
    def apply_code(self, finding: _code.Finding, cwd: Path | None, depth: int, *,
                   count_opaque: bool, beside: Path | None = None) -> None:
        """Add what code would do: its commands, its shell lines, its writes and reads.

        ``beside``: the folder of the script the code came from (its imports are
        looked for there first)."""
        if finding.imports or finding.requires:
            self._imported(finding, cwd, depth, beside)
        for words in finding.commands:
            if not words:
                continue
            self.view.commands.append(Command(words=list(words), dynamic=[False] * len(words),
                                              cwd=cwd, line=max(self._line, 0),
                                              index=self._index))
            self.view.texts.append(" ".join(words))
        for raw, destructive, recursive in finding.writes:
            self._effect(raw, cwd, "write", destructive=destructive, recursive=recursive,
                         program="code")
        for raw in finding.reads:
            self._effect(raw, cwd, "read", program="code")
        for text in finding.mentions:
            self.view.mentions.append((text, cwd, text in finding.read_only))
        for raw in finding.links:
            # A link to a file: whatever is later done through the new name is done
            # to it (security review 7, H7-5) — an operand of ``ln``, not a read.
            self._effect(raw, cwd, "read", program="ln")
        for raw in finding.linked:
            self._effect(raw, cwd, "write", program=LINK)
        for text in finding.lines:
            if count_opaque:
                self.line(text, cwd, depth + 1)
            else:
                sub = ShellView()
                probe = _Probe(self.request, sub)
                probe.line(text, cwd, depth + 1)
                probe.finish()
                self._merge(sub, count_opaque=False)
        for argv in finding.argv:
            # A spawned list of words is a command whose computed words are ``$X``.
            words = [word for word, _ in argv]
            dynamic = [dyn for _, dyn in argv]
            if not words or depth > MAX_DEPTH:
                continue
            self.view.texts.append(" ".join(words))
            sub = ShellView()
            probe = _Probe(self.request, sub)
            probe._line = 0
            probe.words(words, dynamic, cwd, depth + 1, piped_in=False, heredoc=False,
                        stdin_args=False)
            probe.finish()
            self._merge(sub, count_opaque=count_opaque)
        if count_opaque:
            for why in finding.opaque:
                self._opaque(why)
        for why in finding.control:
            self._control(why)

    def _written_here(self, path: str) -> bool:
        """A write earlier on the line lands on ``path`` (or may: its target is unknown)."""
        target = os.path.normpath(path)
        for effect in self.view.effects:
            if effect.kind != "write" or effect.program in _ATTRIBUTE_ONLY:
                continue
            if effect.dynamic:
                return True
            raw = effect.raw
            if raw.startswith("~/"):
                raw = str(self.ctx.user_home) + raw[1:]
            if not raw.startswith("/"):
                if effect.cwd is None:
                    return True
                raw = str(effect.cwd / raw)
            if os.path.normpath(raw) == target:
                return True
        return False

    def _cli_vetted(self, words: list[str], dynamic: list[bool], cwd: Path | None) -> bool:
        """``snowpea <a vetted read-only shape>`` on the agent's own line, run by the
        installed snowpea (m21).  Everything else stays a control action."""
        if not (self.root and self._line == 0) or any(dynamic) or self.ctx.home is None \
                or self.view.path_values is None:
            return False
        if _c._program(words[0]) != "snowpea" or not cli_read_only(words[1:]):
            return False
        entries = cli_entry_points()
        if not entries:
            return False
        first = words[0]
        if "/" in first or first.startswith("~"):
            if first.startswith("~/"):
                expanded = str(self.ctx.user_home) + first[1:]
            elif first.startswith("~"):
                return False
            else:
                expanded = first
            if not os.path.isabs(expanded):
                if cwd is None:
                    return False
                expanded = str(cwd / expanded)
            return _real_file(expanded) in entries
        found_any = False
        for search in candidate_paths(self.view.path_values, self.ctx.user_home, cwd):
            hit = _which(first, search)
            if hit is None:
                continue
            if hit not in entries:
                return False
            found_any = True
        return found_any

    def _merge(self, sub: ShellView, *, count_opaque: bool) -> None:
        if sub.path_values is None or sub.path_values:
            self.view.path_values = None  # a script set PATH: not followed
        self.view.unknown_programs.extend(sub.unknown_programs)
        self.view.stateful_shells |= sub.stateful_shells
        self.view.commands.extend(sub.commands)
        self.view.effects.extend(sub.effects)
        self.view.texts.extend(sub.texts)
        self.view.literal_commands.extend(sub.literal_commands)
        self.view.scripts.extend(sub.scripts)
        self.view.mentions.extend(sub.mentions)
        for why in sub.control:
            if why in sub.details:
                self.view.details.setdefault(why, sub.details[why])
            self._control(why)  # snowpea's own CLI asks whoever wrote the script
        if count_opaque:
            for why, where in sub.details.items():
                self.view.details.setdefault(why, where)
            for why in sub.opaque:
                # Copied as it is: the sub-walk tagged the shell it came from (m21).
                if why not in self.view.opaque:
                    self.view.opaque.append(why)
                self._attribute(why)

    def _script(self, *args: Any, **kwargs: Any) -> None:
        """:meth:`_script_file`, with what its content raises kept apart (m21)."""
        self._in_file(self._script_file, *args, **kwargs)

    def _in_file(self, walk: Any, *args: Any, **kwargs: Any) -> None:
        start = len(self.view.effects)
        self._in_script += 1
        try:
            walk(*args, **kwargs)
        finally:
            self._in_script -= 1
            self.view.script_spans.append((start, len(self.view.effects)))

    def _script_file(self, raw: str, cwd: Path | None, depth: int, default: str, *,
                     root: Path | None = None, strict: bool = False,
                     same_shell: bool = False,
                     args: list[tuple[str, bool]] | None = None) -> None:
        """Read the script file ``raw`` and judge its text (H4 b).  ``strict``: it
        is the agent's own whatever its timestamps say (review 8, H8-3 c).
        ``same_shell``: it runs in this very shell (``source``), not a new one.
        ``args``: the words it is run with — its ``$1``… (m21)."""
        if depth > MAX_DEPTH:
            self._opaque("nesting")
            return
        script = _code.read_script(raw, cwd, default=default)
        if script is None:
            return
        if strict and not script.recent:
            from dataclasses import replace as _replace

            script = _replace(script, recent=True)
        self.view.scripts.append((script.path, script.digest))
        if "snowpea_core" in Path(script.path).parts:
            self._control("snowpea-cli")
        if script.text is None:
            if self._written_here(script.path):
                # m21: a script this very line writes before it runs it: what it
                # holds is not known, and no setting lets that through unasked.
                self._opaque("script-written")
            elif script.recent:
                why = f"script-{script.digest if ':' not in script.digest else 'binary'}"
                if why not in self.view.details:
                    self.view.details[why] = {**self._where(), "subject": script.path}
                self._opaque(why)
                if same_shell:
                    self.view.stateful_shells.add(self._shell_id)  # m21: it may change ours
            return
        if script.language == "shell":
            sub = ShellView()
            probe = _Probe(self.request, sub,
                           positional=None if same_shell else args,
                           outer=self._outer + "\n" + (self._text or ""),
                           file=script.path, budget=self._budget)
            if same_shell:
                probe._shell_id = self._shell_id
            probe.line(script.text, cwd, depth + 1)
            probe.finish()
            # A pre-existing script the person owns: what it plainly does is
            # judged, what it computes is not held against it.
            self._merge(sub, count_opaque=script.recent)
            return
        # A language nothing here reads (lua, R, awk…) comes back ``code:language``:
        # opaque when the script is the agent's own.
        finding = _code.analyse(script.text, script.language, path=script.path)
        self.apply_code(finding, cwd, depth, count_opaque=script.recent,
                        beside=root or Path(script.path).parent)

    # -- writes this module judges itself -------------------------------------
    def _own_writes(  # noqa: C901 - one table of the write programs, kept together
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        recursive = _recursive_flag(program, rest)
        operands = _c._operands(rest, rest_dyn, _VALUED_BY.get(program, frozenset()))
        target_dir = _option_value(rest, rest_dyn, ("-t", "--target-directory"))
        if program == "gio":
            if not operands:
                return
            sub, operands = operands[0][0], operands[1:]
            if sub in ("trash", "remove", "move", "rename"):
                for word, dyn in operands:
                    self._effect(word, cwd, "write", destructive=True, dynamic=dyn)
            elif sub in ("copy", "mkdir", "set", "save"):
                for word, dyn in operands[-1:] if sub == "copy" else operands:
                    self._effect(word, cwd, "write", dynamic=dyn)
            return
        if program in ("chmod", "chown", "chgrp"):
            if not any(w.startswith("--reference") for w in rest):
                operands = operands[1:]
            for word, dyn in operands:
                self._effect(word, cwd, "write", destructive=recursive, recursive=recursive,
                             dynamic=dyn)
            return
        if program in ("chattr", "setfacl"):
            if program == "chattr":
                operands = [(w, d) for w, d in operands if not w.startswith(("+", "="))]
            for word, dyn in operands:
                self._effect(word, cwd, "write", destructive=recursive, recursive=recursive,
                             dynamic=dyn)
            return
        if program in ("rm", "srm", "trash", "trash-put"):
            for word, dyn in operands:
                self._effect(word, cwd, "write", destructive=recursive or program != "rm",
                             recursive=recursive, dynamic=dyn)
            return
        if program in ("shred",):
            for word, dyn in operands:
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn)
            return
        if program in ("rmdir", "unlink", "truncate", "touch", "mkdir", "tee"):
            for word, dyn in operands:
                self._effect(word, cwd, "write", dynamic=dyn)
            return
        if program in ("rename", "mmv"):
            # ``rename from to files…`` / ``rename 's/a/b/' files…``: every file is moved.
            for word, dyn in operands[1:]:
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn, relocates=True)
            return
        # mv, cp, ln, install, rsync: sources, then where they land.
        if target_dir is not None:
            sources, dest = operands, target_dir
        elif len(operands) >= 2:
            sources, dest = operands[:-1], operands[-1]
        else:
            sources, dest = operands, None
        moving = program == "mv"
        symbolic = program == "ln" and any(
            w in ("--symbolic",) or (w.startswith("-") and not w.startswith("--") and "s" in w[1:])
            for w in rest
        )
        linking = program == "cp" and any(
            w in ("--symbolic-link", "--link") or (
                w.startswith("-") and not w.startswith("--") and ("s" in w[1:] or "l" in w[1:]))
            for w in rest
        )
        for word, dyn in sources:
            if moving:
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn, relocates=True)
            elif program == "ln" and not symbolic:
                # A hard link is the same file under a new name: whatever is later
                # written through it is written to the source (security review 5, M6),
                # and what it holds is read through it.
                self._effect(word, cwd, "write", dynamic=dyn, relocates=True)
            elif program == "ln" or linking:
                # A symbolic link (``ln -s``, ``cp -s``, ``cp -l``): what is later
                # written through the new name lands on the source, and the link
                # itself may be made in the same line (security review 7, H7-5).
                self._effect(word, cwd, "read", dynamic=dyn, program="ln")
            else:
                self._effect(word, cwd, "read", recursive=recursive, dynamic=dyn)
        if dest is None:
            return
        deletes = program == "rsync" and any(w.startswith("--del") or w.startswith("--remove")
                                             for w in rest)
        tree = recursive or program == "rsync" or moving
        made = LINK if program == "ln" or linking else ""
        self._effect(dest[0], cwd, "write", destructive=deletes, recursive=tree,
                     dynamic=dest[1], program=made)
        landing = _landing(dest[0], cwd, target_dir is not None or len(sources) > 1)
        if landing is not None:
            for word, dyn in sources:
                name = word.rstrip("/").rsplit("/", 1)[-1]
                if name and not dyn and name not in (".", ".."):
                    self._effect(str(landing / name), None, "write", recursive=tree,
                                 dynamic=dest[1], program=made)

    def _archive_removal(self, program: str, rest: list[str], rest_dyn: list[bool],
                         cwd: Path | None) -> None:
        """``tar --remove-files`` / ``zip -m``: the sources are deleted once archived."""
        if program == "zip":
            moves = any(w == "--move" or (w.startswith("-") and not w.startswith("--")
                                          and "m" in w[1:]) for w in rest)
            if not moves:
                return
            operands = _c._operands(rest, rest_dyn, frozenset({"-b", "-n", "-t", "-tt", "-x",
                                                               "-i", "-P", "--password", "-O",
                                                               "--output-file", "-s", "-Z"}))
            sources = operands[1:]
        else:
            if not any(w.startswith("--remove-f") for w in rest):
                return
            operands = _c._operands(rest, rest_dyn, frozenset(
                {"-f", "--file", "-C", "--directory", "-T", "--files-from", "-X",
                 "--exclude-from", "-b", "--blocking-factor", "-H", "--format", "-I",
                 "--use-compress-program", "--exclude"}))
            if operands and not operands[0][0].startswith("-") and "/" not in operands[0][0] \
                    and re.fullmatch(r"[A-Za-z]*f[A-Za-z]*", operands[0][0]) and not any(
                        w.startswith("-") and "f" in w[1:] for w in rest if not w.startswith("--")):
                operands = operands[2:]  # ``tar cf a.tar --remove-files …``: letters, archive
            sources = operands
        for word, dyn in sources:
            self._effect(word, cwd, "write", destructive=True, recursive=True, dynamic=dyn,
                         program=program, relocates=True)

    def _git_tree(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        """A git command that rewrites (or cleans) the working tree it runs in."""
        root = cwd
        index = 0
        sub = ""
        while index < len(rest):
            word = rest[index]
            if word == "-C" and index + 1 < len(rest):
                if rest_dyn[index + 1]:
                    root = None
                else:
                    root = _c._resolve(rest[index + 1], root, self.ctx)
                index += 2
                continue
            if word in ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env"):
                if word == "--work-tree" and index + 1 < len(rest):
                    root = None if rest_dyn[index + 1] else _c._resolve(rest[index + 1], root,
                                                                        self.ctx)
                index += 2
                continue
            if word.startswith("--work-tree="):
                root = _c._resolve(word.split("=", 1)[1], root, self.ctx)
            if word.startswith("-"):
                index += 1
                continue
            sub = word
            break
        if sub not in _GIT_TREE:
            return
        tail = rest[index + 1 :]
        if sub == "reset" and not any(w in ("--hard", "--merge", "--keep") for w in tail):
            return
        if sub == "stash" and tail[:1] in (["list"], ["show"]):
            return
        destroy = sub in _GIT_DESTROY
        if root is None:
            self._effect("$GIT_WORK_TREE", None, "write", destructive=destroy, recursive=True,
                         dynamic=True, program="git")
            return
        self._effect(str(root), None, "write", destructive=destroy, recursive=True,
                     program="git")


_GIT_SECTION = re.compile(r"^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+\"[^\"]*\")?\s*\]\s*$")
#: Largest git configuration file read for aliases.
_MAX_GIT_CONFIG = 256 * 1024


def _git_config_files(root: Path | None, user_home: Path) -> list[Path] | None:
    """git's configuration files for a command run in ``root`` (system, the
    person's, the repository's); ``None`` when the repository's cannot be named."""
    files = [Path("/etc/gitconfig"), user_home / ".gitconfig",
             user_home / ".config" / "git" / "config"]
    if root is None:
        return None
    folder = root
    for _ in range(64):
        marker = folder / ".git"
        try:
            if marker.is_dir():
                files.append(marker / "config")
                return files
            if marker.is_file():
                # A worktree or a submodule: ``gitdir: <path>``, then ``commondir``.
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = folder / gitdir
                common = gitdir / "commondir"
                if common.is_file():
                    target = Path(common.read_text(encoding="utf-8",
                                                   errors="replace").strip())
                    gitdir = target if target.is_absolute() else gitdir / target
                files.append(gitdir / "config")
                files.append(gitdir / "config.worktree")
                return files
        except OSError:
            return None
        if folder.parent == folder:
            break
        folder = folder.parent
    return files  # not a repository: only the person's own configuration counts


def git_alias(name: str, root: Path | None, user_home: Path) -> str | None:
    """What ``git <name>`` stands for when ``name`` is not one of git's own commands.

    The alias text from git's configuration (``!line`` runs a shell line), ``""``
    when no configuration file defines it (then it is ``git-<name>``, a program
    like any other), ``None`` when that cannot be told: a configuration file
    that cannot be read or that includes another.  Files are only read."""
    files = _git_config_files(root, user_home)
    if files is None:
        return None
    found = ""
    wanted = name.lower()
    for path in files:
        try:
            if not path.is_file():
                continue
            if os.path.getsize(path) > _MAX_GIT_CONFIG:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        section = ""
        for raw in text.replace("\\\n", "").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            header = _GIT_SECTION.match(line)
            if header is not None:
                section = header.group(1).lower()
                if section in ("include", "includeif"):
                    return None
                continue
            if line.startswith("["):
                return None  # a section header this reader does not understand
            key, sep, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if section == "alias" and key == wanted:
                found = value if sep else ""
            elif section == "" and key == f"alias.{wanted}":
                found = value if sep else ""
    return found


# -- security review 8, H8-3: what git and PATH run that the line does not show ----

#: git configuration keys that make git run a program (or load another
#: configuration).  Setting one — ``git config``, ``git -c``,
#: ``--config-env`` — is opaque: what the program does is not on the line.
_GIT_RISKY_KEY = re.compile(
    r"(?i)^(?:core\.(?:fsmonitor|pager|sshcommand|hookspath|editor|askpass|gitproxy)|"
    r"diff\.external|diff\..+\.(?:textconv|command)|filter\..+\.(?:clean|smudge|process)|"
    r"credential\.helper|credential\..+\.helper|gpg\.program|gpg\..+\.program|"
    r"sequence\.editor|merge\..+\.driver|pager\..+|uploadpack\.packobjectshook|"
    r"include\.path|includeif\..+\.path|alias\..+|remote\..+\.(?:uploadpack|receivepack)|"
    r"ssh\.variant|protocol\..+\.allow|sendemail\..*smtpserver|web\.browser|"
    r"(?:browser|man|difftool|mergetool)\..+\.(?:cmd|path)|submodule\..+\.update|"
    r"core\.attributesfile|url\..+\.(?:insteadof|pushinsteadof)|.+\.(?:command|cmd))$")
#: ``git config`` options that take a value of their own.
_GIT_CONFIG_VALUED = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment",
                                "--value", "--url"})
_GIT_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l",
                               "--list", "--get-color", "--get-colorbool"})
_GIT_CONFIG_WRITES = frozenset({"--add", "--replace-all", "--unset", "--unset-all",
                                "--rename-section", "--remove-section"})


def git_config_sets(args: list[str]) -> list[tuple[str, str | None]]:
    """``(key, value)`` a ``git config …`` command (``args`` after ``config``) sets
    or removes (value ``None`` when removed); ``("*", None)`` for ``--edit``."""
    keys = _git_config_keys(args)
    positional = [word for word in args if not word.startswith("-")]
    out: list[tuple[str, str | None]] = []
    for key in keys:
        value: str | None = None
        if key in positional:
            at = positional.index(key)
            value = positional[at + 1] if at + 1 < len(positional) else None
        out.append((key, value))
    return out


def _git_config_keys(args: list[str]) -> list[str]:
    if any(word in ("-e", "--edit") for word in args):
        return ["*"]
    positional: list[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
            continue
        if word in _GIT_CONFIG_VALUED:
            skip = True
            continue
        if word.startswith("-"):
            continue
        positional.append(word)
    if positional[:1] in (["get"], ["list"]):
        return []
    if positional[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"]):
        return positional[1:2]
    if positional[:1] == ["edit"]:
        return ["*"]
    if any(word in _GIT_CONFIG_READS for word in args) and not any(
            word in _GIT_CONFIG_WRITES for word in args):
        return []
    if any(word in _GIT_CONFIG_WRITES for word in args):
        return positional[:1]
    return positional[:1] if len(positional) >= 2 else []


def git_risky(key: str, value: str | None = None) -> bool:
    """``key`` makes git run a program — unless ``value`` runs nothing (``cat``)."""
    if value is not None and value.strip() in _env.HARMLESS_VALUES and key != "*":
        return False
    return key == "*" or bool(_GIT_RISKY_KEY.match(key.strip()))


def git_session_risk(root: Path | None, user_home: Path) -> str | None:
    """``git-hook`` when the repository's hooks folder holds a file the agent wrote
    (or changed), ``git-program`` when the agent wrote the repository's
    configuration and it sets a key that runs a program; ``None`` otherwise."""
    files = _git_config_files(root, user_home)
    if not files or len(files) < 4:
        return None
    config = files[3]
    gitdir = config.parent
    hooks = [gitdir / "hooks"]
    try:
        text = config.read_text(encoding="utf-8", errors="replace") if config.is_file() else ""
    except OSError:
        text = ""
    for match in re.finditer(r"(?im)^\s*hookspath\s*=\s*(.+?)\s*$", text):
        value = match.group(1).strip().strip('"')
        if value:
            path = Path(os.path.expanduser(value))
            hooks.append(path if path.is_absolute() else (root or gitdir) / path)
    if any(written.under(folder) for folder in hooks):
        return "git-hook"
    if written.contains(config) or (len(files) > 4 and written.contains(files[4])):
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            header = _GIT_SECTION.match(line)
            if header is not None:
                section = header.group(1).lower()
                sub = re.match(r'^\s*\[\s*[A-Za-z0-9.-]+\s+"([^"]*)"', line)
                if sub:
                    section = f"{section}.{sub.group(1).lower()}"
                continue
            key = line.partition("=")[0].strip().lower()
            if key and not line.startswith(("#", ";")) and git_risky(f"{section}.{key}"):
                return "git-program"
    return None


def path_program(name: str, cwd: Path | None) -> Path | None:
    """The file a bare program name runs when it is the agent's own (review 8,
    H8-3 c): found on the PATH the child gets, and written by the agent or in a
    temporary folder.  ``None`` for everything else (the system's programs)."""
    import shutil

    from snowpea_core.exec.local import sanitized_path

    if not name or "/" in name or "\\" in name or name in _c.RESERVED_WORDS:
        return None
    try:
        found = shutil.which(name, path=sanitized_path(os.environ.get("PATH", ""), cwd))
    except (OSError, ValueError):
        return None
    if not found:
        return None
    real = Path(os.path.realpath(found))
    if written.contains(real) or written.contains(found) or any(
            folder == real.parent or folder in real.parents for folder in temporary_dirs()):
        return real
    return None


# -- snowpea's own CLI, read only; PATH the line sets (m21) ------------------------------

#: ``snowpea <words> [options]`` that only read and print nothing secret: each
#: checked against the CLI's parser and handlers (``cli/main.py``,
#: ``cli/commands.py``).  ``session list`` → ``session.list``, ``daemon status``
#: → ``system.info`` + ``gateway.list`` (port, pid, version, counters — no
#: token), ``agents`` → ``agent.list``.  ``project list`` and ``sessions`` are
#: usage errors in every release (no such subcommand): nothing runs.  The
#: options listed are the only ones vetted; any other word keeps the run a
#: control action.
CLI_READS: dict[tuple[str, ...], frozenset[str]] = {
    ("session", "list"): frozenset({"--include-closed", "--json"}),
    ("daemon", "status"): frozenset({"--json"}),
    ("agents",): frozenset({"--json"}),
    ("project", "list"): frozenset(),
    ("sessions",): frozenset(),
}
#: ``--help`` after at most two of these words prints the parser's help and exits.
_CLI_HELP_WORD = re.compile(r"[a-z][a-z0-9-]{0,31}")
#: Words the CLI reads before argparse does (``remote up`` has a parser of its own).
_CLI_HELP_NEVER: frozenset[str] = frozenset({"remote"})


def cli_read_only(rest: list[str]) -> bool:
    """``snowpea <rest>`` is one of the vetted read-only shapes."""
    if rest in (["--version"], ["-V"]):
        return True
    if rest and rest[-1] in ("--help", "-h"):
        names = rest[:-1]
        return len(names) <= 2 and all(_CLI_HELP_WORD.fullmatch(word) for word in names) \
            and not (names[:1] and names[0] in _CLI_HELP_NEVER)
    for shape, options in CLI_READS.items():
        if tuple(rest[:len(shape)]) == shape:
            extra = rest[len(shape):]
            return len(set(extra)) == len(extra) and all(word in options for word in extra)
    return False


def cli_local_only(text: str) -> bool:
    """A vetted run that never reaches the daemon (``--help``, ``--version``)."""
    words = text.split()
    return bool(words) and words[-1] in ("--help", "-h", "--version", "-V")


def _real_file(path: str) -> str | None:
    try:
        return os.path.realpath(path) if os.path.isfile(path) else None
    except (OSError, ValueError):
        return None


def _which(name: str, search: str) -> str | None:
    import shutil

    try:
        found = shutil.which(name, path=search)
    except (OSError, ValueError):
        return None
    return _real_file(found) if found else None


def _daemon_path(cwd: Path | None) -> str:
    from snowpea_core.exec.local import sanitized_path

    return sanitized_path(os.environ.get("PATH", ""), cwd)


def _agent_place(path: Path) -> bool:
    """A file or folder the agent wrote, or one in a temporary folder."""
    real = Path(os.path.realpath(path))
    return written.contains(real) or written.under(real) or any(
        folder == real or folder in real.parents for folder in temporary_dirs())


def cli_entry_points() -> frozenset[str]:
    """The installed snowpea program: the file ``snowpea`` names on the daemon's own
    ``PATH``, and the one beside the daemon's Python (its venv) — never one the
    agent wrote or one in a temporary folder."""
    import sys

    found: set[str] = set()
    hit = _which("snowpea", _daemon_path(None))
    if hit:
        found.add(hit)
    beside = _real_file(str(Path(sys.executable).parent / "snowpea"))
    if beside:
        found.add(beside)
    return frozenset(path for path in found if not _agent_place(Path(path)))


def path_folders(value: str, user_home: Path) -> list[str] | None:
    """``PATH=value`` as folders (``"$PATH"`` for the inherited value), or ``None``
    when one entry is not a plain absolute folder."""
    out: list[str] = []
    for entry in value.split(":"):
        if entry in ("$PATH", "${PATH}"):
            out.append("$PATH")
            continue
        text = entry
        for token in ("${HOME}", "$HOME", "~"):
            if text == token or text.startswith(token + "/"):
                text = str(user_home) + text[len(token):]
                break
        if not text.startswith("/") or re.search(r"[$`*?\[\]\\~]", text):
            return None
        out.append(os.path.normpath(text))
    return out


def candidate_paths(values: list[list[str]] | None, user_home: Path,
                    cwd: Path | None) -> list[str]:
    """Every ``PATH`` a command of the line may run with: the inherited one, then
    each value the line gave it so far (a prefix assignment covers one command
    only, so the inherited one always counts too)."""
    del user_home
    base = _daemon_path(cwd)
    out = [base]
    current = base
    for folders in values or []:
        current = os.pathsep.join(current if folder == "$PATH" else folder
                                  for folder in folders)
        out.append(current)
    return list(dict.fromkeys(out))


#: Programs the shell runs itself, without looking at ``PATH``.
_BUILTINS: frozenset[str] = frozenset(
    {"cd", "echo", "printf", "export", "unset", "set", "test", "[", "[[", "true", "false",
     ":", "read", "type", "command", "builtin", "exit", "return", "shift", "local",
     "declare", "typeset", "readonly", "eval", "exec", "source", ".", "pwd", "pushd",
     "popd", "alias", "unalias", "wait", "jobs", "trap", "umask", "hash", "let", "times"}
)


def path_change_harmless(view: ShellView, ctx: Any) -> bool:
    """The ``PATH`` values the line sets change no program it runs (m21): every
    folder added is the person's own (not one the agent wrote, not temporary,
    not inside a project), nothing on the line writes into one of them, and
    every program the line runs by name is the same file under every value
    (snowpea's own program aside, when the new value finds the installed one)."""
    if not view.path_values:
        return False
    known = [*(getattr(ctx, "projects", ()) or ()), getattr(ctx, "workdir", None)]
    owners = [Path(os.path.normpath(str(p))) for p in known if p]
    added: list[Path] = []
    for folders in view.path_values:
        for folder in folders:
            if folder == "$PATH":
                continue
            path = Path(folder)
            if _agent_place(path) or any(path == o or o in path.parents for o in owners):
                return False
            added.append(path)
    for effect in view.effects:
        if effect.kind != "write":
            continue
        if effect.dynamic:
            return False
        raw = effect.raw
        if raw.startswith("~/"):
            raw = str(ctx.user_home) + raw[1:]
        if not raw.startswith("/"):
            if effect.cwd is None:
                return False
            raw = str(effect.cwd / raw)
        target = Path(os.path.normpath(raw))
        if any(target == folder or folder in target.parents or target in folder.parents
               for folder in added):
            return False
    cwd = getattr(ctx, "workdir", None)
    paths = candidate_paths(view.path_values, ctx.user_home, cwd)
    entries: frozenset[str] | None = None
    for cmd in [*view.commands, *view.literal_commands]:
        if not cmd.words or cmd.dynamic[:1] == [True]:
            continue
        name = cmd.words[0]
        if "/" in name or name in _BUILTINS or name in _c.RESERVED_WORDS:
            continue
        found = {_which(name, search) for search in paths}
        if len(found) <= 1:
            continue
        if name == "snowpea":
            entries = cli_entry_points() if entries is None else entries
            if found <= set(entries) | {None} and found & set(entries):
                continue
        return False
    return True


#: Text that changes what a run of snowpea's CLI is (its home, its variables,
#: an alias or a function of that name): such a line keeps every run a control action.
_CLI_SHADOW = re.compile(
    r"SNOWPEA_|--home|(?:^|[\s;&|(])(?:HOME|XDG_[A-Z_]+)\+?=|\balias\b|\bunset\s+HOME\b"
    r"|snowpea[\w-]*\s*\(\s*\)|\bfunction\s+snowpea", re.MULTILINE)


def _settle(view: ShellView, ctx: Any) -> None:
    """After the walk of the agent's own line (m21): a ``PATH`` it sets that changes
    nothing it runs is not opaque; a vetted read-only run of snowpea's CLI stays an
    ordinary read only when nothing on the line can change what that run is."""
    if view.path_values and "env-code:PATH" in view.opaque and path_change_harmless(view, ctx):
        view.opaque.remove("env-code:PATH")
    if not view.cli_reads:
        return
    keep = not any(why.startswith(("env-code", "env-path", "function", "eval", "parse:",
                                   "internal", "nesting")) for why in view.opaque)
    keep = keep and not any(_CLI_SHADOW.search(text) for text in view.texts)
    # A variable that loads code into what runs next (``PYTHONPATH``, ``LD_PRELOAD``…),
    # even one the check otherwise lets through, would load it into the CLI.
    keep = keep and not any(
        _env.is_code(match.group(1))
        for text in view.texts
        for match in re.finditer(r"(?:^|[\s;&|(])([A-Za-z_][A-Za-z0-9_]*)\+?=", text)
        if match.group(1).upper() != "PATH")
    keep = keep and view.path_values is not None
    if keep and not all(cli_local_only(text) for text in view.cli_reads):
        # A run that reaches the daemon reaches the one in the CLI's own home: it
        # must be the daemon judging this line, or the CLI would start another.
        try:
            from snowpea_core.config.paths import resolve_home

            keep = ctx.home is not None and \
                os.path.realpath(resolve_home()) == os.path.realpath(ctx.home)
        except Exception:  # noqa: BLE001 - unknown is a control action
            keep = False
    if not keep and "snowpea-cli" not in view.control:
        view.control.append("snowpea-cli")


def temporary_dirs() -> set[Path]:
    """Folders anyone (the agent too) can fill: a program found there is not the system's."""
    import tempfile

    return {Path(os.path.realpath(tempfile.gettempdir())), Path("/tmp"), Path("/var/tmp")}



def _option_value(
    rest: list[str], rest_dyn: list[bool], names: tuple[str, ...]
) -> tuple[str, bool] | None:
    """The value of the first of ``names`` (``-c x``, ``--command=x``, ``-cx`` for short ones)."""
    for index, word in enumerate(rest):
        if word == "--":
            return None
        for name in names:
            if word == name:
                if index + 1 < len(rest):
                    return rest[index + 1], rest_dyn[index + 1]
                return "", True
            if name.startswith("--") and word.startswith(name + "="):
                return word.split("=", 1)[1], rest_dyn[index]
            if not name.startswith("--") and len(word) > 2 and word.startswith(name) and \
                    not word.startswith("--") and name != "-t":
                return word[len(name):], rest_dyn[index]
    return None


def _recursive_flag(program: str, rest: list[str]) -> bool:
    for word in rest:
        if word == "--":
            break
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if len(name) > 3 and "--recursive".startswith(name):
                return True
            if program in ("cp", "rsync") and len(name) > 3 and "--archive".startswith(name):
                return True
            continue
        if word.startswith("-") and len(word) > 1:
            letters = word[1:]
            if program in ("chmod", "chown", "chgrp", "chattr", "setfacl"):
                if "R" in letters:
                    return True
            elif "r" in letters or "R" in letters or (
                program in ("cp", "rsync") and "a" in letters
            ):
                return True
    return False


def _landing(raw: str, cwd: Path | None, many: bool) -> Path | None:
    """The folder sources land in when ``raw`` is one (an existing folder, or several sources)."""
    if "$" in raw or "`" in raw:
        return None
    text = raw
    if text.startswith("~"):
        return None  # judged through the plain destination effect
    path = Path(text)
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    try:
        if many or raw.endswith("/") or path.is_dir():
            return path
    except OSError:
        return None
    return None


_MAKE_RULE = re.compile(r"^([^\s:#=][^:=#]*):(?![=])([^;#]*)(?:;(.*))?$")


def _make_recipes(text: str, targets: list[str]) -> tuple[list[str], bool]:
    """The recipe lines ``make <targets>`` would run (prerequisites first), and whether
    every target was found written out (no pattern rule, no include, no variable name)."""
    rules: dict[str, tuple[list[str], list[str]]] = {}
    order: list[str] = []
    current: list[str] | None = None
    complete = True
    joined = text.replace("\\\n", " ")
    for raw in joined.splitlines():
        if raw.startswith("\t"):
            if current is not None:
                line = raw.strip().lstrip("@-+").strip()
                if line and not line.startswith("#"):
                    current.append(line)
            continue
        current = None
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"-?s?include\b", stripped):
            complete = False
            continue
        match = _MAKE_RULE.match(raw)
        if match is None:
            continue
        names = match.group(1).split()
        deps = [dep for dep in match.group(2).replace("|", " ").split()]
        recipe: list[str] = []
        if match.group(3) and match.group(3).strip():
            recipe.append(match.group(3).strip().lstrip("@-+").strip())
        for name in names:
            if "%" in name or "$" in name:
                complete = False
            known = rules.get(name)
            if known is None:
                rules[name] = (list(deps), recipe)
            else:
                # A second rule for the same target adds prerequisites and lines.
                known[0].extend(deps)
                known[1].extend(recipe)
                recipe = known[1]
            if name not in order and not name.startswith("."):
                order.append(name)
        current = recipe
    wanted = targets or order[:1]
    lines: list[str] = []
    seen: set[str] = set()

    def visit(name: str, depth: int) -> None:
        nonlocal complete
        if name in seen or depth > 32:
            return
        seen.add(name)
        rule = rules.get(name)
        if rule is None:
            if depth == 0 or "$" in name or "%" in name:
                complete = False  # a target only a pattern rule or a variable gives
            return
        for dep in rule[0]:
            visit(dep, depth + 1)
        lines.extend(rule[1])

    for target in wanted:
        visit(target, 0)
    return lines, complete


def literal_commands(code: str, request: _c.ClassifyRequest, view: ShellView) -> None:
    """Command lines spelled as string literals in ``code`` (best effort, never opaque)."""
    found = 0
    for match in _LITERAL.finditer(code):
        text = match.group(1) or match.group(2) or ""
        if not text.strip() or " " not in text.strip():
            continue
        found += 1
        if found > MAX_LITERALS:
            break
        sub = ShellView()
        try:
            _Probe(request, sub, literals=False).line(text, None, MAX_DEPTH - 1)
        except Exception:  # noqa: BLE001 - a best effort never breaks the judgement
            continue
        view.literal_commands.extend(sub.commands)
        view.literal_commands.extend(sub.literal_commands)


def view(
    command: str,
    *,
    cwd: str | None,
    home: str | None = None,
    user_home: str | None = None,
    projects: tuple[str, ...] = (),
) -> ShellView:
    """Walk ``command`` (run in ``cwd``) and return what it runs and touches.  Never raises.

    ``projects``: the known project folders (a ``PATH`` the line points into one
    of them is never followed, m21)."""
    result = ShellView()
    request = _c.ClassifyRequest(
        tool="shell", args={"command": command}, tags=("exec",), workdir=cwd, home=home,
        user_home=user_home,
    )
    try:
        probe = _Probe(request, result)
        probe.root = True
        start = probe.ctx.workdir
        probe.line(command, start, 0)
        probe.finish()
        from types import SimpleNamespace

        _settle(result, SimpleNamespace(
            workdir=probe.ctx.workdir, home=probe.ctx.home, user_home=probe.ctx.user_home,
            projects=tuple(projects)))
    except Exception:  # noqa: BLE001 - a bug here makes the line unseeable, never allowed
        result.opaque.append("internal")
    return result


def code_view(code: str, *, cwd: str | None, home: str | None = None,
              user_home: str | None = None, language: str = "python") -> ShellView:
    """``execute_code``'s code: its primitives (security review 5, H4) and its string
    literals that read as command lines.  Never raises (a failure is opaque)."""
    result = ShellView()
    request = _c.ClassifyRequest(tool="shell", args={}, workdir=cwd, home=home,
                                 user_home=user_home)
    try:
        literal_commands(code, request, result)
        probe = _Probe(request, result)
        lang = _code.CODE_LANGUAGE_NAMES.get(
            _c._family(str(language or "python").strip().lower()), "python")
        probe.apply_code(_code.analyse(code, lang), probe.ctx.workdir, 0, count_opaque=True)
        probe.finish()
    except Exception:  # noqa: BLE001 - a bug here makes the code unseeable, never allowed
        result.opaque.append("internal")
    return result


__all__ = ["Command", "Effect", "ShellView", "code_view", "view"]
