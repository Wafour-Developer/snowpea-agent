"""What a remembered answer to a shell approval would cover (1.34.0, ``shellRemember``).

Answering "project" or "always" to a shell approval stores an allowlist rule.
Before 1.34.0 that rule was the command's first word, so "always" on
``git status`` allowed every ``git …`` — ``git push --force`` included — and
the card could not say so.  This module proposes the rule *before* the
person answers, so ``approval.request`` can carry it as ``remember`` and the
card can show exactly what "remember" means:

* **The rule** is the program, plus its subcommand for the tools in
  :data:`SUBCOMMAND_TOOLS` (``git status``, ``npm test``, ``cargo build``),
  plus the script or inner command for the runners in :data:`THIRD_WORD`
  (``npm run build``, ``uv run pytest``, ``docker compose up``).  Whatever
  follows is free: the pattern is ``^<words>( .*)?$``.
* **No rule** (``None``) for a line that is not one plain command
  (:func:`plain_words`), for a program named by a path, for an argument that
  runs another command (:func:`runs_commands`), and for programs and
  subcommands a standing rule would be too wide for: see
  :data:`NEVER_REMEMBERED`, :data:`NEVER_REMEMBERED_SUBCOMMANDS` and
  :func:`propose`.  The person can still allow such a call once.
* **Scopes**: ``["project"]`` for everything, ``["project", "always"]`` only
  when the rule's own prefix cannot write or run anything whatever arguments
  follow (:data:`ALWAYS_RULES`) and the command itself is not mutating
  (:func:`~snowpea_core.permissions.safe_commands.is_mutating`).
* **Note**: rules whose program runs what the project's own files say
  (``make <target>``, ``npm run <script>``, ``go run``, ``uv run …``) carry
  :data:`PROJECT_CODE_NOTE`.

The allowlist uses :func:`matchable` too: a stored shell rule only ever
covers one plain command with no command-running argument, so
``^git status( .*)?$`` never covers ``git status ; curl …|sh``.

Like :mod:`~snowpea_core.permissions.safe_commands`, it is a pure,
table-driven function of the command string, conservative by design: a
wrong ``None`` costs a prompt, a wrong rule costs a standing permission.
Nothing here is sent to the model.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from snowpea_core.permissions.safe_commands import is_mutating

#: Characters that are live in every quoting (POSIX shells, PowerShell and
#: cmd.exe alike): a separator, a pipe, a background ``&``, a redirection or
#: heredoc, a substitution or variable, a second line.
COMPLEX_MARKS: tuple[str, ...] = (";", "&", "|", "<", ">", "`", "$", "\n", "\r")

#: Characters that are live outside quotes as well: a subshell or group, a
#: glob (which can expand into a file named ``--pre=sh``), an escape.  Inside
#: single quotes they are literal; inside double quotes only ``\\`` is not.
UNQUOTED_MARKS: frozenset[str] = frozenset("(){}*?[\\")

#: ``NAME=value cmd``: an environment prefix can change what the program does
#: (``GIT_SSH_COMMAND``, ``LD_PRELOAD``), so it is never remembered or matched.
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=", re.ASCII)

#: A word usable as a program: ASCII letters, digits and ``. _ + -``; no path,
#: and no lookalike letter from another script (a Cyrillic ``gіt``).
PROGRAM_WORD = re.compile(r"^[A-Za-z0-9_.+-]+$", re.ASCII)

#: A word usable as a subcommand, script or task name.  No dots or slashes,
#: so a file (``bun index.ts``, ``go run ./cmd``) is never mistaken for one.
SUBCOMMAND_WORD = re.compile(r"^[A-Za-z0-9:][A-Za-z0-9_:-]*$", re.ASCII)

#: Programs never remembered, whatever follows them.
NEVER_REMEMBERED: frozenset[str] = frozenset(
    {
        # privilege
        "sudo", "su", "doas", "pkexec", "runas",
        # destructive or system-wide
        "rm", "rmdir", "dd", "shred", "wipefs", "truncate", "chmod", "chown", "chgrp",
        "kill", "pkill", "killall", "reboot", "shutdown", "halt", "poweroff",
        "systemctl", "service", "launchctl", "crontab", "mount", "umount",
        "fdisk", "parted", "mkswap",
        # shells and interpreters: their arguments are code
        "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "pwsh", "powershell",
        "cmd", "node", "nodejs", "deno", "perl", "ruby", "php", "lua", "tclsh",
        "rscript", "osascript", "awk", "gawk", "mawk", "nawk", "sed",
        # editors, pagers and debuggers that run commands
        "vi", "vim", "nvim", "view", "emacs", "ed", "ex", "less", "more", "man",
        "gdb", "lldb", "tmux", "screen", "expect", "open", "xdg-open",
        # run another command
        "env", "eval", "exec", "xargs", "find", "nohup", "time", "timeout", "nice",
        "ionice", "watch", "command", "builtin", "source", ".", "strace", "chroot",
        "unshare", "nsenter", "script", "parallel", "stdbuf", "setsid", "flock",
        # network fetch / remote exec
        "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat",
        "telnet", "ftp", "socat",
        # package runners: run whatever package they are given
        "npx", "uvx", "pipx", "bunx", "pnpx",
    }
)

#: Interpreter families matched by name shape (``python3.11``, ``mkfs.ext4``).
NEVER_REMEMBERED_SHAPE = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|mkfs(\..+)?)$", re.ASCII)

#: Tools whose rule includes their subcommand.  ``pip`` stands for every
#: ``pip``/``pip3``/``pip3.x`` spelling.
SUBCOMMAND_TOOLS: frozenset[str] = frozenset(
    {
        "git", "npm", "pnpm", "yarn", "bun", "cargo", "go", "docker", "podman",
        "kubectl", "helm", "pip", "uv", "poetry", "pdm", "make", "gradle",
        "gradlew", "mvn", "mvnw", "dotnet", "swift", "terraform", "rustup",
        "composer", "bundle", "mix", "flutter", "dart",
    }
)

#: Subcommands whose rule takes one more word: the script, task or inner
#: command.  For ``uv run`` / ``poetry run`` / ``pdm run`` the inner command
#: is judged by :func:`propose` itself (``uv run python -c …`` is ``None``).
THIRD_WORD: dict[str, frozenset[str]] = {
    "npm": frozenset({"run", "run-script"}),
    "pnpm": frozenset({"run"}),
    "yarn": frozenset({"run"}),
    "bun": frozenset({"run"}),
    "uv": frozenset({"run", "pip"}),
    "poetry": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "docker": frozenset({"compose"}),
    "podman": frozenset({"compose"}),
}

#: Runners whose third word is a whole command, judged recursively.
INNER_COMMAND: frozenset[tuple[str, str]] = frozenset(
    {("uv", "run"), ("poetry", "run"), ("pdm", "run")}
)

#: Subcommands never remembered: they publish, rewrite or discard history or
#: work, talk to a remote that can run commands (``--upload-pack``), install
#: code from anywhere, run other commands, or act on a cluster.
NEVER_REMEMBERED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {
            "push", "reset", "clean", "rm", "rebase", "filter-branch", "filter-repo",
            "update-ref", "gc", "prune", "reflog", "restore", "checkout", "config",
            "submodule", "bisect", "daemon", "send-email", "credential",
            "fetch", "pull", "clone", "ls-remote", "archive", "remote",
        }
    ),
    "npm": frozenset(
        {
            "publish", "unpublish", "exec", "x", "login", "logout", "adduser", "token",
            "owner", "access", "deprecate", "dist-tag",
        }
    ),
    "pnpm": frozenset({"publish", "exec", "dlx", "login", "logout"}),
    "yarn": frozenset({"publish", "dlx", "exec", "npm", "login", "logout"}),
    "bun": frozenset({"publish", "x", "exec"}),
    "cargo": frozenset({"publish", "install", "uninstall", "login", "logout", "owner", "yank"}),
    "go": frozenset({"install"}),
    "docker": frozenset({"push", "login", "logout", "rm", "rmi", "system", "volume", "exec"}),
    "podman": frozenset({"push", "login", "logout", "rm", "rmi", "system", "volume", "exec"}),
    "kubectl": frozenset(
        {
            "delete", "drain", "exec", "cp", "apply", "replace", "patch", "edit", "scale",
            "rollout", "cordon", "uncordon", "taint", "label", "annotate", "run", "create",
            "expose", "set", "port-forward", "proxy", "debug", "attach",
        }
    ),
    "helm": frozenset({"install", "uninstall", "upgrade", "delete", "rollback", "push"}),
    "pip": frozenset({"install", "uninstall"}),
    "uv": frozenset({"tool", "publish", "self"}),
    "poetry": frozenset({"publish", "self"}),
    "pdm": frozenset({"publish", "self"}),
    "dotnet": frozenset({"tool", "nuget"}),
    "terraform": frozenset({"apply", "destroy", "import", "state", "taint", "force-unlock"}),
    "rustup": frozenset({"self"}),
    "composer": frozenset({"exec", "global"}),
    "bundle": frozenset({"exec"}),
    "mix": frozenset({"run"}),
}

#: Third words never remembered (``uv pip install`` is ``pip install``).
NEVER_REMEMBERED_THIRD: dict[tuple[str, str], frozenset[str]] = {
    ("uv", "pip"): frozenset({"install", "uninstall", "sync"}),
}

#: Arguments that make a program run another command (or load code from
#: outside the project), per program.  A remembered rule never covers them,
#: and no rule is offered for a call that has one.  A two-character short
#: flag (``-c``) also counts inside a cluster or with a value attached
#: (``-qc``, ``-cfoo``); a long one as ``--flag`` or ``--flag=value``.
EXEC_FLAGS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {
            "--upload-pack", "--receive-pack", "--exec", "-c", "--config-env",
            "--exec-path", "--template",
        }
    ),
    "rg": frozenset({"--pre"}),
    "find": frozenset(
        {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
         "-fprintf", "-fls"}
    ),
    "tar": frozenset(
        {
            "--to-command", "--use-compress-program", "-I", "--checkpoint-action",
            "--rsh-command", "--info-script", "-F", "--new-volume-script", "--rmt-command",
        }
    ),
    "zip": frozenset({"-TT", "--unzip-command"}),
    "sort": frozenset({"--compress-program"}),
    "rsync": frozenset({"-e", "--rsh", "--rsync-path"}),
    "npm": frozenset({"--script-shell", "--node-options", "--call", "--shell"}),
    "pnpm": frozenset({"--script-shell", "--node-options", "--shell"}),
    "yarn": frozenset({"--script-shell", "--node-options"}),
    "bun": frozenset({"--shell", "--preload", "-r"}),
    "cargo": frozenset({"--config"}),
    "pytest": frozenset({"-p", "-c", "-o", "--override-ini", "--confcutdir", "--rootdir"}),
    "go": frozenset({"-exec", "--exec", "-toolexec", "--toolexec", "-overlay"}),
    "make": frozenset(
        {"-f", "--file", "--makefile", "--eval", "-E", "-C", "--directory", "-I",
         "--include-dir"}
    ),
    "gradle": frozenset({"-I", "--init-script"}),
    "gradlew": frozenset({"-I", "--init-script"}),
    "mvn": frozenset({"-f", "--file"}),
    "mvnw": frozenset({"-f", "--file"}),
    "kubectl": frozenset({"--kubeconfig"}),
    "helm": frozenset({"--kubeconfig", "--post-renderer"}),
}

#: The same, only after a given subcommand (``git grep -O`` opens a pager
#: command; ``git log -O`` only names an order file).
EXEC_FLAGS_BY_SUBCOMMAND: dict[tuple[str, str], frozenset[str]] = {
    ("git", "grep"): frozenset({"-O", "--open-files-in-pager"}),
    ("git", "rebase"): frozenset({"-x", "--exec"}),
    ("git", "difftool"): frozenset({"-x", "--extcmd"}),
    **{
        ("git", sub): frozenset({"-u", "--remote"})
        for sub in ("fetch", "pull", "clone", "ls-remote", "archive")
    },
}

#: Subcommands that run commands of their own (``git submodule foreach``,
#: ``git bisect run``, an alias set by ``git config``): never covered by a
#: rule, even an old first-word ``^git( .*)?$``.
EXEC_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {"submodule", "bisect", "filter-branch", "filter-repo", "send-email", "config"}
    ),
}

#: Git options that take the next word as their value.
_GIT_VALUED = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})

#: Rules that may be remembered "always" (every project): their prefix cannot
#: write a file or run a program, whatever arguments are added later.  Left
#: out on purpose: ``git diff/log/show`` (``--output=FILE`` writes), ``tree``
#: (``-o``), ``file`` (``-C``), ``rg`` (``--pre`` runs a program), ``find``.
ALWAYS_RULES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("ls",), ("pwd",), ("cat",), ("head",), ("tail",), ("wc",), ("stat",),
        ("du",), ("df",), ("echo",), ("grep",), ("which",),
        ("git", "status"), ("git", "blame"), ("git", "ls-files"),
    }
)

#: What the card adds for a rule whose program runs what the project says.
PROJECT_CODE_NOTE = "scripts in this project decide what runs"

#: Rules (by their first words) that run the project's own scripts.
PROJECT_CODE_PROGRAMS: frozenset[str] = frozenset(
    {"make", "gradle", "gradlew", "mvn", "mvnw", "pytest"}
)
PROJECT_CODE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "go": frozenset({"run", "generate", "test"}),
    "cargo": frozenset({"run", "test", "bench", "build"}),
    "npm": frozenset({"run", "run-script", "test", "t", "tst", "start", "stop", "restart"}),
    "pnpm": frozenset({"run", "test", "t", "start"}),
    "yarn": frozenset({"run", "test", "start"}),
    "bun": frozenset({"run", "test"}),
    "uv": frozenset({"run"}),
    "poetry": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "dotnet": frozenset({"run", "test"}),
}


@dataclass(frozen=True)
class Proposal:
    """The rule a remembered answer would store."""

    #: Leading words the rule fixes, e.g. ``("git", "status")``.
    words: tuple[str, ...]
    scopes: tuple[str, ...] = field(default=("project",))
    #: A warning for the card (:data:`PROJECT_CODE_NOTE`), or ``None``.
    note: str | None = None

    @property
    def pattern(self) -> str:
        """``^git status( .*)?$``: these words, then anything."""
        return "^" + " ".join(re.escape(word) for word in self.words) + "( .*)?$"

    @property
    def display(self) -> str:
        """``git status …``: the human form the card shows."""
        return " ".join(self.words) + " …"


def _family(program: str) -> str:
    """``git.exe`` -> ``git``; ``pip3.11`` -> ``pip``."""
    name = program.lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    if re.fullmatch(r"pip[0-9.]*", name):
        return "pip"
    return name


def plain_words(command: str | None) -> list[str] | None:
    """The words of ``command`` when it is one plain command, else ``None``.

    ``None`` for an empty or unparsable line, any :data:`COMPLEX_MARKS`
    character anywhere, an :data:`UNQUOTED_MARKS` character outside single
    quotes (``\\`` and globs are live in double quotes too, ``( ) { }`` are
    not), an unclosed quote, and a ``NAME=value`` prefix.
    """
    text = (command or "").strip()
    if not text or any(mark in text for mark in COMPLEX_MARKS):
        return None
    quote = ""
    for char in text:
        if quote == "'":
            quote = "" if char == "'" else quote
        elif quote == '"':
            if char == '"':
                quote = ""
            elif char in "\\*?[":
                return None
        elif char in "'\"":
            quote = char
        elif char in UNQUOTED_MARKS:
            return None
    if quote:
        return None
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    if not words or ENV_ASSIGNMENT.match(words[0]):
        return None
    return words


def _subcommand(family: str, rest: list[str]) -> str | None:
    """The first non-option word after the program (git's valued options skipped)."""
    skip = False
    for word in rest:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = family == "git" and word in _GIT_VALUED
        else:
            return word
    return None


def _hits(word: str, flag: str) -> bool:
    if word == flag or word.startswith(flag + "="):
        return True
    if len(flag) == 2 and flag[0] == "-" and flag[1] != "-":
        # A short flag inside a cluster (-qc) or with its value attached (-cfoo).
        return word.startswith("-") and not word.startswith("--") and flag[1] in word[1:]
    return False


def runs_commands(words: list[str]) -> bool:
    """True when an argument makes the program run another command (:data:`EXEC_FLAGS`)."""
    if not words:
        return False
    family = _family(words[0].rsplit("/", 1)[-1])
    rest = words[1:]
    sub = _subcommand(family, rest)
    if sub is not None and sub in EXEC_SUBCOMMANDS.get(family, frozenset()):
        return True
    flags = EXEC_FLAGS.get(family, frozenset()) | EXEC_FLAGS_BY_SUBCOMMAND.get(
        (family, sub or ""), frozenset()
    )
    if family == "make" and any("=" in word and not word.startswith("-") for word in rest):
        # ``make test SHELL=/tmp/x`` overrides what every recipe runs.
        return True
    return any(_hits(word, flag) for word in rest for flag in flags)


def matchable(command: str | None) -> bool:
    """True when a stored shell rule may cover ``command`` at all.

    Only one plain command with no command-running argument qualifies, so a
    rule (a remembered one or an old first-word ``^git( .*)?$``) never
    covers ``git status ; id`` or ``git fetch --upload-pack=…``.
    """
    words = plain_words(command)
    return words is not None and not runs_commands(words)


def _words(words: list[str]) -> tuple[str, ...] | None:
    """The rule's words for one plain command already split, else ``None``."""
    if not words:
        return None
    program = words[0]
    if not PROGRAM_WORD.match(program) or program.startswith("-"):
        return None
    family = _family(program)
    if family in NEVER_REMEMBERED or NEVER_REMEMBERED_SHAPE.match(family):
        return None
    if family not in SUBCOMMAND_TOOLS:
        return (program,)
    # A subcommand tool needs a subcommand: ``git`` alone, or ``git -C dir …``
    # (a flag first), would cover the whole tool.
    if len(words) < 2 or not SUBCOMMAND_WORD.match(words[1]):
        return None
    sub = words[1]
    if sub in NEVER_REMEMBERED_SUBCOMMANDS.get(family, frozenset()):
        return None
    if sub not in THIRD_WORD.get(family, frozenset()):
        return (program, sub)
    if (family, sub) in INNER_COMMAND:
        inner = _words(words[2:])
        return (program, sub, *inner) if inner else None
    if len(words) < 3 or not SUBCOMMAND_WORD.match(words[2]):
        return None
    if words[2] in NEVER_REMEMBERED_THIRD.get((family, sub), frozenset()):
        return None
    return (program, sub, words[2])


def _note(rule: tuple[str, ...]) -> str | None:
    family = _family(rule[0])
    if family in PROJECT_CODE_PROGRAMS:
        return PROJECT_CODE_NOTE
    if len(rule) > 1 and rule[1] in PROJECT_CODE_SUBCOMMANDS.get(family, frozenset()):
        return PROJECT_CODE_NOTE
    return None


def propose(command: str | None) -> Proposal | None:
    """The rule "remember this command" would store for ``command``, or ``None``.

    ``None`` for: a line that is not one plain command (:func:`plain_words`:
    ``; && || |``, ``&``, backticks, ``$(…)``, ``$VAR``, redirects and
    heredocs, subshells, unquoted globs, an environment-assignment prefix);
    a program named by a path (``./run.sh``, ``/usr/bin/x``) or spelled with
    a non-ASCII letter; an argument that runs another command
    (:func:`runs_commands`); :data:`NEVER_REMEMBERED` programs (sudo and
    friends, rm/dd/chmod/kill/…, shells, interpreters and editors, wrappers
    that run another command, network tools, package runners such as
    ``npx``); :data:`NEVER_REMEMBERED_SUBCOMMANDS` (``git push/reset/clean/
    rm/fetch/pull/clone/…``, ``npm publish``, ``pip install`` …); and a
    subcommand tool given no subcommand.  The caller adds the request-level
    cases (``onceOnly``, an agent-switch guard, a ``reasonCode``).
    """
    words = plain_words(command)
    if words is None or runs_commands(words):
        return None
    rule = _words(words)
    if rule is None:
        return None
    always = rule in ALWAYS_RULES and not is_mutating(str(command).strip())
    return Proposal(
        rule, ("project", "always") if always else ("project",), _note(rule)
    )


__all__ = [
    "ALWAYS_RULES",
    "COMPLEX_MARKS",
    "EXEC_FLAGS",
    "NEVER_REMEMBERED",
    "NEVER_REMEMBERED_SUBCOMMANDS",
    "PROJECT_CODE_NOTE",
    "SUBCOMMAND_TOOLS",
    "THIRD_WORD",
    "Proposal",
    "matchable",
    "plain_words",
    "propose",
    "runs_commands",
]
