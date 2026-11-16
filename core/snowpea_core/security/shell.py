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

import re
from dataclasses import dataclass, field
from pathlib import Path

from snowpea_core.jarvis import classify as _c
from snowpea_core.security import code as _code

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
#: Common valued options of the coreutils write programs.
_VALUED: frozenset[str] = frozenset(
    {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g",
     "--group", "-b", "--backup", "--context", "-s", "--size", "-r", "--reference", "-n",
     "--iterations", "--from", "-M", "-X", "-x", "--modify-file", "--remove-file",
     "--set-file", "--exclude", "-e", "--rsh", "--files-from", "--include", "--filter"}
)
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


class _Probe(_c._Judge):
    """The classifier's walk, collecting commands and effects instead of a verdict."""

    def __init__(self, request: _c.ClassifyRequest, view: ShellView, *, literals: bool = True):
        super().__init__(request)
        self.view = view
        self.literals = literals
        self._line = -1
        self._index = 0
        self._cmd: _c._Cmd | None = None
        #: The program whose writes this module judges itself (super's are dropped).
        self._own: list[bool] = []
        self._context: list[str] = []

    # -- recording ---------------------------------------------------------
    def _opaque(self, why: str) -> None:
        if why not in self.view.opaque:
            self.view.opaque.append(why)

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
        del rule, shell
        self._effect(raw, cwd, "read", recursive=recursive, dynamic=dynamic)

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
        if parsed.substitution:
            # ``"$(…)"`` and backticks inside double quotes are not split by the
            # parser: read every substitution's body as a line of its own.
            for body in substitution_bodies(command):
                self.line(body, cwd, depth + 1)
        for position, cmd in enumerate(parsed.commands):
            self.fed_by_lister = self.lister_ok and cmd.piped_in
            self.lister_ok = False
            saved = (self._line, self._index, self._cmd)
            self._line, self._index, self._cmd = line_id, position, cmd
            try:
                cwd = self.simple(cmd, cwd, depth)
            finally:
                self._line, self._index, self._cmd = saved
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
        while words and not dynamic[0] and words[0] in _c.RESERVED_WORDS:
            if words[0] == "for":
                return cwd
            words, dynamic = words[1:], dynamic[1:]
        while words and _c._ASSIGNMENT.match(words[0]):
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
        if self._hidden_command(program, family, rest, rest_dyn, cwd, depth, piped_in, heredoc):
            return cwd
        if program in ("tar", "bsdtar", "gtar", "zip"):
            self._archive_removal(program, rest, rest_dyn, cwd)
        own = program in _OWN_WRITE_PROGRAMS
        if own:
            self._own_writes(program, rest, rest_dyn, cwd)
        if program in ("git",) or family == "git":
            self._git_tree(rest, rest_dyn, cwd)
        if family in _c.INTERPRETERS and self.literals:
            if not self._inline_literals(family, rest, cwd, depth):
                self._interpreter_script(family, rest, rest_dyn, cwd, depth)
        elif self.literals and "/" in words[0].replace("\\", "/") and not dynamic[0]:
            # ``./x``, ``scripts/x``, ``/opt/tool/run``: the file itself runs.
            self._script(words[0], cwd, depth, "shell")
        self._own.append(own)
        self._context.append(program)
        try:
            return super().words(words, dynamic, cwd, depth, piped_in=piped_in,
                                 heredoc=heredoc, stdin_args=stdin_args)
        finally:
            self._own.pop()
            self._context.pop()

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
        if program in ("source", "."):
            if rest and self.literals:
                if rest_dyn[0]:
                    self._opaque("script-dynamic")
                else:
                    self._script(rest[0], cwd, depth, "shell")
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

    def _inline_literals(self, family: str, rest: list[str], cwd: Path | None,
                         depth: int) -> bool:
        """Inline code (``python -c``): its primitives (H4) and its string literals
        that read as command lines.  True when the line carried inline code."""
        flags = _c.INTERPRETERS.get(family, frozenset())
        code: list[str] = []
        for index, word in enumerate(rest):
            if word in flags and index + 1 < len(rest):
                code.append(rest[index + 1])
            elif any(word.startswith(flag) and len(word) > len(flag) for flag in flags
                     if len(flag) == 2):
                code.append(word[2:])
        language = _code.LANGUAGES.get(family)
        for text in code:
            literal_commands(text, self.request, self.view)
            if language is not None:
                self.apply_code(_code.analyse(text, language), cwd, depth, count_opaque=True)
        return bool(code)

    # -- code and scripts (security review 5, H4) --------------------------------
    def apply_code(self, finding: _code.Finding, cwd: Path | None, depth: int, *,
                   count_opaque: bool) -> None:
        """Add what code would do: its commands, its shell lines, its writes."""
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
        for text in finding.lines:
            if count_opaque:
                self.line(text, cwd, depth + 1)
            else:
                sub = ShellView()
                probe = _Probe(self.request, sub)
                probe.line(text, cwd, depth + 1)
                self._merge(sub, count_opaque=False)
        if count_opaque:
            for why in finding.opaque:
                self._opaque(why)

    def _merge(self, sub: ShellView, *, count_opaque: bool) -> None:
        self.view.commands.extend(sub.commands)
        self.view.effects.extend(sub.effects)
        self.view.texts.extend(sub.texts)
        self.view.literal_commands.extend(sub.literal_commands)
        self.view.scripts.extend(sub.scripts)
        if count_opaque:
            for why in sub.opaque:
                self._opaque(why)

    def _interpreter_script(self, family: str, rest: list[str], rest_dyn: list[bool],
                            cwd: Path | None, depth: int) -> None:
        """``python x.py`` / ``node x.js`` / ``perl x.pl``: read the script it runs."""
        language = _code.LANGUAGES.get(family)
        if language is None:
            return
        valued = {
            "python": frozenset({"-m", "-W", "-X", "-Q"}),
            "node": frozenset({"-r", "--require", "--import", "--loader", "--env-file",
                               "--conditions", "-C", "--inspect-port"}),
            "ruby": frozenset({"-I", "-r", "-C", "-E", "-K", "-T"}),
            "perl": frozenset({"-I", "-M", "-m", "-x"}),
        }.get(language, frozenset())
        options = _c._leading_options(rest, valued=valued)
        if "-m" in options and language == "python":
            return  # a module, not a file
        if len(rest) <= len(options):
            return
        index = len(options)
        if rest[index] == "-":
            return
        if rest_dyn[index]:
            self._opaque("script-dynamic")
            return
        self._script(rest[index], cwd, depth, language)

    def _script(self, raw: str, cwd: Path | None, depth: int, default: str) -> None:
        """Read the script file ``raw`` and judge its text (H4 b)."""
        if depth > MAX_DEPTH:
            self._opaque("nesting")
            return
        script = _code.read_script(raw, cwd, default=default)
        if script is None:
            return
        self.view.scripts.append((script.path, script.digest))
        if script.text is None:
            if script.recent:
                self._opaque(f"script-{script.digest if ':' not in script.digest else 'binary'}")
            return
        if script.language == "shell":
            sub = ShellView()
            _Probe(self.request, sub).line(script.text, cwd, depth + 1)
            # A pre-existing script the person owns: what it plainly does is
            # judged, what it computes is not held against it.
            self._merge(sub, count_opaque=script.recent)
            return
        finding = _code.analyse(script.text, script.language)
        self.apply_code(finding, cwd, depth, count_opaque=script.recent)

    # -- writes this module judges itself -------------------------------------
    def _own_writes(  # noqa: C901 - one table of the write programs, kept together
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        recursive = _recursive_flag(program, rest)
        operands = _c._operands(rest, rest_dyn, _VALUED)
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
        for word, dyn in sources:
            if moving:
                self._effect(word, cwd, "write", destructive=True, dynamic=dyn)
            elif program == "ln" and not symbolic:
                # A hard link is the same file under a new name: whatever is later
                # written through it is written to the source (security review 5, M6).
                self._effect(word, cwd, "write", dynamic=dyn)
            elif program != "ln":
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
    except Exception:  # noqa: BLE001 - a bug here makes the code unseeable, never allowed
        result.opaque.append("internal")
    return result


__all__ = ["Command", "Effect", "ShellView", "code_view", "view"]
