"""What an approval request is, for Jarvis mode (M19 WP2).

The delegation engine (:mod:`snowpea_core.jarvis.delegation`) is the security
boundary of Jarvis mode, and this module is its eyes: a **pure, deterministic**
function from one approval request (tool, arguments, permission tags, project
root) to a :class:`Verdict`:

``never``
    Never delegable — always the person's to answer (they may still allow
    it): snowpea's authority and secret files under ``$SNOWPEA_HOME`` (the
    area table at :data:`HOME_SECRET_TOP`; writes to its logs and unknown
    areas too), read or written, by any tool, literal, globbed, ``~user``,
    behind a variable, or inside a recursive read of one of its ancestors;
    a project's snowpea settings; the ``snowpea`` CLI; delegation changes,
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
    Allowed by the engine with nobody asked, so it must not be able to write,
    run project- or worker-controlled code, read a secret or snowpea's own
    files, or send anything out — by every parse path.  A tool tagged ``read``
    whose paths all lie in the project, or (security review 3) a line from
    snowpea's own ``shell`` tool whose every command and option is in the
    exact tables of :mod:`snowpea_core.jarvis.inspect_table`, on literal
    paths that stay inside the project.  git is never ``safe``.
``risky``
    Everything else.  A ``risky`` shell line that the same tables call
    read-only but that is not eligible for ``safe`` (git at the project's top
    level, an external CLI's shell, a glob, a recursive option) carries
    :attr:`Verdict.read_only` (rule ``exec:read-like``): Jarvis may decide it.

A request in several classes reports all of them (:attr:`Verdict.guarded`).
A shell line this module cannot see through — a command substitution, a pipe
into a shell or interpreter, ``eval``, ``sh -c``, inline interpreter code, an
encoded payload, an alias or function definition, a git alias, an unbalanced
quote, brace expansion in the program's place, ``case``/``select``, a
database client's shell escape, an awk or sed program read from a file —
**fails closed**: it is reported in every guarded class
(rule ``exec:opaque:<why>``), so only a grant naming all three would let
Jarvis decide it.  Compound lines (``&&``, ``||``, ``;``, plain pipes) are
split and each part judged; ``cd`` is followed so relative paths resolve where
they would run.

It reads nothing but the paths it is given (``Path.resolve`` follows symlinks,
so a link inside the project pointing out is outside), never runs anything,
and never raises on malformed input.
"""

from __future__ import annotations

import contextlib
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
    #: The call comes from an external CLI's tool (``ApprovalRequest.external``, or a
    #: ``ext:`` session id): its shell's folder and state are the CLI's, never ``safe``.
    external: bool = False
    #: The arguments held characters a surface strips (``ApprovalRequest.argsEscaped``):
    #: ``args`` is the escaped copy, so what really runs was never seen (N3).
    args_escaped: bool = False

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
            external=getattr(request, "external", None) is not None,
            args_escaped=bool(getattr(request, "argsEscaped", None)),
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
    #: A ``risky`` shell line that is read-only by the table's knowledge but not
    #: eligible for ``safe`` (rule ``exec:read-like``): Jarvis may allow it once
    #: at either level (security review 3, D2).
    read_only: bool = False

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
        "git_status", "git_diff", "git_log", "todowrite", "todoread",
    }
)
#: Read tools that walk a directory tree (a path naming an ancestor of a
#: secret or of ``$SNOWPEA_HOME`` reads them too).
RECURSIVE_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {"grep", "glob", "search", "codebase_search", "search_files", "find_files", "list_files"}
)
#: Tools that fetch a URL or run a web search: whatever they are handed leaves
#: the machine (a query string, a path), so they are sends, never reads.
WEB_SEND_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "webfetch", "web_fetch", "websearch", "web_search", "web_extract", "fetch",
        "fetch_url", "url_fetch", "read_url", "browse", "web_browse", "google_web_search",
        "web_search_preview", "http_request", "http_get",
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
    r"(click|type|fill|submit|press|select|upload|evaluate|script|send|fetch|download|drag|"
    r"navigate|goto|open_url|open_tab)",
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
        # Agent CLIs' own login state (security review 3, N4).
        ".claude.json", ".claude.json.backup", ".credentials.json", "oauth_creds.json",
        "google_accounts.json", "mcp-oauth-tokens.json",
        # Shell and REPL histories: commands typed with tokens in them.
        ".bash_history", ".zsh_history", ".history", ".histfile", ".sh_history",
        ".python_history", ".node_repl_history", ".psql_history", ".mysql_history",
        ".sqlite_history", ".lesshst", ".rediscli_history", ".irb_history", "fish_history",
    }
)
#: ``(folder parts, file name)``: a credential file inside a CLI's folder.
_SECRET_FILES_IN: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".codex",), "auth.json"),
    ((".claude",), ".credentials.json"),
    ((".gemini",), "oauth_creds.json"),
    ((".config", "cursor"), "auth.json"),
    ((".cursor",), "cli-config.json"),
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
    # security review 3 (N4): cloud CLIs, keyrings, browser profiles, the
    # owner's secrets folder.
    (".config", "snowpea-secrets"),
    (".config", "doctl"),
    (".config", "hcloud"),
    (".config", "heroku"),
    (".config", "configstore"),
    (".oci",),
    (".fly",),
    (".local", "share", "keyrings"),
    ("Library", "Keychains"),
    (".mozilla",),
    (".config", "google-chrome"),
    (".config", "chromium"),
    (".config", "BraveSoftware"),
    (".config", "microsoft-edge"),
    (".config", "cursor"),
)


#: ``/proc/<pid>/<name>`` files that show another process's environment,
#: command line (tokens passed as arguments) or memory.
_PROC_SECRET_NAMES: frozenset[str] = frozenset({"environ", "cmdline", "mem", "auxv"})


def is_secret_path(path: Path) -> bool:
    """True when ``path`` (resolved) is a credential or key location."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "proc" and path.name in _PROC_SECRET_NAMES:
        return True
    try:
        from snowpea_core.agent.prompt_refs import is_secret_pattern

        if is_secret_pattern(path):
            return True
    except Exception:  # noqa: BLE001 - a broken helper must not widen anything
        return True
    if path.name in _EXTRA_SECRET_NAMES or path.name.lower() in _EXTRA_SECRET_NAMES:
        return True
    for run in _EXTRA_SECRET_DIRS:
        width = len(run)
        for index in range(len(parts) - width + 1):
            if tuple(parts[index : index + width]) == run:
                return True
    for run, name in _SECRET_FILES_IN:
        width = len(run)
        if path.name == name and tuple(parts[-1 - width : -1]) == run:
            return True
    return False


#: ``$SNOWPEA_HOME`` areas (owner's rule, 2026-11-16).  Reading under the home
#: works and writing is possible with the person's permission; only authority
#: and secret files are ``never`` (never delegable: always the person's call).
#:
#: ===========  ==========================================  =======================
#: area         what                                        read / write
#: ===========  ==========================================  =======================
#: secret       :data:`HOME_SECRET_TOP`, any                ``never`` / ``never``
#:              :data:`HOME_SECRET_PATTERNS` name anywhere
#:              under the home, ``classify.is_secret_path``
#: own          ``plans/`` and session workspaces           as in the project /
#:                                                          as in the project
#: content      :data:`HOME_CONTENT_DIRS`                   as in the project
#:                                                          (literal read ``safe``) /
#:                                                          ``guarded[outside_destructive]``
#: logs         ``logs/``                                   ``risky`` / ``never``
#: other        anything else under the home, and the       ``risky`` / ``never``
#:              home folder itself
#: ===========  ==========================================  =======================
#:
#: A glob, ``~user`` or dynamic path takes the strictest area it could match; a
#: recursive read of an ancestor of the home (``grep -r x ~``), of the home
#: itself, or of an ``other`` folder below it is ``never`` (it could reach a
#: secret file); one of a ``content``/``own``/``logs`` folder is ``risky``.
HOME_SECRET_TOP: frozenset[str] = frozenset({
    "token", "settings.json", "mcp-oauth.json", "daemon.json", "escalations.json",
    "budget.json", "jarvis", "external", "attachments", ".claude", ".codex",
})
HOME_SECRET_PATTERNS: tuple[str, ...] = (
    "credentials*.json", "gateway*", "*.db", "*.db-*", "*.sqlite", "*.sqlite3",
    "*.sqlite-*", "delegation.db*", "ledger.jsonl*", "presets.json",
)
HOME_OWN_DIRS: frozenset[str] = frozenset({"plans"})
HOME_CONTENT_DIRS: frozenset[str] = frozenset({
    "skills", "agents", "extensions", "plugins", "docs", "memory", "reports", "commands",
    "prompts", "templates",
})
HOME_LOG_DIRS: frozenset[str] = frozenset({"logs"})

#: A shell line that names snowpea's secret files (whatever the parse makes of it).
_SNOWPEA_HOME_TEXT = re.compile(
    r"(\$\{?SNOWPEA_HOME|"
    r"\.snowpea/(settings|credentials)\.json|\bdelegation\.db\b|jarvis/ledger|"
    r"\bmcp-oauth\.json\b)",
    re.IGNORECASE,
)
#: A literal ``~/.snowpea…`` (or ``$HOME/.snowpea…``) path token in a shell line.
_SNOWPEA_HOME_TOKEN = re.compile(r"(~|\$HOME|\$\{HOME\})/\.snowpea[^\s'\"`;|&()<>]*")


def _home_secret_name(name: str) -> bool:
    import fnmatch

    return any(fnmatch.fnmatchcase(name, pattern) for pattern in HOME_SECRET_PATTERNS)


def home_area(path: Path, roots: Iterable[Path]) -> str | None:
    """Which :data:`HOME_SECRET_TOP` table area ``path`` is in (``None``: not in a home).

    ``root`` is a home folder itself.  ``roots`` are the snowpea homes in reach
    (the daemon's and the user's default ``~/.snowpea``).
    """
    for root in roots:
        if not _under(path, root):
            continue
        rel = path.relative_to(root).parts
        if not rel:
            return "root"
        if rel[0] in HOME_SECRET_TOP or any(_home_secret_name(part) for part in rel):
            return "secret"
        if is_secret_path(path):
            return "secret"
        if rel[0] in HOME_OWN_DIRS:
            return "own"
        if rel[0] == "sessions" and len(rel) >= 2 and re.match(r"^\d{4}.*_.", rel[1]):
            return "own"
        if rel[0] in HOME_LOG_DIRS:
            return "logs"
        if rel[0] in HOME_CONTENT_DIRS:
            return "content"
        return "other"
    return None
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
        # Where ``cd`` goes, and where every tool looks for its own configuration.
        "CDPATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "ZDOTDIR", "GNUPGHOME",
        "GLOBIGNORE", "HISTFILE", "LESSOPEN", "LESSCLOSE", "MANPAGER", "MANOPT", "BROWSER",
        "GOFLAGS", "GOTOOLCHAIN", "RUSTC_WRAPPER", "RUSTC", "RUSTDOCFLAGS", "RUSTFLAGS", "CC",
        "CXX", "LD", "MAKEFLAGS", "MAKEFILES", "PYTHONUSERBASE", "RIPGREP_CONFIG_PATH",
        "TAR_OPTIONS", "SQLITE_HISTORY", "PSQLRC", "MYSQL_HISTFILE", "INPUTRC", "TERMINFO",
        "TMPDIR", "SHELL",
        # Security review 8, M8-2: more variables that load code or point a tool
        # at a configuration of the line's choosing.
        "LUA_INIT", "JDK_JAVA_OPTIONS", "DOTNET_STARTUP_HOOKS", "PHPRC", "PHP_INI_SCAN_DIR",
        "VIMINIT", "EXINIT", "ERL_AFLAGS", "ERL_ZFLAGS", "RSYNC_RSH", "CURL_HOME", "WGETRC",
        "AWS_CONFIG_FILE", "KUBECONFIG", "DOCKER_CONFIG", "GCONV_PATH", "PYTHONPYCACHEPREFIX",
        "LESSKEYIN",
    }
)
#: ``GIT_*`` variables that only change what git prints (every other one may
#: point git at another repository, config, hook or program).
_GIT_HARMLESS_VARIABLES = re.compile(
    r"^GIT_(TRACE.*|TERMINAL_PROMPT|AUTHOR_(NAME|EMAIL|DATE)|COMMITTER_(NAME|EMAIL|DATE)|"
    r"OPTIONAL_LOCKS|FLUSH|CURL_VERBOSE|MERGE_VERBOSITY|PROGRESS_DELAY|REDACT_COOKIES)$"
)


def _is_code_variable(name: str) -> bool:
    """An environment variable that loads code or redirects configuration."""
    upper = name.upper()
    if name in _CODE_VARIABLES or upper in _CODE_VARIABLES:
        return True
    if upper.startswith(("NPM_CONFIG_", "YARN_", "PNPM_", "CARGO_", "PIP_", "UV_", "POETRY_",
                         "BUNDLE_", "GEM_", "DYLD_", "LD_")):
        return True
    return upper.startswith("GIT_") and not _GIT_HARMLESS_VARIABLES.match(upper)

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
    #: Per word: it came out of a brace expansion (``{a,b}``, ``{1..3}``).
    braced: list[bool] = field(default_factory=list)
    #: The text a here-string or here-document feeds the command (the security
    #: lists read a shell's script from it); ``None`` without one.
    stdin_text: str | None = None
    #: That text is expanded by the shell (an unquoted delimiter with ``$``/backticks,
    #: or a here-string with an expansion): what the command reads is not known.
    stdin_dynamic: bool = False
    #: Reads a pipe whose writer is not a command of this line (a command of a
    #: ``>(…)`` body, a substitution in a piped command): stdin from who knows.
    fed: bool = False


#: Most words one brace expansion may produce before the line is opaque.
BRACE_LIMIT = 64


def _brace_expand(text: str, mask: list[bool], budget: list[int]) -> list[str]:
    """Bash brace expansion of the unquoted braces (``mask`` marks quoted chars).

    Raises :class:`_Opaque` past :data:`BRACE_LIMIT` words.  Words that come
    out empty are dropped, as bash drops them.
    """
    index = 0
    while index < len(text):
        if text[index] != "{" or mask[index]:
            index += 1
            continue
        depth = 0
        commas: list[int] = []
        close = -1
        for j in range(index + 1, len(text)):
            if mask[j]:
                continue
            char = text[j]
            if char == "{":
                depth += 1
            elif char == "}":
                if depth == 0:
                    close = j
                    break
                depth -= 1
            elif char == "," and depth == 0:
                commas.append(j)
        if close < 0:
            return [text] if text else []
        prefix, pmask = text[:index], mask[:index]
        suffix, smask = text[close + 1 :], mask[close + 1 :]
        alternatives: list[tuple[str, list[bool]]] = []
        if commas:
            bounds = [index, *commas, close]
            for start, end in zip(bounds, bounds[1:], strict=False):
                alternatives.append((text[start + 1 : end], mask[start + 1 : end]))
        else:
            inner = text[index + 1 : close]
            numeric = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", inner)
            alpha = re.fullmatch(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?", inner)
            values: list[str] = []
            if numeric or alpha:
                found = numeric or alpha
                assert found is not None
                first, last = found.group(1), found.group(2)
                step = abs(int(found.group(3) or 1)) or 1
                low, high = (int(first), int(last)) if numeric else (ord(first), ord(last))
                count = abs(high - low) // step + 1
                if count > BRACE_LIMIT:
                    raise _Opaque("brace-expansion")
                direction = 1 if high >= low else -1
                values = [
                    str(v) if numeric else chr(v)
                    for v in range(low, high + direction, direction * step)
                ]
            if not values:
                index += 1  # ``{x}`` / ``{}``: literal braces
                continue
            alternatives = [(v, [True] * len(v)) for v in values]
        words: list[str] = []
        for alt, amask in alternatives:
            budget[0] -= 1
            if budget[0] < 0:
                raise _Opaque("brace-expansion")
            words.extend(_brace_expand(prefix + alt + suffix, pmask + amask + smask, budget))
        return words
    return [text] if text else []


@dataclass
class _Line:
    commands: list[_Cmd]
    #: ``$(…)``, backticks, ``<(…)``/``>(…)`` or ``$((…))`` appeared.
    substitution: bool = False
    #: ``name() …`` — a shell function definition.
    function: bool = False
    #: The names so defined.
    functions: list[str] = field(default_factory=list)
    #: ``(opener, body, command index)`` of every substitution the scanner met, in
    #: the order met (``$``, a backtick, ``<`` or ``>``): each body is a line of its
    #: own that runs, with the folder and stdin of the command at that index.
    #: Recorded where the scanner reads them, so quoting can never make a second
    #: reading disagree with it.
    bodies: list[tuple[str, str, int]] = field(default_factory=list)
    #: Why the end of a quoted substitution or a here-document's one may be
    #: misread (:func:`unsure_body`): the line is opaque for each, though what
    #: was read of it is still judged (security review 4).
    unsure: list[str] = field(default_factory=list)


class ProcessWord(str):
    """A word that is exactly one process substitution (``<(…)``/``>(…)``).

    The shell gives the command a pipe's name (``/dev/fd/63``) in its place: not
    a file anyone's, so no read or write of it is judged.  The commands inside
    are read as commands of the line (:attr:`_Line.bodies`).  The type
    marks the word: no text spelled in a line can be mistaken for one."""


def quote_end(text: str, start: int) -> int:
    """The index just past the quoted run that opens at ``start`` (``'…'``,
    ``$'…'`` with its backslash escapes, or ``"…"`` with the substitutions inside
    it, whose own quotes nest); -1 when it is not closed.  The one place every
    reader of this module skips a quote."""
    n = len(text)
    if text.startswith("$'", start):
        j = start + 2
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "'":
                return j + 1
            j += 1
        return -1
    if text[start : start + 1] == "'":
        end = text.find("'", start + 1)
        return -1 if end < 0 else end + 1
    j = start + 1
    while j < n:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == '"':
            return j + 1
        if char == "$" and text[j + 1 : j + 2] == "(":
            end = closing_paren(text, j + 2)
            if end < 0:
                return -1
            j = end + 1
            continue
        if char == "$" and text[j + 1 : j + 2] == "{":
            end = brace_end(text, j + 1, in_double=True)
            if end < 0:
                return -1
            j = end + 1
            continue
        if char == "`":
            end = _backtick_end(text, j + 1)
            if end >= n:
                return -1
            j = end + 1
            continue
        j += 1
    return -1


_CASE_WORD = re.compile(r"(case|esac)(?=[\s;&|()]|$)")
#: ``<<EOF`` / ``<<-'EOF'`` / ``<< E"O"F``: a here-document's delimiter, one
#: word that may be quoted in parts.
_HEREDOC_START = re.compile(
    r"<<(?P<strip>-)?[ \t]*(?P<word>(?:'[^'\n]*'|\"(?:[^\"\\\n]|\\.)*\"|\\.|[^\s;&|()<>'\"\\])+)")
#: Words after which the next word is a command (``then case …``).
_LEADS: frozenset[str] = frozenset({"then", "do", "{", "!", "else", "elif", "if", "while",
                                    "until", "time"})


#: ``case``/``esac`` anywhere in a text (security review 4: where a quoted
#: substitution holds one, its ``)`` may be misread whatever the rules).
_CASE_ANYWHERE = re.compile(r"(?<![\w-])(?:case|esac)(?![\w-])")


def unsure_body(inner: str) -> str | None:
    """Why the end :func:`closing_paren` found for a substitution inside double
    quotes (or a here-document) cannot be trusted, or ``None``: a ``case``/``esac``
    in it, a line continuation (``ca\\⏎se``), a here-document word with ``$'``/
    ``$"`` (security review 4).  The quoted text after a misread ``)`` hides
    commands, so such a line is opaque rather than modelled more closely."""
    if _CASE_ANYWHERE.search(inner):
        return "case-in-quoted-substitution"
    if "\\\n" in inner:
        return "continuation-in-quoted-substitution"
    if any(heredoc_delimiter(found.group("word")) is None
           for found in _HEREDOC_START.finditer(inner)):
        return "heredoc-dollar-quote"
    return None


def heredoc_delimiter(word: str) -> str | None:
    """The delimiter a here-document's word spells: its quotes and backslashes
    removed, as the shell removes them (``E"O"F`` and ``'E'OF`` are ``EOF``;
    inside ``"…"`` only ``\\"``, ``\\\\``, ``\\``` and ``\\$`` lose the backslash,
    so ``"E\\F"`` is ``E\\F``).  ``None`` for a word with ``$'`` or ``$"`` in it,
    which the reader does not spell.  The scanner and :func:`closing_paren`
    both use it, so they agree on where a body ends."""
    if "$'" in word or '$"' in word:
        return None
    out: list[str] = []
    index = 0
    while index < len(word):
        char = word[index]
        if char == "\\" and index + 1 < len(word):
            if word[index + 1] != "\n":
                out.append(word[index + 1])
            index += 2
        elif char == "'":
            end = word.find("'", index + 1)
            end = len(word) if end < 0 else end
            out.append(word[index + 1 : end])
            index = end + 1
        elif char == '"':
            index += 1
            while index < len(word) and word[index] != '"':
                if word[index] == "\\" and index + 1 < len(word) and word[index + 1] in '"\\`$\n':
                    if word[index + 1] != "\n":
                        out.append(word[index + 1])
                    index += 2
                    continue
                out.append(word[index])
                index += 1
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _command_position(text: str, start: int, at: int, *, esac: bool = False,
                      in_case: bool = False, pattern_close: int = -1) -> bool:
    """The word at ``at`` is where a command starts: after a newline, ``;``,
    ``;;``, ``&``, ``|`` or ``(`` (or ``start``), after the case pattern's ``)``
    at ``pattern_close``, or after a word that leads one (``then``, ``do``,
    ``{``, ``!``, ``else``…).

    A ``case`` missed lets a pattern's ``)`` close the text early, and so does
    an ``esac`` counted that is not one; so for a ``case`` any ``)``, ``}``,
    ``fi``, ``done`` or ``esac`` before it counts too, while an ``esac``
    (``esac``) counts only where it surely is one: its leading word must stand
    where a command starts itself (``echo then esac`` is three words), and
    inside a ``case`` (``in_case``) a word after ``|`` or ``(`` is a pattern
    (``a|esac)``, ``(esac)``)."""
    k = at - 1
    while k >= start and text[k] in " \t":
        k -= 1
    if k < start or k == pattern_close:
        return True
    char = text[k]
    if char in "|(" and text[k - 1 : k + 1] != "||":
        return not (esac and in_case)
    if char in "\n;&|":
        return True
    if char == ")":
        return not esac
    end = k + 1
    while k >= start and text[k] not in " \t\n;&|()":
        k -= 1
    word = text[k + 1 : end]
    if word in ("}", "fi", "done", "esac"):
        return not esac
    return word in _LEADS and (not esac or _command_position(
        text, start, k + 1, esac=True, in_case=in_case, pattern_close=pattern_close))


def closing_paren(text: str, start: int) -> int:
    """The index of the ``)`` closing a ``(`` that opened just before ``start``
    (quotes, ``$'…'``, backticks, comments and backslashes respected, nested
    parentheses counted, a ``case`` pattern's ``)`` skipped); -1 when none, and
    for a here-document inside (its body is not read here, so nothing is)."""
    depth = 0
    cases = 0
    #: Where the last ``a)`` of a case pattern ended (a command starts after it).
    pattern_close = -1
    j = start
    n = len(text)
    heredocs: list[tuple[str, bool]] = []
    while j < n:
        char = text[j]
        if char == "\n" and heredocs:
            # The here-documents opened on this line: their bodies are text.
            j += 1
            for delimiter, strip in heredocs:
                while True:
                    end = text.find("\n", j)
                    line = text[j:] if end < 0 else text[j:end]
                    if (line.lstrip("\t") if strip else line) == delimiter:
                        j = n if end < 0 else end
                        break
                    if end < 0:
                        return -1  # never closed
                    j = end + 1
            heredocs = []
            continue
        if char == "\\":
            j += 2
            continue
        if char in "'\"" or text.startswith("$'", j):
            end = quote_end(text, j)
            if end < 0:
                return -1
            j = end
            continue
        if char == "`":
            end = _backtick_end(text, j + 1)
            if end >= n:
                return -1
            j = end + 1
            continue
        word_start = j == start or text[j - 1] in " \t\n;&|()"
        if char == "#" and word_start:
            # A comment: its quotes and parentheses are text (``# don't``).
            end = text.find("\n", j)
            if end < 0:
                return -1
            j = end
            continue
        if text.startswith("<<", j) and not text.startswith("<<<", j):
            # A here-document: its delimiter, then its body after this line.
            found = _HEREDOC_START.match(text, j)
            delimiter = None if found is None else heredoc_delimiter(found.group("word"))
            if found is None or delimiter is None:
                return -1
            heredocs.append((delimiter, bool(found.group("strip"))))
            j = found.end()
            continue
        if word_start:
            found = _CASE_WORD.match(text, j)
            # Only a ``case``/``esac`` where a command starts (``echo esac`` is a word).
            if found is not None and _command_position(
                    text, start, j, esac=found.group(1) == "esac", in_case=cases > 0,
                    pattern_close=pattern_close):
                cases = cases + 1 if found.group(1) == "case" else max(cases - 1, 0)
                j = found.end()
                continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                if cases:
                    pattern_close = j
                    j += 1  # ``a)``: a case pattern ends
                    continue
                return j
            depth -= 1
        j += 1
    return -1


def brace_end(text: str, start: int, *, in_double: bool = False) -> int:
    """The index of the ``}`` closing the ``{`` at ``start`` (a ``${…}``): quotes,
    substitutions and nested braces skipped; -1 when none.  ``in_double``: the
    expansion sits inside double quotes, where a ``"`` opens a nested quote
    (``"${X:-"'"}"``) and a bare ``'`` is not read at all (-1): bash quotes with
    it there for pattern operators (``"${X#'"'}"``), dash does not (security
    review 4); nor is a ``$(…)`` whose end :func:`unsure_body` doubts."""
    depth = 0
    j = start
    n = len(text)
    while j < n:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if in_double and char == "'":
            return -1
        if char == '"' or (not in_double and (char == "'" or text.startswith("$'", j))):
            # ``${X:-"}"}``: a quoted brace closes nothing.
            j = quote_end(text, j)
            if j < 0:
                return -1
            continue
        if text.startswith("$(", j) or char == "`":
            inner = closing_paren(text, j + 2) if char == "$" else _backtick_end(text, j + 1)
            if inner < 0 or inner >= n:
                return -1
            if in_double and char == "$" and unsure_body(text[j + 2 : inner]):
                return -1
            j = inner + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def is_arithmetic(text: str, start: int) -> bool:
    """``$((…))`` at ``start`` is arithmetic; ``$((cmd); …)`` is a command
    substitution whose body starts with a subshell."""
    if not text.startswith("$((", start):
        return False
    inner = closing_paren(text, start + 3)
    return inner >= 0 and text[inner + 1 : inner + 2] == ")"


def _backtick_end(text: str, start: int) -> int:
    """The index of the backtick closing one opened just before ``start`` (or the end)."""
    j = start
    while j < len(text) and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    return min(j, len(text))


def embedded_bodies(text: str, *, heredoc: bool = False) -> list[tuple[str, str]]:
    """``(opener, body)`` of every substitution in a piece of text the scanner
    hands over whole (a ``${…}`` expansion, an unquoted here-document's body).
    ``heredoc``: quotes are text there, and ``<(`` is not a substitution.  A
    ``<(``/``>(`` inside double quotes is text to the shell; it is reported all
    the same (reading it as commands only ever asks more)."""
    bodies: list[tuple[str, str]] = []
    index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1 : index + 2]
        if char == "\\":
            index += 2
            continue
        if not heredoc and not quoted and (char == "'" or text.startswith("$'", index)):
            end = quote_end(text, index)
            if end < 0:
                break
            index = end
            continue
        if char == '"' and not heredoc:
            quoted = not quoted
            index += 1
            continue
        if nxt == "(" and ((char == "$" and not is_arithmetic(text, index))
                           or (char in "<>" and not heredoc)):
            end = closing_paren(text, index + 2)
            end = len(text) if end < 0 else end
            bodies.append((char, text[index + 2 : end]))
            index = end + 1
            continue
        if char == "`":
            end = _backtick_end(text, index + 1)
            bodies.append(("`", text[index + 1 : end].replace("\\`", "`")))
            index = end + 1
            continue
        index += 1
    return [(opener, body) for opener, body in bodies if body.strip()]


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
        #: Per character of the word: it was quoted, escaped or part of an
        #: expansion (so a ``{`` in it does not brace-expand).
        self.mask: list[bool] = []
        self.in_word = False
        self.dynamic = False
        #: Inside an unquoted backtick substitution.
        self.backtick = False
        self.pending_redirect: str | None = None
        #: Where the word after the pending redirection starts in the text.
        self.redirect_at = 0
        self.pending_heredocs: list[tuple[str, bool, bool, _Cmd]] = []
        #: The word being read had a quote or a backslash in it.
        self.quoted = False
        self.piped_in = False
        #: The text of a process substitution the word being read holds.
        self.process: str | None = None
        #: Where the last kept substitution body ends (:meth:`_record`).
        self.body_end = -1
        #: ``case`` minus ``esac`` where a command starts.
        self.cases = 0

    # -- words -----------------------------------------------------------
    def _add(self, chunk: str, protected: bool) -> None:
        self.word.append(chunk)
        self.mask.extend([protected] * len(chunk))
        self.in_word = True

    def _end_word(self) -> None:
        if not self.in_word:
            return
        text = "".join(self.word)
        if self.process is not None and text == self.process:
            text = ProcessWord(text)
        mask = self.mask
        expanded = [text]
        if any(c == "{" and not m for c, m in zip(text, mask, strict=False)):
            expanded = _brace_expand(text, list(mask), [BRACE_LIMIT])
        braced = expanded != [text]
        if self.pending_redirect is not None:
            operator = self.pending_redirect
            self.pending_redirect = None
            if braced and len(expanded) != 1:
                raise _Opaque("brace-redirect")
            target = expanded[0] if expanded else ""
            if operator in ("<<", "<<-"):
                # Spelled from the raw word by the one function closing_paren
                # uses too (security review 4, F2).
                delimiter = heredoc_delimiter(self.text[self.redirect_at : self.i].strip(" \t"))
                if delimiter is None:
                    raise _Opaque("heredoc-dollar-quote")
                self.pending_heredocs.append((delimiter, operator == "<<-", self.quoted,
                                              self.cmd))
                self.cmd.heredoc = True
            elif operator == "<<<":
                self.cmd.heredoc = True
                self.cmd.stdin_text = text
                self.cmd.stdin_dynamic = self.dynamic
            else:
                self.cmd.redirects.append((operator, target, self.dynamic))
        elif text in ("{", "}") and not self.dynamic and not self.quoted:
            pass  # a brace group: its commands are judged like any others
        else:
            if text in ("case", "esac") and not self.quoted and not self.dynamic and all(
                    word in RESERVED_WORDS for word in self.cmd.words):
                # Where a command starts: one ``case`` more than ``esac`` means a
                # ``)`` of the text was read as the wrong one (closing_paren).
                self.cases += 1 if text == "case" else -1
            for word in expanded:
                self.cmd.words.append(word)
                self.cmd.dynamic.append(self.dynamic)
                self.cmd.braced.append(braced)
        self.word = []
        self.mask = []
        self.in_word = False
        self.dynamic = False
        self.quoted = False
        self.process = None

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
        for delimiter, strip_tabs, quoted, owner in self.pending_heredocs:
            body: list[str] = []
            begin = self.i
            while self.i <= len(text):
                end = text.find("\n", self.i)
                end = len(text) if end < 0 else end
                line = text[self.i : end]
                self.i = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                body.append(line.lstrip("\t") if strip_tabs else line)
                if not quoted and ("`" in line or "$(" in line):
                    # An unquoted delimiter: the body is expanded, substitutions run.
                    self.line.substitution = True
                if not quoted and ("$" in line or "`" in line):
                    owner.stdin_dynamic = True
                if end >= len(text):
                    break
            owner.stdin_text = "\n".join(body)
            if not quoted:
                # The body's substitutions run where the command that reads it
                # runs (``cd ~/.ssh; cat <<EOF; cd /tmp`` reads it in ~/.ssh).
                owner_index = next((index for index, cmd in enumerate(self.line.commands)
                                    if cmd is owner), max(len(self.line.commands) - 1, 0))
                found = embedded_bodies(owner.stdin_text, heredoc=True)
                self.line.unsure.extend(filter(None, (unsure_body(inner) for opener, inner
                                                      in found if opener == "$")))
                self._record(found, begin, self.i, index=owner_index)
        self.pending_heredocs = []

    def _record(self, found: list[tuple[str, str]], at: int, end: int,
                index: int | None = None) -> None:
        """Keep the substitution bodies met at ``at`` (spanning to ``end``) with the
        command they belong to (``index``: by default the one being read).  One
        that starts inside a body already kept is that body's: it is read when
        that body is (a nest of ``$(`` is read once per level, not once per
        enclosing body)."""
        if at < self.body_end:
            return
        self.body_end = end
        where = len(self.line.commands) if index is None else index
        self.line.bodies.extend((opener, body, where) for opener, body in found
                                if body.strip())

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
                    self._add(nxt, True)
                    self.i += 2
                else:
                    self._add(char, True)
                    self.i += 1
            elif char == "'":
                self.quoted = True
                end = text.find("'", self.i + 1)
                if end < 0:
                    raise _Opaque("unbalanced-quote")
                self._add(text[self.i + 1 : end], True)
                self.i = end + 1
            elif char == '"':
                self._double_quoted()
            elif char == "$":
                self._dollar(nxt)
            elif char == "`":
                self.line.substitution = True
                self.backtick = not self.backtick
                if self.backtick:
                    end = _backtick_end(text, self.i + 1)
                    self._record([("`", text[self.i + 1 : end].replace("\\`", "`"))],
                                 self.i, end)
                    # An opening backtick is ``$(``: the word it is part of (or
                    # starts) is one only the shell knows — ``X=`cmd` `` used to
                    # read as ``X=`` and `` `echo rm` -rf / `` as the program
                    # ``-rf`` (security review 7).
                    self.dynamic = True
                    self._add("$", True)
                    self._end_word()
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
                    self.line.functions.append("".join(self.word))
                    self.i += 2
                    self._end_word()
                    # The name ends here: what follows is the body, a command of
                    # its own (it used to read as arguments of the name, so
                    # ``f() { rm -rf /; }`` hid its body; security review 7).
                    self._end_command(";")
                else:
                    self._end_command("(")
                    self.i += 1
            elif char == ")":
                self._end_command(")")
                self.i += 1
            elif char in "<>":
                self._angle(char, nxt)
            else:
                self._add(char, False)
                self.i += 1
        self._end_command(None)
        if self.cases != 0:
            # A ``case`` with no ``esac`` (or one more ``esac``): a ``)`` misread.
            raise _Opaque("case")
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
                    self._add(text[self.i + 1], True)
                self.i += 2
                continue
            pair = text[self.i : self.i + 2]
            if pair == "$(" and not is_arithmetic(text, self.i):
                # ``"$(…)"``: its own quotes nest; the whole of it is one part
                # of the word, and its body a line that runs.
                end = closing_paren(text, self.i + 2)
                # Security review 4: where the end found may be the wrong ``)``
                # the text after it hides commands as quoted text — opaque (the
                # body found is still read: what it shows still counts).
                unsure = unsure_body(text[self.i + 2 : end if end >= 0 else len(text)])
                if unsure and end < 0:
                    raise _Opaque(unsure)
                if end < 0:
                    raise _Opaque("unbalanced-paren")
                if unsure:
                    self.line.unsure.append(unsure)
                self.line.substitution = True
                self.dynamic = True
                self._record([("$", text[self.i + 2 : end])], self.i, end)
                self._add(text[self.i : end + 1], True)
                self.i = end + 1
                continue
            if pair == "${":
                # ``"${X:-"'"}"``: quotes inside nest; the whole of it is one part.
                end = brace_end(text, self.i + 1, in_double=True)
                if end < 0:
                    # Unclosed, a bare ``'`` inside, or a ``$(…)`` whose end is
                    # unsure (security review 4).
                    raise _Opaque("quoted-brace")
                body = text[self.i : end + 1]
                if any(mark in body for mark in ("$(", "`", "<(", ">(")):
                    self.line.substitution = True
                    self._record(embedded_bodies(body[2:-1]), self.i, end)
                self.dynamic = True
                self._add(body, True)
                self.i = end + 1
                continue
            if char == "`":
                end = _backtick_end(text, self.i + 1)
                self.line.substitution = True
                self.dynamic = True
                self._record([("`", text[self.i + 1 : end].replace("\\`", "`"))], self.i, end)
                self._add(text[self.i : end + 1], True)
                self.i = end + 1
                continue
            if pair in ("<(", ">("):
                # Text to the shell; read as commands all the same (the safe side).
                end = closing_paren(text, self.i + 2)
                end = end if end >= 0 else len(text)
                self._record([(char, text[self.i + 2 : end])], self.i, end)
            if pair == "$(":
                self.line.substitution = True  # ``$((…))``: arithmetic
                self.dynamic = True
            elif char == "$" and re.match(r"[A-Za-z0-9_{@*#?$!-]", text[self.i + 1 : self.i + 2]):
                self.dynamic = True
            self._add(char, True)
            self.i += 1

    def _dollar(self, nxt: str) -> None:
        text = self.text
        if nxt == "(":
            # ``$(…)`` / ``$((…))``: the parenthesis is then read as an operator,
            # so the commands inside are judged too.
            self.line.substitution = True
            self.dynamic = True
            if not is_arithmetic(text, self.i):
                end = closing_paren(text, self.i + 2)
                end = end if end >= 0 else len(text)
                self._record([("$", text[self.i + 2 : end])], self.i, end)
            self._add("$", True)
            self.i += 1
            self._end_word()
            return
        if nxt == "{":
            # ``${…}``, nested ones included: a command substitution or a
            # backtick anywhere inside (``${X:-$(git push)}``) runs a command.
            end = brace_end(text, self.i + 1)
            if end < 0:
                raise _Opaque("unbalanced-brace")
            body = text[self.i : end + 1]
            if "$(" in body or "`" in body or "<(" in body or ">(" in body:
                self.line.substitution = True
                self._record(embedded_bodies(body[2:-1]), self.i, end)
            self._add(body, True)
            self.dynamic = True
            self.i = end + 1
            return
        if nxt == "'":
            # ``$'…'``: ANSI-C quoting, which can spell anything with escapes
            # (``$'\''`` is one quote, not the end of it).
            end = quote_end(text, self.i) - 1
            if end < 0:
                raise _Opaque("unbalanced-quote")
            body = text[self.i + 2 : end]
            if "\\" in body:
                self.dynamic = True
            self.quoted = True
            self._add(body, True)
            self.i = end + 1
            return
        if nxt and re.match(r"[A-Za-z0-9_@*#?$!-]", nxt):
            self.dynamic = True
        self._add("$", True)
        self.i += 1

    def _redirect(self, operator: str) -> None:
        # A word made only of digits right before the operator is its fd.
        if self.in_word and "".join(self.word).isdigit():
            self.word = []
            self.mask = []
            self.in_word = False
        else:
            self._end_word()
        if self.pending_redirect is not None:
            raise _Opaque("syntax")
        self.pending_redirect = operator
        self.i += len(operator)
        self.redirect_at = self.i
        # ``>&2`` / ``2>&1``: a duplicated descriptor, not a file.
        match = re.match(r"\s*(\d+|-)(?=$|[\s;&|)])", self.text[self.i :])
        if operator.endswith("&") and match:
            self.pending_redirect = None
            self.i += match.end()

    def _angle(self, char: str, nxt: str) -> None:
        text = self.text
        if nxt == "(":
            # A process substitution: one word, a pipe's name only the shell
            # knows.  Its body is read as commands of the line by the caller.
            end = closing_paren(text, self.i + 2)
            if end < 0:
                raise _Opaque("unbalanced-paren")
            self.line.substitution = True
            self.dynamic = True
            self._record([(char, text[self.i + 2 : end])], self.i, end)
            self.process = text[self.i : end + 1] if not self.in_word else None
            self._add(text[self.i : end + 1], True)
            self.i = end + 1
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
    # More runtimes that take code on the command line or from stdin (security
    # review 7, H7-2): none has a table, so their inline code is never readable.
    "elixir": frozenset({"-e", "--eval"}),
    "iex": frozenset({"-e", "--eval"}),
    "escript": frozenset(),
    "gdb": frozenset({"-ex", "-iex", "-x", "--eval-command", "--command", "--init-command",
                      "--init-eval-command", "-ix"}),
    "lldb": frozenset({"-o", "-O", "-s", "-S", "-k", "-K", "--one-line", "--source",
                       "--one-line-before-file", "--source-before-file"}),
    "clojure": frozenset({"-e", "--eval"}),
    "clj": frozenset({"-e", "--eval"}),
    "bb": frozenset({"-e", "--eval"}),
    "racket": frozenset({"-e", "--eval"}),
    "guile": frozenset({"-c"}),
    "sbcl": frozenset({"--eval", "--load", "--script"}),
    "ghci": frozenset({"-e"}),
    "runghc": frozenset(),
    "runhaskell": frozenset(),
    "expect": frozenset({"-c"}),
    "octave": frozenset({"--eval"}),
    "matlab": frozenset({"-r", "-batch"}),
    "raku": frozenset({"-e"}),
    "tsx": frozenset({"-e", "--eval"}),
    "ts-node": frozenset({"-e", "--eval", "-p", "--print"}),
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
#: Short options of an HTTP client: ``(take a value, take none)``.  A word of
#: several (``-sSLo f``, ``-qO-``) is read as those options one by one.
_HTTP_SHORT: dict[str, tuple[str, str]] = {
    "curl": ("AbcCdDeEFHKmoPQrTtuUwXxYyz", "0123456#:aBfgGIiJjkLlMNnOpqRsSvVZ"),
    "wget": ("aBDeiIloOPQtTUwXARn", "46bcdEFHhkKLmNpqrSvVx"),
}
_HTTP_SHORT["curlie"] = _HTTP_SHORT["curl"]
#: curl's long options that take no value (what follows one is a URL).
_CURL_LONG_FLAGS: frozenset[str] = frozenset({
    "--remote-name", "--remote-name-all", "--remote-header-name", "--silent", "--show-error",
    "--location", "--location-trusted", "--fail", "--fail-with-body", "--insecure",
    "--compressed", "--verbose", "--include", "--head", "--progress-bar",
    "--no-progress-meter", "--globoff", "--create-dirs", "--http1.1", "--http2",
    "--retry-all-errors", "--ssl-reqd", "--get", "--no-buffer", "--disable"})
_HTTP_SHORT["wget2"] = _HTTP_SHORT["wget"]


def _split_short_flags(program: str, rest: list[str],
                       rest_dyn: list[bool]) -> tuple[list[str], list[bool]]:
    """``rest`` with each word of combined short options (``-so``, ``-fsSLo``,
    ``-qOfile``) split into one word per option, the value apart.  A word with a
    letter the table does not know is kept as it is."""
    valued, plain = _HTTP_SHORT.get(program, ("", ""))
    if not valued:
        return rest, rest_dyn
    words: list[str] = []
    dyns: list[bool] = []
    for index, (word, dyn) in enumerate(zip(rest, rest_dyn, strict=False)):
        if word == "--":
            words.extend(rest[index:])
            dyns.extend(rest_dyn[index:])
            break
        if not word.startswith("-") or word.startswith("--") or len(word) < 3:
            words.append(word)
            dyns.append(dyn)
            continue
        split: list[tuple[str, bool]] = []
        for position, letter in enumerate(word[1:], start=1):
            if letter in valued:
                split.append(("-" + letter, False))
                if word[position + 1:]:
                    split.append((word[position + 1:], dyn))
                break
            if letter not in plain:
                split = [(word, dyn)]
                break
            split.append(("-" + letter, False))
        words.extend(w for w, _ in split)
        dyns.extend(d for _, d in split)
    return words, dyns


#: Programs that send a name or a host out (DNS lookups, pings): a send.
DNS_SENDERS: frozenset[str] = frozenset(
    {"dig", "nslookup", "host", "ping", "ping6", "traceroute", "traceroute6", "tracepath",
     "mtr", "whois", "drill", "kdig", "delv", "arping", "nping", "hping3"}
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
#: Database-client meta commands and statements that leave the database: a
#: shell escape, a file written or read, an extension (code) loaded.
_DB_SHELL_ESCAPE = re.compile(
    r"(^|[\s;'\"])\.(shell|system|sh|output|once|import|read|load|backup|save|open|"
    r"excel|log|cd|clone|archive)\b|"
    r"\\(!|o|w|i|ir|include|include_relative|copy|g|gx|gexec|lo_export|lo_import|"
    r"T|tee|P|pager|\.|source|setenv|cd|s|e|ef|ev)(\s|$|['\";])|"
    r"(^|[\s;'\"])(system|tee|pager|source|edit|nopager)\s|"
    r"\b(into\s+(out|dump)file|load_file\s*\(|load\s+data|from\s+program|to\s+program|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|copy\s+\S+\s+(to|from)\s+'|"
    r"load_extension\s*\(|readfile\s*\(|writefile\s*\(|edit\s*\(|attach\s+database|"
    r"read_csv|read_text|read_blob|copy\s+.*\s+to\s+)",
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
    {"rm", "rmdir", "unlink", "mv", "cp", "ln", "link", "touch", "mkdir", "chmod", "chown",
     "chgrp",
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
DESTINATION_ONLY: frozenset[str] = frozenset({"cp", "rsync", "install", "ln", "link"})
#: Shell reserved words that may start a simple command of a compound one.
RESERVED_WORDS: frozenset[str] = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "for", "case",
     "esac", "select", "in", "coproc", "function", "{", "}"}
)
#: Programs that print the content of the files they are given: one whose name
#: only the shell knows may be any secret.
CONTENT_READERS: frozenset[str] = frozenset(
    {"cat", "tac", "head", "tail", "less", "more", "most", "bat", "batcat", "nl", "od", "xxd",
     "hexdump", "strings", "grep", "egrep", "fgrep", "rg", "ag", "ack", "ugrep", "sed", "gsed",
     "awk", "gawk", "mawk", "nawk", "cut", "sort", "uniq", "diff", "cmp", "base64", "base32",
     "md5sum", "sha1sum", "sha256sum", "sha512sum", "shasum", "b2sum", "jq", "yq", "column",
     "fold", "rev", "comm", "join", "paste", "expand", "unexpand", "fmt", "pr", "view", "vim",
     "vi", "nvim", "nano", "emacs", "code", "open", "xdg-open", "iconv", "dos2unix", "zcat",
     "bzcat", "xzcat", "zless", "zgrep", "look", "split"}
)
#: Content readers whose first operand is a pattern or a program, not a file
#: (unless one of the options in :data:`_PATTERN_GIVEN` supplied it).
_PATTERN_FIRST: frozenset[str] = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ugrep", "zgrep", "sed", "gsed", "awk",
     "gawk", "mawk", "nawk", "jq", "yq", "look"}
)
_PATTERN_GIVEN: frozenset[str] = frozenset(
    {"-e", "-f", "--regexp", "--file", "--expression", "--source", "--from-file"}
)
#: Options of content readers that take the next word as their value.
_READER_VALUED: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "-s", "--sleep-interval", "--pid"}),
    "grep": frozenset({"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "--regexp", "--file",
                       "--max-count", "--context", "--after-context", "--before-context",
                       "--include", "--exclude", "--exclude-dir", "--exclude-from",
                       "--label", "--binary-files", "--devices", "--directories"}),
    "rg": frozenset({"-e", "-f", "-m", "-A", "-B", "-C", "-g", "--glob", "--iglob", "-t", "-T",
                     "--type", "--type-not", "--type-add", "-M", "--max-columns", "-j",
                     "--threads", "--max-depth", "-d", "--maxdepth", "-r", "--replace",
                     "--pre", "--pre-glob", "--ignore-file", "-E", "--encoding", "--sort",
                     "--sortr", "--colors", "--context-separator", "--max-filesize",
                     "--engine", "--regexp", "--file", "--max-count", "--context",
                     "--after-context", "--before-context", "--path-separator"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields", "--characters",
                      "--bytes", "--output-delimiter"}),
    "sort": frozenset({"-k", "-t", "-S", "-o", "-T", "--key", "--field-separator",
                       "--buffer-size", "--output", "--temporary-directory", "--parallel",
                       "--batch-size", "--compress-program", "--files0-from"}),
    "uniq": frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}),
    "sed": frozenset({"-e", "-f", "--expression", "--file", "-l", "--line-length"}),
    "awk": frozenset({"-f", "-v", "-F", "--file", "--assign", "--field-separator", "-e",
                      "--source", "-i", "--include", "-l", "--load"}),
    "jq": frozenset({"-f", "--from-file", "--indent", "-L", "--tab"}),
    "diff": frozenset({"-U", "-C", "--unified", "--context", "-x", "-X", "--exclude",
                       "--exclude-from", "-I", "--ignore-matching-lines", "--label", "-L",
                       "-W", "--width", "-F", "--show-function-line"}),
    "xxd": frozenset({"-s", "-l", "-c", "-g", "-o", "-n", "-C"}),
    "od": frozenset({"-j", "-N", "-t", "-A", "-w", "-S"}),
    "hexdump": frozenset({"-s", "-n", "-e", "-f"}),
    "base64": frozenset({"-w", "--wrap"}),
    "column": frozenset({"-s", "-t", "-c", "-o", "-N", "-R", "-H", "-E", "-W"}),
    "fold": frozenset({"-w", "-b", "--width"}),
    "nl": frozenset({"-b", "-i", "-n", "-s", "-v", "-w", "-l", "-d", "-h", "-f"}),
    "strings": frozenset({"-n", "-t", "-e", "-T", "--bytes", "--radix", "--encoding"}),
    "split": frozenset({"-l", "-b", "-C", "-n", "-a", "--additional-suffix"}),
    "iconv": frozenset({"-f", "-t", "--from-code", "--to-code", "-o", "--output"}),
}
for _alias, _base in (("egrep", "grep"), ("fgrep", "grep"), ("zgrep", "grep"),
                      ("ugrep", "grep"), ("ag", "grep"), ("ack", "grep"), ("gsed", "sed"),
                      ("gawk", "awk"), ("mawk", "awk"), ("nawk", "awk"), ("yq", "jq")):
    _READER_VALUED[_alias] = _READER_VALUED[_base]


def _reader_operands(
    program: str, rest: list[str], rest_dyn: list[bool]
) -> list[tuple[str, bool]]:
    """The file operands of a content reader (its pattern or program left out)."""
    valued = _READER_VALUED.get(program, frozenset())
    operands: list[tuple[str, bool]] = []
    index = 0
    only = False
    while index < len(rest):
        word = rest[index]
        dyn = rest_dyn[index] if index < len(rest_dyn) else True
        if only:
            operands.append((word, dyn))
        elif word == "--":
            only = True
        elif word in ("--arg", "--argjson", "--slurpfile", "--rawfile") and program in (
            "jq", "yq"
        ):
            index += 3
            continue
        elif word in valued:
            index += 2
            continue
        elif word.startswith("-") and len(word) > 1 and not dyn:
            pass
        else:
            operands.append((word, dyn))
        index += 1
    if program in _PATTERN_FIRST and operands and not any(
        w in _PATTERN_GIVEN or w.startswith(tuple(f"{f}=" for f in _PATTERN_GIVEN if
                                                  f.startswith("--")))
        for w in rest
    ):
        operands = operands[1:]
    return [(w, d) for w, d in operands if w != "-"]
#: Options whose value is a name pattern, not a path (``find -name '*.pem'``).
_PATTERN_OPTIONS: frozenset[str] = frozenset(
    {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
     "-lname", "-ilname", "--include", "--exclude", "--exclude-dir", "-g", "--glob", "--iglob",
     "-e", "--regexp", "--type-add"}
)
_PATTERN_PREFIXES: tuple[str, ...] = (
    "--include=", "--exclude=", "--exclude-dir=", "--glob=", "--iglob=", "--regexp=",
)
#: Options that make a program write a file (or a directory) at their value.
#: ``=`` forms and, for short ones, attached values (``-oout.txt``) count too.
OUTPUT_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset({"--basetemp", "--junitxml", "--junit-xml", "--resultlog",
                         "--result-log", "--log-file", "--report-log", "--html",
                         "--json-report-file", "--cov-report", "--debug", "-o",
                         "--override-ini", "--confcutdir"}),
    "tree": frozenset({"-o"}),
    "sort": frozenset({"-o", "--output", "-T", "--temporary-directory"}),
    "jest": frozenset({"--outputFile", "--coverageDirectory", "--cacheDirectory"}),
    "vitest": frozenset({"--outputFile", "--outputFile.json", "--coverage.reportsDirectory"}),
    "tsc": frozenset({"--outDir", "--outFile", "--out", "--declarationDir",
                      "--tsBuildInfoFile"}),
    "esbuild": frozenset({"--outfile", "--outdir"}),
    "gcc": frozenset({"-o"}), "cc": frozenset({"-o"}), "clang": frozenset({"-o"}),
    "g++": frozenset({"-o"}), "clang++": frozenset({"-o"}), "c++": frozenset({"-o"}),
    "rustc": frozenset({"-o", "--out-dir"}),
    "cargo": frozenset({"--target-dir", "--out-dir", "--artifact-dir"}),
    "go": frozenset({"-o", "-coverprofile", "-cpuprofile", "-memprofile", "-blockprofile",
                     "-mutexprofile", "-trace", "-outputdir"}),
    "pandoc": frozenset({"-o", "--output"}),
    "mypy": frozenset({"--cache-dir", "--html-report", "--txt-report", "--xml-report",
                       "--junit-xml", "--linecount-report", "--any-exprs-report",
                       "--cobertura-xml-report", "--lineprecision-report",
                       "--xslt-html-report", "--xslt-txt-report", "--linecoverage-report"}),
    "ruff": frozenset({"--cache-dir", "--output-file", "-o"}),
    "eslint": frozenset({"-o", "--output-file", "--cache-location"}),
    "prettier": frozenset({"--cache-location"}),
    "coverage": frozenset({"-o", "--data-file", "-d", "--directory"}),
    "openssl": frozenset({"-out", "-keyout"}),
    "pip": frozenset({"-t", "--target", "--prefix", "--root", "-d", "--dest", "--log",
                      "--src", "--cache-dir", "--report"}),
    "uv": frozenset({"-o", "--output-file", "--target", "--prefix", "--cache-dir"}),
    "pip-compile": frozenset({"-o", "--output-file"}),
    "javac": frozenset({"-d", "-s", "-h"}),
    "terraform": frozenset({"-out", "-state", "-state-out", "-backup"}),
    "pyinstaller": frozenset({"--distpath", "--workpath", "--specpath"}),
    "npm": frozenset({"--pack-destination", "--cache"}),
    "ffmpeg": frozenset({"-passlogfile"}),
    "ssh-keygen": frozenset({"-f"}),
}
#: Options whose value is a file the program reads (security review 3, N4):
#: ``(long names, short letters)``.  The value is judged as a read (a secret is
#: ``secret_send``); long names count in any abbreviation.
FILE_READ_OPTIONS: dict[str, tuple[frozenset[str], str]] = {
    "diff": (frozenset({"--from-file", "--to-file", "--exclude-from"}), "X"),
    "date": (frozenset({"--file", "--reference"}), "fr"),
    "touch": (frozenset({"--reference"}), "r"),
    "file": (frozenset({"--magic-file", "--files-from"}), "mf"),
    "grep": (frozenset({"--file", "--exclude-from"}), "f"),
    "egrep": (frozenset({"--file", "--exclude-from"}), "f"),
    "fgrep": (frozenset({"--file", "--exclude-from"}), "f"),
    "rg": (frozenset({"--file", "--ignore-file"}), "f"),
    "sort": (frozenset({"--files0-from", "--random-source"}), ""),
    "wc": (frozenset({"--files0-from"}), ""),
    "du": (frozenset({"--files0-from", "--exclude-from"}), "X"),
    "jq": (frozenset({"--from-file", "--rawfile", "--slurpfile", "--library-path"}), "fL"),
    "tar": (frozenset({"--files-from", "--exclude-from"}), "TX"),
    "tree": (frozenset({"--fromfile"}), ""),
    "chmod": (frozenset({"--reference"}), ""),
    "chown": (frozenset({"--reference"}), ""),
}
#: Programs whose command-running options (``shell_remember.EXEC_FLAGS``) are
#: checked before their own handler runs (security review 3, N10).
_EXEC_FLAG_FAMILIES: frozenset[str] = frozenset(
    {"rsync", "zip", "sort", "npm", "pnpm", "yarn", "bun", "cargo", "go", "kubectl", "helm"}
)
#: Of those, the ones whose file is a list of further names to read: what is
#: then read is known to nobody (``sort --files0-from=-``).
_NAME_LIST_OPTIONS: frozenset[str] = frozenset({"--files0-from", "--files-from", "--fromfile"})
#: Short letters of the same kind, per program.
_NAME_LIST_LETTERS: dict[str, str] = {"file": "f", "tar": "T"}
#: jq's options that take a name and then a file.
_TWO_VALUE_OPTIONS: frozenset[str] = frozenset({"--rawfile", "--slurpfile"})
#: Of :data:`OUTPUT_FLAGS`, those that take their value only as ``--flag=value``.
_EQUALS_ONLY_FLAGS: frozenset[str] = frozenset({"--debug"})
#: Long ``--flag=value`` options any program may use to name a file it writes.
GENERIC_OUTPUT_FLAGS: frozenset[str] = frozenset(
    {"--output", "--output-file", "--outfile", "--out-file", "--output-dir", "--outdir",
     "--out-dir", "--output-directory", "--log-file", "--logfile", "--basetemp", "--junitxml",
     "--target-dir", "--cache-dir", "--report-file", "--save", "--dump", "--export"}
)
#: Options that point a program at another directory to run in.
DIR_FLAGS: dict[str, frozenset[str]] = {
    "make": frozenset({"-C", "--directory", "-f", "--file", "--makefile", "-I",
                       "--include-dir"}),
    "gmake": frozenset({"-C", "--directory", "-f", "--file", "--makefile"}),
    "npm": frozenset({"--prefix", "-C", "--cwd"}),
    "pnpm": frozenset({"-C", "--dir", "--prefix"}),
    "yarn": frozenset({"--cwd"}),
    "bun": frozenset({"--cwd"}),
    "npx": frozenset({"--prefix"}),
    "cargo": frozenset({"--manifest-path", "-C"}),
    "just": frozenset({"-d", "--working-directory", "-f", "--justfile"}),
    "task": frozenset({"-d", "--dir", "-t", "--taskfile"}),
    "ninja": frozenset({"-C", "-f"}),
    "go": frozenset({"-C", "-modfile"}),
    "uv": frozenset({"--directory", "--project"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "mvn": frozenset({"-f", "--file"}),
    "gradle": frozenset({"-p", "--project-dir", "-b", "--build-file", "-c",
                         "--settings-file"}),
    "tox": frozenset({"-c", "--conf", "--workdir"}),
    "pytest": frozenset({"--rootdir", "-c", "--config-file", "--confcutdir"}),
    "docker": frozenset({"-f", "--file"}),
    "terraform": frozenset({"-chdir"}),
    "bazel": frozenset({"--output_base", "--output_user_root"}),
}


def _chdir_of(judge: Any, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> Path | None:
    """``sudo -D dir`` / ``--chdir=dir`` / ``run0 -D dir``: where the command runs."""
    for index, word in enumerate(rest):
        if word in ("-D", "--chdir") and index + 1 < len(rest):
            if rest_dyn[index + 1]:
                return None
            return _resolve(rest[index + 1], cwd, judge.ctx)
        if word.startswith("--chdir="):
            return None if rest_dyn[index] else _resolve(word.split("=", 1)[1], cwd, judge.ctx)
    return cwd


def _ps_shows_environment(rest: list[str]) -> bool:
    """``ps e``, ``ps auxe``, ``ps eww``, ``ps -E``: every process's environment."""
    skip = False
    for word in rest:
        if skip:
            skip = False
            continue
        if word.startswith("--"):
            if word in ("--format", "--sort", "--pid", "--ppid", "--user", "--group"):
                skip = True
            continue
        if word.startswith("-"):
            if "E" in word[1:]:
                return True
            if word[-1:] in ("o", "O", "p", "t", "u", "U", "g", "G", "C", "q"):
                skip = True
            continue
        if re.fullmatch(r"[A-Za-z]+", word):
            if "e" in word:
                return True
            if word[-1:] in ("o", "O", "p", "t", "U", "q"):
                skip = True
    return False
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
#: Git subcommands that only read the repository (``git -C <elsewhere> <one>``
#: is not a change outside the project).
GIT_READ_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "diff", "log", "show", "blame", "annotate", "ls-files", "ls-tree", "rev-parse",
     "rev-list", "describe", "shortlog", "merge-base", "show-ref", "show-branch", "cat-file",
     "count-objects", "check-ignore", "check-attr", "check-mailmap", "name-rev", "cherry",
     "whatchanged", "version", "help", "grep", "for-each-ref", "diff-files", "diff-index",
     "diff-tree", "range-diff", "var", "verify-commit", "verify-tag", "fsck", "ls-remote"}
)
#: ``git -c key=value`` keys that change nothing about what runs.
_GIT_HARMLESS_CONFIG = re.compile(
    r"^(user\.(name|email|signingkey)|color\..*|core\.quotepath|core\.autocrlf|"
    r"commit\.gpgsign|tag\.gpgsign|init\.defaultbranch|advice\..*|log\..*|diff\.(renames|"
    r"algorithm|mnemonicprefix|noprefix|colormoved)|merge\.(conflictstyle|ff)|pull\.(rebase|ff)|"
    r"push\.(default|autosetupremote)|safe\.directory|i18n\..*|gc\.auto|"
    r"maintenance\.auto|rebase\.autostash|status\..*|column\..*|blame\..*|"
    r"protocol\.version|http\.version)="
    # An editor or pager that is exactly a program that does nothing (``git -c
    # core.editor=true rebase --continue``): nothing else runs.  Any other value
    # of these keys is a command line (security review 6: now asked in every mode).
    r"|^(core\.editor|sequence\.editor)=(true|:)$|^core\.pager=(cat|less|more)?$",
    re.IGNORECASE,
)
#: git subcommands that write work-tree files out of history (security review 4, H6).
_GIT_MATERIALISE: frozenset[str] = frozenset(
    {"switch", "checkout", "merge", "rebase", "cherry-pick", "am", "pull", "checkout-index",
     "revert", "restore", "apply"}  # apply: review 5 re-check (a patch may write control files)
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
    """Where a path argument lands, symlinks followed; ``None`` when unknown.

    The word is taken as the shell passes it, spaces included (security review
    4, M1): ``'notes.txt '`` is not ``notes.txt``.
    """
    text = raw
    if not text.strip() or "$" in text or "`" in text:
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


_GLOB_CHARS = re.compile(r"[*?\[]")
#: An expansion inside a word: ``${…}``, ``$NAME``, ``$1``, ``$(``, a backtick.
_EXPANSION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?$!-]|\$\(|`")
#: Names that make a globbed path a credential (or snowpea's home) when a
#: wildcard may stand for them.
_GLOB_SAMPLES: tuple[str, ...] = (
    ".ssh", ".gnupg", ".aws", ".docker", ".kube", ".azure", ".config", "gh", "gcloud", "hub",
    ".password-store", ".terraform.d", ".1password", "op", ".snowpea", ".netrc", ".npmrc",
    ".pypirc", ".git-credentials", ".pgpass", ".my.cnf", ".vault-token", ".boto", ".s3cfg",
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials", "credentials.json", "credentials.toml", "token", "key.pem", "server.key",
    "secrets.json", "secrets.yaml", "service-account.json", "hosts.yml", "config.json",
    "settings.json", "delegation.db", "ledger.jsonl", "jarvis", "mcp-oauth.json", "environ",
    "cmdline", "mem", "proc", "self", "shadow", "master.key", "access_tokens.db",
    ".codex", "auth.json", ".claude", ".credentials.json", ".claude.json", ".gemini",
    "oauth_creds.json", ".bash_history", ".zsh_history", ".python_history", ".mozilla",
    "snowpea-secrets", "keyrings",
)
#: Most concrete paths one glob is expanded into before it counts as unknown.
GLOB_LIMIT = 512


def _has_glob(word: str) -> bool:
    return bool(_GLOB_CHARS.search(word))


def _strip_expansions(word: str) -> str:
    """``~/.snow${X}pea`` -> ``~/.snowpea``: the literal text a dynamic word keeps."""
    return _EXPANSION.sub("", word)


def _glob_variants(word: str, samples: Iterable[str], dotglob: bool) -> list[str] | None:
    """Concrete paths a glob may stand for, as far as secrets go; ``None`` past the limit.

    Each wildcard component is replaced by every sample name it matches (a
    leading dot only by a pattern that starts with one, unless ``dotglob``)
    and by its own literal remainder (the "anything else" case).  A last
    component made only of wildcards (``*``) matches no file name of its own,
    so ``rm -rf dist/*`` is not read as touching a key; as a directory
    (``/home/*/.snowpea``) it matches every sample.
    """
    import fnmatch
    import itertools

    sample_list = tuple(dict.fromkeys(samples))
    options: list[list[str]] = []
    parts = word.split("/")
    for position, part in enumerate(parts):
        if not _has_glob(part):
            options.append([part])
            continue
        literal = re.sub(r"\[[^\]]*\]|[*?]", "", part)
        found = [literal]
        pure = not literal
        last = position == len(parts) - 1
        explicit_dot = part.startswith((".", "[."))
        for sample in sample_list:
            if sample.startswith(".") and not (explicit_dot or dotglob):
                continue
            if pure and last and not sample.startswith("."):
                continue  # ``dist/*``: no file name of its own
            if fnmatch.fnmatchcase(sample, part):
                found.append(sample)
        options.append(list(dict.fromkeys(found)))
    total = 1
    for choice in options:
        total *= len(choice)
        if total > GLOB_LIMIT:
            return None
    return ["/".join(combo) for combo in itertools.product(*options)]


#: Device files a redirection may target without writing anything of anyone's.
_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"})


def _harmless_target(path: Path) -> bool:
    return str(path) in _DEVICES or str(path).startswith("/dev/fd/")


def _git_plumbing(path: Path) -> bool:
    """``<repo>/.git`` itself, its ``config*`` or anything under a ``hooks/`` in it.

    Writing them makes later git commands (``git status``: ``core.fsmonitor``,
    ``git diff``: ``diff.external``, any commit: a hook) run what was written.
    """
    parts = path.parts
    for index, part in enumerate(parts):
        if part != ".git":
            continue
        rest = parts[index + 1 :]
        if not rest:
            return True
        if any(name == "hooks" or name.startswith("config") for name in rest):
            return True
        # Security review 4, M7: borrowed objects, grafts and replace refs change
        # what history every later git command reads.
        if rest[:3] == ("objects", "info", "alternates") or rest[:2] == ("info", "grafts") or (
            rest[:2] == ("refs", "replace")
        ):
            return True
    return False


#: Folders whose files decide what an agent CLI may do unasked (permission
#: rules, hooks, commands, MCP servers): writing them is ``never`` for every
#: tool and redirection (security review 3, N6).  Matched as path parts
#: anywhere (the project's own and the user's home alike).
_CLI_CONTROL_DIRS: tuple[tuple[str, ...], ...] = (
    (".claude", "hooks"),
    (".claude", "commands"),
    (".claude", "agents"),
    (".claude", "skills"),
    (".claude", "plugins"),
    (".codex",),
    (".cursor",),
    (".gemini",),
    (".agy",),
    (".config", "claude"),
    (".config", "cursor"),
    (".config", "codex"),
)
#: The agent CLIs' own folders: writing one as a whole (``cp -r x .claude``) is control.
_CLI_CONTROL_TOPS: frozenset[str] = frozenset({".claude", ".codex", ".cursor", ".gemini", ".agy"})
#: Single files of the same kind.
_CLI_CONTROL_NAMES: frozenset[str] = frozenset(
    {".mcp.json", ".claude.json", ".cursorrules", ".cursorignore", ".geminiignore"}
)


def cli_control_path(path: Path, user_home: Path | None = None) -> bool:
    """``path`` is an agent CLI's permission, hook or config file (N6)."""
    parts = path.parts
    if path.name in _CLI_CONTROL_NAMES:
        return True
    for index, part in enumerate(parts[:-1]):
        if part == ".claude" and index == len(parts) - 2:
            name = parts[-1]
            if name.startswith("settings") and name.endswith(".json"):
                return True
    for run in _CLI_CONTROL_DIRS:
        width = len(run)
        for index in range(len(parts) - width + 1):
            if tuple(parts[index : index + width]) == run:
                return True
    del user_home  # the same names count everywhere, not only under the home
    return False


def git_control_path(path: Path) -> bool:
    """A write that turns later git commands into code (security review 3, N5).

    Attribute and config files git reads (``.gitattributes``, ``.gitconfig``,
    ``.git/info/attributes``), any ``HEAD`` outside a ``.git`` folder, and a
    ``config`` / ``objects`` / ``refs`` entry that would make a folder a bare
    repository (a ``config``/``HEAD`` next to ``objects``/``refs``, or anything
    under an ``objects``/``refs`` folder next to a ``HEAD``/``config``).
    """
    parts = path.parts
    if path.name in (".gitattributes", ".gitconfig", ".gitmodules"):
        return True
    if ".git" in parts:
        rest = parts[parts.index(".git") + 1 :]
        return rest[:2] == ("info", "attributes")
    if path.name in ("HEAD", "commondir", "gitdir"):
        return True
    with contextlib.suppress(OSError):
        if path.name in ("config", "packed-refs") and any(
            (path.parent / sibling).exists() for sibling in ("objects", "refs", "HEAD")
        ):
            return True
        for index in range(1, len(parts)):
            if parts[index] in ("objects", "refs"):
                folder = Path(*parts[:index])
                if any((folder / name).exists() for name in ("HEAD", "config")):
                    return True
    return False


#: ``NAME=``, ``NAME+=``, ``NAME[i]=``: an assignment, not a program.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")

#: Characters a surface would strip or that change how text reads (security
#: review 3, N3): C0 controls but tab and newline, DEL, C1 controls (U+0080 to
#: U+009F, including the 8-bit CSI/OSC introducers), ESC sequences, and the
#: Unicode bidi and invisible format controls.
_UNCLEAN = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f؜​-‏‪-‮⁠-⁤"
    r"⁦-⁩﻿]"
)


def unclean_text(value: Any, depth: int = 0) -> bool:
    """True when a string (or any string or key inside ``value``) holds such a character."""
    if isinstance(value, str):
        return bool(_UNCLEAN.search(value))
    if depth > 64:
        return True
    if isinstance(value, Mapping):
        return any(unclean_text(k, depth + 1) or unclean_text(v, depth + 1)
                   for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(unclean_text(item, depth + 1) for item in value)
    return False


def escape_text(value: Any, depth: int = 0) -> Any:
    """``value`` with every such character written out (``\\x9d``, ``\\u202e``), for a card."""
    if isinstance(value, str):
        return _UNCLEAN.sub(lambda m: repr(m.group(0))[1:-1], value)
    if depth > 64:
        return value
    if isinstance(value, Mapping):
        return {escape_text(k, depth + 1): escape_text(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [escape_text(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(escape_text(item, depth + 1) for item in value)
    return value


def _worse_level(first: str | None, second: str | None) -> str | None:
    if first is None or second is None:
        return None
    return "readlike" if "readlike" in (first, second) else "safe"


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
        #: From an external CLI's tool: never ``safe`` (D1).
        self.external = bool(request.external) or str(request.session_id or "").startswith(
            "ext:"
        )
        #: The folder a shell line runs in is known: snowpea's own shell (a fresh
        #: ``sh -c`` per call, in ``args.cwd`` or the project), or a CLI that sent
        #: its ``cwd`` with the call.
        self._cwd_known = not self.external
        #: The line is read-only by the table's knowledge, but not ``safe`` (D2).
        self.read_like = False
        #: :meth:`_table_path` accepts the snowpea home's content/own folders (only
        #: to tell a plain read of them from a write; never for a verdict).
        self._home_relaxed = False
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
        #: A tool or path read outside the project (so a read is not ``safe``).
        self.outside_read = False
        #: ``shopt -s dotglob`` / ``GLOBIGNORE=`` ran earlier on the line.
        self.dotglob = False
        #: The arguments ``xargs`` / ``find -exec`` pass come from a listing of
        #: known roots (judged recursively already), not from anywhere.
        self.stdin_from_roots = False
        #: The command just judged listed names under known, judged roots
        #: (``find src``, ``git ls-files``): a following ``| xargs`` reads those.
        self.lister_ok = False
        self.fed_by_lister = False
        #: The simple command being judged reads a file on its stdin (``< f``).
        self.stdin_redirect = False
        #: A read in the snowpea home that is never ``safe`` (logs, other areas).
        self.risky_read = False
        #: ``~/.snowpea/…`` paths the raw line names, and every path the parse
        #: judged as a read or a write: one it did not judge may be written.
        self.home_tokens: set[Path] = set()
        self.judged: set[Path] = set()
        #: The literal folders of globs (``~/.snowpea/*`` stands for every entry).
        self.glob_bases: set[Path] = set()
        roots: list[Path] = []
        for root in (home, user_home / ".snowpea"):
            if root is None:
                continue
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                resolved = Path(root).expanduser().resolve()
                if resolved not in roots:
                    roots.append(resolved)
        self.home_roots = tuple(roots)
        samples = list(_GLOB_SAMPLES)
        # Every secret name of the snowpea home, so a glob there finds it.
        samples.extend(sorted(HOME_SECRET_TOP))
        samples.extend(("state.db", "gateway-chats.json", "presets.json", "credentials-x.json"))
        for base in (home, user_home):
            if base is not None:
                samples.extend(part for part in base.parts if part not in ("/", ""))
        self.samples = tuple(dict.fromkeys(samples))
        secret_roots = [
            user_home / name
            for name in (".ssh", ".gnupg", ".aws", ".docker", ".kube", ".azure", ".config/gh",
                         ".config/gcloud", ".config/hub", ".config/op", ".password-store",
                         ".terraform.d", ".1password", ".netrc", ".npmrc", ".pypirc",
                         ".git-credentials", ".pgpass", ".my.cnf", ".vault-token", ".boto",
                         ".s3cfg", ".local/share/keyrings", "Library/Keychains",
                         ".codex/auth.json", ".claude/.credentials.json", ".claude.json",
                         ".gemini/oauth_creds.json", ".config/cursor", ".config/snowpea-secrets",
                         ".bash_history", ".zsh_history", ".python_history", ".mozilla",
                         ".config/google-chrome", ".config/chromium", ".oci", ".fly")
        ]
        self.secret_roots = (*secret_roots, Path("/proc"), Path("/root"), Path("/etc/ssh"))

    # -- paths -----------------------------------------------------------
    def _home_reachable(self) -> bool:
        """``$SNOWPEA_HOME`` lies in the user's home: a path nobody can read may be it."""
        home = self.ctx.home
        return home is None or _under(home, self.ctx.user_home)

    def _tilde_variants(self, word: str, cwd: Path | None) -> list[str] | None:
        """``~user/x`` may be the user's own home or a sibling's; ``~-`` is unknown."""
        if not word.startswith("~"):
            return [word]
        head, sep, rest = word[1:].partition("/")
        if not head:
            return [word]
        if head == "+":
            if cwd is None:
                return None
            return [str(cwd / rest) if sep else str(cwd)]
        if head == "-" or not re.fullmatch(r"[A-Za-z0-9._-]+", head):
            return None
        user_home = self.ctx.user_home
        options = [str(user_home.parent / head / rest) if sep else str(user_home.parent / head)]
        options.append(f"~/{rest}" if sep else "~")
        return list(dict.fromkeys(options))

    def _candidates(self, raw: str, cwd: Path | None) -> list[Path] | None:
        """Every place a path argument may land (globs, ``~user``); ``None`` when unknown."""
        text = raw  # never stripped: the shell keeps the spaces (M1)
        if not text.strip():
            return []
        words = [text]
        base_word: str | None = None
        if _has_glob(text):
            variants = _glob_variants(text, self.samples, self.dotglob)
            if variants is None:
                return None
            base_word = _glob_base(text)
            words = [*variants, base_word]
        found: list[Path] = []
        for word in words:
            variants_ = self._tilde_variants(word, cwd)
            if variants_ is None:
                return None
            for variant in variants_:
                path = _resolve(variant, cwd, self.ctx)
                if path is None:
                    # A relative path with no working directory: somewhere
                    # outside any project, but nothing a variable could hide.
                    self.outside_read = True
                    continue
                if word == base_word:
                    self.glob_bases.add(path)
                if path not in found:
                    found.append(path)
        return found

    def _home_content_only(self, args: Mapping[str, Any]) -> bool:
        """Every path of the call is a literal one in a home ``content``/``own`` folder."""
        try:
            from snowpea_core.tools.call_paths import call_paths

            raws = call_paths(dict(args or {}))
        except Exception:  # noqa: BLE001 - unknown is not content
            return False
        if not raws:
            return False
        for raw in raws:
            if not isinstance(raw, str) or "$" in raw or "`" in raw:
                return False
            paths = self._candidates(raw, self.ctx.workdir)
            if not paths:
                return False
            if any(self._area(path) not in ("content", "own") for path in paths):
                return False
        return True

    def _area(self, path: Path) -> str | None:
        """The snowpea-home area of ``path`` (``home_area``), the strictest for a glob folder."""
        area = home_area(path, self.home_roots)
        if path in self.glob_bases and area is not None:
            # ``~/.snowpea/*`` stands for every entry: the home's include secrets,
            # an unknown folder's may; a content or own folder's are its own.
            if area in ("root", "other"):
                return "secret"
        return area

    def _recursive_read(self, path: Path, rule: str) -> None:
        """``path`` is read with everything below it."""
        if any(_under(root, path) for root in self.home_roots):
            self.hits.never_(f"{rule}:snowpea-home-ancestor")
            self.hits.guard(SECRET, f"{rule}:secret-ancestor")
            return
        if any(_under(root, path) for root in self.secret_roots) or _under(
            self.ctx.user_home, path
        ):
            self.hits.guard(SECRET, f"{rule}:secret-ancestor")

    def _judge_read(self, path: Path, rule: str, recursive: bool) -> None:
        self.judged.add(path)
        area = self._area(path)
        if area is not None:
            self._judge_home_read(path, area, rule, recursive)
            return
        if self._is_snowpea_config(path):
            # snowpea's OWN settings only.  The wider control-file notion of review
            # 5 (a project's hooks, an agent CLI's config) is about WRITES: reading
            # one is ordinary work, and a credential among them is a secret below.
            self.hits.never_(f"{rule}:snowpea-config")
            if path.name in ("credentials.json", "token", "mcp-oauth.json"):
                self.hits.guard(SECRET, f"{rule}:snowpea-secret")
            return
        if self._outside(path):
            self.outside_read = True
        if is_secret_path(path):
            self.hits.guard(SECRET, f"{rule}:secret-path")
        elif cli_control_path(path, self.ctx.user_home):
            # A CLI's settings may hold an ``env`` block with keys: never read unasked.
            self.risky_read = True
            self.hits.risky.append(f"{rule}:cli-control-read")
        if recursive:
            self._recursive_read(path, rule)

    def _judge_home_read(self, path: Path, area: str, rule: str, recursive: bool) -> None:
        """A read in the snowpea home, by the area table (owner's rule 2026-11-16)."""
        if area == "secret":
            self.hits.never_(f"{rule}:snowpea-secret")
            if _home_secret_name(path.name) or path.name in ("token", "mcp-oauth.json"):
                self.hits.guard(SECRET, f"{rule}:snowpea-secret")
            return
        if recursive and area in ("root", "other"):
            self.hits.never_(f"{rule}:snowpea-home-recursive")
            return
        if area in ("own", "content"):
            if recursive:
                self.risky_read = True  # whatever lies below is not known here
                self.hits.risky.append(f"{rule}:snowpea-home-recursive")
            return  # read like a file in the project
        # logs (command lines), the home folder itself, anything unknown
        self.risky_read = True
        self.hits.risky.append(f"{rule}:snowpea-home-{area}")

    def dynamic_read(self, raw: str, cwd: Path | None, rule: str) -> None:
        """A file is read whose name only the shell knows (``cat $F``, ``xargs cat``).

        It may be any secret (``secret_send``), and — when ``$SNOWPEA_HOME``
        is within reach — snowpea's own token (``never``).
        """
        self.unknown_args = True
        lowered = raw.lower()
        if ".snow" in lowered or "snowpea" in lowered:
            self.hits.never_(f"{rule}:dynamic-snowpea")
        literal = _strip_expansions(raw)
        if literal.strip() and literal != raw:
            for path in self._candidates(literal, cwd) or []:
                self._judge_read(path, rule, False)
        self.hits.guard(SECRET, f"{rule}:dynamic-read")
        if self._home_reachable():
            self.hits.never_(f"{rule}:dynamic-read")
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

    def _is_snowpea_config(self, path: Path) -> bool:
        """snowpea's own settings and state, not the wider control files (for reads)."""
        try:
            from snowpea_core.tools.config_guard import is_snowpea_config_path

            return is_snowpea_config_path(str(path), home=self.ctx.home)
        except Exception:  # noqa: BLE001 - unknown means it might be
            return True

    def _outside(self, path: Path | None) -> bool:
        root = self.ctx.workdir
        if path is None or root is None:
            return True
        if _under(path, root) or _harmless_target(path):
            return False
        # Core's own working areas (a session workspace, $SNOWPEA_HOME/plans) and,
        # for reads, its content folders are not outside (see home_area).
        return home_area(path, self.home_roots) not in ("own", "content")

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
        """A path that is read: credentials are ``secret_send``.

        Anything of snowpea's own (``$SNOWPEA_HOME`` but its working areas, a
        project's ``.snowpea/settings.json``) is ``never``, for every tool,
        whether named, globbed (``~/.snowpe?/token``), ``~user``-relative, or
        inside a ``recursive`` read of an ancestor (``grep -r x ~``).  A path
        only the shell knows (``dynamic``) is :meth:`dynamic_read`.
        """
        del shell  # every reader is judged alike now
        if dynamic or "$" in raw or "`" in raw:
            self.dynamic_read(raw, cwd, rule)
            return
        paths = self._candidates(raw, cwd)
        if paths is None:
            self.dynamic_read(raw, cwd, rule)
            return
        for path in paths:
            self._judge_read(path, rule, recursive)

    def check_write_path(self, raw: str, cwd: Path | None, rule: str, dynamic: bool) -> None:
        """A path that is written or deleted.

        One only the shell knows is outside, and ``never`` when it may be
        snowpea's own (it names ``.snow…``/``snowpea``, or ``$SNOWPEA_HOME``
        lies in the user's home).  ``.git/config``, ``.git/hooks/`` and a
        ``.git`` file turn later git commands into code: every guarded class.
        """
        if dynamic or "$" in raw or "`" in raw:
            lowered = raw.lower()
            if ".snow" in lowered or "snowpea" in lowered or self._home_reachable():
                self.hits.never_(f"{rule}:dynamic-path")
            self.hits.guard(OUTSIDE, f"{rule}:unknown-path")
            return
        paths = self._candidates(raw, cwd)
        if not paths:
            if paths is None and self._home_reachable():
                self.hits.never_(f"{rule}:unknown-path")
            self.hits.guard(OUTSIDE, f"{rule}:unknown-path")
            return
        for path in paths:
            self.judged.add(path)
            area = self._area(path)
            if area in ("secret", "logs", "other", "root"):
                # Authority, secrets, logs and anything unknown in the home:
                # never delegable (the person can still approve it).
                self.hits.never_(f"{rule}:snowpea-home-{area}")
                continue
            if area == "content":
                self.hits.guard(OUTSIDE, f"{rule}:snowpea-home-content")
                continue
            if area == "own":
                continue  # plans/ and session workspaces: not outside
            if self._is_config(path):
                self.hits.never_("path:snowpea-config")
                continue
            if cli_control_path(path, self.ctx.user_home) or path.name in _CLI_CONTROL_TOPS:
                # An agent CLI's permission, hook or MCP file (or its whole folder,
                # review 4 H6): after such a write the CLI stops asking while its
                # row still says controlled (N6).
                self.hits.never_(f"{rule}:cli-control")
            if _git_plumbing(path):
                self.hits.guard_all(f"{rule}:git-plumbing")
            if git_control_path(path):
                self.hits.guard_all(f"{rule}:git-control")
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
        if request.args_escaped or unclean_text(request.args) or unclean_text(tool):
            # What runs is not what a surface shows or a cache keys on (N3).
            self.hits.guard_all("exec:opaque:control-chars")
        if lower in NEVER_TOOL_NAMES or lower.startswith("jarvis."):
            self.hits.never_("never:delegation")
        if ("config" in tags or request.reason_code == "config") and not self._home_content_only(
            request.args
        ):
            # A write into the snowpea home's content folders (skills, agents, …)
            # is tagged config too; its paths are judged by the area table below.
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
        if lower in WEB_SEND_TOOL_NAMES or name in WEB_SEND_TOOL_NAMES:
            # A URL or a query leaves the machine with whatever was put in it.
            self.hits.guard(SECRET, "tool:web-send")
            self.hits.network = True
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
        recursive = base in RECURSIVE_READ_TOOL_NAMES
        paths = _call_paths(args)
        if recursive and not writes and not paths and self.ctx.workdir is not None:
            paths = [str(self.ctx.workdir)]  # the tool's default: the project itself
        for raw in paths:
            if writes:
                self.check_write_path(raw, self.ctx.workdir, "path", False)
            else:
                self.check_read_path(raw, self.ctx.workdir, "path", recursive=recursive)

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
            if safe_rule and not hits.network and not self.risky_read:
                return Verdict("safe", (), (safe_rule,))
            if (
                self.read_like
                and not hits.network
                and not self.risky_read
                and not self.outside_read
                and not hits.risky
            ):
                return Verdict("risky", (), ("exec:read-like",), read_only=True)
            return Verdict("risky", (), tuple(hits.risky) or ("exec:command",))
        if tags == {"read"} and not hits.network:
            if self.risky_read:
                return Verdict("risky", (), tuple(hits.risky) or ("path:snowpea-home-read",))
            if self.outside_read:
                # Outside the project only a denylist would tell a secret apart.
                return Verdict("risky", (), ("path:outside-project-read",))
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
            if self.external and raw_cwd.strip().startswith("/"):
                self._cwd_known = True  # the CLI said where this call runs (Codex)
            if cwd is not None and home_area(cwd, self.home_roots) in ("secret", "root"):
                self.hits.never_("exec:snowpea-home")
        self._raw_scan(command)
        self.line(command, cwd, depth=0)
        if self.home_tokens - self.judged and not self.hits.never:
            # A ~/.snowpea path the parse did not judge as a read or a write (an
            # interpreter's code, an unknown tool's argument): it may be written.
            self.hits.guard(OUTSIDE, "exec:snowpea-home-path")
        if (
            self.hits.any()
            or self.hits.network
            or self.risky_read
            or args.get("background")
            or self.writes
            or self.unknown_args
            or self.ctx.workdir is None
            or cwd is None
            or not _under(cwd, self.ctx.workdir)
        ):
            self._home_content_written()
            return None
        try:
            level = self.inspect_line(command, cwd)
        except Exception:  # noqa: BLE001 - a failure here only costs the safe verdict
            level = None
        if level == "safe" and not self._vouched():
            level = "readlike"
        if level == "safe" and not self._programs_trusted(command):
            # Security review 4, M3: ``ls`` must be the system's, not a file in a
            # project or a world-writable folder found first on PATH.
            level = "readlike"
        if level is None:
            self._home_relaxed = True
            try:
                plain = self.inspect_line(command, cwd)
            except Exception:  # noqa: BLE001
                plain = None
            finally:
                self._home_relaxed = False
            if plain is None:
                # Not a plain inspection: it may write a home content path it names.
                self._home_content_written()
            # A plain inspection of the snowpea home's content or own folders
            # reads outside the project: ``risky`` (D4), never safe or read-like.
            return None
        if level == "readlike":
            self.read_like = True
            return None
        return "exec:inspection"

    def _programs_trusted(self, command: str) -> bool:
        """Every program of the line resolves, on the PATH the shell tool uses
        (``exec.local.sanitized_path``), to a file outside every project root and
        outside world-writable folders."""
        import shutil
        import stat as stat_module

        try:
            from snowpea_core.exec.local import project_roots, sanitized_path

            parsed = parse_line(command)
        except Exception:  # noqa: BLE001 - unknown is not trusted
            return False
        workdir = self.ctx.workdir
        search = sanitized_path(os.environ.get("PATH", ""), workdir)
        # Every known project, not only this one (security review 5, round-4 M3).
        roots = project_roots(workdir)
        for cmd in parsed.commands:
            words = [w for w in cmd.words if not _ASSIGNMENT.match(w)]
            while words and words[0] in RESERVED_WORDS:
                words = words[1:]
            if not words:
                continue
            name = words[0]
            if "/" in name:
                return False
            found = shutil.which(name, path=search)
            if found is None:
                continue  # a shell builtin (``cd``, ``pwd``, ``test``) or nothing at all
            try:
                real = Path(found).resolve()
            except (OSError, RuntimeError):
                return False
            if any(_under(real, root) for root in roots):
                return False
            for folder in (real.parent, Path(found).parent):
                with contextlib.suppress(OSError):
                    mode = folder.stat().st_mode
                    if mode & stat_module.S_IWOTH:
                        return False
        return True

    def _vouched(self) -> bool:
        """snowpea's own ``shell`` tool: a fresh ``sh -c`` per call (``exec/local.py``),
        in ``args.cwd`` or the project, with the daemon's environment — no earlier
        line can have moved it or changed ``PATH``, aliases or functions (F2).
        Any other shell (an external CLI's, an extension's, a background
        process) is never ``safe``."""
        request = self.request
        return request.tool == "shell" and not self.external and not request.jarvis_session

    def _home_content_written(self) -> None:
        """A line that is not a plain inspection may write a home content path it names."""
        if self.hits.never:
            return
        if any(self._area(path) == "content" for path in self.home_tokens):
            self.hits.guard(OUTSIDE, "exec:snowpea-home-content")

    def _raw_scan(self, command: str) -> None:
        """Checks on the raw text, whatever the parse makes of it."""
        lowered = command.lower()
        if _SNOWPEA_HOME_TEXT.search(command):
            self.hits.never_("exec:snowpea-home")
        tokens = [re.sub(r"^(\$HOME|\$\{HOME\})", "~", m.group(0))
                  for m in _SNOWPEA_HOME_TOKEN.finditer(command)]
        for root in self.home_roots:
            pattern = re.escape(str(root)) + r"(?:/[^\s'\"`;|&()<>]*)?(?![^\s'\"`;|&()<>/])"
            tokens.extend(m.group(0) for m in re.finditer(pattern, command))
        for token in tokens:
            paths = None if "$" in token[1:] else self._candidates(token, self.ctx.workdir)
            if not paths:
                self.hits.never_("exec:snowpea-home")
                continue
            for path in paths:
                if self._area(path) in ("secret", "root"):
                    self.hits.never_("exec:snowpea-home")
                self.home_tokens.add(path)
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
        for why in parsed.unsure:
            self.hits.guard_all(f"exec:opaque:{why}")
        if parsed.substitution:
            self.hits.guard_all("exec:opaque:substitution")
        if parsed.function:
            self.hits.guard_all("exec:opaque:function")
        for cmd in parsed.commands:
            self.fed_by_lister = self.lister_ok and cmd.piped_in
            self.lister_ok = False
            cwd = self.simple(cmd, cwd, depth)

    def simple(self, cmd: _Cmd, cwd: Path | None, depth: int) -> Path | None:
        """One simple command; returns the working directory after it."""
        for operator, target, unknown in cmd.redirects:
            if operator in ("<", "<&"):
                self.check_read_path(target, cwd, "redirect", dynamic=unknown)
            else:
                if target not in _DEVICES:
                    self.writes = True
                self.check_write_path(target, cwd, "redirect", unknown)
        words, dynamic = list(cmd.words), list(cmd.dynamic)
        braced = list(cmd.braced) or [False] * len(words)
        # The program's own word came out of a brace expansion (``{git,push}``):
        # what runs is spelled by the expansion, not by a word anyone wrote.
        start = 0
        while start < len(words) and (
            words[start] in RESERVED_WORDS or _ASSIGNMENT.match(words[start])
        ):
            start += 1
        if start < len(words) and start < len(braced) and braced[start]:
            self.hits.guard_all("exec:opaque:brace-program")
            return cwd
        self.stdin_redirect = any(op in ("<", "<&", "<>") for op, _, _ in cmd.redirects)
        try:
            return self.words(words, dynamic, cwd, depth, piped_in=cmd.piped_in or cmd.fed,
                              heredoc=cmd.heredoc, stdin_args=False)
        finally:
            self.stdin_redirect = False

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
        # Reserved words before the command (``then git push``, ``! rm -rf x``).
        while words and not dynamic[0] and words[0] in RESERVED_WORDS:
            if words[0] in ("case", "select", "coproc", "function"):
                hits.guard_all(f"exec:opaque:{words[0]}")
                return cwd
            if words[0] == "for":
                return cwd  # ``for name in words``: the loop's body is judged on its own
            words, dynamic = words[1:], dynamic[1:]
        # Leading NAME=value assignments.
        while words and _ASSIGNMENT.match(words[0]):
            name, _, value = words[0].partition("=")
            # ``PATH+=x`` and ``PATH[0]=x`` (bash) assign PATH too.
            name = name.rstrip("+").split("[", 1)[0]
            if _is_code_variable(name):
                hits.guard_all("exec:opaque:env-code")
            elif _SECRET_NAME.search(name):
                hits.guard(SECRET, "exec:env-secret")
            if name == "GLOBIGNORE":
                self.dotglob = True
            if not dynamic[0]:
                # ``F=~/.ssh/id_rsa; cat $F``: the value is the path that is read.
                for candidate in _path_candidates(value):
                    if _looks_like_path(candidate):
                        self.check_read_path(candidate, cwd, "exec")
            else:
                lowered = value.lower()
                if ".snow" in lowered or "snowpea" in lowered:
                    hits.never_("exec:dynamic-snowpea")
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
        if program in CONTENT_READERS:
            # A file whose name only the shell (or another program) knows.
            for word, dyn in _reader_operands(program, rest, rest_dyn):
                if dyn:
                    self.dynamic_read(word, cwd, "exec")
            if stdin_args and not self.stdin_from_roots:
                self.dynamic_read("", cwd, "exec:stdin-args")
        if "/" in head:
            # ``./deploy.sh``, ``scripts/release.py``: a script of the project's.
            self.check_read_path(head, cwd, "exec", shell=True)
            if _DEPLOY_NAME.search(program):
                hits.guard(DEPLOY, "exec:deploy-script")
        # Every argument that names a credential, wherever it sits (but not the
        # name patterns of ``find -name`` / ``--include``).
        previous = ""
        # Security review 4, H5: once the line has moved outside the project, a
        # bare name (``cat apple-notary.env``) is a path there like any other.
        moved_out = (
            cwd is not None and self.ctx.workdir is not None and not _under(cwd, self.ctx.workdir)
        )
        for word, dyn in zip(rest, rest_dyn, strict=False):
            pattern_value = previous in _PATTERN_OPTIONS or word.startswith(_PATTERN_PREFIXES)
            previous = word
            if dyn or pattern_value:
                continue
            for candidate in _path_candidates(word):
                if _looks_like_path(candidate) or (
                    moved_out and not candidate.startswith("-") and candidate == word
                ):
                    self.check_read_path(candidate, cwd, "exec", shell=True)
        if any(word in ("--force", "--force-with-lease", "--force-if-includes")
               or word.startswith("--force=") for word in rest):
            hits.guard(OUTSIDE, "exec:force-flag")
        self._output_flags(program, family, rest, rest_dyn, cwd)
        self._dir_flags(program, family, rest, rest_dyn, cwd)
        self._file_options(program, rest, rest_dyn, cwd)
        if family in _EXEC_FLAG_FAMILIES and self._runs_commands([family, *rest]):
            # ``rsync --rsync-p=…``, ``npm --script-sh=…``: any abbreviation of an
            # option that runs a command keeps the line opaque (N10), whichever
            # handler below judges the rest.
            hits.guard_all("exec:opaque:runs-commands")
        if program == "shopt" and any("dotglob" in w or "globstar" in w for w in rest):
            self.dotglob = True
        if program == "ps" and _ps_shows_environment(rest):
            hits.guard(SECRET, "exec:ps-environment")
        if program in ("vim", "vi", "nvim", "ex", "view", "gvim", "vimdiff") and any(
            w.startswith(("-c", "+", "--cmd", "-S", "-u", "-s", "-W", "-w", "--remote"))
            for w in rest
        ):
            hits.guard_all("exec:opaque:editor-commands")  # ``vim -c '!id'``
        if program in ("emacs", "emacsclient") and any(
            w.startswith(("--eval", "-l", "--load", "-f", "--funcall", "--script", "-batch",
                          "--batch", "-x"))
            for w in rest
        ):
            hits.guard_all("exec:opaque:editor-commands")
        if program in ("less", "more", "most", "man") and any(
            w.startswith(("+", "-P", "--prompt")) or w in ("-k",) for w in rest
        ):
            hits.guard_all("exec:opaque:pager-commands")  # ``less +'!id'``
        if program in ("jq", "yq", "gojq", "jaq") and any(
            re.search(r"(^|[^A-Za-z_])\$?(ENV|env)\b|\$__prog_args|input_filename", w)
            for w in rest
        ):
            hits.guard(SECRET, "exec:env-dump")
        if program == "sort" and any(
            w.startswith("--") and _long_match(w, ("--compress-program",)) is not None
            for w in rest
        ):
            hits.guard_all("exec:opaque:runs-commands")

        # -- wrappers and runners -------------------------------------
        if program in ("cd", "pushd", "chdir"):
            target = next((w for w in rest if not w.startswith("-")), None)
            if target is None:
                return self.ctx.user_home
            index = rest.index(target)
            if rest_dyn[index] or target == "-":
                return None
            # Security review 4, H5: the folder a line moves into is judged like a
            # read of it (``cd ~/.config && cd gcloud && cat credentials.db``).
            self.check_read_path(target, cwd, "exec:cd")
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
                name = word.split("=", 1)[0].rstrip("+").split("[", 1)[0]
                if _is_code_variable(name):
                    hits.guard_all("exec:opaque:env-code")
            return cwd
        if program == "printf":
            # ``printf -v NAME`` (any spelling: ``-vPATH``, ``-v`` in a cluster)
            # assigns a variable, which may be PATH or HOME for what follows (N8).
            for word in rest:
                if word == "--" or not word.startswith("-") or word == "-":
                    break
                if "v" in word[1:]:
                    hits.guard_all("exec:opaque:printf-v")
                    return cwd
        if program in ("read", "mapfile", "readarray", "getopts"):
            # They assign the names they are given (``read PATH < f``).
            names = [w.split("[", 1)[0] for w in rest if not w.startswith("-")]
            if any(_is_code_variable(name) for name in names):
                hits.guard_all("exec:opaque:env-code")
            return cwd
        if program == "hash" and any(w.startswith("-") and "p" in w for w in rest):
            # ``hash -p ./x ls``: ``ls`` now runs ./x.
            hits.guard_all("exec:opaque:hash-path")
            return cwd
        if program in ("test", "[", "[[") and any(
            w in ("-v", "-R") or ("[" in w and ("$" in w or "`" in w)) for w in rest
        ):
            # Security review 4, C2: bash evaluates an array subscript (``-v
            # 'a[$(cmd)]'``), which runs whatever the substitution says.
            hits.guard_all("exec:opaque:test-subscript")
            return cwd
        if program in ("true", "false", ":", "test", "[", "[[", "echo", "printf", "sleep",
                       "wait", "exit", "return", "break", "continue", "shift",
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
            self._recurse_words(rest, rest_dyn, inner, _chdir_of(self, rest, rest_dyn, cwd),
                                depth, piped_in, heredoc, stdin_args)
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
                moved = self._recurse_words(rest, rest_dyn, inner, cwd, depth, piped_in,
                                            heredoc, stdin_args)
                if program in ("builtin", "command") and _program(inner[0]) in (
                    "cd", "pushd", "popd", "chdir"
                ):
                    return moved  # ``builtin cd /tmp`` moves the shell like ``cd`` does
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
            from_file = any(w in ("-a", "--arg-file") or w.startswith("--arg-file=")
                            for w in rest[: len(rest) - len(inner)])
            previous = self.stdin_from_roots
            self.stdin_from_roots = self.fed_by_lister and piped_in and not from_file
            try:
                self._recurse_words(rest, rest_dyn, inner, cwd, depth, False, False, True)
            finally:
                self.stdin_from_roots = previous
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
            self._awk(program, rest, rest_dyn, cwd)
            return cwd
        if program in ("sed", "gsed"):
            if any(w == "-i" or w.startswith("-i") or w.startswith("--in-place") for w in rest):
                self.writes = True
                for word, dyn in _reader_operands(program, rest, rest_dyn):
                    self.check_write_path(word, cwd, "exec", dyn or stdin_args)
                if stdin_args:
                    hits.guard(OUTSIDE, "exec:unknown-path")
            if any(w in ("-f", "--file") or w.startswith("--file=")
                   or (w.startswith("-f") and not w.startswith("--")) for w in rest):
                hits.guard_all("exec:opaque:sed-program-file")
            script = " ".join(rest)
            # ``1e id``, ``$e``: the ``e`` command right after an address runs a command
            # too (found while pinning security review 6).
            if re.search(r"(^|[;}\s/0-9$!])e(\s|$|;)|/e\b|(^|[;\s])w\s|/w\s|"
                         r"(^|[;{}\n\s0-9$/])[rRwW]\s+\S", script):
                hits.guard_all("exec:opaque:sed-program")
            return cwd
        if program in ("grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "ugrep"):
            self._search(program, rest, rest_dyn, cwd)
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
        if program in DNS_SENDERS:
            # A name looked up or a host pinged carries whatever was spelled into it.
            hits.guard(SECRET, "exec:dns-send")
            hits.network = True
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
                if piped_in or heredoc or self.stdin_redirect or any(
                    w in ("-f", "--file", "-i", "--init-command", "--queries-file", "-init",
                          "--init")
                    or w.startswith(("--file=", "--init-command=", "--queries-file="))
                    for w in rest
                ):
                    # Statements nobody can see: they may hold a shell escape.
                    hits.guard_all("exec:opaque:db-script")
                if program in ("mongo", "mongosh") and (
                    any(w in ("--eval", "--file", "-f") for w in rest)
                    or any(w.endswith(".js") for w in rest)
                ):
                    hits.guard_all("exec:opaque:inline-code")
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
                       "bunzip2", "xz", "unxz", "zstd", "lz4", "brotli"):
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
    ) -> Path | None:
        """Judge ``inner`` (a tail of ``words``) as a command of its own."""
        offset = len(words) - len(inner)
        inner_dyn = dynamic[offset:] if 0 <= offset <= len(dynamic) else [False] * len(inner)
        if depth >= 6:
            self.hits.guard_all("exec:opaque:nesting")
            return None
        return self.words(list(inner), list(inner_dyn), cwd, depth + 1, piped_in=piped_in,
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
        inner_cwd = cwd
        while index < len(rest):
            word = rest[index]
            if word in ("-S", "--split-string") or word.startswith(("-S", "--split-string=")):
                self.hits.guard_all("exec:opaque:shell-string")
                return cwd
            if word in ("-C", "--chdir") or word.startswith(("--chdir=", "-C")):
                # ``env -C dir cmd``: the command runs (and its paths resolve) there.
                if word in ("-C", "--chdir"):
                    target = rest[index + 1] if index + 1 < len(rest) else ""
                    dyn = rest_dyn[index + 1] if index + 1 < len(rest_dyn) else True
                    index += 2
                else:
                    target = word.split("=", 1)[1] if word.startswith("--") else word[2:]
                    dyn = rest_dyn[index]
                    index += 1
                inner_cwd = None if dyn else _resolve(target, inner_cwd, self.ctx)
                continue
            if word in ("-u", "--unset"):
                index += 2
                continue
            if word.startswith("-") and word != "-":
                index += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word) or word == "-":
                name = word.split("=", 1)[0]
                if _is_code_variable(name):
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
        self._recurse_words(rest, rest_dyn, inner, inner_cwd, depth, piped_in, heredoc,
                            stdin_args)
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
        git_cwd = cwd
        elsewhere: list[tuple[str, bool]] = []  # --git-dir / --work-tree values
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
            if word.startswith(("--exec-path", "--upload-pack", "--receive-pack",
                                "--config-env=")):
                hits.guard_all("exec:opaque:git-exec")
                index += 1
                continue
            if word == "-C":
                target = rest[index + 1] if index + 1 < len(rest) else ""
                dyn = rest_dyn[index + 1] if index + 1 < len(rest_dyn) else True
                git_cwd = None if dyn else _resolve(target, git_cwd, self.ctx)
                index += 2
                continue
            if word in ("--git-dir", "--work-tree") or word.startswith(("--git-dir=",
                                                                         "--work-tree=")):
                if "=" in word:
                    elsewhere.append((word.split("=", 1)[1], rest_dyn[index]))
                    index += 1
                else:
                    elsewhere.append((rest[index + 1] if index + 1 < len(rest) else "",
                                      rest_dyn[index + 1] if index + 1 < len(rest_dyn)
                                      else True))
                    index += 2
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
        args_dyn = rest_dyn[index + 1 :]
        if sub not in GIT_SUBCOMMANDS:
            hits.guard_all("exec:opaque:git-alias")
            return
        cwd = git_cwd
        if sub not in GIT_READ_SUBCOMMANDS:
            # A repository outside the project (``git -C ~/x commit``, ``--work-tree``).
            if cwd is None or self._outside(cwd):
                hits.guard(OUTSIDE, "exec:git-outside-project")
            for value, dyn in elsewhere:
                self.check_write_path(value, cwd, "exec", dyn)
        self._git_files(sub, args, args_dyn, cwd)
        staged_only = sub == "restore" and ("--staged" in args or "-S" in args) and not (
            "--worktree" in args or "-W" in args
        )
        if (sub in _GIT_MATERIALISE and not staged_only) or (
            sub == "stash" and args[:1] in (["apply"], ["pop"])
        ) or (
            sub == "read-tree" and any(w == "-u" or (w.startswith("-") and not w.startswith("--")
                                                     and "u" in w[1:]) for w in args)
        ):
            # Security review 4, H6: files come out of history, a branch or a patch
            # under names nobody sees here (``.claude/settings.local.json``,
            # ``.gitattributes``): a control file may be among them.
            hits.guard_all("exec:git-materialise")
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
        if sub in ("submodule", "send-email", "imap-send", "request-pull", "credential",
                   "fetch-pack", "http-fetch", "http-push", "daemon", "instaweb"):
            hits.network = True
        if cwd is not None and (
            sub in ("ls-files", "ls-tree")
            or (sub in ("diff", "log", "show") and "--name-only" in args)
            or (sub == "grep" and any(w in ("-l", "--files-with-matches") for w in args))
        ):
            # ``git ls-files | xargs …``: the names are the project's own files.
            self.lister_ok = self._roots_listable([(str(cwd), False)], cwd)

    def _git_files(
        self, sub: str, args: list[str], args_dyn: list[bool], cwd: Path | None
    ) -> None:
        """Files a git subcommand writes, and options that make it run something."""
        hits = self.hits

        def value_at(index: int) -> tuple[str, bool]:
            if index < len(args):
                return args[index], args_dyn[index] if index < len(args_dyn) else True
            return "", True

        index = 0
        while index < len(args):
            word = args[index]
            dyn = args_dyn[index] if index < len(args_dyn) else True
            name, eq, value = word.partition("=")
            if word == "--":
                break
            output = (
                (name.startswith("--ou") and "--output".startswith(name))
                or name == "--output"
                or (name.startswith("--output-d") and "--output-directory".startswith(name))
                or name in ("--separate-git-dir", "--checkout-index-prefix")
                or (sub == "checkout-index" and name == "--prefix")
                or (sub == "apply" and name == "--directory")
            )
            if output:
                target, tdyn = (value, dyn) if eq else value_at(index + 1)
                self.writes = True
                self.check_write_path(target, cwd, "exec", tdyn)
                index += 1 if eq else 2
                continue
            read_file = None
            if word.startswith("--"):
                read_file = _long_match(word, ("--contents", "--ignore-revs-file", "--orderfile",
                                               "--file", "--exclude-from", "--pathspec-from-file",
                                               "--stdin-paths"))
            elif sub in ("blame", "annotate") and word.startswith("-S"):
                read_file = "-S"
            elif sub in ("diff", "log", "show", "diff-tree", "diff-index", "diff-files") and (
                word.startswith("-O")
            ):
                read_file = "-O"
            elif sub == "grep" and word.startswith("-f"):
                read_file = "-f"
            if read_file is not None and read_file != "--stdin-paths":
                # A file git reads (``blame -S revs``, ``--contents F``): judged as a read (N4).
                if word.startswith("--") and eq:
                    target, tdyn = value, dyn
                elif not word.startswith("--") and len(word) > 2:
                    target, tdyn = word[2:], dyn
                else:
                    target, tdyn = value_at(index + 1)
                    index += 1
                if target != "-":
                    self.check_read_path(target, cwd, "exec:git-file-option", dynamic=tdyn)
                index += 1
                continue
            if word == "-o" and sub in ("format-patch", "archive", "bundle"):
                target, tdyn = value_at(index + 1)
                self.writes = True
                self.check_write_path(target, cwd, "exec", tdyn)
                index += 2
                continue
            if word.startswith("-o") and len(word) > 2 and sub in ("format-patch", "archive"):
                self.writes = True
                self.check_write_path(word[2:], cwd, "exec", dyn)
            if name in ("--template", "--upload-pack", "-u") and sub in ("init", "clone"):
                hits.guard_all("exec:opaque:git-template")  # hooks copied from anywhere
            if sub in ("clone", "submodule") and name in ("-c", "--config"):
                setting = value if eq else value_at(index + 1)[0]
                if not _GIT_HARMLESS_CONFIG.match(setting):
                    hits.guard_all("exec:opaque:git-config")
            if sub == "apply" and word == "--unsafe-paths":
                hits.guard(OUTSIDE, "exec:git-unsafe-paths")
            index += 1
        positional = [(w, d) for w, d in zip(args, args_dyn, strict=False)
                      if not w.startswith("-")]
        if sub == "worktree" and positional[:1] and positional[0][0] == "add":
            valued = frozenset({"-b", "-B", "--reason", "--orphan"})
            operands = _operands(args[1:], args_dyn[1:], valued)
            if operands:
                self.writes = True
                self.check_write_path(operands[0][0], cwd, "exec", operands[0][1])
        elif sub == "init":
            operands = _operands(args, args_dyn, frozenset({"-b", "--initial-branch",
                                                             "--object-format", "--shared"}))
            target, tdyn = operands[0] if operands else (".", False)
            self.writes = True
            self.check_write_path(target, cwd, "exec", tdyn)
        elif sub == "bundle" and positional[:1] and positional[0][0] == "create":
            if len(positional) > 1:
                self.writes = True
                self.check_write_path(positional[1][0], cwd, "exec", positional[1][1])
        elif sub in ("mv", "rm"):
            for target, tdyn in _operands(args, args_dyn, frozenset()):
                self.check_write_path(target, cwd, "exec", tdyn)
        elif sub == "config":
            reading = args[:1] in (["get"], ["list"], ["get-color"]) or any(
                w.startswith("--get") or w in ("-l", "--list", "--show-origin", "--show-scope",
                                               "--name-only", "--get-colorbool")
                for w in args
            )
            if any(w in ("-e", "--edit") or w == "edit" for w in args):
                hits.guard_all("exec:opaque:git-config-edit")
            elif not reading:
                keys = [w for w in args if not w.startswith("-")]
                if keys[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"]):
                    keys = keys[1:]
                if keys and not _GIT_HARMLESS_CONFIG.match(keys[0] + "="):
                    hits.guard_all("exec:opaque:git-config")
            for flag_index, word in enumerate(args):
                if word in ("-f", "--file", "--blob"):
                    target, tdyn = value_at(flag_index + 1)
                    self.check_write_path(target, cwd, "exec", tdyn)
                elif word.startswith("--file="):
                    self.check_write_path(word.split("=", 1)[1], cwd, "exec",
                                          args_dyn[flag_index])

    def _awk(self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        """An awk program: what it runs, writes (``print > "f"``) and reads (``getline <``)."""
        hits = self.hits
        if any(
            w in ("-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec")
            or w.startswith(("--file=", "--include=", "--load=", "--exec="))
            or (w.startswith(("-f", "-i", "-l", "-E")) and not w.startswith("--"))
            for w in rest
        ):
            hits.guard_all("exec:opaque:awk-program-file")
            return
        scripts: list[tuple[str, bool]] = []
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in ("-e", "--source"):
                if index + 1 < len(rest):
                    scripts.append((rest[index + 1], rest_dyn[index + 1]))
                index += 2
                continue
            if word.startswith("--source="):
                scripts.append((word.split("=", 1)[1], rest_dyn[index]))
            index += 1
        if not scripts:
            operands = _operands(rest, rest_dyn, frozenset({"-v", "-F", "--assign",
                                                            "--field-separator"}))
            if operands:
                scripts.append(operands[0])
        for script, dyn in scripts:
            if dyn:
                hits.guard_all("exec:opaque:awk-program")
                continue
            if re.search(r"system\s*\(|\|\s*getline|\|&|@load|@include|"
                         r"\b(print|printf)\b[^;{}]*\|", script):
                hits.guard_all("exec:opaque:awk-program")
            if re.search(r"\bENVIRON\b|\bPROCINFO\b", script):
                hits.guard(SECRET, "exec:env-dump")
            for match in re.finditer(
                r"\b(?:print|printf)\b[^;{}]*?>>?\s*(\"((?:[^\"\\]|\\.)*)\"|[^\s;{}]+)", script
            ):
                self.writes = True
                if match.group(2) is not None:
                    self.check_write_path(match.group(2), cwd, "exec", False)
                else:
                    hits.guard_all("exec:opaque:awk-redirect")  # a file named by a variable
            for match in re.finditer(r"\bgetline\b[^;{}]*?<\s*(\"((?:[^\"\\]|\\.)*)\"|[^\s;{}]+)",
                                     script):
                if match.group(2) is not None:
                    self.check_read_path(match.group(2), cwd, "exec")
                else:
                    self.dynamic_read(match.group(1), cwd, "exec:awk-getline")

    def _search(
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        """grep / rg and friends: a recursive search reads every file under its roots."""
        hits = self.hits
        if any(w.startswith("--") and _long_match(w, ("--pre", "--hostname-bin")) is not None
               for w in rest):
            hits.guard_all("exec:opaque:runs-commands")
        if program in ("rg", "ag", "ack", "ugrep"):
            recursive = True
        else:
            recursive = any(
                w in ("-r", "-R", "--recursive", "--dereference-recursive", "--directories=recurse")
                or (w.startswith("-") and not w.startswith("--") and ("r" in w[1:] or "R" in w[1:]))
                for w in rest
            ) or ("-d" in rest and "recurse" in rest)
        operands = _reader_operands(program, rest, rest_dyn)
        if not operands and recursive:
            operands = [(".", False)]
        for word, dyn in operands:
            self.check_read_path(word, cwd, "exec", recursive=recursive, dynamic=dyn)
        names_only = any(w in ("-l", "-L", "--files", "--files-with-matches",
                               "--files-without-match") for w in rest)
        if names_only and not any(d for _, d in operands):
            self.lister_ok = self._roots_listable(operands, cwd)

    def _roots_listable(self, roots: list[tuple[str, bool]], cwd: Path | None) -> bool:
        """Names listed under ``roots`` stay in the project and away from secrets."""
        workdir = self.ctx.workdir
        if workdir is None or not roots:
            return False
        for raw, dyn in roots:
            if dyn:
                return False
            paths = self._candidates(raw, cwd)
            if not paths:
                return False
            for path in paths:
                if not _under(path, workdir) or self._in_snowpea_home(path):
                    return False
                if any(_under(root, path) for root in self.secret_roots):
                    return False
        return True

    def _output_flags(
        self, program: str, family: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        """``--flag=FILE`` / ``-o FILE`` options that write a file (C1)."""
        spec = OUTPUT_FLAGS.get(program) or OUTPUT_FLAGS.get(family) or frozenset()
        if program in ("py.test",):
            spec = OUTPUT_FLAGS["pytest"]
        targets: list[tuple[str, bool]] = []
        index = 0
        while index < len(rest):
            word = rest[index]
            dyn = rest_dyn[index] if index < len(rest_dyn) else True
            name, eq, value = word.partition("=")
            if word == "--":
                break
            if spec and word.startswith("--"):
                # ``--o=FILE`` / ``--outp FILE`` abbreviate ``--output`` (N2, N10).
                matched = _long_match(word, spec)
                if matched is not None:
                    name = matched
                    if not eq:
                        word = matched
            if spec and word in spec and word not in _EQUALS_ONLY_FLAGS:
                if index + 1 < len(rest):
                    targets.append((rest[index + 1], rest_dyn[index + 1]))
                index += 2
                continue
            if spec and eq and name in spec:
                targets.append((value, dyn))
            elif spec and not word.startswith("--") and len(word) > 2:
                for flag in spec:
                    if len(flag) == 2 and word.startswith(flag) and not eq:
                        targets.append((word[2:], dyn))
            elif eq and name in GENERIC_OUTPUT_FLAGS and not dyn and _looks_like_path(value):
                targets.append((value, dyn))
            index += 1
        for target, dyn in targets:
            if "=" in target and not dyn:
                target = target.split("=", 1)[1]  # pytest -o cache_dir=…
            if re.match(r"^[a-z-]+:", target) and "://" not in target:
                target = target.split(":", 1)[1]  # --cov-report=html:dir
            if not target:
                continue
            self.writes = True
            self.check_write_path(target, cwd, "exec", dyn)
        if program in ("uniq", "xxd", "iconv") and not any(w in ("-r", "-revert") for w in rest):
            operands = _reader_operands(program, rest, rest_dyn)
            if program != "iconv" and len(operands) > 1:
                self.writes = True
                self.check_write_path(operands[1][0], cwd, "exec", operands[1][1])
        if program == "ffmpeg":
            outputs = [(w, d) for w, d in zip(rest, rest_dyn, strict=False)
                       if not w.startswith("-")]
            if outputs:
                self.writes = True
                self.check_write_path(outputs[-1][0], cwd, "exec", outputs[-1][1])

    def _file_options(
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        """Files named by options (``date -f F``, ``diff --from-file=F``), read (N4)."""
        spec = FILE_READ_OPTIONS.get(program)
        if spec is None:
            return
        longs, letters = spec
        lists = _NAME_LIST_LETTERS.get(program, "")
        index = 0
        while index < len(rest):
            word = rest[index]
            index += 1
            if word == "--":
                break
            values: list[tuple[str, bool]] = []
            name_list = False
            if word.startswith("--"):
                matched = _long_match(word, longs)
                if matched is None:
                    continue
                name_list = matched in _NAME_LIST_OPTIONS
                if "=" in word:
                    values.append((word.split("=", 1)[1], rest_dyn[index - 1]))
                else:
                    count = 2 if matched in _TWO_VALUE_OPTIONS else 1
                    for _ in range(count):
                        if index < len(rest):
                            values.append((rest[index], rest_dyn[index]))
                            index += 1
            elif word.startswith("-") and len(word) > 1:
                # A cluster (``-uf F``, ``-fF``): the first file letter's value is
                # the rest of the word or the next one.
                for position, letter in enumerate(word[1:], start=1):
                    if letter not in letters:
                        continue
                    name_list = letter in lists
                    remainder = word[position + 1 :]
                    if remainder:
                        values.append((remainder, rest_dyn[index - 1]))
                    elif index < len(rest):
                        values.append((rest[index], rest_dyn[index]))
                        index += 1
                    break
            for value, dyn in values:
                if value == "-" and not name_list:
                    continue
                if value != "-":
                    self.check_read_path(value, cwd, "exec:file-option", dynamic=dyn)
                if name_list:
                    # The files read are the names in that list: anyone's.
                    self.dynamic_read("", cwd, "exec:name-list")

    def _dir_flags(
        self, program: str, family: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        """``make -C ~/other``, ``npm --prefix /x``: it runs and writes over there."""
        spec = DIR_FLAGS.get(program) or DIR_FLAGS.get(family)
        if not spec:
            return
        index = 0
        while index < len(rest):
            word = rest[index]
            name, eq, value = word.partition("=")
            if word.startswith("--"):
                matched = _long_match(word, spec)
                if matched is not None:
                    name = matched
                    if not eq:
                        word = matched
            target: tuple[str, bool] | None = None
            if word in spec and index + 1 < len(rest):
                target = (rest[index + 1], rest_dyn[index + 1])
                index += 1
            elif eq and name in spec:
                target = (value, rest_dyn[index])
            elif not word.startswith("--") and len(word) > 2 and word[:2] in spec:
                target = (word[2:], rest_dyn[index])
            if target is not None:
                raw, dyn = target
                paths = None if dyn else self._candidates(raw, cwd)
                if not paths or any(self._outside(p) for p in paths):
                    self.hits.guard(OUTSIDE, "exec:runs-outside-project")
                for path in paths or []:
                    if self._is_config(path):
                        self.hits.never_("exec:snowpea-config")
            index += 1

    # -- safe / read-like: the exact table (security review 3) -------------
    def inspect_line(self, command: str, cwd: Path) -> str | None:
        """``safe``, ``readlike`` or ``None`` for a line that raised nothing else.

        Every simple command must pass :func:`inspect_table.judge_command`
        (exact option allowlists), every operand :meth:`_table_path`; no
        assignment, no here-document, no wrapper, redirections only into
        ``/dev/null`` (or from a file in the project), expansions only in
        ``echo``/``printf`` (and then read-like).  ``git`` is read-like at most
        and only at the project's top level.
        """
        from snowpea_core.jarvis import inspect_table

        workdir = self.ctx.workdir
        if workdir is None:
            return None
        parsed = parse_line(command)
        if parsed.substitution or parsed.function or not parsed.commands:
            return None
        level: str | None = "safe"
        here = cwd
        for cmd in parsed.commands:
            if cmd.heredoc or not cmd.words or (cmd.braced and cmd.braced[0]):
                return None
            for operator, target, unknown in cmd.redirects:
                if unknown:
                    return None
                if operator == "<":
                    level = _worse_level(level, self._table_path(target, here, True))
                elif operator not in (">", ">>", "&>", "&>>", ">|") or target != "/dev/null":
                    return None
                if level is None:
                    return None
            words, dynamic = cmd.words, cmd.dynamic
            head = words[0]
            if dynamic[0] or head in RESERVED_WORDS or _ASSIGNMENT.match(head):
                return None
            rest, rest_dyn = words[1:], dynamic[1:]
            if head == "cd":
                if len(rest) != 1 or rest_dyn[0] or rest[0].startswith("-"):
                    return None
                level = _worse_level(level, self._table_path(rest[0], here, True))
                moved = _resolve(rest[0], here, self.ctx)
                if level is None or moved is None:
                    return None
                here = moved
                continue
            if any(rest_dyn):
                if head not in ("echo", "printf"):
                    return None
                level = _worse_level(level, "readlike")
            if head == "git" and (here != workdir or not self._cwd_known):
                # A bare repository in a subfolder, or a shell whose folder
                # nobody knows: git there may run what a worker wrote.
                return None
            base = here

            def check(raw: str, dir_ok: bool, base: Path = base) -> str | None:
                return self._table_path(raw, base, dir_ok)

            level = _worse_level(level, inspect_table.judge_command(head, rest, check))
            if level is None:
                return None
        return level

    def _table_path(self, raw: str, cwd: Path, dir_ok: bool) -> str | None:
        """An operand of a table command: ``safe`` (a literal path that stays in the
        project, resolved and unresolved), ``readlike`` (a glob in the project, or a
        directory where ``dir_ok`` is false) or ``None``."""
        workdir = self.ctx.workdir
        if workdir is None or not raw or "$" in raw or "`" in raw or "\x00" in raw:
            return None
        if raw != raw.strip():
            # The shell keeps the spaces; a resolver that strips them judges another
            # file (security review 4, M1): never ``safe``.
            return None
        if raw == "-":
            return "safe"
        if raw.startswith("~") and not self._home_relaxed:
            return None  # the user's home: outside the project
        level = "safe"
        if self.external and not self._cwd_known and not raw.startswith("/"):
            # The CLI's shell keeps a folder nobody told us about (an earlier
            # ``cd``): a relative name may be anywhere (security review 4, M2).
            return None
        if _has_glob(raw):
            paths = self._candidates(raw, cwd)
            if not paths:
                return None
            level = "readlike"
        else:
            lexical = Path(posixpath.normpath(str(cwd / raw) if not raw.startswith("/") else raw))
            if not _under(lexical, workdir) and not self._home_relaxed:
                return None
            resolved = _resolve(raw, cwd, self.ctx)
            if resolved is None:
                return None
            paths = [resolved]
        for path in paths:
            area = self._area(path)
            if self._home_relaxed and area in ("own", "content") and not is_secret_path(path):
                continue  # only to tell a plain read of a content folder from a write
            if not _under(path, workdir):
                return None
            if area is not None or self._is_config(path) or is_secret_path(path):
                return None
            if cli_control_path(path, self.ctx.user_home):
                return None
            if not dir_ok and level == "safe":
                with contextlib.suppress(OSError):
                    if path.is_dir():
                        level = "readlike"
            try:
                info = path.lstat() if path.is_symlink() else path.stat()
            except OSError:
                continue  # nothing there yet: the command fails, it reads nothing
            import stat as stat_module

            if not (stat_module.S_ISREG(info.st_mode) or stat_module.S_ISDIR(info.st_mode)):
                # A FIFO, socket or device: reading it may hang or read another
                # process's data (security review 4, Low).
                return None
            if stat_module.S_ISREG(info.st_mode) and info.st_nlink > 1 and level == "safe":
                # A hard link made earlier may be a secret under a new name (H5):
                # a pure check cannot tell, so it is never ``safe``.
                level = "readlike"
        return level

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
        recursive = any(
            w in ("-r", "-R", "-a", "--recursive", "--archive")
            or (w.startswith("-") and not w.startswith("--") and any(c in w for c in "rRa"))
            for w in rest
        )
        if local and not remote:
            *sources, dest = local
            for word, dyn in sources:
                self.check_read_path(word, cwd, "exec", recursive=recursive, dynamic=dyn)
            self.check_write_path(dest[0], cwd, "exec", dest[1] or stdin_args)
        elif local:
            # Local operands of a transfer: the ones read are sent; one written is checked.
            last_is_local = operands and operands[-1][0] not in remote
            for position, (word, dyn) in enumerate(local):
                if last_is_local and position == len(local) - 1:
                    self.check_write_path(word, cwd, "exec", dyn or stdin_args)
                else:
                    self.check_read_path(word, cwd, "exec", recursive=recursive, dynamic=dyn)

    def _http(self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        hits = self.hits
        hits.network = True
        # Any request, a GET too, sends its URL (path, query, headers) out: a send,
        # exactly as a web-fetch tool is.
        hits.guard(SECRET, "exec:http-request")
        rest, rest_dyn = _split_short_flags(program, rest, rest_dyn)
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
        if program in ("curl", "curlie") and any(
                w in ("-O", "--remote-name", "--remote-name-all") for w in rest):
            # ``curl -O URL`` writes the URL's last part into the current folder
            # (or ``--output-dir``); ``-J`` lets the server choose the name.
            server_named = any(w in ("-J", "--remote-header-name") for w in rest)
            folder = next((rest[i + 1] for i, w in enumerate(rest[:-1])
                           if w == "--output-dir"), "")
            for word in rest:
                if word.startswith("--output-dir="):
                    folder = word.split("=", 1)[1]
                    self.check_write_path(folder, cwd, "exec", False)
            valued = _HTTP_SHORT["curl"][0]
            for position, (word, dyn) in enumerate(zip(rest, rest_dyn, strict=False)):
                prev = rest[position - 1] if position else ""
                if word.startswith("-") or (prev.startswith("--") and "=" not in prev
                                            and prev not in _CURL_LONG_FLAGS) or (
                        len(prev) == 2 and prev[0] == "-" and prev[1] in valued):
                    continue  # an option, or an option's value
                if "://" not in word and "." not in word and "/" not in word:
                    continue
                address = word.split("://", 1)[1] if "://" in word else word
                name = address.split("?", 1)[0].split("#", 1)[0]
                name = name.rsplit("/", 1)[-1] if "/" in name else ""
                path = f"{folder.rstrip('/')}/{name}" if folder and name else name
                self.check_write_path(path or word, cwd, "exec",
                                      dyn or server_named or not name)
        if send:
            hits.guard(SECRET, "exec:http-send")

    def _database(self, program: str, words: list[str], cwd: Path | None) -> None:
        text = " ".join(words)
        if program in DB_CLIENTS and _DB_SHELL_ESCAPE.search(text):
            # ``.shell`` / ``\!`` / ``system`` run a command; ``.output`` /
            # ``\o`` / ``INTO OUTFILE`` / ``COPY … PROGRAM`` write or run anywhere.
            self.hits.guard_all("exec:opaque:db-shell")
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
            if not operands and "-o" not in rest:
                # ``patch -p1 < x.diff``: the diff names the files (security review 4,
                # H6) — an agent CLI's permission file or a git control file among them.
                hits.guard_all("exec:patch-unknown-files")
        else:
            operands = _operands(rest, rest_dyn, valued)
        # ``-t DIR``, ``-tDIR``, ``-vt DIR``, ``--target-directory=DIR`` and its
        # unambiguous abbreviations (``--target=DIR``): every operand is a source.
        target_dir = False
        target_value: tuple[str, bool] | None = None
        if program in ("cp", "mv", "ln", "install"):
            for idx, word in enumerate(rest):
                value: tuple[str, bool] | None = None
                if word == "--":
                    break
                name, eq, val = word.partition("=")
                if name.startswith("--t") and "--target-directory".startswith(name):
                    value = (val, rest_dyn[idx]) if eq else (
                        (rest[idx + 1], rest_dyn[idx + 1]) if idx + 1 < len(rest) else ("", True)
                    )
                elif re.fullmatch(r"-[A-Za-z]*t", word):
                    value = (rest[idx + 1], rest_dyn[idx + 1]) if idx + 1 < len(rest) else (
                        "", True
                    )
                elif re.match(r"^-[A-Za-z]*t.", word) and not word.startswith("--"):
                    value = (word[word.index("t") + 1 :], rest_dyn[idx])
                if value is not None:
                    target_dir = True
                    target_value = value
                    self.check_write_path(value[0], cwd, "exec", value[1] or stdin_args)
            if target_dir:
                operands = [
                    (w, d) for w, d in _operands(rest, rest_dyn, valued)
                    if not re.match(r"^-[A-Za-z]*t", w)
                ]
        if stdin_args:
            hits.guard(OUTSIDE, "exec:unknown-path")
            if program in DESTINATION_ONLY and not self.stdin_from_roots:
                self.dynamic_read("", cwd, "exec:stdin-args")
        if not operands:
            return
        recursive = any(
            w in ("-r", "-R", "-a", "--recursive", "--archive")
            or (w.startswith("-") and not w.startswith("--") and any(c in w for c in "rRa"))
            for w in rest
        )
        if program in DESTINATION_ONLY and (len(operands) > 1 or target_dir):
            sources, dest = (operands, None) if target_dir else (operands[:-1], operands[-1])
            symbolic = program == "ln" and any(
                w in ("-s", "--symbolic") or (w.startswith("-") and not w.startswith("--")
                                              and "s" in w[1:])
                or (w.startswith("--sym") and "--symbolic".startswith(w))
                for w in rest
            )
            for word, dynamic_source in sources:
                if symbolic:
                    continue  # a symlink's target is only named, never read
                # A hard link (``ln`` without -s, ``link``) makes the file readable
                # under a new name (security review 4, H5): its source is read.
                # What is copied is read (and may land somewhere a send can reach).
                self.check_read_path(word, cwd, "exec", recursive=recursive,
                                     dynamic=dynamic_source)
            if dest is not None:
                self.check_write_path(dest[0], cwd, "exec", dest[1])
            self._landing(target_value if target_dir else dest, sources, cwd,
                          many=target_dir or len(sources) > 1)
            return
        for word, dyn in operands:
            self.check_write_path(word, cwd, "exec", dyn)
        if program == "mv" and (len(operands) > 1 or target_dir):
            sources = operands if target_dir else operands[:-1]
            self._landing(target_value if target_dir else operands[-1], sources, cwd,
                          many=target_dir or len(sources) > 1)

    def _landing(
        self, dest: tuple[str, bool] | None, sources: list[tuple[str, bool]], cwd: Path | None,
        *, many: bool,
    ) -> None:
        """``cp x dir/`` writes ``dir/x``: judge where each source lands (review 4, H6)."""
        if dest is None or dest[1]:
            return
        folder = _resolve(dest[0], cwd, self.ctx)
        if folder is None:
            return
        try:
            is_dir = folder.is_dir()
        except OSError:
            is_dir = False
        if not (many or is_dir or dest[0].endswith("/")):
            return
        for word, dyn in sources:
            name = word.rstrip("/").rsplit("/", 1)[-1]
            if dyn or not name or name in (".", ".."):
                continue
            self.check_write_path(str(folder / name), cwd, "exec", False)

    def _find(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None, depth: int) -> None:
        hits = self.hits
        roots: list[tuple[str, bool]] = []
        options = 0
        while options < len(rest):
            leading = rest[options]
            if leading in ("-L", "-H", "-P") or re.fullmatch(r"-O\d", leading):
                options += 1
            elif leading == "-D":
                options += 2
            elif leading.startswith("-D"):
                options += 1
            else:
                break
        for word, dyn in zip(rest[options:], rest_dyn[options:], strict=False):
            if word.startswith(("-", "(", "!", ")")):
                break
            roots.append((word, dyn))
        roots = roots or [(".", False)]
        if any(w in ("-exec", "-execdir", "-ok", "-okdir") for w in rest):
            # What -exec runs is handed every file under the roots.
            for root, dyn in roots:
                self.check_read_path(root, cwd, "exec", recursive=True, dynamic=dyn)
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in ("-delete",):
                hits.guard(OUTSIDE, "exec:find-delete")
                for root, dyn in roots:
                    self.check_write_path(root, cwd, "exec", dyn)
            elif word in ("-exec", "-execdir", "-ok", "-okdir"):
                end = index + 1
                while end < len(rest) and rest[end] not in (";", "+", "\\;"):
                    end += 1
                inner = rest[index + 1 : end]
                inner_dyn = rest_dyn[index + 1 : end]
                if inner:
                    previous = self.stdin_from_roots
                    self.stdin_from_roots = not any(dyn for _, dyn in roots)
                    try:
                        self.words(list(inner), list(inner_dyn), cwd, depth + 1,
                                   piped_in=False, heredoc=False, stdin_args=True)
                    finally:
                        self.stdin_from_roots = previous
                index = end
            elif word in ("-fprint", "-fprint0", "-fprintf", "-fls"):
                self.writes = True
                if index + 1 < len(rest):
                    self.check_write_path(rest[index + 1], cwd, "exec", rest_dyn[index + 1])
            index += 1
        self.lister_ok = self._roots_listable(roots, cwd)

    def _tar(self, rest: list[str], rest_dyn: list[bool], cwd: Path | None) -> None:
        rest = list(rest)
        if rest and re.fullmatch(r"[A-Za-z]+", rest[0]) and re.search(r"[cxtru]", rest[0]):
            rest[0] = "-" + rest[0]  # ``tar czf out.tgz src``: old-style bundled options
        joined = " ".join(rest)
        if self._runs_commands(["tar", *rest]) or any(
            w.startswith(("--to-command", "--use-compress-program", "--checkpoint-action",
                          "--info-script", "--new-volume-script", "--rsh-command", "-I"))
            or w in ("-F", "--rmt-command")
            for w in rest
        ):
            self.hits.guard_all("exec:opaque:runs-commands")
        if re.search(r"(^|\s)(-[A-Za-z]*[cru]|--create|--append|--update)", joined):
            valued = {"-f", "--file", "-C", "--directory", "-T", "--files-from", "-X",
                      "--exclude-from", "-b", "-H", "--format", "--exclude", "-g",
                      "--listed-incremental", "-K", "-N", "--newer", "-V", "--label",
                      "--owner", "--group", "--mode", "--transform", "-L", "--tape-length"}
            skip: set[int] = set()
            for idx, word in enumerate(rest):
                if word in valued or re.match(r"^-[A-Za-z]*f$", word):
                    skip.add(idx + 1)
                if word in ("-T", "--files-from") or word.startswith("--files-from="):
                    self.dynamic_read("", cwd, "exec:files-from")
            for idx, (word, dyn) in enumerate(zip(rest, rest_dyn, strict=False)):
                if idx in skip or word.startswith("-"):
                    continue
                # Everything below a source goes into the archive.
                self.check_read_path(word, cwd, "exec", recursive=True, dynamic=dyn)
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
        if extract:
            # Members land under names nobody sees here; one may be an agent CLI's
            # permission file or a git control file (security review 4, H6).
            self.hits.guard_all("exec:archive-extract")

    def _archive(
        self, program: str, rest: list[str], rest_dyn: list[bool], cwd: Path | None
    ) -> None:
        listing = {"unzip": ("-l", "-t", "-Z", "-p", "-v", "-z"), "unrar": ("l", "lb", "lt", "v",
                                                                             "t", "p")}
        if program == "unzip" and not any(w in listing["unzip"] for w in rest):
            self.hits.guard_all("exec:archive-extract")  # H6: members nobody sees
        if program in ("7z", "7za") and rest[:1] and rest[0] in ("x", "e"):
            self.hits.guard_all("exec:archive-extract")
        if program == "unrar" and not (rest[:1] and rest[0] in listing["unrar"]):
            self.hits.guard_all("exec:archive-extract")
        if program == "unzip" and "-d" in rest:
            idx = rest.index("-d")
            if idx + 1 < len(rest):
                self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        if program in ("7z", "7za"):
            for word in rest:
                if word.startswith("-o") and len(word) > 2:
                    self.check_write_path(word[2:], cwd, "exec", False)
        if program == "zip":
            # ``zip [-r] out.zip sources…``: the archive is written, the sources read.
            operands = _operands(rest, rest_dyn, frozenset({"-b", "-n", "-t", "-tt", "-x",
                                                            "-i", "-P", "--password", "-O",
                                                            "--output-file", "-s", "-Z"}))
            recursive = any(w in ("-r", "-R", "--recurse-paths") or (
                w.startswith("-") and not w.startswith("--") and "r" in w) for w in rest)
            if operands:
                self.writes = True
                self.check_write_path(operands[0][0], cwd, "exec", operands[0][1])
                for word, dyn in operands[1:]:
                    self.check_read_path(word, cwd, "exec", recursive=recursive, dynamic=dyn)
            for flag in ("-O", "--output-file"):
                if flag in rest:
                    idx = rest.index(flag)
                    if idx + 1 < len(rest):
                        self.check_write_path(rest[idx + 1], cwd, "exec", rest_dyn[idx + 1])
        if program in ("gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "lz4",
                       "brotli") and not any(
            w in ("-c", "--stdout", "--to-stdout", "-t", "--test", "-l", "--list")
            or (w.startswith("-") and not w.startswith("--") and any(c in w for c in "ctl"))
            for w in rest
        ):
            # The file is replaced in place by its (de)compressed version.
            for word, dyn in _operands(rest, rest_dyn, frozenset({"-S", "--suffix", "-o",
                                                                  "-T", "--threads"})):
                self.writes = True
                self.check_write_path(word, cwd, "exec", dyn)
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
    if word == flag or word.startswith(flag + "=") or _long_abbrev(word, flag):
        return True
    if len(flag) == 2 and flag[0] == "-" and flag[1] != "-":
        return word.startswith("-") and not word.startswith("--") and flag[1] in word[1:]
    return False


def _long_abbrev(word: str, flag: str) -> bool:
    """``--o`` / ``--outp=x`` stand for ``--output``: getopt_long and git's
    parse-options accept any unambiguous prefix, so every prefix counts (N10)."""
    if not (flag.startswith("--") and word.startswith("--")):
        return False
    name = word.split("=", 1)[0]
    return len(name) > 2 and flag.startswith(name)


def _long_match(word: str, spec: Iterable[str]) -> str | None:
    """The long option of ``spec`` that ``word`` (``--name`` / ``--name=value``) spells."""
    name = word.split("=", 1)[0]
    if name in spec:
        return name
    for flag in spec:
        if _long_abbrev(word, flag):
            return flag
    return None


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
        # Never delegable: the person answers what the classifier could not read (N12).
        return Verdict("never", GUARDED_CLASSES, ("internal:classifier-error",))


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
