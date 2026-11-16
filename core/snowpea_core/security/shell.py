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
#: Most nested bodies (``sh -c "bash -c '…'"``) followed before the line is opaque.
MAX_DEPTH = 6
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

    def __init__(self, request: _c.ClassifyRequest, view: ShellView, *, literals: bool = True):
        super().__init__(request)
        self.view = view
        self.literals = literals
        self._line = -1
        self._index = 0
        self._cmd: _c._Cmd | None = None
        #: The command whose output the current one reads through a pipe.
        self._feeder: _c._Cmd | None = None
        #: The program whose writes this module judges itself (super's are dropped).
        self._own: list[bool] = []
        self._context: list[str] = []
        #: Per command being walked: the classifier's opaque marks this module
        #: analysed itself for that command (security review 7, P1).
        self._claims: list[set[str]] = []
        guard_all = self.hits.guard_all

        def tracked(rule: str) -> None:
            guard_all(rule)
            if rule.startswith("exec:opaque:"):
                self._unseen(rule[len("exec:opaque:"):])

        self.hits.guard_all = tracked  # type: ignore[method-assign]

    # -- recording ---------------------------------------------------------
    def _opaque(self, why: str) -> None:
        if why not in self.view.opaque:
            self.view.opaque.append(why)

    def _unseen(self, why: str) -> None:
        """The classifier could not judge something: opaque, unless the command
        being walked had exactly that analysed here."""
        claimed = self._claims[-1] if self._claims else frozenset()
        if why.split(":", 1)[0] not in claimed:
            self._opaque(why)

    def _control(self, why: str) -> None:
        if why not in self.view.control:
            self.view.control.append(why)

    def finish(self) -> None:
        """Nothing is left to copy at the end: every opaque mark of the classifier
        is taken (or dropped as analysed) when it is made (:meth:`_unseen`)."""

    def _effect(self, raw: str, cwd: Path | None, kind: str, *, destructive: bool = False,
                recursive: bool = False, dynamic: bool = False, program: str = "") -> None:
        dyn = dynamic or "$" in raw or "`" in raw
        self.view.effects.append(Effect(raw, cwd, kind, destructive, recursive, dyn,
                                        program or (self._context[-1] if self._context else "")))

    # -- the classifier's hooks -------------------------------------------
    def check_write_path(self, raw: str, cwd: Path | None, rule: str, dynamic: bool) -> None:
        if self._own and self._own[-1] and rule != "redirect":
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
        # ``< file`` only ever reads; any other word is an operand of the program
        # being walked (which decides what happens to the file it names).
        self._effect(raw, cwd, "read", recursive=recursive, dynamic=dynamic,
                     program="redirect" if rule == "redirect" else "")

    def dynamic_read(self, raw: str, cwd: Path | None, rule: str) -> None:
        del rule
        self._effect(raw or "$?", cwd, "read", dynamic=True)

    # -- the walk ------------------------------------------------------------
    def line(self, command: str, cwd: Path | None, depth: int) -> None:
        if depth > MAX_DEPTH:
            self._opaque("nesting")
            return
        self.view.texts.append(command)
        try:
            parsed = _c.parse_line(command)
        except _c._Opaque as exc:
            self._opaque(f"parse:{exc}")
            return
        except Exception:  # noqa: BLE001 - a parser bug is a line nobody can see
            self._opaque("parse:error")
            return
        self._line += 1
        line_id = self._line
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
        if parsed.substitution:
            # ``"$(…)"`` and backticks inside double quotes are not split by the
            # parser: read every substitution's body as a line of its own.
            for body in substitution_bodies(command):
                self.line(body, cwd, depth + 1)
        for position, cmd in enumerate(parsed.commands):
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
        while words and _c._ASSIGNMENT.match(words[0]):
            self._assignment(words[0], dynamic[0], cwd)
            words, dynamic = words[1:], dynamic[1:]
        if not words:
            return cwd
        if dynamic[0]:
            # ``$X args`` / ``$(echo rm) -rf``: what runs is not in the line.
            self._opaque("dynamic-program")
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
                if self._git_alias(rest, rest_dyn, cwd, depth):
                    claims.add("git-alias")
            if program in _c.CONTENT_READERS:
                # A file named without a folder (``cat token`` after ``cd``) is read
                # like any other: the classifier's own scan only takes path-like words.
                for word, dyn in _c._reader_operands(program, rest, rest_dyn):
                    if not dyn and word and "/" not in word and not word.startswith(("-", "~")):
                        self._effect(word, cwd, "read", program=program)
            if program == "snowpea" or program.startswith("snowpea-"):
                # snowpea's own CLI talks to the daemon as the local person (H-1).
                self._control("snowpea-cli")
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
            elif self.literals and "/" in words[0].replace("\\", "/") and not dynamic[0]:
                # ``./x``, ``scripts/x``, ``/opt/tool/run``: the file itself runs.
                self._script(words[0], cwd, depth, "shell")
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
                if rest_dyn[0]:
                    self._opaque("script-dynamic")
                else:
                    self._script(rest[0], cwd, depth, "shell")
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
                self._body(rest[after], rest_dyn[after], cwd, depth, "shell-body")
            else:
                self._opaque("shell-body")
            del index
            return
        script = rest[len(options)] if len(rest) > len(options) else None
        if script is not None and not any(w in ("-s", "--stdin") for w in options):
            # Runs a script file: read it (security review 5, H4).
            if rest_dyn[len(options)]:
                self._opaque("script-dynamic")
            elif self.literals:
                self._script(script, cwd, depth, "shell")
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
    def _assignment(self, word: str, dyn: bool, cwd: Path | None) -> None:
        """``NAME=value`` — before a command, alone, after ``env`` or ``export``.

        The classifier says which variables carry code or redirect configuration;
        :func:`snowpea_core.security.envvars.judge` lets a value through only when
        it can say why it is harmless (security review 7, H7-1).  What the value
        names is judged like an operand of a program nobody knows: ``X=<a control
        file>; echo … > $X`` names that file."""
        name, _, value = word.partition("=")
        name = name.rstrip("+").split("[", 1)[0]
        why = _env.judge(name, value, dyn, cwd=cwd, user_home=self.ctx.user_home,
                         home=self.ctx.home)
        if why is not None:
            self._opaque(why)
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
            if operands[0][1]:
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
    def _project_runner(self, program: str, rest: list[str], rest_dyn: list[bool],
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

    def _merge(self, sub: ShellView, *, count_opaque: bool) -> None:
        self.view.commands.extend(sub.commands)
        self.view.effects.extend(sub.effects)
        self.view.texts.extend(sub.texts)
        self.view.literal_commands.extend(sub.literal_commands)
        self.view.scripts.extend(sub.scripts)
        self.view.mentions.extend(sub.mentions)
        for why in sub.control:
            self._control(why)  # snowpea's own CLI asks whoever wrote the script
        if count_opaque:
            for why in sub.opaque:
                self._opaque(why)

    def _script(self, raw: str, cwd: Path | None, depth: int, default: str, *,
                root: Path | None = None) -> None:
        """Read the script file ``raw`` and judge its text (H4 b)."""
        if depth > MAX_DEPTH:
            self._opaque("nesting")
            return
        script = _code.read_script(raw, cwd, default=default)
        if script is None:
            return
        self.view.scripts.append((script.path, script.digest))
        if "snowpea_core" in Path(script.path).parts:
            self._control("snowpea-cli")
        if script.text is None:
            if script.recent:
                self._opaque(f"script-{script.digest if ':' not in script.digest else 'binary'}")
            return
        if script.language == "shell":
            sub = ShellView()
            probe = _Probe(self.request, sub)
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
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn)
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
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn)
            elif program == "ln" and not symbolic:
                # A hard link is the same file under a new name: whatever is later
                # written through it is written to the source (security review 5, M6).
                self._effect(word, cwd, "write", dynamic=dyn)
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
        self._effect(dest[0], cwd, "write", destructive=deletes, recursive=tree,
                     dynamic=dest[1])
        landing = _landing(dest[0], cwd, target_dir is not None or len(sources) > 1)
        if landing is not None:
            for word, dyn in sources:
                name = word.rstrip("/").rsplit("/", 1)[-1]
                if name and not dyn and name not in (".", ".."):
                    self._effect(str(landing / name), None, "write", recursive=tree,
                                 dynamic=dest[1])

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
                         program=program)

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


def substitution_bodies(text: str) -> list[str]:
    """The bodies of ``$(…)``, ``<(…)``, ``>(…)`` and backtick substitutions in ``text``.

    Single-quoted text is skipped (nothing in it is substituted); an
    unbalanced substitution yields what follows it.
    """
    bodies: list[str] = []
    index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        if char == "\\" and not quoted:
            index += 2
            continue
        if char == "'" and not quoted:
            end = text.find("'", index + 1)
            if end < 0:
                break
            index = end + 1
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        opens = text[index + 1 : index + 2] == "(" and text[index + 2 : index + 3] != "("
        if char in "$<>" and opens:
            depth = 0
            start = index + 2
            j = start
            while j < len(text):
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    if depth == 0:
                        break
                    depth -= 1
                j += 1
            bodies.append(text[start:j])
            index = j + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            bodies.append(text[index + 1 : end].replace("\\`", "`"))
            index = end + 1
            continue
        index += 1
    return [body for body in bodies if body.strip()]


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
) -> ShellView:
    """Walk ``command`` (run in ``cwd``) and return what it runs and touches.  Never raises."""
    result = ShellView()
    request = _c.ClassifyRequest(
        tool="shell", args={"command": command}, tags=("exec",), workdir=cwd, home=home,
        user_home=user_home,
    )
    try:
        probe = _Probe(request, result)
        start = probe.ctx.workdir
        probe.line(command, start, 0)
        probe.finish()
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
        lang = _code.LANGUAGES.get(_c._family(str(language or "python").lower()), "python")
        probe.apply_code(_code.analyse(code, lang), probe.ctx.workdir, 0, count_opaque=True)
        probe.finish()
    except Exception:  # noqa: BLE001 - a bug here makes the code unseeable, never allowed
        result.opaque.append("internal")
    return result


__all__ = ["Command", "Effect", "ShellView", "code_view", "view"]
