"""What an approval request is, for Jarvis mode (M19 WP2).

The delegation engine (:mod:`snowpea_core.jarvis.delegation`) is the security
boundary of Jarvis mode, and this module is its eyes: a **pure, deterministic**
function from one approval request (tool, arguments, permission tags, project
root) to a :class:`Verdict`:

``never``
    Never decided by Jarvis: snowpea's own configuration (tag ``config``, any
    path under ``$SNOWPEA_HOME``, the ``snowpea`` CLI), delegation changes,
    browser-owned requests and the Jarvis session's own requests.
``guarded``
    One or more of the classes the person must hand over one by one
    (:data:`GUARDED_CLASSES`): ``deploy`` (push, release, publish, remote
    apply, ssh/scp/rsync to a remote, ``deploy*``/``release*`` scripts),
    ``outside_destructive`` (files outside the project, recursive deletes,
    hard resets, force flags, system-wide changes) and ``secret_send``
    (credentials, secret files, mutating network calls, uploads, mail,
    MCP tools that may write, payment-like tools).
``safe``
    Only reads: tag ``read`` with no secret path, or a shell line
    :mod:`snowpea_core.permissions.safe_commands` knows to be an inspection or a
    test run, with nothing above matching.
``risky``
    Everything else.

A request in several classes reports all of them (:attr:`Verdict.guarded`).
A shell line this module cannot see through — a command substitution, a pipe
into a shell or interpreter, ``eval``, ``sh -c``, inline interpreter code, an
encoded payload, an alias or function definition, a git alias, an unbalanced
quote — **fails closed**: it is reported in every guarded class
(rule ``exec:opaque:<why>``), so only a grant naming all three would let
Jarvis decide it.  Compound lines (``&&``, ``||``, ``;``, plain pipes) are
split and each part judged; ``cd`` is followed so relative paths resolve where
they would run.

It reads nothing but the paths it is given (``Path.resolve`` follows symlinks,
so a link inside the project pointing out is outside), never runs anything,
and never raises on malformed input.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

GuardedClass = Literal["deploy", "outside_destructive", "secret_send"]
Classification = Literal["safe", "risky", "guarded", "never"]

#: Every guarded class, in the order verdicts report them.
GUARDED_CLASSES: tuple[GuardedClass, ...] = ("deploy", "outside_destructive", "secret_send")

DEPLOY: GuardedClass = "deploy"
OUTSIDE: GuardedClass = "outside_destructive"
SECRET: GuardedClass = "secret_send"


@dataclass(frozen=True)
class ClassifyRequest:
    """One approval request, as the engine sees it.

    ``tags`` are the call's effective permission tags
    (:func:`snowpea_core.tools.registry.effective_permission`); empty means
    unknown, and the tool name is used instead.  ``workdir`` is the project
    root the session works in; ``None`` means no project, so every write is
    outside one.  ``home`` is ``$SNOWPEA_HOME`` (resolved from the environment
    when omitted) and ``user_home`` what ``~`` expands to.
    """

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    workdir: str | None = None
    session_id: str | None = None
    #: The request comes from the Jarvis session itself (or a child of it).
    jarvis_session: bool = False
    #: The request belongs to a browser-owned job: only that browser answers.
    browser_owned: bool = False
    #: ``ApprovalRequest.reasonCode`` (``config``, ``secret``, ``outside_project``).
    reason_code: str | None = None
    #: An MCP tool's ``readOnlyHint``; ``None`` when unknown.
    read_only_hint: bool | None = None
    home: str | None = None
    user_home: str | None = None

    @classmethod
    def from_approval(
        cls,
        request: Any,
        *,
        tags: Iterable[str] = (),
        workdir: str | os.PathLike[str] | None = None,
        jarvis_session: bool = False,
        browser_owned: bool = False,
        read_only_hint: bool | None = None,
        home: str | os.PathLike[str] | None = None,
    ) -> ClassifyRequest:
        """Build one from a :class:`~snowpea_core.server.protocol.ApprovalRequest`."""
        return cls(
            tool=str(getattr(request, "tool", "") or ""),
            args=dict(getattr(request, "args", None) or {}),
            tags=tuple(tags),
            workdir=str(workdir) if workdir else None,
            session_id=getattr(request, "sessionId", None),
            jarvis_session=jarvis_session,
            browser_owned=browser_owned,
            reason_code=getattr(request, "reasonCode", None),
            read_only_hint=read_only_hint,
            home=str(home) if home else None,
        )


@dataclass(frozen=True)
class Verdict:
    """How the engine classified one request."""

    classification: Classification
    #: Every guarded class the request belongs to, in :data:`GUARDED_CLASSES` order.
    #: Also filled for ``never``, for the record.
    guarded: tuple[GuardedClass, ...] = ()
    #: Every rule that matched, most decisive first (``tag:config``,
    #: ``exec:git-push``, ``exec:opaque:substitution``, ``tag:read``, ...).
    rules: tuple[str, ...] = ()

    @property
    def rule(self) -> str | None:
        """The rule the ledger names: the first that matched."""
        return self.rules[0] if self.rules else None

    @property
    def guarded_class(self) -> GuardedClass | None:
        """The first guarded class, for wire fields that carry one."""
        return self.guarded[0] if self.guarded else None


# ---------------------------------------------------------------------------
# tool names
# ---------------------------------------------------------------------------

#: Tools whose ``command`` argument is a shell line (snowpea's own, and the
#: names the external CLIs use for theirs).
SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "shell", "bash", "run_command", "exec", "execute", "terminal",
        "run_shell_command", "commandexecution", "process_start", "local_shell",
    }
)
#: Tools that run arbitrary code given inline.
CODE_TOOL_NAMES: frozenset[str] = frozenset({"execute_code", "run_code", "python", "eval"})
#: Tools that write files at their path arguments.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "write_file", "patch", "write", "edit", "multiedit", "notebookedit", "apply_patch",
        "filechange", "edit_file", "create_file", "str_replace", "str_replace_editor",
        "move_file", "copy_file", "rename_file", "lsp_rename", "git_commit",
    }
)
#: Tools that only read.
READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file", "read", "grep", "glob", "list_dir", "ls", "view_image", "notebookread",
        "git_status", "git_diff", "git_log", "web_search", "web_extract", "webfetch",
        "websearch", "todowrite", "todoread",
    }
)
#: Tools that change Jarvis's own authority: never decided by Jarvis.
NEVER_TOOL_NAMES: frozenset[str] = frozenset(
    {"delegation_set", "jarvis_delegation_set", "jarvis.delegation.set", "jarvis.delegation.revoke"}
)

#: Tool-name words that send something out of the machine.
_SEND_WORDS = re.compile(
    r"(^|[_.\-])(send|post|email|mail|message|msg|reply|forward|upload|share|tweet|"
    r"comment|notify|invite|publish|broadcast|sms|dm|chat_?postmessage|create_?issue|"
    r"create_?pull_?request|transfer)([_.\-]|$)",
    re.IGNORECASE,
)
#: Tool-name words that move money.
_PAY_WORDS = re.compile(
    r"(^|[_.\-])(pay|payment|payout|charge|purchase|checkout|invoice|refund|order|buy|"
    r"subscribe|subscription|billing|wallet|withdraw|deposit|stripe)([_.\-]|$)",
    re.IGNORECASE,
)
#: Tool-name words that deploy or release.
_DEPLOY_WORDS = re.compile(
    r"(^|[_.\-])(deploy|release|publish|rollout|promote|merge_?pull_?request|merge_?pr)"
    r"([_.\-]|$)",
    re.IGNORECASE,
)
#: Tool-name words that destroy.
_DELETE_WORDS = re.compile(
    r"(^|[_.\-])(delete|remove|rm|destroy|drop|purge|wipe|erase|truncate|archive|trash)"
    r"([_.\-]|$)",
    re.IGNORECASE,
)
#: Browser actions that act on a page rather than read it.
_BROWSER_ACTION_WORDS = re.compile(
    r"(click|type|fill|submit|press|select|upload|evaluate|script|send|fetch|download|drag)",
    re.IGNORECASE,
)

MCP_PREFIX = "mcp__"

# ---------------------------------------------------------------------------
# secret paths
# ---------------------------------------------------------------------------

#: Credential files the shared pattern list does not name.
_EXTRA_SECRET_NAMES: frozenset[str] = frozenset(
    {
        ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", ".my.cnf",
        ".htpasswd", "credentials", "credentials.json", "credentials.toml", "secrets.json",
        "secrets.yaml", "secrets.yml", "secrets.toml", ".vault-token", "service-account.json",
        "application_default_credentials.json", "access_tokens.db", "hosts.yml",
        "shadow", "gshadow", "master.key", ".boto", ".s3cfg", ".dockercfg",
    }
)
#: Credential directories (as consecutive path parts) beyond the shared list.
_EXTRA_SECRET_DIRS: tuple[tuple[str, ...], ...] = (
    (".config", "gh"),
    (".config", "gcloud"),
    (".config", "hub"),
    (".password-store",),
    (".gnupg",),
    (".ssh",),
    (".aws",),
    (".azure",),
    (".kube",),
    (".docker",),
    (".terraform.d",),
    (".config", "op"),
    (".1password",),
)


def is_secret_path(path: Path) -> bool:
    """True when ``path`` (resolved) is a credential or key location."""
    try:
        from snowpea_core.agent.prompt_refs import is_secret_pattern

        if is_secret_pattern(path):
            return True
    except Exception:  # noqa: BLE001 - a broken helper must not widen anything
        return True
    if path.name in _EXTRA_SECRET_NAMES or path.name.lower() in _EXTRA_SECRET_NAMES:
        return True
    parts = path.parts
    for run in _EXTRA_SECRET_DIRS:
        width = len(run)
        for index in range(len(parts) - width + 1):
            if tuple(parts[index : index + width]) == run:
                return True
    return False


#: A shell line that names snowpea's own home or its config files.
_SNOWPEA_HOME_TEXT = re.compile(
    r"((~|\$HOME|\$\{HOME\})/\.snowpea(/|\b)|\$\{?SNOWPEA_HOME|"
    r"\.snowpea/(settings|credentials)\.json|\bdelegation\.db\b|jarvis/ledger|"
    r"\bmcp-oauth\.json\b)",
    re.IGNORECASE,
)
#: ``$NAME`` / ``${NAME}`` references to credential-looking variables.
_SECRET_VARIABLE = re.compile(
    r"\$\{?[A-Za-z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|APIKEY|"
    r"PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY|AUTH|SESSION_?KEY|COOKIE)[A-Za-z0-9_]*\}?",
    re.IGNORECASE,
)
#: Assignment names that carry a credential (``GITHUB_TOKEN=… gh …``).
_SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|APIKEY|PRIVATE_?KEY|CREDENTIAL|"
    r"ACCESS_?KEY|AUTH)",
    re.IGNORECASE,
)
#: Environment variables that load or run code in whatever runs next.
_CODE_VARIABLES: frozenset[str] = frozenset(
    {
        "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS",
        "PS4", "IFS", "PATH", "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS",
        "NODE_PATH", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB", "GIT_SSH", "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH", "GIT_EDITOR", "GIT_PAGER", "GIT_ASKPASS", "GIT_PROXY_COMMAND",
        "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
        "EDITOR", "VISUAL", "PAGER", "SSH_ASKPASS", "SUDO_ASKPASS", "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS", "MAVEN_OPTS", "npm_config_script_shell", "NPM_CONFIG_SCRIPT_SHELL",
    }
)

# ---------------------------------------------------------------------------
# the shell line
# ---------------------------------------------------------------------------


class _Opaque(Exception):
    """The line cannot be judged; the reason becomes ``exec:opaque:<why>``."""


@dataclass
class _Cmd:
    """One simple command of a line."""

    words: list[str] = field(default_factory=list)
    #: Per word: it holds an expansion (``$X``, ``${X}``, ``$(…)``, a backtick).
    dynamic: list[bool] = field(default_factory=list)
    #: ``(operator, target, dynamic)`` for each redirection.
    redirects: list[tuple[str, str, bool]] = field(default_factory=list)
    #: Fed by a pipe from the command before it.
    piped_in: bool = False
    #: Reads a here-document or here-string.
    heredoc: bool = False


@dataclass
class _Line:
    commands: list[_Cmd]
    #: ``$(…)``, backticks, ``<(…)``/``>(…)`` or ``$((…))`` appeared.
    substitution: bool = False
    #: ``name() …`` — a shell function definition.
    function: bool = False


class _Scanner:
    """A small POSIX-shell lexer: words, operators, redirections, here-docs.

    It does not evaluate anything; it only has to agree with the shell on
    where words and commands begin and end, and to notice every construct
    whose effect it cannot see.  When in doubt it raises :class:`_Opaque`.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.line = _Line(commands=[])
        self.cmd = _Cmd()
        self.word: list[str] = []
        self.in_word = False
        self.dynamic = False
        self.pending_redirect: str | None = None
        self.pending_heredocs: list[tuple[str, bool, bool]] = []
        #: The word being read had a quote or a backslash in it.
        self.quoted = False
        self.piped_in = False

    # -- words -----------------------------------------------------------
    def _end_word(self) -> None:
        if not self.in_word:
            return
        text = "".join(self.word)
        if self.pending_redirect is not None:
            operator = self.pending_redirect
            self.pending_redirect = None
            if operator in ("<<", "<<-"):
                self.pending_heredocs.append((text, operator == "<<-", self.quoted))
                self.cmd.heredoc = True
            elif operator == "<<<":
                self.cmd.heredoc = True
            else:
                self.cmd.redirects.append((operator, text, self.dynamic))
        elif text in ("{", "}") and not self.dynamic:
            pass  # a brace group: its commands are judged like any others
        else:
            self.cmd.words.append(text)
            self.cmd.dynamic.append(self.dynamic)
        self.word = []
        self.in_word = False
        self.dynamic = False
        self.quoted = False

    def _end_command(self, operator: str | None) -> None:
        self._end_word()
        if self.pending_redirect is not None:
            raise _Opaque("syntax")
        if self.cmd.words or self.cmd.redirects or self.cmd.heredoc:
            self.cmd.piped_in = self.piped_in
            self.line.commands.append(self.cmd)
        elif operator in ("|", "|&", "&&", "||"):
            raise _Opaque("syntax")
        elif operator == "\n":
            return  # a pipe or ``&&`` may continue on the next line
        self.cmd = _Cmd()
        self.piped_in = operator in ("|", "|&")

    # -- here-documents ----------------------------------------------------
    def _read_heredocs(self) -> None:
        text = self.text
        for delimiter, strip_tabs, quoted in self.pending_heredocs:
            while self.i <= len(text):
                end = text.find("\n", self.i)
                end = len(text) if end < 0 else end
                line = text[self.i : end]
                self.i = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                if not quoted and ("`" in line or "$(" in line):
                    # An unquoted delimiter: the body is expanded, substitutions run.
                    self.line.substitution = True
                if end >= len(text):
                    break
        self.pending_heredocs = []

    # -- the scan ----------------------------------------------------------
    def scan(self) -> _Line:
        text = self.text
        n = len(text)
        while self.i < n:
            char = text[self.i]
            nxt = text[self.i + 1] if self.i + 1 < n else ""
            if char in " \t\r":
                self._end_word()
                self.i += 1
            elif char == "\n":
                self._end_command("\n")
                self.i += 1
                if self.pending_heredocs:
                    self._read_heredocs()
            elif char == "#" and not self.in_word:
                end = text.find("\n", self.i)
                self.i = n if end < 0 else end
            elif char == "\\":
                self.quoted = True
                if nxt == "\n":
                    self.i += 2
                elif nxt:
                    self.word.append(nxt)
                    self.in_word = True
                    self.i += 2
                else:
                    self.word.append(char)
                    self.in_word = True
                    self.i += 1
            elif char == "'":
                self.quoted = True
                end = text.find("'", self.i + 1)
                if end < 0:
                    raise _Opaque("unbalanced-quote")
                self.word.append(text[self.i + 1 : end])
                self.in_word = True
                self.i = end + 1
            elif char == '"':
                self._double_quoted()
            elif char == "$":
                self._dollar(nxt)
            elif char == "`":
                self.line.substitution = True
                self._end_command(";")
                self.i += 1
            elif char == "|":
                operator = "||" if nxt == "|" else ("|&" if nxt == "&" else "|")
                self._end_command(operator)
                self.i += len(operator)
            elif char == "&":
                if nxt == "&":
                    self._end_command("&&")
                    self.i += 2
                elif nxt == ">":
                    self._redirect("&>>" if text[self.i + 2 : self.i + 3] == ">" else "&>")
                else:
                    self._end_command("&")
                    self.i += 1
            elif char == ";":
                self._end_command(";")
                self.i += 2 if nxt == ";" else 1
            elif char == "(":
                if self.in_word and nxt == ")":
                    self.line.function = True
                    self.i += 2
                    self._end_word()
                else:
                    self._end_command("(")
                    self.i += 1
            elif char == ")":
                self._end_command(")")
                self.i += 1
            elif char in "<>":
                self._angle(char, nxt)
            else:
                self.word.append(char)
                self.in_word = True
                self.i += 1
        self._end_command(None)
        return self.line

    def _double_quoted(self) -> None:
        text = self.text
        self.quoted = True
        self.i += 1
        self.in_word = True
        while True:
            if self.i >= len(text):
                raise _Opaque("unbalanced-quote")
            char = text[self.i]
            if char == '"':
                self.i += 1
                return
            if char == "\\" and self.i + 1 < len(text) and text[self.i + 1] in '$`"\\\n':
                if text[self.i + 1] != "\n":
                    self.word.append(text[self.i + 1])
                self.i += 2
                continue
            if char == "`" or (char == "$" and text[self.i + 1 : self.i + 2] == "("):
                self.line.substitution = True
                self.dynamic = True
            elif char == "$" and re.match(r"[A-Za-z0-9_{@*#?$!-]", text[self.i + 1 : self.i + 2]):
                self.dynamic = True
            self.word.append(char)
            self.i += 1

    def _dollar(self, nxt: str) -> None:
        text = self.text
        if nxt == "(":
            # ``$(…)`` / ``$((…))``: the parenthesis is then read as an operator,
            # so the commands inside are judged too.
            self.line.substitution = True
            self.dynamic = True
            self.word.append("$")
            self.in_word = True
            self.i += 1
            self._end_word()
            return
        if nxt == "{":
            end = text.find("}", self.i)
            if end < 0:
                raise _Opaque("unbalanced-brace")
            self.word.append(text[self.i : end + 1])
            self.dynamic = True
            self.in_word = True
            self.i = end + 1
            return
        if nxt == "'":
            # ``$'…'``: ANSI-C quoting, which can spell anything with escapes.
            end = text.find("'", self.i + 2)
            if end < 0:
                raise _Opaque("unbalanced-quote")
            body = text[self.i + 2 : end]
            if "\\" in body:
                self.dynamic = True
            self.word.append(body)
            self.in_word = True
            self.i = end + 1
            return
        if nxt and re.match(r"[A-Za-z0-9_@*#?$!-]", nxt):
            self.dynamic = True
        self.word.append("$")
        self.in_word = True
        self.i += 1

    def _redirect(self, operator: str) -> None:
        # A word made only of digits right before the operator is its fd.
        if self.in_word and "".join(self.word).isdigit():
            self.word = []
            self.in_word = False
        else:
            self._end_word()
        if self.pending_redirect is not None:
            raise _Opaque("syntax")
        self.pending_redirect = operator
        self.i += len(operator)
        # ``>&2`` / ``2>&1``: a duplicated descriptor, not a file.
        match = re.match(r"\s*(\d+|-)(?=$|[\s;&|)])", self.text[self.i :])
        if operator.endswith("&") and match:
            self.pending_redirect = None
            self.i += match.end()

    def _angle(self, char: str, nxt: str) -> None:
        text = self.text
        if nxt == "(":
            self.line.substitution = True  # process substitution
            self._end_command("(")
            self.i += 2
            return
        if char == "<":
            if text.startswith("<<<", self.i):
                self._redirect("<<<")
            elif text.startswith("<<-", self.i):
                self._redirect("<<-")
            elif nxt == "<":
                self._redirect("<<")
            elif nxt == "&":
                self._redirect("<&")
            elif nxt == ">":
                self._redirect("<>")
            else:
                self._redirect("<")
            return
        if nxt == ">":
            self._redirect(">>")
        elif nxt == "|":
            self._redirect(">|")
        elif nxt == "&":
            self._redirect(">&")
        else:
            self._redirect(">")


def parse_line(command: str) -> _Line:
    """Split a shell line into simple commands; :class:`_Opaque` when it cannot."""
    return _Scanner(command).scan()


# ---------------------------------------------------------------------------
# program tables
# ---------------------------------------------------------------------------

#: Shells: ``-c`` is inline code, no script argument reads stdin.
SHELLS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "ash", "busybox",
     "pwsh", "powershell", "cmd", "rbash", "yash", "nu", "xonsh", "elvish"}
)
#: Interpreters: an inline-code flag, or no script, is opaque.
INTERPRETERS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "pypy": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E", "-n", "-p", "-i"}),
    "ruby": frozenset({"-e", "-n", "-p"}),
    "php": frozenset({"-r", "-B", "-R", "-E", "-F"}),
    "lua": frozenset({"-e"}),
    "luajit": frozenset({"-e"}),
    "tclsh": frozenset(),
    "wish": frozenset(),
    "rscript": frozenset({"-e", "--expr"}),
    "osascript": frozenset({"-e"}),
    "jshell": frozenset(),
    "irb": frozenset(),
    "julia": frozenset({"-e", "--eval", "-E", "--print"}),
    "groovy": frozenset({"-e"}),
    "scala": frozenset({"-e"}),
    "swift": frozenset({"-e"}),
}
#: Programs that run their arguments as a command line through a shell.
SHELL_STRING_RUNNERS: frozenset[str] = frozenset({"watch", "parallel", "script", "su", "runuser"})
#: Agent CLIs: their prompt is an instruction to do anything.
AGENT_CLIS: frozenset[str] = frozenset(
    {"claude", "codex", "agy", "cursor-agent", "gemini", "aider", "opencode", "goose",
     "amp", "crush", "qwen", "hermes"}
)
#: Wrappers that run the rest of the line as a command, and the options
#: (taking a value) they may carry before it.
WRAPPERS: dict[str, frozenset[str]] = {
    "nohup": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "chronic": frozenset(),
    "caffeinate": frozenset({"-t", "-w"}),
    "unbuffer": frozenset(),
    "setsid": frozenset(),
    "xvfb-run": frozenset({"-n", "-s", "-f", "-e", "-p"}),
    "strace": frozenset({"-o", "-e", "-p", "-s", "-u"}),
    "ltrace": frozenset({"-o", "-e", "-p", "-s", "-u"}),
    "catchsegv": frozenset(),
    "taskset": frozenset(),
    "systemd-run": frozenset({"-p", "--property", "-u", "--unit"}),
}
#: Wrappers that elevate privileges: system-wide by definition.
PRIVILEGE: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user",
                       "--group", "--host", "--prompt", "--chdir"}),
    "doas": frozenset({"-u", "-C"}),
    "pkexec": frozenset({"--user"}),
    "run0": frozenset({"-u", "--user", "-g", "--group", "-D", "--chdir"}),
    "gosu": frozenset(),
    "chroot": frozenset(),
}
#: Runners of a package's or project's command (``npx tool``, ``uv run tool``).
RUNNERS: dict[tuple[str, ...], frozenset[str]] = {
    ("npx",): frozenset({"-p", "--package", "-c", "--call"}),
    ("bunx",): frozenset({"-p", "--package"}),
    ("pnpx",): frozenset(),
    ("uvx",): frozenset({"--from", "--with", "-p", "--python"}),
    ("pipx", "run"): frozenset({"--spec"}),
    ("uv", "run"): frozenset({"--with", "--python", "-p", "--project", "--directory",
                              "--package", "--env-file", "--group", "--extra"}),
    ("uv", "tool", "run"): frozenset({"--from", "--with", "-p", "--python"}),
    ("poetry", "run"): frozenset(),
    ("pipenv", "run"): frozenset(),
    ("pdm", "run"): frozenset(),
    ("hatch", "run"): frozenset(),
    ("rye", "run"): frozenset(),
    ("bundle", "exec"): frozenset(),
    ("pnpm", "exec"): frozenset(),
    ("pnpm", "dlx"): frozenset(),
    ("yarn", "exec"): frozenset(),
    ("yarn", "dlx"): frozenset(),
    ("bun", "x"): frozenset(),
    ("npm", "exec"): frozenset({"-p", "--package", "-c", "--call"}),
    ("npm", "x"): frozenset({"-p", "--package", "-c", "--call"}),
    ("mise", "exec"): frozenset(),
    ("mise", "x"): frozenset(),
    ("asdf", "exec"): frozenset(),
    ("rbenv", "exec"): frozenset(),
    ("pyenv", "exec"): frozenset(),
    ("conda", "run"): frozenset({"-n", "--name", "-p", "--prefix"}),
    ("micromamba", "run"): frozenset({"-n", "--name", "-p", "--prefix"}),
    ("dotenv", "run"): frozenset(),
    ("devbox", "run"): frozenset(),
    ("nix", "develop", "-c"): frozenset(),
}
#: Runners that inject secrets into the command they run.
SECRET_RUNNERS: dict[tuple[str, ...], frozenset[str]] = {
    ("op", "run"): frozenset({"--env-file"}),
    ("doppler", "run"): frozenset({"-p", "--project", "-c", "--config"}),
    ("infisical", "run"): frozenset({"--env", "--path"}),
    ("aws-vault", "exec"): frozenset(),
    ("vault", "exec"): frozenset(),
    ("chamber", "exec"): frozenset(),
    ("sops", "exec-env"): frozenset(),
    ("dotenvx", "run"): frozenset({"-f", "--env-file"}),
    ("envchain",): frozenset(),
}

#: ``deploy*`` / ``release*`` / ``publish*`` as a script, target or program name.
_DEPLOY_NAME = re.compile(
    r"(^|[-_.:/])(deploy|deployment|release|publish|ship|rollout|promote|upload|push)"
    r"([-_.:]|$)",
    re.IGNORECASE,
)

#: Package-manager subcommands that publish.
_PUBLISH_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"publish", "unpublish", "deprecate", "dist-tag", "owner", "access",
                      "team", "undeprecate"}),
    "pnpm": frozenset({"publish"}),
    "yarn": frozenset({"publish", "npm"}),
    "bun": frozenset({"publish"}),
    "cargo": frozenset({"publish", "yank", "owner"}),
    "twine": frozenset({"upload", "register"}),
    "poetry": frozenset({"publish"}),
    "flit": frozenset({"publish"}),
    "hatch": frozenset({"publish"}),
    "pdm": frozenset({"publish"}),
    "uv": frozenset({"publish"}),
    "rye": frozenset({"publish"}),
    "gem": frozenset({"push", "yank", "owner"}),
    "mvn": frozenset({"deploy", "release:perform", "release:prepare"}),
    "mvnw": frozenset({"deploy", "release:perform", "release:prepare"}),
    "nuget": frozenset({"push", "delete"}),
    "pod": frozenset({"trunk"}),
    "dart": frozenset({"pub"}),
    "flutter": frozenset({"pub"}),
    "mix": frozenset({"hex.publish"}),
    "lerna": frozenset({"publish", "version"}),
    "changeset": frozenset({"publish"}),
    "vsce": frozenset({"publish", "unpublish"}),
    "ovsx": frozenset({"publish"}),
    "goreleaser": frozenset({"release", "publish"}),
    "composer": frozenset(),
    "jsr": frozenset({"publish"}),
    "deno": frozenset({"publish", "deploy"}),
}
#: Package-manager subcommands that log in or print a token.
_LOGIN_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"login", "adduser", "token", "logout"}),
    "pnpm": frozenset({"login", "logout"}),
    "yarn": frozenset({"login", "logout"}),
    "cargo": frozenset({"login", "logout"}),
    "docker": frozenset({"login"}),
    "podman": frozenset({"login"}),
    "helm": frozenset({"registry"}),
    "gem": frozenset({"signin"}),
}
#: Whole programs that release or deploy whatever they are given.
DEPLOY_PROGRAMS: frozenset[str] = frozenset(
    {
        "semantic-release", "release-it", "np", "goreleaser", "ansible-playbook", "cap",
        "capistrano", "mina", "fastlane", "skopeo", "crane", "ko", "eb", "copilot",
        "kamal", "dokku", "railway", "render", "surge", "now", "gh-pages",
        "azcopy", "s3cmd", "s5cmd", "kubectl-apply", "argocd", "flux", "kustomize-apply",
        "nixops", "deployctl", "zappa", "chalice", "shipit",
    }
)
#: Cloud and cluster CLIs, and their subcommands that only read.
CLOUD_READ_ONLY: dict[str, frozenset[str]] = {
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "api-resources",
                          "api-versions", "version", "cluster-info", "auth", "diff", "wait",
                          "events"}),
    "oc": frozenset({"get", "describe", "logs", "status", "whoami", "version", "projects"}),
    "helm": frozenset({"list", "ls", "status", "get", "history", "show", "search", "template",
                       "lint", "version", "env", "dependency", "repo", "verify", "plugin"}),
    "terraform": frozenset({"plan", "init", "validate", "fmt", "show", "output", "providers",
                            "version", "graph", "console", "workspace", "get", "test"}),
    "tofu": frozenset({"plan", "init", "validate", "fmt", "show", "output", "providers",
                       "version", "graph", "console", "workspace", "get", "test"}),
    "terragrunt": frozenset({"plan", "init", "validate", "fmt", "show", "output",
                             "version", "graph", "hclfmt", "render-json"}),
    "pulumi": frozenset({"preview", "stack", "config", "whoami", "version", "about", "logs",
                         "plugin", "login"}),
    "cdk": frozenset({"synth", "diff", "list", "ls", "doctor", "version", "context", "init",
                      "bootstrap"}),
    "sam": frozenset({"build", "validate", "local", "init", "logs", "list"}),
    "serverless": frozenset({"package", "print", "info", "invoke", "logs", "plugin"}),
    "sls": frozenset({"package", "print", "info", "invoke", "logs", "plugin"}),
    "vercel": frozenset({"ls", "list", "inspect", "logs", "whoami", "dev", "build", "pull",
                         "env", "domains", "projects", "teams", "help", "--version", "-v"}),
    "netlify": frozenset({"status", "dev", "build", "open", "sites:list", "logs", "watch",
                          "functions:list", "env:list", "--version"}),
    "firebase": frozenset({"emulators:start", "emulators:exec", "serve", "projects:list",
                           "login", "logout", "use", "init", "--version", "apps:list"}),
    "fly": frozenset({"status", "logs", "info", "list", "ls", "auth", "version", "dashboard",
                      "doctor", "platform", "regions", "ips", "volumes"}),
    "flyctl": frozenset({"status", "logs", "info", "list", "ls", "auth", "version",
                         "dashboard", "doctor", "platform", "regions", "ips", "volumes"}),
    "heroku": frozenset({"logs", "apps:info", "info", "ps", "status", "whoami", "releases",
                         "addons", "domains", "help", "version", "--version"}),
    "wrangler": frozenset({"dev", "whoami", "tail", "types", "init", "login", "logout",
                           "--version", "docs", "pages"}),
    "eas": frozenset({"whoami", "login", "logout", "build:list", "build:view", "init",
                      "project:info", "config", "diagnostics"}),
    "expo": frozenset({"start", "doctor", "install", "prebuild", "config", "export",
                       "whoami", "login", "logout", "run:ios", "run:android", "lint"}),
    "gcloud": frozenset(),
    "az": frozenset(),
    "aws": frozenset(),
    "doctl": frozenset(),
    "linode-cli": frozenset(),
    "hcloud": frozenset(),
    "oci": frozenset(),
    "ibmcloud": frozenset(),
    "rclone": frozenset({"ls", "lsd", "lsl", "lsf", "lsjson", "cat", "check", "config",
                         "about", "size", "tree", "version", "listremotes", "md5sum",
                         "sha1sum", "hashsum", "ncdu", "mount"}),
    "gsutil": frozenset({"ls", "cat", "du", "stat", "hash", "version", "help"}),
}
#: Words that mean "only reads" inside a generic cloud CLI's command path.
_CLOUD_READ_VERB = re.compile(
    r"^(list|ls|describe|get|show|view|status|info|logs|log|tail|read|print|version|help|"
    r"whoami|search|find|query|inspect|wait|check|validate|explain|account|config|auth|"
    r"login|init|--version|-v|--help|-h)([-_].*)?$",
    re.IGNORECASE,
)
#: Words in a cloud CLI's command path that reveal a secret.
_CLOUD_SECRET_WORD = re.compile(
    r"(secret|password|credential|token|access-key|get-secret-value|get-parameter|"
    r"print-access-token|print-identity-token|get-access-token|export-credentials|"
    r"decrypt|keys?$|sas|kms)",
    re.IGNORECASE,
)
#: Words in a cloud CLI's command path that send messages.
_CLOUD_SEND_WORD = re.compile(
    r"^(send-email|send-raw-email|send-message|send-message-batch|publish|invoke|"
    r"put-events|send|notify)$",
    re.IGNORECASE,
)
#: Words in a cloud CLI's command path that destroy.
_CLOUD_DESTROY_WORD = re.compile(
    r"^(delete|destroy|remove|rm|rb|terminate|purge|drop|uninstall|delete-.+|"
    r"terminate-.+|remove-.+|deregister-.+)$",
    re.IGNORECASE,
)

#: Secret managers, keychains and token printers: ``(program, subcommand)`` pairs,
#: ``None`` meaning every subcommand.
SECRET_READERS: dict[str, frozenset[str] | None] = {
    "security": frozenset({"find-generic-password", "find-internet-password",
                           "dump-keychain", "export", "find-certificate", "find-identity"}),
    "pass": None,
    "gopass": None,
    "op": None,
    "bw": None,
    "lpass": None,
    "keepassxc-cli": None,
    "vault": None,
    "secret-tool": frozenset({"lookup", "search"}),
    "keyring": frozenset({"get"}),
    "sops": None,
    "age": frozenset({"-d", "--decrypt"}),
    "chamber": None,
    "doppler": None,
    "infisical": None,
    "aws-vault": None,
    "kwallet-query": None,
    "kubeseal": None,
    "printenv": None,
    "gpg": frozenset({"--export-secret-keys", "--export-secret-subkeys", "--decrypt", "-d",
                      "--export-ownertrust", "--gen-revoke"}),
    "gpg2": frozenset({"--export-secret-keys", "--export-secret-subkeys", "--decrypt", "-d"}),
}
#: Programs that send mail or speak raw network protocols.
SENDERS: frozenset[str] = frozenset(
    {
        "mail", "mailx", "sendmail", "mutt", "neomutt", "msmtp", "swaks", "ssmtp", "s-nail",
        "nc", "ncat", "netcat", "socat", "telnet", "ftp", "tftp", "lftp", "sftp", "curlftpfs",
        "smbclient", "mosquitto_pub", "kcat", "kafkacat", "websocat", "wscat", "grpcurl",
        "irssi", "weechat", "signal-cli", "telegram-send", "slack", "twilio", "aws-ses",
        "transfer", "croc", "magic-wormhole", "wormhole", "ffsend", "rsh", "rcp", "rexec",
    }
)
#: HTTP clients (judged by their flags).
HTTP_CLIENTS: frozenset[str] = frozenset(
    {"curl", "wget", "wget2", "http", "https", "xh", "xhs", "curlie", "httpie", "aria2c",
     "fetch", "lwp-request"}
)
#: Programs that reach the network even when they only read (never ``safe``).
#: Programs that only sometimes do (git, npm, docker, ...) say so per subcommand.
NETWORK_PROGRAMS: frozenset[str] = HTTP_CLIENTS | SENDERS | frozenset(
    {"ssh", "scp", "rsync", "gh", "glab", "hub", "aws", "gcloud", "az", "ping", "dig",
     "nslookup", "host", "whois", "traceroute", "openssl", "brew", "rclone", "gsutil"}
)
#: Builtin tools whose names would trip the send/delete name rules but that do
#: neither (talking to a subagent, the todo list, the memory store).
NAME_RULE_EXEMPT: frozenset[str] = frozenset(
    {"subagent_message", "subagent_wait", "delegate_task", "queue_command", "goal_update",
     "plan_update_step", "plan_save", "write_todos", "ask_user", "save_memory",
     "search_memory", "process_output", "process_list", "set_mode", "tool_search",
     "settings_get", "skill_view", "skill_list", "skill_search"}
)
#: Package managers that install system-wide.
SYSTEM_INSTALLERS: dict[str, frozenset[str]] = {
    "brew": frozenset({"install", "uninstall", "remove", "rm", "upgrade", "reinstall", "tap",
                       "untap", "link", "unlink", "cleanup", "services", "update", "autoremove"}),
    "apt": frozenset({"install", "remove", "purge", "upgrade", "full-upgrade", "dist-upgrade",
                      "autoremove", "update", "reinstall"}),
    "apt-get": frozenset({"install", "remove", "purge", "upgrade", "dist-upgrade",
                          "autoremove", "update", "reinstall"}),
    "aptitude": frozenset({"install", "remove", "purge", "upgrade", "safe-upgrade",
                           "full-upgrade", "update"}),
    "dpkg": frozenset({"-i", "--install", "-r", "--remove", "-P", "--purge",
                       "--configure"}),
    "dnf": frozenset({"install", "remove", "erase", "upgrade", "update", "reinstall",
                      "downgrade", "autoremove"}),
    "yum": frozenset({"install", "remove", "erase", "upgrade", "update", "reinstall",
                      "downgrade", "autoremove"}),
    "rpm": frozenset({"-i", "-U", "-e", "--install", "--upgrade", "--erase"}),
    "zypper": frozenset({"install", "in", "remove", "rm", "update", "up", "dist-upgrade",
                         "dup"}),
    "pacman": frozenset({"-S", "-R", "-U", "-Syu", "-Sy", "-Rs", "-Rns", "-Su"}),
    "yay": frozenset({"-S", "-R", "-U", "-Syu"}),
    "apk": frozenset({"add", "del", "upgrade", "update"}),
    "snap": frozenset({"install", "remove", "refresh", "revert"}),
    "flatpak": frozenset({"install", "uninstall", "update"}),
    "port": frozenset({"install", "uninstall", "upgrade", "selfupdate"}),
    "choco": frozenset({"install", "uninstall", "upgrade"}),
    "winget": frozenset({"install", "uninstall", "upgrade"}),
    "scoop": frozenset({"install", "uninstall", "update"}),
    "nix-env": frozenset({"-i", "-e", "-u", "--install", "--uninstall", "--upgrade"}),
    "pipx": frozenset({"install", "uninstall", "upgrade", "reinstall", "inject",
                       "upgrade-all", "uninstall-all", "ensurepath"}),
    "cargo": frozenset({"install", "uninstall"}),
    "go": frozenset({"install"}),
    "gem": frozenset({"install", "uninstall", "update"}),
    "rustup": frozenset({"install", "uninstall", "self", "update", "default", "toolchain",
                         "component", "target"}),
    "sdk": frozenset({"install", "uninstall", "default"}),
    "nvm": frozenset({"install", "uninstall", "alias", "use"}),
    "volta": frozenset({"install", "uninstall", "pin"}),
}
#: Commands that change the machine, its services or its users.
SYSTEM_PROGRAMS: frozenset[str] = frozenset(
    {
        "shutdown", "reboot", "halt", "poweroff", "init", "telinit", "systemctl", "launchctl",
        "service", "crontab", "at", "batch", "mount", "umount", "modprobe", "insmod",
        "rmmod", "sysctl", "iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "pfctl",
        "useradd", "userdel", "usermod", "groupadd", "groupdel", "passwd", "chpasswd",
        "visudo", "fdisk", "parted", "gdisk", "sfdisk", "wipefs", "mkswap", "swapon",
        "swapoff", "dd", "shred", "diskutil", "csrutil", "spctl", "defaults", "scutil",
        "networksetup", "nvram", "bless", "update-alternatives", "update-grub", "grub-install",
        "ldconfig", "setenforce", "chattr", "setfacl", "killall", "pkill", "xkill",
        "kextload", "kextunload", "dscl", "tmutil", "launchd", "loginctl", "timedatectl",
        "hostnamectl", "localectl", "journalctl", "vipw", "chsh", "chfn",
    }
)
#: Of :data:`SYSTEM_PROGRAMS`, the invocations that only read.
SYSTEM_READ_ONLY: dict[str, frozenset[str]] = {
    "systemctl": frozenset({"status", "show", "list-units", "list-unit-files", "is-active",
                            "is-enabled", "is-failed", "cat", "list-timers", "--version"}),
    "launchctl": frozenset({"list", "print", "print-disabled", "blame", "version", "help"}),
    "crontab": frozenset({"-l"}),
    "journalctl": frozenset(),
    "sysctl": frozenset({"-a", "-n"}),
    "diskutil": frozenset({"list", "info", "activity"}),
    "defaults": frozenset({"read", "read-type", "domains", "find", "export"}),
    "tmutil": frozenset({"listbackups", "status", "latestbackup", "destinationinfo"}),
    "loginctl": frozenset({"list-sessions", "show-session", "session-status", "list-users"}),
    "timedatectl": frozenset({"status", "show"}),
    "hostnamectl": frozenset({"status"}),
    "localectl": frozenset({"status"}),
    "mount": frozenset(),
    "iptables": frozenset({"-L", "-S", "--list"}),
    "ufw": frozenset({"status"}),
    "nft": frozenset({"list"}),
    "scutil": frozenset({"--get", "--dns", "--proxy", "--nwi"}),
    "networksetup": frozenset({"-listallnetworkservices", "-getinfo", "-listallhardwareports"}),
    "update-alternatives": frozenset({"--display", "--query", "--list"}),
    "spctl": frozenset({"--status", "-a", "--assess"}),
    "csrutil": frozenset({"status"}),
}
#: Database clients whose inline SQL is checked for destructive statements.
DB_CLIENTS: frozenset[str] = frozenset(
    {"psql", "mysql", "mariadb", "sqlite3", "sqlite", "sqlcmd", "mongo", "mongosh",
     "redis-cli", "clickhouse-client", "clickhouse", "cqlsh", "duckdb", "cockroach", "usql",
     "pgcli", "mycli", "litecli", "bq", "snowsql", "trino", "presto", "influx", "vsql"}
)
_SQL_DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|database|schema|index|view|user|role|collection|keyspace|materialized)|"
    r"truncate(\s+table)?\s+\w|delete\s+from\s+[\w.\"`]+\s*(;|$)|"
    r"alter\s+table\s+\S+\s+drop|flushall|flushdb|dropdatabase\s*\(|\.drop\s*\(|"
    r"deletemany\s*\(\s*\{\s*\}\s*\)|del\s+\S+|unlink\s+\S+|remove\s*\(\s*\{\s*\}\s*\))",
    re.IGNORECASE,
)
#: Commands that reset or drop a database through a framework.
_DB_RESET = re.compile(
    r"(db:(drop|reset|purge|schema:load|migrate:reset|migrate:fresh|wipe|seed:replant)|"
    r"migrate:(fresh|reset|rollback)|migrate\s+reset|--force-reset|flush\b|reset_db|"
    r"dropdb|dropuser|resetdb)",
    re.IGNORECASE,
)
#: Programs that only touch the paths they name (judged as writes).
WRITE_PROGRAMS: frozenset[str] = frozenset(
    {"rm", "rmdir", "unlink", "mv", "cp", "ln", "touch", "mkdir", "chmod", "chown", "chgrp",
     "truncate", "tee", "install", "rsync", "shred", "srm", "trash", "trash-put", "gio",
     "mktemp", "split", "csplit", "patch", "rename", "mmv"}
)
#: Programs whose arguments may come from ``xargs`` / ``find -exec`` without
#: changing what kind of thing they do.
STDIN_ARGS_OK: frozenset[str] = frozenset(
    {"grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "ls", "stat", "file", "echo",
     "printf", "sort", "uniq", "md5sum", "sha1sum", "sha256sum", "shasum", "du", "basename",
     "dirname", "realpath", "readlink", "test", "[", "diff", "cmp", "jq", "yq", "less",
     "more", "nl", "cut", "tr", "column", "xxd", "hexdump", "od", "strings", "awk", "gawk",
     "sed", "dos2unix", "unix2dos", "true"} | WRITE_PROGRAMS
)
#: Of :data:`WRITE_PROGRAMS`, those whose earlier operands are only read.
DESTINATION_ONLY: frozenset[str] = frozenset({"cp", "rsync", "install", "ln"})
#: Programs that only read the files they name, safe to read anywhere.
#: Decoding programs whose output is a payload nobody has seen.
_DECODERS: dict[str, frozenset[str]] = {
    "base64": frozenset({"-d", "--decode", "-D"}),
    "base32": frozenset({"-d", "--decode"}),
    "basenc": frozenset({"-d", "--decode"}),
    "xxd": frozenset({"-r", "-revert"}),
    "uudecode": frozenset(),
    "openssl": frozenset({"-d", "base64", "enc"}),
    "gpg": frozenset({"-d", "--decrypt"}),
    "certutil": frozenset({"-decode", "-decodehex"}),
    "rot13": frozenset(),
}

#: Git subcommands git itself ships; anything else is an alias or an
#: external ``git-*`` program, and could be anything.
GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch", "bundle",
        "cat-file", "check-attr", "check-ignore", "check-mailmap", "check-ref-format",
        "checkout", "checkout-index", "cherry", "cherry-pick", "citool", "clean", "clone",
        "column", "commit", "commit-graph", "commit-tree", "config", "count-objects",
        "credential", "describe", "diff", "diff-files", "diff-index", "diff-tree",
        "difftool", "fetch", "fetch-pack", "filter-branch", "fmt-merge-msg", "for-each-ref",
        "for-each-repo", "format-patch", "fsck", "gc", "get-tar-commit-id", "grep", "gui",
        "hash-object", "help", "imap-send", "index-pack", "init", "instaweb",
        "interpret-trailers", "log", "ls-files", "ls-remote", "ls-tree", "mailinfo",
        "mailsplit", "maintenance", "merge", "merge-base", "merge-file", "merge-tree",
        "mergetool", "mktag", "mktree", "mv", "name-rev", "notes", "pack-objects",
        "pack-refs", "prune", "pull", "push", "range-diff", "read-tree", "rebase", "reflog",
        "remote", "repack", "replace", "request-pull", "rerere", "reset", "restore",
        "rev-list", "rev-parse", "revert", "rm", "send-email", "shortlog", "show",
        "show-branch", "show-ref", "sparse-checkout", "stash", "status", "stripspace",
        "submodule", "switch", "symbolic-ref", "tag", "update-index", "update-ref",
        "verify-commit", "verify-pack", "verify-tag", "version", "whatchanged", "worktree",
        "write-tree", "lfs", "filter-repo", "var", "bugreport", "diagnose", "sparse",
        "range", "backfill", "replay", "last-modified", "refs", "repo",
    }
)
#: ``git -c key=value`` keys that change nothing about what runs.
_GIT_HARMLESS_CONFIG = re.compile(
    r"^(user\.(name|email|signingkey)|color\..*|core\.quotepath|core\.autocrlf|"
    r"commit\.gpgsign|tag\.gpgsign|init\.defaultbranch|advice\..*|log\..*|diff\.(renames|"
    r"algorithm|mnemonicprefix|noprefix|colormoved)|merge\.(conflictstyle|ff)|pull\.(rebase|ff)|"
    r"push\.(default|autosetupremote)|safe\.directory|i18n\..*|gc\.auto|"
    r"maintenance\.auto|rebase\.autostash|status\..*|column\..*|blame\..*|"
    r"protocol\.version|http\.version)=",
    re.IGNORECASE,
)
_GIT_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                         "--super-prefix", "--config-env", "--list-cmds", "--attr-source"})


# ---------------------------------------------------------------------------
# the judge
# ---------------------------------------------------------------------------


@dataclass
class _Hits:
    never: list[str] = field(default_factory=list)
    guarded: dict[str, list[str]] = field(default_factory=dict)
    #: Rules that matched but leave the request ``risky`` (for the record).
    risky: list[str] = field(default_factory=list)
    #: The line talks to the network, so it is never ``safe``.
    network: bool = False

    def guard(self, cls: GuardedClass, rule: str) -> None:
        rules = self.guarded.setdefault(cls, [])
        if rule not in rules:
            rules.append(rule)

    def guard_all(self, rule: str) -> None:
        for cls in GUARDED_CLASSES:
            self.guard(cls, rule)

    def never_(self, rule: str) -> None:
        if rule not in self.never:
            self.never.append(rule)

    def any(self) -> bool:
        return bool(self.never or self.guarded)


@dataclass
class _Ctx:
    """What path arguments are judged against."""

    workdir: Path | None
    home: Path | None
    user_home: Path


def _program(word: str) -> str:
    """``/usr/bin/Git.exe`` -> ``git``; ``python3.11`` stays as typed for the caller."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def _family(program: str) -> str:
    """``python3.11`` -> ``python``; ``pip3`` -> ``pip``; ``gradlew`` -> ``gradle``."""
    if re.fullmatch(r"python[0-9.]*", program):
        return "python"
    if re.fullmatch(r"pypy[0-9.]*", program):
        return "pypy"
    if re.fullmatch(r"pip[0-9.]*", program):
        return "pip"
    if re.fullmatch(r"(node|ruby|perl|php|lua)[0-9.]*", program):
        return re.sub(r"[0-9.]+$", "", program)
    return {"gradlew": "gradle", "mvnw": "mvn", "podman-remote": "podman"}.get(program, program)


def _resolve(raw: str, cwd: Path | None, ctx: _Ctx) -> Path | None:
    """Where a path argument lands, symlinks followed; ``None`` when unknown."""
    text = raw.strip()
    if not text or "$" in text or "`" in text:
        return None
    if text.startswith("~"):
        head, _, rest = text[1:].partition("/")
        if head:
            return None  # ~otheruser
        base = ctx.user_home
        text = str(base / rest) if rest else str(base)
    elif text.startswith("file://"):
        text = text[len("file://") :]
    candidate = Path(text)
    if not candidate.is_absolute():
        if cwd is None:
            return None
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(posixpath.normpath(str(candidate)))


def _glob_base(word: str) -> str:
    """The part of a glob before its first wildcard, cut back to a whole directory."""
    match = re.search(r"[*?\[]", word)
    if not match:
        return word
    head = word[: match.start()]
    if "/" not in head:
        return "."
    return head.rsplit("/", 1)[0] or "/"


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


#: Device files a redirection may target without writing anything of anyone's.
_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"})


def _harmless_target(path: Path) -> bool:
    return str(path) in _DEVICES or str(path).startswith("/dev/fd/")


def _looks_like_path(word: str) -> bool:
    return (
        "/" in word
        or word.startswith(("~", "."))
        or word in _EXTRA_SECRET_NAMES
        or bool(re.match(r"^(id_(rsa|ed25519|ecdsa|dsa)|.+\.(pem|key|p12|pfx|kdbx|ppk))", word))
    )


class _Judge:
    """Applies the rules to one request."""

    def __init__(self, request: ClassifyRequest) -> None:
        self.request = request
        self.hits = _Hits()
        #: The shell line redirects output into a file (so it is never ``safe``).
        self.writes = False
        #: A program got an argument only the shell knows (``cat $F``): never ``safe``.
        self.unknown_args = False
        workdir: Path | None = None
        if request.workdir:
            try:
                workdir = Path(request.workdir).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                workdir = None
        home: Path | None
        try:
            from snowpea_core.config.paths import resolve_home

            home = resolve_home(request.home)
        except Exception:  # noqa: BLE001 - an unresolvable home disables only that check
            home = None
        try:
            user_home = Path(request.user_home or Path.home()).expanduser()
        except (OSError, RuntimeError):
            user_home = Path("/nonexistent-home")
        self.ctx = _Ctx(workdir=workdir, home=home, user_home=user_home)

    # -- paths -----------------------------------------------------------
    def _in_snowpea_home(self, path: Path | None) -> bool:
        home = self.ctx.home
        return path is not None and home is not None and _under(path, home)

    def _is_config(self, path: Path) -> bool:
        """snowpea's own settings, credentials or state (``jarvis/`` too), per config_guard."""
        try:
            from snowpea_core.tools.config_guard import is_config_path

            return is_config_path(str(path), home=self.ctx.home)
        except Exception:  # noqa: BLE001 - unknown means it might be
            return True

    def _outside(self, path: Path | None) -> bool:
        root = self.ctx.workdir
        if path is None or root is None:
            return True
        if _under(path, root) or _harmless_target(path):
            return False
        # Core's own working areas (a session workspace, $SNOWPEA_HOME/plans) are
        # not outside, exactly as for the ``outside`` tag (config_guard.outside_paths).
        return not (self._in_snowpea_home(path) and not self._is_config(path))

    def check_read_path(self, raw: str, cwd: Path | None, rule: str, shell: bool = False) -> None:
        """A path that is read: credentials are ``secret_send``.

        On a shell line (``shell``) a word naming snowpea's own config is
        ``never``: what the program does with it is not visible
        (``sqlite3 …/jarvis/delegation.db 'insert …'``).
        """
        path = _resolve(_glob_base(raw), cwd, self.ctx)
        if path is None:
            return
        if shell and self._is_config(path):
            self.hits.never_(f"{rule}:snowpea-config")
        if self._in_snowpea_home(path):
            home = self.ctx.home
            assert home is not None
            secret_files = {home / "credentials.json", home / "token", home / "mcp-oauth.json"}
            if path in secret_files:
                self.hits.guard(SECRET, f"{rule}:snowpea-secret")
            return
        if is_secret_path(path):
            self.hits.guard(SECRET, f"{rule}:secret-path")

    def check_write_path(self, raw: str, cwd: Path | None, rule: str, dynamic: bool) -> None:
        """A path that is written or deleted."""
        if dynamic:
            self.hits.guard(OUTSIDE, f"{rule}:unknown-path")
            return
        path = _resolve(_glob_base(raw), cwd, self.ctx)
        if path is None:
            self.hits.guard(OUTSIDE, f"{rule}:unknown-path")
            return
        if self._is_config(path):
            self.hits.never_("path:snowpea-config")
            return
        if is_secret_path(path):
            self.hits.guard(SECRET, f"{rule}:secret-path")
        if self._outside(path):
            self.hits.guard(OUTSIDE, f"{rule}:outside-project")

    # -- the request -----------------------------------------------------
    def run(self) -> Verdict:
        request = self.request
        tool = request.tool or ""
        lower = tool.lower()
        tags = {str(tag) for tag in request.tags}
        if not tags:
            tags = {self._tag_from_name(lower)}
        if request.jarvis_session:
            self.hits.never_("never:jarvis-session")
        if request.browser_owned:
            self.hits.never_("never:browser-owned")
        if lower in NEVER_TOOL_NAMES or lower.startswith("jarvis."):
            self.hits.never_("never:delegation")
        if "config" in tags or request.reason_code == "config":
            self.hits.never_("tag:config")
        if "outside" in tags or request.reason_code == "outside_project":
            self.hits.guard(OUTSIDE, "tag:outside")
        if "secret" in tags or request.reason_code == "secret":
            self.hits.guard(SECRET, "tag:secret")
        if "send" in tags:
            self.hits.guard(SECRET, "tag:send")
        self._by_name(lower, tags)
        args = request.args if isinstance(request.args, Mapping) else {}
        command = _command_text(args)
        is_shell = lower in SHELL_TOOL_NAMES or (command is not None and "exec" in tags)
        safe_rule: str | None = None
        if lower in CODE_TOOL_NAMES or (
            "exec" in tags and command is None and any(k in args for k in ("code", "script"))
        ):
            self.hits.guard_all("exec:opaque:inline-code")
        elif is_shell:
            if command is None:
                self.hits.guard_all("exec:opaque:no-command")
            else:
                safe_rule = self.shell(command, args)
        self._paths(args, tags, lower)
        # An MCP tool talks to its server, except one the person tagged ``read``
        # (``mcp.permissions`` per-tool key, as an assistant preset sets for its
        # read tools): that one only reads, like any other read tool.
        if tags & {"network"} or (lower.startswith(MCP_PREFIX) and tags != {"read"}):
            self.hits.network = True
        return self._verdict(tags, lower, safe_rule, is_shell)

    def _tag_from_name(self, lower: str) -> str:
        if lower in SHELL_TOOL_NAMES:
            return "exec"
        if lower in CODE_TOOL_NAMES:
            return "exec"
        if lower in WRITE_TOOL_NAMES:
            return "write"
        if lower in READ_TOOL_NAMES:
            return "read"
        if lower.startswith(MCP_PREFIX):
            return "network"
        return "unknown"

    def _by_name(self, lower: str, tags: set[str]) -> None:
        """Rules keyed on the tool's name: MCP tools, host actions, send-like names."""
        name = lower
        if lower.startswith(MCP_PREFIX):
            name = lower[len(MCP_PREFIX) :].split("__", 1)[-1]
            if self.request.read_only_hint is not True and "read" not in tags:
                self.hits.guard(SECRET, "mcp:not-read-only")
            if tags == {"read"}:
                # The person tagged this one tool ``read`` (``mcp.permissions``
                # ``"<server>/<tool>"``): its name (``read-mail-text``) is not a send.
                return
        if lower.startswith(("repl.", "browser_")):
            action = lower.split(".", 1)[-1] if lower.startswith("repl.") else lower
            if _BROWSER_ACTION_WORDS.search(action):
                self.hits.guard(SECRET, "tool:page-action")
            return
        if (
            lower in SHELL_TOOL_NAMES
            or lower in READ_TOOL_NAMES
            or lower in WRITE_TOOL_NAMES
            or lower in NAME_RULE_EXEMPT
            or "delegate" in tags
        ):
            return
        if _PAY_WORDS.search(name):
            self.hits.guard(SECRET, "tool:payment-like")
        if _SEND_WORDS.search(name):
            self.hits.guard(SECRET, "tool:send-like")
        if _DEPLOY_WORDS.search(name):
            self.hits.guard(DEPLOY, "tool:deploy-like")
        if _DELETE_WORDS.search(name):
            self.hits.guard(OUTSIDE, "tool:delete-like")

    def _paths(self, args: Mapping[str, Any], tags: set[str], lower: str) -> None:
        """Path arguments of a file tool (not the shell line, judged on its own)."""
        base = lower.split("__")[-1] if lower.startswith(MCP_PREFIX) else lower
        writes = (
            bool(tags & {"write", "outside", "config"})
            or base in WRITE_TOOL_NAMES
            or bool(_DELETE_WORDS.search(base))
            or bool(re.search(r"(^|[_.\-])(write|edit|create|move|rename|copy|save|put|"
                              r"mkdir|append|patch|upload)([_.\-]|$)", base))
        )
        for raw in _call_paths(args):
            if writes:
                self.check_write_path(raw, self.ctx.workdir, "path", False)
            else:
                self.check_read_path(raw, self.ctx.workdir, "path")

    def _verdict(self, tags: set[str], lower: str, safe_rule: str | None, shell: bool) -> Verdict:
        hits = self.hits
        guarded = tuple(cls for cls in GUARDED_CLASSES if cls in hits.guarded)
        guarded_rules: list[str] = []
        for cls in guarded:
            for rule in hits.guarded[cls]:
                if rule not in guarded_rules:
                    guarded_rules.append(rule)
        if hits.never:
            return Verdict("never", guarded, tuple(hits.never + guarded_rules))
        if guarded:
            return Verdict("guarded", guarded, tuple(guarded_rules))
        if shell:
            if safe_rule and not hits.network:
                return Verdict("safe", (), (safe_rule,))
            return Verdict("risky", (), tuple(hits.risky) or ("exec:command",))
        if tags == {"read"} and not hits.network:
            return Verdict("safe", (), ("tag:read",))
        first = sorted(tags)[0] if tags else "unknown"
        return Verdict("risky", (), tuple(hits.risky) or (f"tag:{first}",))

    # -- the shell line ----------------------------------------------------
    def shell(self, command: str, args: Mapping[str, Any]) -> str | None:
        """Judge a shell line; return the ``safe`` rule when it only inspects."""
        cwd = self.ctx.workdir
        raw_cwd = args.get("cwd") or args.get("workdir") or args.get("dir")
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            cwd = _resolve(raw_cwd, cwd, self.ctx)
            if cwd is not None and self._in_snowpea_home(cwd):
                self.hits.never_("exec:snowpea-home")
        self._raw_scan(command)
        self.line(command, cwd, depth=0)
        if self.hits.any() or args.get("background") or self.writes or self.unknown_args:
            return None
        from snowpea_core.permissions.safe_commands import is_mutating, is_read_only

        try:
            if is_read_only(command):
                return "exec:read-only"
            if self.ctx.workdir is not None and not is_mutating(
                command, workdir=self.ctx.workdir, home=self.ctx.home
            ):
                return "exec:inspection"
        except Exception:  # noqa: BLE001 - a helper failure only costs the safe verdict
            return None
        return None

    def _raw_scan(self, command: str) -> None:
        """Checks on the raw text, whatever the parse makes of it."""
        lowered = command.lower()
        if _SNOWPEA_HOME_TEXT.search(command):
            self.hits.never_("exec:snowpea-home")
        if "delegation.db" in lowered or "ledger.jsonl" in lowered:
            self.hits.never_("exec:jarvis-store")
        if _SECRET_VARIABLE.search(command):
            self.hits.guard(SECRET, "exec:secret-variable")

    def line(self, command: str, cwd: Path | None, depth: int) -> None:
        if depth > 4:
            self.hits.guard_all("exec:opaque:nesting")
            return
        try:
            parsed = parse_line(command)
        except _Opaque as exc:
            self.hits.guard_all(f"exec:opaque:{exc}")
            return
        if parsed.substitution:
            self.hits.guard_all("exec:opaque:substitution")
        if parsed.function:
            self.hits.guard_all("exec:opaque:function")
        for cmd in parsed.commands:
            cwd = self.simple(cmd, cwd, depth)

    def simple(self, cmd: _Cmd, cwd: Path | None, depth: int) -> Path | None:
        """One simple command; returns the working directory after it."""
        for operator, target, unknown in cmd.redirects:
            if operator in ("<", "<&"):
                self.check_read_path(target, cwd, "redirect")
            else:
                if target not in _DEVICES:
                    self.writes = True
                self.check_write_path(target, cwd, "redirect", unknown)
        return self.words(list(cmd.words), list(cmd.dynamic), cwd, depth, piped_in=cmd.piped_in,
                          heredoc=cmd.heredoc, stdin_args=False)

    def words(  # noqa: C901 - one dispatch table, kept in one place on purpose
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
        hits = self.hits
        # Leading NAME=value assignments.
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            name = words[0].split("=", 1)[0]
            if name in _CODE_VARIABLES or name.upper() in _CODE_VARIABLES:
                hits.guard_all("exec:opaque:env-code")
            elif _SECRET_NAME.search(name):
                hits.guard(SECRET, "exec:env-secret")
            words, dynamic = words[1:], dynamic[1:]
        if not words:
            return cwd
        if dynamic[0]:
            hits.guard_all("exec:opaque:dynamic-program")
            return cwd
        head = words[0]
        program = _program(head)
        family = _family(program)
        rest, rest_dyn = words[1:], dynamic[1:]
        if any(rest_dyn) and program not in ("echo", "printf"):
            self.unknown_args = True
        if program in NETWORK_PROGRAMS:
            hits.network = True
        if stdin_args and program not in STDIN_ARGS_OK and program not in WRAPPERS:
            # ``xargs git`` / ``find -exec gh {}``: the arguments that decide what
            # it does are not in the line.
            hits.guard_all("exec:opaque:stdin-args")
            return cwd
        if "/" in head:
            # ``./deploy.sh``, ``scripts/release.py``: a script of the project's.
            self.check_read_path(head, cwd, "exec", shell=True)
            if _DEPLOY_NAME.search(program):
                hits.guard(DEPLOY, "exec:deploy-script")
        # Every argument that names a credential, wherever it sits.
        for word, dyn in zip(rest, rest_dyn, strict=False):
            if dyn:
                continue
            for candidate in _path_candidates(word):
                if _looks_like_path(candidate):
                    self.check_read_path(candidate, cwd, "exec", shell=True)
        if any(word in ("--force", "--force-with-lease", "--force-if-includes")
               or word.startswith("--force=") for word in rest):
            hits.guard(OUTSIDE, "exec:force-flag")

        # -- wrappers and runners -------------------------------------
        if program in ("cd", "pushd", "chdir"):
            target = next((w for w in rest if not w.startswith("-")), None)
            if target is None:
                return self.ctx.user_home
            index = rest.index(target)
            if rest_dyn[index] or target == "-":
                return None
            return _resolve(target, cwd, self.ctx)
        if program in ("popd",):
            return None
        if program in ("eval", "alias", "trap", "unalias", "enable", "complete",
                       "function", "declare", "typeset", "readonly", "let", "coproc"):
            if program in ("declare", "typeset", "readonly") and not any(
                w.startswith("-f") or w.startswith("-") and "f" in w for w in rest
            ):
                if any(w in ("-x", "-p") or w == "-px" for w in rest) and not any(
                    "=" in w for w in rest
                ):
                    hits.guard(SECRET, "exec:env-dump")
                return cwd
            hits.guard_all(f"exec:opaque:{program}")
            return cwd
        if program in ("source", "."):
            hits.guard_all("exec:opaque:source")
            return cwd
        if program in ("export", "set", "unset", "local", "shopt", "umask", "ulimit"):
            if program in ("export", "set") and not rest:
                hits.guard(SECRET, "exec:env-dump")
            if program == "export" and rest == ["-p"]:
                hits.guard(SECRET, "exec:env-dump")
            for word in rest:
                name = word.split("=", 1)[0]
                if name in _CODE_VARIABLES:
                    hits.guard_all("exec:opaque:env-code")
            return cwd
        if program in ("true", "false", ":", "test", "[", "[[", "echo", "printf", "sleep",
                       "wait", "read", "exit", "return", "break", "continue", "shift",
                       "pwd", "type", "hash", "jobs", "fg", "bg", "disown", "times"):
            if program in ("echo", "printf") and any(
                re.search(r"\\(x[0-9a-fA-F]|[0-7]{3}|u[0-9a-fA-F])", w) for w in rest
            ):
                hits.risky.append("exec:escaped-text")
            return cwd
        if program in PRIVILEGE:
            hits.guard(OUTSIDE, f"exec:{program}")
            inner = _skip_options(rest, PRIVILEGE[program])
            if inner is None:
                return cwd
            if program == "chroot" and inner:
                inner = inner[1:]
            self._recurse_words(rest, rest_dyn, inner, cwd, depth, piped_in, heredoc, stdin_args)
            return cwd
        if program == "env":
            return self._env(rest, rest_dyn, cwd, depth, piped_in, heredoc, stdin_args)
        if program in WRAPPERS:
            if program == "command" and rest[:1] and rest[0] in ("-v", "-V"):
                return cwd
            inner = _skip_options(rest, WRAPPERS[program])
            if inner and program in ("timeout",):
                inner = inner[1:]
            if inner:
                self._recurse_words(rest, rest_dyn, inner, cwd, depth, piped_in, heredoc,
                                    stdin_args)
            return cwd
        if program == "timeout":
            inner = _skip_options(rest, frozenset({"-s", "--signal", "-k", "--kill-after"}))
            if inner:
                self._recurse_words(rest, rest_dyn, inner[1:], cwd, depth, piped_in, heredoc,
                                    stdin_args)
            return cwd
        if program == "flock":
            inner = _skip_options(
                rest, frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"})
            )
            if inner:
                if inner[1:2] in (["-c"], ["--command"]):
                    hits.guard_all("exec:opaque:shell-string")
                    return cwd
                self._recurse_words(rest, rest_dyn, inner[1:], cwd, depth, piped_in, heredoc,
                                    stdin_args)
            return cwd
        if program == "xargs":
            inner = _skip_options(
                rest,
                frozenset({"-n", "-I", "-i", "-P", "-d", "-L", "-l", "-E", "-e", "-s", "-a",
                           "--max-args", "--replace", "--max-procs", "--delimiter",
                           "--max-lines", "--eof", "--max-chars", "--arg-file",
                           "--process-slot-var"}),
            )
            if not inner:
                inner = ["echo"]
            self._recurse_words(rest, rest_dyn, inner, cwd, depth, False, False, True)
            return cwd
        if program in SHELL_STRING_RUNNERS:
            hits.guard_all("exec:opaque:shell-string")
            return cwd
        for prefix, valued in SECRET_RUNNERS.items():
            if tuple(_program(w) for w in words[: len(prefix)]) == prefix:
                hits.guard(SECRET, "exec:secret-runner")
                inner = _skip_options(words[len(prefix) :], valued)
                if inner and inner[0] == "--":
                    inner = inner[1:]
                if inner:
                    self._recurse_words(words, dynamic, inner, cwd, depth, piped_in, heredoc,
                                        stdin_args)
                return cwd
        for prefix, valued in RUNNERS.items():
            if tuple(_program(w) for w in words[: len(prefix)]) == prefix:
                tail = words[len(prefix) :]
                if prefix in (("npx",), ("npm", "exec"), ("npm", "x")) and any(
                    w in ("-c", "--call") or w.startswith("--call=") for w in tail
                ):
                    hits.guard_all("exec:opaque:shell-string")
                    return cwd
                inner = _skip_options(tail, valued | frozenset({"-y", "--yes", "--no-install",
                                                                 "--quiet", "-q"}))
                if inner and inner[0] == "--":
                    inner = inner[1:]
                if inner:
                    self._recurse_words(words, dynamic, inner, cwd, depth, piped_in, heredoc,
                                        stdin_args)
                return cwd

        # -- shells, interpreters, encoded payloads -------------------------
        if family in SHELLS:
            return self._shell_program(family, rest, rest_dyn, cwd, depth, piped_in, heredoc)
        if family in INTERPRETERS:
            return self._interpreter(family, rest, rest_dyn, cwd, depth, piped_in, heredoc,
                                     stdin_args)
        if program in AGENT_CLIS:
            hits.guard_all("exec:opaque:agent-cli")
            return cwd
        if program in _DECODERS:
            flags = _DECODERS[program]
            if not flags or any(w in flags for w in rest):
                hits.guard_all("exec:opaque:encoded-payload")
                if program == "gpg":
                    hits.guard(SECRET, "exec:decrypt")
                return cwd
        if program in ("awk", "gawk", "mawk", "nawk"):
            script = " ".join(w for w in rest if not w.startswith("-"))
            if re.search(r"system\s*\(|\|\s*getline|\|\s*\"|print\s*>|printf[^;]*>|"
                         r"close\s*\(|@load", script):
                hits.guard_all("exec:opaque:awk-program")
            return cwd
        if program in ("sed", "gsed"):
            if any(w == "-i" or w.startswith("-i") or w.startswith("--in-place") for w in rest):
                operands = _operands(rest, rest_dyn, frozenset({"-e", "-f", "--expression",
                                                                "--file"}))
                if not any(w in ("-e", "-f", "--expression", "--file")
                           or w.startswith(("--expression=", "--file=")) for w in rest):
                    operands = operands[1:]
                for word, dyn in operands:
                    self.check_write_path(word, cwd, "exec", dyn or stdin_args)
                if stdin_args:
                    hits.guard(OUTSIDE, "exec:unknown-path")
            script = " ".join(rest)
            if re.search(r"(^|[;}\s/])e(\s|$|;)|/e\b|(^|[;\s])w\s|/w\s", script):
                hits.guard_all("exec:opaque:sed-program")
            return cwd

        if program == "snowpea":
            hits.never_("exec:snowpea-cli")
            return cwd
        if family == "git" or program == "git":
            self._git(rest, rest_dyn, cwd)
            return cwd
        if program in ("gh", "hub", "glab", "tea", "fj"):
            self._forge(program, rest)
            return cwd
        if program in ("ssh", "mosh", "autossh"):
            hits.guard(DEPLOY, "exec:ssh-remote")
            hits.network = True
            return cwd
        if program in ("scp", "rsync", "rclone", "gsutil", "sftp", "s3cmd", "azcopy"):
            self._copy_remote(program, rest, rest_dyn, cwd, stdin_args)
            return cwd
        if program in HTTP_CLIENTS:
            self._http(program, rest, rest_dyn, cwd)
            return cwd
        if program in SENDERS:
            hits.guard(SECRET, "exec:send")
            hits.network = True
            return cwd
        if program in ("openssl",):
            hits.network = True
            if rest[:1] == ["s_client"]:
                hits.guard(SECRET, "exec:send")
            return cwd
        if program in SECRET_READERS:
            subs = SECRET_READERS[program]
            first = next((w for w in rest if not w.startswith("-")), "")
            if subs is None or first in subs or any(w in subs for w in rest):
                hits.guard(SECRET, "exec:secret-reader")
            return cwd
        if program in ("env", "printenv") or (program == "export" and rest == ["-p"]):
            hits.guard(SECRET, "exec:env-dump")
            return cwd
        if program in DB_CLIENTS or family in ("rails", "rake", "prisma", "sequelize", "knex",
                                               "typeorm", "drizzle-kit", "diesel", "alembic"):
            self._database(program, words, cwd)
            if program in DB_CLIENTS:
                return cwd
        if program in ("dropdb", "dropuser"):
            hits.guard(OUTSIDE, "exec:database-destructive")
            return cwd
        if program in SYSTEM_PROGRAMS:
            reads = SYSTEM_READ_ONLY.get(program)
            if reads is not None and (
                program == "journalctl"
                or (not rest and program in ("mount", "systemctl", "launchctl", "ufw"))
                or any(w in reads for w in rest[:2])
            ):
                return cwd
            if program == "dd":
                for word in rest:
                    if word.startswith("of="):
                        self.check_write_path(word[3:], cwd, "exec", False)
                    if word.startswith("if="):
                        self.check_read_path(word[3:], cwd, "exec")
            hits.guard(OUTSIDE, "exec:system")
            return cwd
        if program == "kill":
            targets = list(rest)
            if targets[:1] in (["-s"], ["-n"], ["--signal"]):
                targets = targets[2:]
            elif targets and targets[0] != "-1" and re.match(r"^-[A-Za-z0-9]+$", targets[0]):
                targets = targets[1:]
            if targets[:1] == ["--"]:
                targets = targets[1:]
            if any(w in ("-1", "0") or w.startswith("-") and w[1:].isdigit() for w in targets):
                hits.guard(OUTSIDE, "exec:kill-group")
            return cwd
        if program in SYSTEM_INSTALLERS or family in SYSTEM_INSTALLERS:
            subs = SYSTEM_INSTALLERS.get(program) or SYSTEM_INSTALLERS.get(family, frozenset())
            first = next((w for w in rest if not w.startswith("-")), "")
            if first in subs or any(w in subs for w in rest if w.startswith("-")):
                hits.guard(OUTSIDE, "exec:system-install")
                hits.network = True
                return cwd
        if program in ("mkfs",) or program.startswith("mkfs."):
            hits.guard(OUTSIDE, "exec:system")
            return cwd
        if program in WRITE_PROGRAMS:
            self._write_program(program, rest, rest_dyn, cwd, stdin_args)
            return cwd
        if program == "find":
            self._find(rest, rest_dyn, cwd, depth)
            return cwd
        if program in ("tar", "bsdtar", "gtar"):
            self._tar(rest, rest_dyn, cwd)
            return cwd
        if program in ("unzip", "7z", "7za", "unrar", "zip", "gzip", "gunzip", "bzip2",
                       "xz", "zstd"):
            self._archive(program, rest, rest_dyn, cwd)
            return cwd
        if family in ("npm", "pnpm", "yarn", "bun"):
            if family == "bun" and any(
                w in ("-e", "--eval", "-p", "--print") or w.startswith(("--eval=", "--print="))
                for w in rest
            ):
                hits.guard_all("exec:opaque:inline-code")
                return cwd
            self._node_pm(family, rest, cwd)
            return cwd
        if program == "deno":
            first = next((w for w in rest if not w.startswith("-")), "")
            if first in ("eval", "repl", "") or any(w in ("-A", "--allow-all", "--allow-run")
                                                    for w in rest):
                hits.guard_all("exec:opaque:inline-code" if first in ("eval", "repl", "")
                               else "exec:opaque:allow-all")
            if first in ("deploy", "publish"):
                hits.guard(DEPLOY, "exec:deno-publish")
            if first == "install" and any(w in ("-g", "--global") for w in rest):
                hits.guard(OUTSIDE, "exec:system-install")
            return cwd
        if family in ("pip",) or (program == "uv" and rest[:1] == ["pip"]):
            tail = rest[1:] if program == "uv" else rest
            if tail[:1] in (["install"], ["uninstall"]) and any(
                w in ("--user", "--break-system-packages", "--system", "--target", "-t",
                      "--prefix", "--root") or w.startswith(("--target=", "--prefix=",
                                                             "--root="))
                for w in tail
            ):
                hits.guard(OUTSIDE, "exec:system-install")
            hits.network = True
            return cwd
        if family in ("docker", "podman", "nerdctl", "finch"):
            self._container(family, rest, rest_dyn, cwd)
            return cwd
        if program in ("make", "gmake", "just", "task", "rake", "invoke", "fab", "mage",
                       "ninja", "cake", "nx", "turbo", "lerna", "moon", "earthly", "bazel",
                       "bazelisk", "buck2", "pants", "gradle", "gradlew", "mvn", "mvnw", "sbt",
                       "ant", "grunt", "gulp", "dotnet", "msbuild", "xcodebuild", "mix",
                       "cargo-make", "invoke", "nox", "tox", "doit", "scons", "meson") or (
            family in ("gradle", "mvn")
        ):
            self._build_tool(program, family, rest, words)
            return cwd
        if family in _PUBLISH_SUBCOMMANDS or program in _PUBLISH_SUBCOMMANDS:
            subs = _PUBLISH_SUBCOMMANDS.get(program) or _PUBLISH_SUBCOMMANDS.get(
                family, frozenset()
            )
            first = next((w for w in rest if not w.startswith("-")), "")
            if first in subs or (program == "pod" and rest[:2] == ["trunk", "push"]):
                if program in ("dart", "flutter") and rest[1:2] != ["publish"]:
                    pass
                else:
                    hits.guard(DEPLOY, f"exec:{family}-publish")
            login = _LOGIN_SUBCOMMANDS.get(family, frozenset())
            if first in login:
                hits.guard(SECRET, "exec:registry-login")
            if family == "cargo" and first in ("install", "uninstall"):
                hits.guard(OUTSIDE, "exec:system-install")
            if family in ("cargo", "uv", "poetry", "pdm", "hatch", "gem", "dotnet", "go"):
                hits.network = hits.network or first in ("install", "add", "update", "sync",
                                                         "publish", "get", "fetch")
            return cwd
        if program in DEPLOY_PROGRAMS:
            hits.guard(DEPLOY, f"exec:{program}")
            hits.network = True
            return cwd
        if program in CLOUD_READ_ONLY:
            self._cloud(program, rest)
            return cwd
        if program in ("vercel", "netlify", "firebase", "fly", "flyctl", "heroku", "wrangler",
                       "eas", "expo", "pulumi", "cdk", "sam", "serverless", "sls"):
            self._cloud(program, rest)
            return cwd
        if program in ("ansible",) and any(w in ("-m", "--module-name") for w in rest):
            hits.guard(DEPLOY, "exec:ansible")
            return cwd
        if program in ("open", "xdg-open", "start", "explorer"):
            hits.network = True
            return cwd
        if _DEPLOY_NAME.search(program) and program not in ("git",):
            hits.guard(DEPLOY, "exec:deploy-script")
        if self._runs_commands(words):
            hits.guard_all("exec:opaque:runs-commands")
        return cwd

    # -- helpers for the dispatch ----------------------------------------
    def _recurse_words(
        self,
        words: list[str],
        dynamic: list[bool],
        inner: list[str],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
        stdin_args: bool,
    ) -> None:
        """Judge ``inner`` (a tail of ``words``) as a command of its own."""
        offset = len(words) - len(inner)
        inner_dyn = dynamic[offset:] if 0 <= offset <= len(dynamic) else [False] * len(inner)
        if depth >= 6:
            self.hits.guard_all("exec:opaque:nesting")
            return
        self.words(list(inner), list(inner_dyn), cwd, depth + 1, piped_in=piped_in,
                   heredoc=heredoc, stdin_args=stdin_args)

    def _env(
        self,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
        stdin_args: bool,
    ) -> Path | None:
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in ("-S", "--split-string") or word.startswith(("-S", "--split-string=")):
                self.hits.guard_all("exec:opaque:shell-string")
                return cwd
            if word in ("-u", "--unset", "-C", "--chdir"):
                index += 2
                continue
            if word.startswith("-") and word != "-":
                index += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word) or word == "-":
                name = word.split("=", 1)[0]
                if name in _CODE_VARIABLES:
                    self.hits.guard_all("exec:opaque:env-code")
                elif _SECRET_NAME.search(name):
                    self.hits.guard(SECRET, "exec:env-secret")
                index += 1
                continue
            break
        inner = rest[index:]
        if not inner:
            self.hits.guard(SECRET, "exec:env-dump")
            return cwd
        self._recurse_words(rest, rest_dyn, inner, cwd, depth, piped_in, heredoc, stdin_args)
        return cwd

    def _shell_program(
        self,
        family: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
    ) -> Path | None:
        hits = self.hits
        if family == "busybox":
            if rest:
                self._recurse_words(rest, rest_dyn, rest, cwd, depth, piped_in, heredoc, False)
            return cwd
        options = _leading_options(rest)
        for word in options if family != "cmd" else rest:
            lower = word.lower()
            if family == "cmd":
                inline = lower in ("/c", "/k", "/r")
            elif family in ("pwsh", "powershell"):
                inline = lower in ("-c", "-e", "-ec", "-enc", "-cwa", "-command",
                                   "-encodedcommand", "-commandwithargs", "-file", "-f") or (
                    lower.startswith(("-command", "-encodedcommand", "-encoded"))
                )
            else:
                inline = word in ("-c", "--command") or (
                    word.startswith("-") and not word.startswith("--") and "c" in word[1:]
                )
            if inline:
                hits.guard_all("exec:opaque:shell-c")
                return cwd
        script = rest[len(options)] if len(rest) > len(options) else None
        if (
            script is None
            or piped_in
            or heredoc
            or any(w in ("-s", "-i", "--stdin") for w in options)
            or family in ("pwsh", "powershell", "cmd")
        ):
            hits.guard_all("exec:opaque:shell-stdin")
            return cwd
        self.check_read_path(script, cwd, "exec")
        if _DEPLOY_NAME.search(_program(script)):
            hits.guard(DEPLOY, "exec:deploy-script")
        return cwd

    def _interpreter(
        self,
        family: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        depth: int,
        piped_in: bool,
        heredoc: bool,
        stdin_args: bool,
    ) -> Path | None:
        hits = self.hits
        inline = INTERPRETERS[family]
        valued = {
            "python": frozenset({"-m", "-W", "-X", "-Q"}),
            "pypy": frozenset({"-m", "-W", "-X"}),
            "node": frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader",
                               "--env-file", "--conditions", "-C", "--inspect-port"}),
            "nodejs": frozenset({"-r", "--require", "--import", "--loader"}),
            "ruby": frozenset({"-I", "-r", "-C", "-E", "-K", "-T"}),
            "perl": frozenset({"-I", "-M", "-m", "-x"}),
        }.get(family, frozenset())
        options = _leading_options(rest, valued=valued)
        short = [flag for flag in inline if len(flag) == 2]
        for word in options:
            if not word.startswith("-"):
                continue  # the value of an option before it
            if (
                word in inline
                or (not word.startswith("--") and any(word.startswith(flag) for flag in short))
                or (not word.startswith("--") and len(word) > 2
                    and any(flag[1] in word[1:] for flag in short))
                or any(word.startswith(flag + "=") for flag in inline if flag.startswith("--"))
            ):
                hits.guard_all("exec:opaque:inline-code")
                return cwd
        if family in ("node", "nodejs") and any(
            w in ("-r", "--require", "--import", "--loader") for w in options
        ):
            hits.guard_all("exec:opaque:preload")
            return cwd
        if family in ("python", "pypy") and "-m" in options:
            index = rest.index("-m")
            module = rest[index + 1 : index + 2]
            if not module:
                hits.guard_all("exec:opaque:interpreter-stdin")
                return cwd
            if module[0] == "pip":
                self.words(["pip", *rest[index + 2 :]], [False, *rest_dyn[index + 2 :]],
                           cwd, depth + 1, piped_in=piped_in, heredoc=heredoc,
                           stdin_args=stdin_args)
                return cwd
            self._recurse_words(rest, rest_dyn, rest[index + 1 :], cwd, depth, piped_in,
                                heredoc, stdin_args)
            return cwd
        script = rest[len(options)] if len(rest) > len(options) else None
        if script is None or script == "-" or piped_in or heredoc:
            hits.guard_all("exec:opaque:interpreter-stdin")
            return cwd
        self.check_read_path(script, cwd, "exec")
        if _DEPLOY_NAME.search(_program(script)):
            hits.guard(DEPLOY, "exec:deploy-script")
        if _DB_RESET.search(" ".join(rest)):
            hits.guard(OUTSIDE, "exec:database-destructive")
        return cwd

    def _runs_commands(self, words: list[str]) -> bool:
        try:
            from snowpea_core.permissions.shell_remember import runs_commands

            return runs_commands(words)
        except Exception:  # noqa: BLE001
            return True

    def _git(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        hits = self.hits
        index = 0
        sub: str | None = None
        while index < len(rest):
            word = rest[index]
            if word in ("-c", "--config-env"):
                value = rest[index + 1] if index + 1 < len(rest) else ""
                if word == "--config-env" or not _GIT_HARMLESS_CONFIG.match(value):
                    hits.guard_all("exec:opaque:git-config")
                index += 2
                continue
            if word.startswith("-c") and len(word) > 2:
                if not _GIT_HARMLESS_CONFIG.match(word[2:]):
                    hits.guard_all("exec:opaque:git-config")
                index += 1
                continue
            if word.startswith(("--exec-path", "--upload-pack", "--receive-pack")):
                hits.guard_all("exec:opaque:git-exec")
                index += 1
                continue
            if word in _GIT_VALUED:
                index += 2
                continue
            if word.startswith("-"):
                index += 1
                continue
            sub = word
            break
        if sub is None:
            return
        if rest_dyn[index] if index < len(rest_dyn) else False:
            hits.guard_all("exec:opaque:git-alias")
            return
        args = rest[index + 1 :]
        if sub not in GIT_SUBCOMMANDS:
            hits.guard_all("exec:opaque:git-alias")
            return
        if sub in ("push", "send-pack"):
            hits.guard(DEPLOY, "exec:git-push")
            hits.network = True
            if any(
                w in ("-f", "--force", "--mirror", "--delete", "-d", "--prune")
                or w.startswith(("--force", "+"))
                or (w.startswith(":") and len(w) > 1)
                or (w.startswith("-") and not w.startswith("--") and "f" in w[1:])
                for w in args
            ):
                hits.guard(OUTSIDE, "exec:git-force-push")
        elif sub in ("send-email", "imap-send", "request-pull"):
            hits.guard(SECRET, "exec:git-send")
            hits.network = True
        elif sub == "credential":
            hits.guard(SECRET, "exec:git-credential")
        elif sub == "reset" and any(w in ("--hard", "--merge", "--keep") for w in args):
            hits.guard(OUTSIDE, "exec:git-reset-hard")
        elif sub == "clean" and not any(w in ("-n", "--dry-run") for w in args):
            hits.guard(OUTSIDE, "exec:git-clean")
        elif sub in ("checkout", "switch") and any(
            w in ("-f", "--force", "--", ".", "--discard-changes") or w.startswith("--force")
            for w in args
        ):
            hits.guard(OUTSIDE, "exec:git-discard")
        elif sub == "restore" and not (
            "--staged" in args and "--worktree" not in args and "-W" not in args
        ):
            hits.guard(OUTSIDE, "exec:git-discard")
        elif sub == "branch" and any(
            w in ("-D", "--force", "-f") or (w.startswith("-") and not w.startswith("--")
                                             and "D" in w)
            for w in args
        ):
            hits.guard(OUTSIDE, "exec:git-branch-delete")
        elif sub == "stash" and args[:1] in (["drop"], ["clear"]):
            hits.guard(OUTSIDE, "exec:git-stash-drop")
        elif sub in ("filter-branch", "filter-repo", "replace"):
            hits.guard(OUTSIDE, "exec:git-rewrite")
            if sub == "filter-branch":
                hits.guard_all("exec:opaque:runs-commands")
        elif sub == "reflog" and args[:1] in (["expire"], ["delete"]):
            hits.guard(OUTSIDE, "exec:git-rewrite")
        elif sub == "gc" and any(w.startswith("--prune") for w in args):
            hits.guard(OUTSIDE, "exec:git-rewrite")
        elif sub == "update-ref" and "-d" in args:
            hits.guard(OUTSIDE, "exec:git-rewrite")
        elif sub == "worktree" and args[:1] == ["remove"] and any(
            w in ("-f", "--force") for w in args
        ):
            hits.guard(OUTSIDE, "exec:git-discard")
        elif sub == "config":
            if any(w in ("--global", "--system") or w.startswith("--file") or w == "-f"
                   for w in args):
                hits.guard(OUTSIDE, "exec:git-config-global")
            if any(w.lower().startswith(("alias.", "core.hookspath", "core.sshcommand",
                                         "core.pager", "core.editor", "core.fsmonitor",
                                         "credential.", "url.", "include.", "includeif."))
                   or ".command" in w.lower() or ".cmd" in w.lower()
                   for w in args):
                hits.guard_all("exec:opaque:git-config")
        elif sub in ("submodule", "bisect") and any(w in ("foreach", "run") for w in args):
            hits.guard_all("exec:opaque:runs-commands")
        elif sub in ("clone", "fetch", "pull", "ls-remote", "remote", "lfs", "archive"):
            hits.network = True
            positional = [w for w in args if not w.startswith("-")]
            if sub == "clone" and len(positional) >= 2:
                self.check_write_path(positional[1], cwd, "exec", False)
        elif sub == "tag" and any(w in ("-d", "--delete") for w in args):
            hits.risky.append("exec:git-tag-delete")
        # Arguments that make git run a program of their own (``--upload-pack``,
        # ``rebase -x``, ``grep -O``); the subcommands that do were judged above.
        try:
            from snowpea_core.permissions.shell_remember import (
                EXEC_FLAGS,
                EXEC_FLAGS_BY_SUBCOMMAND,
            )

            flags = (EXEC_FLAGS.get("git", frozenset()) - {"-c"}) | EXEC_FLAGS_BY_SUBCOMMAND.get(
                ("git", sub), frozenset()
            )
        except Exception:  # noqa: BLE001
            flags = frozenset({"--upload-pack", "--receive-pack", "--exec", "-x", "-O"})
        if any(_flag_hit(word, flag) for word in args for flag in flags):
            hits.guard_all("exec:opaque:runs-commands")

    def _forge(self, program: str, rest: list[str]) -> None:
        hits = self.hits
        hits.network = True
        words = [w for w in rest if not w.startswith("-")]
        first = words[0] if words else ""
        second = words[1] if len(words) > 1 else ""
        if program == "hub" and first in GIT_SUBCOMMANDS:
            self._git(rest, [False] * len(rest), self.ctx.workdir)
            return
        if first == "release" and second not in ("list", "view", "download", "verify",
                                                  "verify-asset", ""):
            hits.guard(DEPLOY, f"exec:{program}-release")
            if second == "delete" or second == "delete-asset":
                hits.guard(OUTSIDE, f"exec:{program}-delete")
        elif first in ("pr", "mr") and second == "merge":
            hits.guard(DEPLOY, f"exec:{program}-merge")
        elif first == "repo" and second in ("create", "delete", "rename", "archive", "edit",
                                            "fork", "sync", "deploy-key"):
            hits.guard(DEPLOY, f"exec:{program}-repo")
            if second in ("delete", "archive"):
                hits.guard(OUTSIDE, f"exec:{program}-delete")
        elif first in ("workflow", "run") and second in ("run", "enable", "disable", "rerun",
                                                         "cancel", "delete"):
            hits.guard(DEPLOY, f"exec:{program}-workflow")
        elif first in ("secret", "variable", "ssh-key", "gpg-key") and second in (
            "set", "delete", "remove", "add", "list"
        ):
            hits.guard(SECRET, f"exec:{program}-secret")
        elif first == "auth" and (second in ("token", "login", "refresh", "setup-git")
                                  or "--show-token" in rest or "-t" in rest):
            hits.guard(SECRET, f"exec:{program}-auth")
        elif first == "api":
            mutating = any(
                w in ("-f", "-F", "--field", "--raw-field", "--input")
                or w.startswith(("--field=", "--raw-field=", "--input="))
                for w in rest
            )
            for flag in ("-X", "--method"):
                if flag in rest:
                    idx = rest.index(flag)
                    method = rest[idx + 1] if idx + 1 < len(rest) else ""
                    mutating = mutating or method.upper() not in ("GET", "HEAD")
            mutating = mutating or any(
                re.match(r"^(-X(?!GET$|HEAD$).+|--method=(?!GET$|HEAD$).*)$", w, re.IGNORECASE)
                for w in rest
            )
            if mutating:
                hits.guard(SECRET, f"exec:{program}-api-write")
        elif first in (
            "pr", "mr", "issue", "gist", "discussion", "comment", "release"
        ) and second in (
            "create", "comment", "edit", "close", "reopen", "review", "delete", "lock",
            "unlock", "transfer", "pin", "develop", "ready", "note"
        ):
            hits.guard(SECRET, f"exec:{program}-post")
            if second == "delete":
                hits.guard(OUTSIDE, f"exec:{program}-delete")
        elif first == "gist" and second in ("", "create", "new"):
            hits.guard(SECRET, f"exec:{program}-post")
        elif first in ("extension", "ext") and second in ("install", "exec", "upgrade"):
            hits.guard_all("exec:opaque:runs-commands")
        elif first == "alias":
            hits.guard_all("exec:opaque:alias")
        elif first == "codespace" and second in ("ssh", "cp", "create", "delete"):
            hits.guard(DEPLOY, f"exec:{program}-codespace")
        elif first in ("label", "milestone", "project", "cache", "ruleset") and second in (
            "create", "delete", "edit", "clone", "close"
        ):
            hits.guard(SECRET, f"exec:{program}-post")
        elif first == "" or first in ("pr", "issue", "repo", "release", "run", "workflow",
                                      "search", "status", "browse", "api", "gist", "auth",
                                      "label", "cache", "codespace", "org", "project",
                                      "ruleset", "attestation", "help", "version", "mr",
                                      "ci", "config", "completion"):
            if first == "config" and second == "set":
                hits.risky.append(f"exec:{program}-config")
        else:
            hits.guard_all(f"exec:opaque:{program}-alias")

    def _copy_remote(
        self,
        program: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        stdin_args: bool,
    ) -> None:
        hits = self.hits
        valued = {
            "scp": frozenset({"-i", "-P", "-o", "-F", "-c", "-l", "-S", "-J"}),
            "rsync": frozenset({"-e", "--rsh", "--exclude", "--include", "--filter", "-f",
                                "--port", "--password-file", "--log-file", "--rsync-path",
                                "--exclude-from", "--include-from", "--files-from",
                                "--chmod", "--chown", "--backup-dir", "--suffix", "-T",
                                "--temp-dir", "--partial-dir", "--compare-dest",
                                "--link-dest", "--copy-dest", "--max-size", "--min-size",
                                "--bwlimit", "--timeout"}),
        }.get(program, frozenset())
        operands = _operands(rest, rest_dyn, valued)
        remote = [w for w, _ in operands if re.match(r"^([\w.@-]+@)?[\w.-]+:", w)
                  and not w.startswith(("/", ".", "~"))]
        if program in ("rclone",):
            remote = [w for w, _ in operands if re.match(r"^[\w.-]+:", w)]
            sub = operands[0][0] if operands else ""
            if sub in CLOUD_READ_ONLY["rclone"]:
                return
            if sub in ("delete", "deletefile", "purge", "rmdir", "rmdirs", "cleanup", "dedupe"):
                hits.guard(OUTSIDE, "exec:rclone-delete")
            if sub in ("sync", "move", "moveto") or "--delete" in " ".join(rest):
                hits.guard(OUTSIDE, "exec:sync-delete")
            operands = operands[1:]
        if program == "gsutil":
            sub = operands[0][0] if operands else ""
            if sub in CLOUD_READ_ONLY["gsutil"]:
                return
            remote = [w for w, _ in operands if w.startswith("gs://")]
            if sub in ("rm", "rb"):
                hits.guard(OUTSIDE, "exec:gsutil-delete")
            operands = operands[1:]
        if program in ("sftp", "s3cmd", "azcopy"):
            remote = remote or ["remote"]
        hits.network = hits.network or bool(remote)
        if remote:
            hits.guard(DEPLOY, f"exec:{program}-remote")
            hits.guard(SECRET, f"exec:{program}-upload")
        if program == "rsync" and any(w.startswith("--delete") or w == "--remove-source-files"
                                      for w in rest):
            hits.guard(OUTSIDE, "exec:sync-delete")
        local = [(w, d) for w, d in operands if w not in remote]
        if local and not remote:
            *sources, dest = local
            for word, _ in sources:
                self.check_read_path(word, cwd, "exec")
            self.check_write_path(dest[0], cwd, "exec", dest[1] or stdin_args)
        elif local:
            # Local operands of a transfer: the ones read are sent; one written is checked.
            last_is_local = operands and operands[-1][0] not in remote
            for position, (word, dyn) in enumerate(local):
                if last_is_local and position == len(local) - 1:
                    self.check_write_path(word, cwd, "exec", dyn or stdin_args)
                else:
                    self.check_read_path(word, cwd, "exec")

    def _http(self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        hits = self.hits
        hits.network = True
        send = False
        index = 0
        while index < len(rest):
            word = rest[index]
            value = rest[index + 1] if index + 1 < len(rest) else ""
            lower = word.lower()
            if program in ("curl", "curlie"):
                if word in ("-X", "--request"):
                    send = send or value.upper() not in ("GET", "HEAD", "OPTIONS")
                    index += 2
                    continue
                if word.startswith("-X") and len(word) > 2:
                    send = send or word[2:].upper() not in ("GET", "HEAD", "OPTIONS")
                elif word.startswith("--request="):
                    send = send or word.split("=", 1)[1].upper() not in ("GET", "HEAD")
                elif word.startswith(("--data", "--form", "--json", "--upload-file",
                                      "--post301", "--post302", "--post303", "--mail-rcpt",
                                      "--mail-from", "--ftp-create-dirs")):
                    send = True
                elif word in ("-d", "-F", "-T", "--url-query") or (
                    word.startswith("-") and not word.startswith("--")
                    and any(flag in word[1:] for flag in "dFT")
                    and not word.startswith(("-H", "-o", "-A", "-e", "-w", "-u", "-x", "-K",
                                             "-b", "-c", "-E", "-r", "-m", "-y", "-Y", "-z"))
                ):
                    send = True
                elif word in ("-u", "--user", "--oauth2-bearer", "-E", "--cert", "--key",
                              "--netrc-file", "--proxy-user", "--aws-sigv4") or word.startswith(
                    ("--user=", "--oauth2-bearer=", "--cert=", "--key=")
                ) or word == "-n" or word == "--netrc":
                    hits.guard(SECRET, "exec:http-credential")
                elif word in ("-H", "--header") and re.search(
                    r"^\s*(authorization|cookie|x-api-key|api-key|x-auth-token|private-token|"
                    r"x-access-token|proxy-authorization)\s*:",
                    value,
                    re.IGNORECASE,
                ):
                    hits.guard(SECRET, "exec:http-credential")
                elif word in ("-K", "--config") or word.startswith("--config="):
                    self.hits.guard_all("exec:opaque:curl-config")
                elif word in ("-o", "--output", "--output-dir", "-D", "--dump-header",
                              "--trace", "--trace-ascii", "-c", "--cookie-jar", "--stderr"):
                    self.check_write_path(value, cwd, "exec", rest_dyn[index + 1]
                                          if index + 1 < len(rest_dyn) else True)
                    index += 2
                    continue
                if word in ("-d", "-F", "-T", "-H", "-u", "-A", "-e", "-b", "-x", "--data",
                            "--data-binary", "--data-raw", "--data-urlencode", "--form",
                            "--json", "--header", "--user", "--upload-file", "--user-agent",
                            "--cookie", "--proxy", "-w", "--write-out", "--url", "-m",
                            "--max-time", "--connect-timeout", "--retry", "--cacert",
                            "--resolve", "-E", "--cert", "--key", "--oauth2-bearer"):
                    if value.startswith("@"):
                        self.check_read_path(value[1:], cwd, "exec")
                        if word in ("-d", "-F", "--data", "--data-binary", "--form", "-T",
                                    "--json"):
                            send = True
                    if word in ("-T", "--upload-file"):
                        self.check_read_path(value, cwd, "exec")
                    index += 2
                    continue
            elif program in ("wget", "wget2"):
                if lower.startswith(("--post-data", "--post-file", "--body-data", "--body-file",
                                     "--method")):
                    method = word.split("=", 1)[1] if "=" in word else value
                    if not lower.startswith("--method") or method.upper() not in ("GET", "HEAD"):
                        send = True
                elif lower.startswith(("--password", "--http-password", "--ftp-password",
                                       "--header=authorization", "--user", "--http-user")):
                    hits.guard(SECRET, "exec:http-credential")
                elif word in ("-O", "--output-document", "-P", "--directory-prefix", "-o",
                              "--output-file", "-a", "--append-output"):
                    self.check_write_path(value, cwd, "exec", False)
                    index += 2
                    continue
                elif lower.startswith(("--output-document=", "--directory-prefix=")):
                    self.check_write_path(word.split("=", 1)[1], cwd, "exec", False)
            else:  # httpie and friends: METHOD url field=value ...
                if word.upper() in ("POST", "PUT", "PATCH", "DELETE") and index == 0:
                    send = True
                elif not word.startswith("-") and index > 0 and re.match(
                    r"^[^=:@/]+(==|=|:=|@|=@|:=@)", word
                ) and "://" not in word:
                    if "==" not in word or "=" in word.replace("==", ""):
                        send = send or ("=" in word.replace("==", "") or "@" in word)
                elif word in ("-a", "--auth", "-A", "--auth-type") or word.startswith("--auth"):
                    hits.guard(SECRET, "exec:http-credential")
                elif re.match(r"^(authorization|cookie|x-api-key|api-key|x-auth-token|"
                              r"private-token|x-access-token):", word, re.IGNORECASE):
                    hits.guard(SECRET, "exec:http-credential")
                elif word in ("-f", "--form", "--multipart", "--raw") or word.startswith("--raw"):
                    send = True
                elif word in ("-o", "--output", "-d", "--download"):
                    if word in ("-o", "--output"):
                        self.check_write_path(value, cwd, "exec", False)
                        index += 2
                        continue
            index += 1
        if send:
            hits.guard(SECRET, "exec:http-send")

    def _database(self, program: str, words: list[str], cwd: Path | None) -> None:
        text = " ".join(words)
        if program in DB_CLIENTS and _SQL_DESTRUCTIVE.search(text):
            self.hits.guard(OUTSIDE, "exec:database-destructive")
        if _DB_RESET.search(text):
            self.hits.guard(OUTSIDE, "exec:database-destructive")
        if program in DB_CLIENTS:
            self.hits.network = True

    def _write_program(
        self,
        program: str,
        rest: list[str],
        rest_dyn: list[bool],
        cwd: Path | None,
        stdin_args: bool,
    ) -> None:
        hits = self.hits
        if program in ("rm", "srm", "shred") and any(
            w in ("-r", "-R", "--recursive")
            or (w.startswith("-") and not w.startswith("--") and ("r" in w or "R" in w))
            for w in rest
        ):
            hits.guard(OUTSIDE, f"exec:{program}-recursive")
        if program in ("shred", "srm"):
            hits.guard(OUTSIDE, "exec:shred")
        if program in ("chmod", "chown", "chgrp") and any(
            w in ("-R", "--recursive") or (w.startswith("-") and "R" in w) for w in rest
        ):
            hits.risky.append(f"exec:{program}-recursive")
        if program == "gio" and rest[:1] not in (["trash"], ["remove"], ["rename"], ["move"],
                                                 ["copy"], ["mkdir"], ["set"]):
            return
        if program == "mktemp":
            return
        valued: frozenset[str] = frozenset()
        if program != "tee":
            valued = frozenset({"-t", "--target-directory", "-S", "--suffix", "-m", "--mode",
                                "-o", "--owner", "-g", "--group", "--reference", "-b", "-e",
                                "--exclude", "--backup", "--context"})
        if program in ("chmod", "chown", "chgrp"):
            operands = _operands(rest, rest_dyn, valued)[1:]
        elif program == "patch":
            operands = [(w, d) for w, d in _operands(rest, rest_dyn, frozenset({"-p", "-i",
                                                                                   "-o", "-d",
                                                                                   "-B", "-r"}))]
            for flag in ("-o", "-d", "-i"):
                if flag in rest:
                    idx = rest.index(flag)
                    if idx + 1 < len(rest):
                        if flag == "-i":
                            self.check_read_path(rest[idx + 1], cwd, "exec")
                        else:
                            self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        else:
            operands = _operands(rest, rest_dyn, valued)
        for flag in ("-t", "--target-directory"):
            if flag in rest:
                idx = rest.index(flag)
                if idx + 1 < len(rest):
                    self.check_write_path(rest[idx + 1], cwd, "exec",
                                          rest_dyn[idx + 1] or stdin_args)
        if stdin_args:
            hits.guard(OUTSIDE, "exec:unknown-path")
        if not operands:
            return
        if program in DESTINATION_ONLY and len(operands) > 1:
            *sources, dest = operands
            for word, dynamic_source in sources:
                if not dynamic_source:
                    self.check_read_path(word, cwd, "exec")
            if program == "ln":
                # The link is created at the destination; the target is only named.
                self.check_write_path(dest[0], cwd, "exec", dest[1])
            else:
                self.check_write_path(dest[0], cwd, "exec", dest[1])
            return
        for word, dyn in operands:
            self.check_write_path(word, cwd, "exec", dyn)

    def _find(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None, depth: int) -> None:
        hits = self.hits
        roots: list[tuple[str, bool]] = []
        for word, dyn in zip(rest, rest_dyn, strict=False):
            if word.startswith(("-", "(", "!", ")")):
                break
            roots.append((word, dyn))
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in ("-delete",):
                hits.guard(OUTSIDE, "exec:find-delete")
                for root, dyn in roots or [(".", False)]:
                    self.check_write_path(root, cwd, "exec", dyn)
            elif word in ("-exec", "-execdir", "-ok", "-okdir"):
                end = index + 1
                while end < len(rest) and rest[end] not in (";", "+", "\\;"):
                    end += 1
                inner = rest[index + 1 : end]
                inner_dyn = rest_dyn[index + 1 : end]
                if inner:
                    self.words(list(inner), list(inner_dyn), cwd, depth + 1, piped_in=False,
                               heredoc=False, stdin_args=True)
                index = end
            elif word in ("-fprint", "-fprint0", "-fprintf", "-fls"):
                if index + 1 < len(rest):
                    self.check_write_path(rest[index + 1], cwd, "exec", rest_dyn[index + 1])
            index += 1

    def _tar(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        joined = " ".join(rest)
        if self._runs_commands(["tar", *rest]):
            self.hits.guard_all("exec:opaque:runs-commands")
        extract = bool(re.search(r"(^|\s)(-[A-Za-z]*x|x[A-Za-z]*\s|--extract|--get)", joined))
        create = bool(re.search(r"(^|\s)(-[A-Za-z]*[cru]|[cru][A-Za-z]*\s|--create|--append|"
                                r"--update)", joined))
        for flag in ("-C", "--directory"):
            if flag in rest and extract:
                idx = rest.index(flag)
                if idx + 1 < len(rest):
                    self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        for idx, word in enumerate(rest):
            if (word in ("-f", "--file") or (re.match(r"^-[A-Za-z]*f$", word))) and idx + 1 < len(
                rest
            ):
                if create:
                    self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
                else:
                    self.check_read_path(rest[idx + 1], cwd, "exec")
        if extract and any(w in ("-P", "--absolute-names") for w in rest):
            self.hits.guard(OUTSIDE, "exec:tar-absolute")

    def _archive(
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        if program == "unzip" and "-d" in rest:
            idx = rest.index("-d")
            if idx + 1 < len(rest):
                self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        if program in ("7z", "7za"):
            for word in rest:
                if word.startswith("-o") and len(word) > 2:
                    self.check_write_path(word[2:], cwd, "exec", False)
        if self._runs_commands([program, *rest]):
            self.hits.guard_all("exec:opaque:runs-commands")

    def _node_pm(self, family: str, rest: list[str], cwd: Path | None) -> None:
        hits = self.hits
        positional = [w for w in rest if not w.startswith("-")]
        first = positional[0] if positional else ""
        if family == "yarn" and first == "global":
            hits.guard(OUTSIDE, "exec:system-install")
            return
        if first in _PUBLISH_SUBCOMMANDS.get(family, frozenset()) and not (
            family == "yarn" and first == "npm" and positional[1:2] != ["publish"]
        ):
            hits.guard(DEPLOY, f"exec:{family}-publish")
        if first in _LOGIN_SUBCOMMANDS.get(family, frozenset()):
            hits.guard(SECRET, "exec:registry-login")
        if first in ("install", "i", "add", "update", "upgrade", "link", "remove", "uninstall",
                     "rm", "un", "unlink", "ci", "up") and any(
            w in ("-g", "--global", "--location=global") for w in rest
        ):
            hits.guard(OUTSIDE, "exec:system-install")
        if first in ("install", "i", "add", "ci", "update", "upgrade", "up", "outdated", "view",
                     "info", "search", "audit", "publish", "create", "init", "dlx"):
            hits.network = True
        script: str | None = None
        if first in ("run", "run-script", "rum", "urn"):
            script = positional[1] if len(positional) > 1 else None
        elif family in ("yarn", "pnpm", "bun") and first and first not in (
            "install", "i", "add", "remove", "rm", "upgrade", "up", "update", "info", "list",
            "ls", "why", "outdated", "audit", "init", "create", "config", "cache", "link",
            "unlink", "pack", "test", "t", "dlx", "exec", "x", "workspaces", "workspace",
            "set", "plugin", "constraints", "version", "help", "store", "import", "prune",
            "rebuild", "patch", "patch-commit", "publish", "npm", "login", "logout", "global",
            "build", "dev", "start", "lint", "format", "typecheck",
        ):
            script = first
        elif first in ("start", "test", "t", "stop", "restart", "version"):
            script = first
        if first == "version":
            hits.risky.append("exec:version-bump")
        if script and _DEPLOY_NAME.search(script):
            hits.guard(DEPLOY, "exec:deploy-script")
        if first in ("config", "c") and positional[1:2] in (["set"], ["delete"], ["edit"]):
            if any(w in ("-g", "--global", "--location=global", "--location=user")
                   for w in rest):
                hits.guard(OUTSIDE, "exec:global-config")
            if any(_SECRET_NAME.search(w) or "_auth" in w for w in positional[2:]):
                hits.guard(SECRET, "exec:registry-login")

    def _container(
        self, family: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        hits = self.hits
        positional = [w for w in rest if not w.startswith("-")]
        first = positional[0] if positional else ""
        second = positional[1] if len(positional) > 1 else ""
        if first in ("push",) or (first == "image" and second == "push") or (
            first in ("buildx", "build", "image", "compose") and any(
                w == "--push" or w.startswith("--push=") or w.startswith("--output=type=registry")
                for w in rest
            )
        ) or (first in ("manifest", "compose") and second == "push"):
            hits.guard(DEPLOY, f"exec:{family}-push")
            hits.network = True
        if first in ("login",):
            hits.guard(SECRET, "exec:registry-login")
        if first in ("stack", "service", "swarm", "context") and second in (
            "deploy", "update", "rm", "remove", "create", "scale", "rollback", "init", "join",
            "leave"
        ):
            hits.guard(DEPLOY, f"exec:{family}-deploy")
        if first in ("rmi", "prune") or (first in ("rm", "kill") and any(
            w in ("-f", "--force", "-v", "--volumes") for w in rest
        )) or (first in ("system", "volume", "image", "container", "network", "builder",
                         "buildx") and second in ("prune", "rm", "remove", "reset")) or (
            first == "compose" and second == "down" and any(w in ("-v", "--volumes", "--rmi")
                                                            for w in rest)
        ):
            hits.guard(OUTSIDE, f"exec:{family}-destroy")
        if first in ("run", "create") or (first == "container" and second in ("run", "create")) or (
            first == "compose" and second in ("run", "up")
        ):
            if any(w in ("--privileged", "--pid=host", "--net=host", "--network=host",
                         "--userns=host", "--ipc=host") or w.startswith("--cap-add") or
                   w.startswith("--device") or w.startswith("--security-opt")
                   for w in rest):
                hits.guard(OUTSIDE, f"exec:{family}-privileged")
            for idx, word in enumerate(rest):
                spec: str | None = None
                if word in ("-v", "--volume", "--mount") and idx + 1 < len(rest):
                    spec = rest[idx + 1]
                elif word.startswith(("--volume=", "--mount=")):
                    spec = word.split("=", 1)[1]
                elif word.startswith("-v") and len(word) > 2:
                    spec = word[2:]
                if spec is None:
                    continue
                source = spec
                if "source=" in spec or "src=" in spec:
                    match = re.search(r"(?:source|src)=([^,]+)", spec)
                    source = match.group(1) if match else ""
                else:
                    source = spec.split(":", 1)[0]
                if source.startswith(("/", ".", "~", "$")):
                    self.check_write_path(source, cwd, "exec", "$" in source)
        if first == "exec" or (first == "compose" and second == "exec"):
            hits.risky.append(f"exec:{family}-exec")
        if first in ("cp",):
            for word in positional[1:]:
                if ":" not in word:
                    self.check_write_path(word, cwd, "exec", False)
        if first in ("save", "export") and any(w in ("-o", "--output") for w in rest):
            idx = max(rest.index(w) for w in ("-o", "--output") if w in rest)
            if idx + 1 < len(rest):
                self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        if first in ("pull", "build", "run", "search", "compose"):
            hits.network = True

    def _build_tool(self, program: str, family: str, rest: list[str], words: list[str]) -> None:
        hits = self.hits
        valued = frozenset({"-C", "--directory", "-f", "--file", "--makefile", "-j", "--jobs",
                            "-l", "-o", "-W", "-I", "-p", "-P", "-D", "--project", "-c",
                            "--config", "-s", "--settings", "-e", "-t"})
        targets = [w for w, _ in _operands(rest, [False] * len(rest), valued)
                   if "=" not in w or program not in ("make", "gmake")]
        for target in targets:
            if _DEPLOY_NAME.search(target):
                hits.guard(DEPLOY, "exec:deploy-target")
                break
        if family == "gradle" and any(t.lower().startswith(("publish", "upload", "release"))
                                      or ":publish" in t.lower() for t in targets):
            hits.guard(DEPLOY, "exec:gradle-publish")
        if family == "mvn" and any(t in ("deploy", "release:perform", "release:prepare",
                                         "site-deploy", "jib:build", "wagon:upload")
                                   for t in targets):
            hits.guard(DEPLOY, "exec:mvn-deploy")
        if program == "dotnet" and targets[:2] in (["nuget", "push"], ["nuget", "delete"]):
            hits.guard(DEPLOY, "exec:dotnet-publish")
        if program == "dotnet" and targets[:1] == ["tool"] and any(
            w in ("-g", "--global") for w in rest
        ):
            hits.guard(OUTSIDE, "exec:system-install")
        if program == "mix" and any(t in ("hex.publish", "hex.retire") for t in targets):
            hits.guard(DEPLOY, "exec:mix-publish")
        if program == "xcodebuild" and any(w in ("-exportArchive",) for w in rest):
            hits.risky.append("exec:xcode-export")
        if program in ("lerna",) and targets[:1] in (["publish"], ["version"]):
            hits.guard(DEPLOY, "exec:lerna-publish")
        if self._runs_commands(words):
            hits.guard_all("exec:opaque:runs-commands")

    def _cloud(self, program: str, rest: list[str]) -> None:
        hits = self.hits
        hits.network = True
        positional = [w for w in rest if not w.startswith("-")]
        first = positional[0] if positional else ""
        second = positional[1] if len(positional) > 1 else ""
        if rest[:1] in (["--version"], ["--help"], ["-h"], ["-v"], ["version"], ["help"]):
            return
        if program in ("aws", "gcloud", "az", "doctl", "linode-cli", "hcloud", "oci",
                       "ibmcloud"):
            path = positional[:4]
            if any(_CLOUD_SECRET_WORD.search(w) for w in path) or (
                program == "aws" and first == "configure"
            ):
                hits.guard(SECRET, f"exec:{program}-secret")
            if any(_CLOUD_SEND_WORD.match(w) for w in path):
                hits.guard(SECRET, f"exec:{program}-send")
            if any(_CLOUD_DESTROY_WORD.match(w) for w in path):
                hits.guard(DEPLOY, f"exec:{program}-change")
                hits.guard(OUTSIDE, f"exec:{program}-destroy")
                return
            if program == "aws" and first == "s3":
                if second in ("ls", ""):
                    return
                if second == "presign":
                    hits.guard(SECRET, "exec:aws-secret")
                    return
                hits.guard(DEPLOY, "exec:aws-change")
                if second in ("rm", "rb", "mv") or (second == "sync" and "--delete" in rest):
                    hits.guard(OUTSIDE, "exec:aws-destroy")
                if second in ("cp", "sync", "mv"):
                    hits.guard(SECRET, "exec:aws-upload")
                return
            if not path or program == "aws" and first == "configure":
                return
            if any(_CLOUD_READ_VERB.match(w) for w in path[1:]) or (
                len(path) == 1 and _CLOUD_READ_VERB.match(path[0])
            ):
                return
            hits.guard(DEPLOY, f"exec:{program}-change")
            return
        if program in ("kubectl", "oc", "helm"):
            if any(re.match(r"^secrets?(/|$)", w) for w in positional) or (
                first == "config" and any(w in ("--raw", "--flatten") for w in rest)
            ):
                hits.guard(SECRET, f"exec:{program}-secret")
            if first == "config" and program != "helm":
                return
        if program in ("terraform", "tofu", "terragrunt") and first == "state":
            if second in ("list", "show", "pull"):
                return
            hits.guard(DEPLOY, f"exec:{program}-deploy")
            if second in ("rm", "mv", "push", "replace-provider"):
                hits.guard(OUTSIDE, f"exec:{program}-destroy")
            return
        reads = CLOUD_READ_ONLY.get(program, frozenset())
        if program == "vercel" and not positional:
            hits.guard(DEPLOY, "exec:vercel-deploy")
            return
        if not rest or first in reads:
            if program == "pulumi" and first == "stack" and second in ("rm", "select", "init"):
                hits.guard(DEPLOY, "exec:pulumi-change")
            if program == "pulumi" and first == "config" and second == "set" and "--secret" in rest:
                hits.guard(SECRET, "exec:pulumi-secret")
            if program == "vercel" and first == "env" and second in ("pull", "add", "rm", "ls"):
                hits.guard(SECRET, "exec:vercel-env")
            if program == "wrangler" and first == "pages" and second == "deploy":
                hits.guard(DEPLOY, "exec:wrangler-deploy")
            return
        hits.guard(DEPLOY, f"exec:{program}-deploy")
        if re.search(r"(destroy|delete|remove|\brm\b|uninstall|purge|rollback|drain|cordon)",
                     " ".join(positional[:2])):
            hits.guard(OUTSIDE, f"exec:{program}-destroy")
        if re.search(r"(secret|env:set|config:set|env add)", " ".join(positional[:2])):
            hits.guard(SECRET, f"exec:{program}-secret")


def _flag_hit(word: str, flag: str) -> bool:
    """``word`` is ``flag``, ``flag=value``, or a short flag inside a cluster (``-qx``)."""
    if word == flag or word.startswith(flag + "="):
        return True
    if len(flag) == 2 and flag[0] == "-" and flag[1] != "-":
        return word.startswith("-") and not word.startswith("--") and flag[1] in word[1:]
    return False


def _leading_options(words: list[str], valued: frozenset[str] = frozenset()) -> list[str]:
    """The option words before the first operand (``valued`` take the next word along)."""
    found: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            found.append(word)
            break
        if not word.startswith(("-", "+")) or word in ("-", "+"):
            break
        found.append(word)
        if word in valued and index + 1 < len(words):
            found.append(words[index + 1])
            index += 2
            continue
        index += 1
    return found


def _skip_options(words: list[str], valued: frozenset[str]) -> list[str] | None:
    """``words`` after a wrapper's own options (``valued`` take the next word)."""
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            return words[index + 1 :]
        if word in valued:
            index += 2
            continue
        if word.startswith("-") and len(word) > 1:
            index += 1
            continue
        return words[index:]
    return []


def _operands(
    words: list[str], dynamic: list[bool], valued: frozenset[str]
) -> list[tuple[str, bool]]:
    """Non-option words, skipping the values of ``valued`` options."""
    found: list[tuple[str, bool]] = []
    index = 0
    only_operands = False
    while index < len(words):
        word = words[index]
        dyn = dynamic[index] if index < len(dynamic) else True
        if only_operands:
            found.append((word, dyn))
        elif word == "--":
            only_operands = True
        elif word in valued:
            index += 1
        elif word.startswith("-") and len(word) > 1:
            pass
        else:
            found.append((word, dyn))
        index += 1
    return found


def _path_candidates(word: str) -> list[str]:
    """``--file=~/.ssh/k`` -> ``~/.ssh/k``; ``@./x`` -> ``./x``; ``host:path`` -> ``path``."""
    found = [word]
    if "=" in word:
        found.append(word.split("=", 1)[1])
    if word.startswith("@"):
        found.append(word[1:])
    if word.startswith("-") and len(word) > 2 and not word.startswith("--"):
        found.append(word[2:])
    return [w for w in found if w]


def _command_text(args: Mapping[str, Any]) -> str | None:
    """The shell line of a tool call: ``command`` as a string or an argv list."""
    for key in ("command", "cmd", "script_command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list | tuple) and value and all(isinstance(v, str) for v in value):
            argv = list(value)
            # ``["bash", "-lc", "…"]`` is how CLIs wrap a line: judge the line itself.
            if len(argv) == 3 and _program(argv[0]) in SHELLS and argv[1] in ("-c", "-lc"):
                return argv[2]
            return shlex.join(argv)
    return None


#: Argument keys that name a file a tool reads or writes.
_PATH_KEYS = ("path", "file_path", "filePath", "notebook_path", "target", "destination",
              "dest", "source", "src", "file", "filename", "directory", "dir_path")


def _call_paths(args: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    try:
        from snowpea_core.tools.call_paths import call_paths

        found.extend(call_paths(dict(args)))
    except Exception:  # noqa: BLE001
        pass
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    for key in ("paths", "files", "edits", "changes"):
        value = args.get(key)
        if isinstance(value, list | tuple):
            for item in value:
                if isinstance(item, str) and item.strip():
                    found.append(item.strip())
                elif isinstance(item, Mapping):
                    for sub in ("path", "file_path", "filePath", "file"):
                        inner = item.get(sub)
                        if isinstance(inner, str) and inner.strip():
                            found.append(inner.strip())
        elif isinstance(value, Mapping):
            found.extend(str(k) for k in value if isinstance(k, str))
    unique: list[str] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def classify(request: ClassifyRequest) -> Verdict:
    """Classify one approval request.  Pure; never raises."""
    try:
        return _Judge(request).run()
    except Exception:  # noqa: BLE001 - a bug here must fail closed, not open
        import logging

        logging.getLogger("snowpea.jarvis").exception("classifier failed on %s", request.tool)
        return Verdict("guarded", GUARDED_CLASSES, ("internal:classifier-error",))


__all__ = [
    "DEPLOY",
    "GUARDED_CLASSES",
    "OUTSIDE",
    "SECRET",
    "Classification",
    "ClassifyRequest",
    "GuardedClass",
    "Verdict",
    "classify",
    "is_secret_path",
    "parse_line",
]
