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
* **No rule** (``None``) for a line this module cannot read as one plain
  command, and for programs and subcommands a standing rule would be too
  wide for: see :data:`NEVER_REMEMBERED`, :data:`NEVER_REMEMBERED_SUBCOMMANDS`
  and :func:`propose`.  The person can still allow such a call once.
* **Scopes**: ``["project"]`` for everything, ``["project", "always"]`` only
  when the rule's own prefix cannot write or run anything whatever arguments
  follow (:data:`ALWAYS_RULES`) and the command itself is not mutating
  (:func:`~snowpea_core.permissions.safe_commands.is_mutating`).

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

#: Characters that make a line more than one plain command: a separator, a
#: pipe, a background ``&``, a redirection or heredoc, a substitution, a
#: subshell or group, a variable, or a second line.
COMPLEX_MARKS: tuple[str, ...] = (
    ";", "&", "|", "<", ">", "`", "$", "(", ")", "{", "}", "\n", "\r", "\\",
)

#: ``NAME=value cmd``: an environment prefix can change what the program does
#: (``GIT_SSH_COMMAND``, ``LD_PRELOAD``), so it is never remembered.
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: A word usable as a program: letters, digits and ``. _ / + -``.
PROGRAM_WORD = re.compile(r"^[\w./+-]+$")

#: A word usable as a subcommand, script or task name.  No dots or slashes,
#: so a file (``bun index.ts``, ``go run ./cmd``) is never mistaken for one.
SUBCOMMAND_WORD = re.compile(r"^[A-Za-z0-9:][\w:-]*$")

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

#: Interpreter families matched by name shape (``python3.11``, ``pip3``).
NEVER_REMEMBERED_SHAPE = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|mkfs(\..+)?)$")

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
#: work, install code from anywhere, run other commands, or act on a cluster.
NEVER_REMEMBERED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {
            "push", "reset", "clean", "rm", "rebase", "filter-branch", "filter-repo",
            "update-ref", "gc", "prune", "reflog", "restore", "checkout", "config",
            "submodule", "bisect", "daemon", "send-email", "credential",
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


@dataclass(frozen=True)
class Proposal:
    """The rule a remembered answer would store."""

    #: Leading words the rule fixes, e.g. ``("git", "status")``.
    words: tuple[str, ...]
    scopes: tuple[str, ...] = field(default=("project",))

    @property
    def pattern(self) -> str:
        """``^git status( .*)?$``: these words, then anything."""
        return "^" + " ".join(re.escape(word) for word in self.words) + "( .*)?$"

    @property
    def display(self) -> str:
        """``git status …``: the human form the card shows."""
        return " ".join(self.words) + " …"


def _family(program: str) -> str:
    """``/usr/bin/git`` -> ``git``; ``pip3.11`` -> ``pip``; ``./gradlew`` -> ``gradlew``."""
    name = program.rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    if re.fullmatch(r"pip[0-9.]*", name):
        return "pip"
    return name


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


def propose(command: str | None) -> Proposal | None:
    """The rule "remember this command" would store for ``command``, or ``None``.

    ``None`` for: an empty or unparsable line; any compound or complex line
    (:data:`COMPLEX_MARKS`: ``; && || |``, backticks, ``$(…)``, ``$VAR``,
    redirects and heredocs, a trailing ``&``, subshells and groups); an
    environment-assignment prefix; :data:`NEVER_REMEMBERED` programs (sudo
    and friends, rm/dd/chmod/kill/…, shells and interpreters, wrappers that
    run another command, network tools, package runners such as ``npx``);
    :data:`NEVER_REMEMBERED_SUBCOMMANDS` (``git push/reset/clean/rm/…``,
    ``npm publish``, ``pip install`` …); and a subcommand tool given no
    subcommand.  The caller adds the request-level cases (``onceOnly``, an
    agent-switch guard, a ``reasonCode``).
    """
    text = (command or "").strip()
    if not text or any(mark in text for mark in COMPLEX_MARKS):
        return None
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    if not words or ENV_ASSIGNMENT.match(words[0]):
        return None
    rule = _words(words)
    if rule is None:
        return None
    always = rule in ALWAYS_RULES and not is_mutating(text)
    return Proposal(rule, ("project", "always") if always else ("project",))


__all__ = [
    "ALWAYS_RULES",
    "COMPLEX_MARKS",
    "NEVER_REMEMBERED",
    "NEVER_REMEMBERED_SUBCOMMANDS",
    "SUBCOMMAND_TOOLS",
    "THIRD_WORD",
    "Proposal",
    "propose",
]
