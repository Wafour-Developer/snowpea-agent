"""Protocol single source of truth.

Every JSON-RPC method and notification the daemon speaks is declared here as a
pydantic v2 model.  ``scripts/gen_protocol.py`` turns :func:`dump_schema` into
``sdk/src/protocol.ts`` and ``docs/protocol.md``, so nothing else in the tree
may invent a method name or a payload field.

Field ``description``s and per-method ``summary``s are load-bearing: they are
the text of the generated TypeScript JSDoc and of the markdown field tables.

See ``docs/design/m1-core-contract.md`` §1 and plan §3.5.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

from snowpea_core import __version__ as _core_version
from snowpea_core.server.errors import DETAIL_REASONS, ERROR_CODES, PERSON_ONLY_KINDS

# Bumped in protocol_version.py: ``snowpea-core --version`` reads it from there.
from snowpea_core.server.protocol_version import PROTOCOL_VERSION

SERVER_VERSION = _core_version

Mode = Literal["plan", "accept", "auto"]
#: What kind of thread a session is (CORE-session-kind).  ``chat`` is a thread
#: a human opened; ``scheduled`` is one a job fired; ``subagent`` is a child a
#: parent agent spawned; ``agent`` is a persistent named agent's own session.
#: A surface that does not know the field sees every session as before.
SessionKind = Literal["chat", "scheduled", "subagent", "agent"]
#: ``config`` marks a call that changes snowpea's own settings, credentials or
#: state.  It is never a silent ``allow``: plan denies it, accept and auto both
#: ask, and the allowlist may not promote it (CORE-search-fix).
PermissionTag = Literal[
    "read", "write", "exec", "network", "send", "config", "delegate", "secret", "outside"
]
ToolState = Literal["active", "inactive"]
#: ``builtin``, ``global``, ``project``, ``skill`` or ``plugin:<plugin name>``;
#: a free string because a plugin names itself (M6 contract §1).
CommandSource = str
Decision = Literal["allow", "deny"]
#: ``site`` (1.6.0) stores an allowlist entry keyed by the tool *and* the
#: site's origin ("always allow on github.com").
#: ``task`` (1.68.0, ``jobApprovals``): remembered for one scheduled job, across
#: its runs (stored with the job, keyed by its id): offered only on a request
#: whose ``job`` is set.
ApprovalScope = Literal["once", "session", "project", "always", "site", "task"]
AllowlistScope = Literal["session", "project", "always"]
#: How hard a reasoning model may think; one scale for every vendor.
EffortLevel = Literal["low", "medium", "high", "max"]
#: Which rule decided the effective effort, for a surface that shows why.
EffortSource = Literal["session", "model", "vendor", "default"]
#: What a language server is doing right now (M13 contract §4).
LspServerState = Literal["starting", "ready", "broken", "stopped"]
#: Where an MCP server is declared (M14 contract §2).  ``plugin`` and
#: ``settings`` are read-only: the management API refuses to rewrite them.
McpScope = Literal["project", "global", "plugin", "settings"]
#: Scopes ``mcp.add``/``mcp.remove``/``mcp.update`` may write.
McpWritableScope = Literal["project", "global"]
#: How the daemon talks to an MCP server; inferred from ``command``/``url``
#: when the entry does not say.
McpTransport = Literal["stdio", "http", "sse"]
#: What an MCP server is doing right now (M14 contract §3).
McpState = Literal["stopped", "starting", "ready", "error"]
#: State of a vendor's stored credential (CORE-codex-login).
AuthStatus = Literal["unconfigured", "active", "expired"]
#: ``budget`` (CORE-subagent-budget) is additive: the turn used its whole
#: tool-round budget, wrote a report and ended.  A surface that does not know
#: it must treat it as an ordinary end of turn.
TurnReason = Literal[
    "complete", "interrupted", "error", "denied", "timeout", "budget", "stalled", "empty_response"
]
#: Why a queued prompt left the queue: it started running, it was dropped, or
#: it was merged into the running turn as a steer message.
QueuedTurnReason = Literal["started", "dropped", "steered"]
TaskState = Literal["pending", "running", "done", "failed"]
#: States a team task walks through (M7 contract §5).  A superset of
#: :data:`TaskState`, so nothing that already emitted a task state breaks.
TeamTaskState = Literal[
    "pending",
    "queued",
    "claimed",
    "running",
    "done",
    "conflict",
    "merged",
    "failed",
]
SkillKind = Literal["skill", "agent", "command", "plugin"]
#: The layout a skill, command, agent or plugin was written for (1.52.0):
#: snowpea's own, Claude Code's (``plugin.json`` / ``.claude-plugin/``) or
#: Codex's (``.codex-plugin/``, ``~/.codex``).
ItemFormat = Literal["snowpea", "claude", "codex"]

_FORMAT_DESCRIPTION = (
    "The layout it was written for (1.52.0): snowpea, claude (Claude Code format) "
    "or codex (Codex format)."
)
SkillScope = Literal["project", "global"]
SkillEntryKind = Literal["skill", "agent", "command"]
#: Where ``skill.read`` may look and what it answers (1.33.0): the two writable
#: scopes, plus the skills that ship with the engine or come with a plugin.
SkillReadScope = Literal["project", "global", "builtin", "plugin"]
#: Where ``skill.remove`` may look (1.33.0).
SkillRemoveScope = Literal["project", "global", "plugin"]
#: Entry keys ``mcp.update {unset}`` may clear (1.33.0).
McpUnsetKey = Literal[
    "args",
    "cwd",
    "disabled",
    "env",
    "headers",
    "timeoutSec",
    "toolTimeoutSec",
    "toolsExclude",
    "toolsInclude",
    "type",
    "oauth",
]
#: Whether an http/sse MCP server holds a usable OAuth sign-in (1.34.0):
#: ``ok`` tokens are stored for its URL, ``needs_auth`` it answered 401 and a
#: refresh did not help, ``none`` nothing is stored and nothing was refused.
McpAuthState = Literal["ok", "needs_auth", "none"]
#: How a catalog preset authenticates (1.34.0): ``oauth`` sign in with
#: ``mcp.authorize``, ``token`` a key the user pastes, ``none`` nothing.
McpCatalogAuth = Literal["oauth", "token", "none"]
#: The ``models.categories`` slots core reads (1.33.0); advertised as
#: ``system.hello {modelCategories}``.
ModelCategory = Literal["fast", "standard", "deep", "vision"]
#: ``"global"`` is ``$SNOWPEA_HOME/settings.json``; ``"project"`` is
#: ``<workdir>/.snowpea/settings.json`` (settings.get / settings.set, M8).
SettingsScope = Literal["global", "project"]
Direction = Literal["c2s", "s2c"]
#: Where an update comes from: a PyPI release or a git tag (CORE-update).
UpdateChannel = Literal["git", "pypi"]
#: Phases ``system.updateProgress`` reports.
UpdatePhase = Literal["started", "done", "failed"]
#: Phases ``provider.loginProgress`` reports for a browser login.
LoginProgressPhase = Literal["started", "await_user", "polling", "done", "failed"]
#: ``provider.loginWeb`` result status once the device code/PKCE URL is known.
LoginWebStatus = Literal["await_user", "done", "failed"]
CheckpointKind = Literal["turn", "restore"]
CheckpointFileStatus = Literal["modified", "created", "deleted"]
CheckpointFileSource = Literal["tool", "shell"]
CheckpointSkipped = Literal["too_large", "excluded", "unreadable"]
CheckpointRestoreSkipReason = Literal[
    "changed_since", "not_restorable", "missing_blob", "excluded"
]


class Payload(BaseModel):
    """Base for every wire model: JSON objects, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class Empty(Payload):
    """A method that takes no parameters (``params`` is still an object)."""


class ActorParams(Payload):
    """Params of a config method that takes nothing but the browser marker (1.53.0)."""

    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "'person' when a browser host (clientKind 'browser', clientVersion 0.3.0 or "
            "later; capability 'browserActor') makes the call from its own Settings page "
            "under a user gesture. Required from such a browser on the marker methods "
            "(jarvis_person_only otherwise); accepted and ignored from every other "
            "connection. Any other value is invalid_params."
        ),
    )


class Ok(Payload):
    ok: bool = Field(default=True, description="True when the call succeeded.")


# --------------------------------------------------------------------------
# M19 Jarvis mode (1.53.0): types the older models below gain as fields
# --------------------------------------------------------------------------

#: Whose words a ``message.user`` is (1.53.0, capability ``messageOrigin``):
#: ``human`` the session's own person on any surface (IDE, TUI, CLI, web,
#: browser, or the bound person in a messenger); ``jarvis`` the Jarvis session
#: (reserved, sent from M19 WP4); ``schedule`` a scheduled job; ``gateway``
#: someone other than the bound person in a messenger group chat; ``subagent``
#: delegation traffic (a lead's brief or message in a child, a child's
#: hand-back announcement in its lead); ``system`` text core or a model wrote
#: (a goal round, a queued command, ``session.continue``).
OriginKind = Literal["human", "jarvis", "schedule", "gateway", "subagent", "system"]
#: How much Jarvis may decide for a session (1.53.0): ``staged`` only safe
#: approvals and how-to questions; ``full`` also risky approvals, stops,
#: questions and incomplete endings. Guarded classes need their own grant.
JarvisLevel = Literal["staged", "full"]
#: Classes Jarvis never decides without an explicit grant naming them
#: (1.53.0): ``deploy`` (push, release, publish, remote apply),
#: ``outside_destructive`` (outside the project, recursive deletes, hard
#: resets, force flags), ``secret_send`` (credentials, sending, mutating
#: network calls, payments).
GuardedClass = Literal["deploy", "outside_destructive", "secret_send"]
#: How the delegation engine classified a request (1.53.0): ``guarded`` comes
#: with a :data:`GuardedClass`; ``never`` is never delegable (snowpea's own
#: config, delegation changes, browser-owned requests, Jarvis's own requests).
JarvisClassification = Literal["safe", "risky", "guarded", "never"]
#: Who an approval or question is routed to (1.53.0): ``person`` waits for a
#: human as before; ``jarvis`` the Jarvis session decides; ``auto`` the
#: engine allowed it without a model call (shown for the record).
ApprovalRoute = Literal["person", "jarvis", "auto"]
#: External agent CLIs snowpea can host (1.53.0): Claude Code, Codex,
#: Antigravity (agy) and Cursor agent.
ExternalCli = Literal["claude", "codex", "agy", "cursor"]
#: What an external session is doing (1.53.0); where its process lives is
#: :data:`ExternalHost`, a separate field.
ExternalStatus = Literal["idle", "running", "error"]
#: Where an external session's live process is (1.53.0): ``none`` no live
#: process (snowpea can continue it); ``terminal`` the person runs it in a
#: plain terminal (view only, a live tail; available once that process
#: exits); ``tmux`` it runs in a tmux pane (view, and snowpea may type into
#: the same process — pending spike S5; its approvals are the person's, never
#: Jarvis's); ``snowpea`` the daemon runs it over the CLI's structured
#: transport (full: unified approvals, Jarvis may decide).
ExternalHost = Literal["none", "terminal", "tmux", "snowpea"]
#: How an external session's permission prompts are answered (1.53.0):
#: ``bridged`` they become snowpea approvals (person or Jarvis); ``person``
#: only the person answers, in the CLI's own pane (tmux); ``degraded`` edits
#: auto-allowed, the rest blocked (Cursor); ``none`` nothing runs here to ask
#: (host none or terminal).
ExternalApprovals = Literal["bridged", "person", "degraded", "none"]
#: How an external session is run (1.53.0, owner decision): ``default``
#: approvals bridged where the CLI has them (claude, codex; agy when its own
#: policy asks) — not offered for cursor, which has no permission mechanism;
#: ``readonly`` the CLI's plan/ask/read-only mode (claude ``--permission-mode
#: plan``, codex read-only sandbox, agy ``--mode plan``, cursor ``--mode ask``);
#: ``unrestricted`` the CLI's own bypass (claude/agy
#: ``--dangerously-skip-permissions``, codex ``--yolo`` / app-server
#: approvalPolicy never + danger-full-access, cursor ``--yolo``): snowpea no
#: longer controls what it runs.  Only a human connection may choose
#: ``unrestricted``; Jarvis can only ask the person to confirm it.
ExternalSessionMode = Literal["default", "readonly", "unrestricted"]
#: Does the CLI stop and ask the person in a native mode (1.56.0).
ExternalNativeAsks = Literal["always", "sometimes", "never"]
#: What a native mode lets the CLI write (1.56.0): ``plan`` only its plan or
#: notes file; ``workspace`` the project folder; ``anywhere`` everything.
ExternalNativeWrites = Literal["none", "plan", "workspace", "anywhere"]
#: Whether a native mode's prompts reach snowpea's approval queue (1.56.0).
ExternalNativeApprovals = Literal["bridged", "none"]
#: How urgent a Jarvis notice is (1.53.0): clients badge every one, raise an
#: OS notification from ``normal`` up (jarvis.notify.os), and the messenger
#: forwards ``high`` and ``urgent`` (jarvis.notify.messenger).
JarvisImportance = Literal["low", "normal", "high", "urgent"]


class MessageOrigin(Payload):
    """Whose words a ``message.user`` carries (1.53.0, capability ``messageOrigin``)."""

    kind: OriginKind = Field(
        description=(
            "human (the session's own person), jarvis (reserved for the Jarvis session), "
            "schedule, gateway (another member of a messenger chat), subagent (delegation "
            "traffic) or system (text core or a model wrote: queued commands, Jarvis "
            "wakes)."
        )
    )
    surface: str | None = Field(
        default=None,
        description=(
            "Where it was typed, when known: the client kind from system.hello ('ide', "
            "'tui', 'cli', 'web', 'browser', ...) or the messenger platform ('telegram', "
            "'slack', ...) for human and gateway; null otherwise."
        ),
    )


class ExternalRef(Payload):
    """One session of an external agent CLI (1.53.0)."""

    cli: ExternalCli = Field(description="claude, codex, agy or cursor.")
    id: str = Field(description="The CLI's own session (thread, chat) id.")


#: Why a session's Jarvis switch cannot be changed (1.56.0, ``jarvisSessionSwitch``).
JarvisSwitchLock = Literal["project_hidden", "jarvis_session", "browser_hidden"]


class SessionJarvisState(Payload):
    """How Jarvis stands towards one session (1.53.0, capability ``jarvis``)."""

    exposed: bool = Field(
        description="False when the session's project has jarvis.expose off: Jarvis neither "
        "sees nor decides for it."
    )
    level: JarvisLevel | None = Field(
        default=None,
        description=(
            "Delegation in force (the most specific grant: session, then project, then "
            "global, then jarvis.delegation.default); null while Jarvis mode is off."
        ),
    )
    pausedUntil: str | None = Field(
        default=None,
        description=(
            "UTC ISO time Jarvis's authority returns: set when the person types into the "
            "session (now + jarvis.quietMinutes, extended by each input) or takes it over "
            "with a limit. Null when Jarvis may decide now, or while takenOver holds it."
        ),
    )
    takenOver: bool = Field(
        default=False,
        description="True after jarvis.takeover without a limit, until jarvis.handback.",
    )
    levelScope: Literal["session", "project", "global", "default"] | None = Field(
        default=None,
        description=(
            "Which scope supplied level: a session, project or global grant, or "
            "jarvis.delegation.default (no grant); null while level is null."
        ),
    )
    grantId: str | None = Field(
        default=None,
        description="The grant in force (jarvis.delegation.list id); null for the default.",
    )


# --------------------------------------------------------------------------
# system.*
# --------------------------------------------------------------------------


class HelloParams(Payload):
    token: str = Field(description="Shared secret read from $SNOWPEA_HOME/token or daemon.json.")
    clientVersion: str = Field(description="Version string of the connecting client.")
    protocolVersion: str = Field(description="Protocol semver the client speaks; major must match.")
    clientKind: str | None = Field(
        default=None, description='What the client is, e.g. "tui", "desktop", "browser" (1.6.0).'
    )
    clientId: str | None = Field(
        default=None,
        description=(
            "Id stable across restarts of this client install (1.6.0). Sessions a "
            "previous connection with the same clientId started re-bind to this one."
        ),
    )
    instanceId: str | None = Field(
        default=None, description="Id of this run of the client (1.6.0)."
    )
    keepAlive: bool = Field(
        default=False,
        description=(
            "Keep the daemon from its idle shutdown while this client is connected (1.6.0)."
        ),
    )
    holdSec: int | None = Field(
        default=None,
        ge=30,
        le=3600,
        description=(
            "A connector-held client (1.40.0, capability 'holdSec'): once nothing but "
            "loaded, idle sessions is left, the daemon exits this many seconds after the "
            "last connection goes instead of after daemon.idleTimeoutSec. A running turn, "
            "job, gateway binding, named agent or keep-alive client still keeps it up. "
            "Clamped to 30..3600; a later hello from a tui/desktop/ide client without it "
            "restores the ordinary timeout."
        ),
    )
    locale: str | None = Field(
        default=None,
        description=(
            "1.65.0 (capability 'clientLocale'): the person's language as a BCP 47 tag, e.g. "
            "'ko', 'ko-KR', 'ja', 'es-ES', 'zh-CN'. Clients should send it: validation "
            "problems and refusals (RPC error messages, details.problems, security.get "
            "problems) then come back in that language: 'en', 'ko', 'ja', 'es' or 'zh-CN', "
            "a regional tag falling back to its language, anything else to English. A "
            "method's own locale param still wins for that call. At most 35 characters of "
            "letters, digits, '-' and '_'; normalised ('ko_kr' -> 'ko-KR'). A value that is "
            "not a language tag is ignored, never refused. Machine codes (details.reason, "
            "problemItems[].reason/code) are never translated."
        ),
    )

    @field_validator("locale", mode="before")
    @classmethod
    def _locale_or_none(cls, value: Any) -> Any:
        # 1.65.0: an odd locale never refuses the handshake; it is ignored.
        return value if isinstance(value, str) else None


class MachineInfo(Payload):
    """Which machine (and state home) a daemon runs on (1.40.0, capability 'machineIdentity')."""

    id: str = Field(
        description=(
            "UUID made once and kept in <home>/machine-id (mode 0600): stable for one machine "
            "and one state home, not derived from hardware."
        )
    )
    hostname: str = Field(description="socket.gethostname() of the daemon's machine.")
    os: Literal["linux", "darwin", "windows"] = Field(description="The daemon machine's OS.")
    container: bool = Field(
        description="True when the daemon runs inside Docker (or another Linux container)."
    )


class HelloResult(Payload):
    protocolVersion: str = Field(description="Protocol semver the daemon speaks.")
    serverVersion: str = Field(description="Version of the running daemon.")
    capabilities: list[str] = Field(
        default_factory=list, description="Feature flags this daemon supports."
    )
    modelCategories: list[ModelCategory] = Field(
        default_factory=list,
        description=(
            "The models.categories slots this daemon routes by (1.33.0, capability "
            "'modelCategories'); empty from an older daemon."
        ),
    )
    machine: MachineInfo | None = Field(
        default=None,
        description="The machine this daemon runs on (1.40.0); absent from an older daemon.",
    )
    settingsDegraded: list[str] = Field(
        default_factory=list,
        description=(
            "1.55.0, to a local person's connection only: as system.info settingsDegraded "
            "(settings.json sections that did not validate); show it when not empty."
        ),
    )


class LifecycleStatus(Payload):
    """Idle-shutdown snapshot: ``Lifecycle.status()`` without the counters."""

    willExit: bool = Field(default=False, description="True while the idle timer is running.")
    reason: str = Field(default="busy", description="Why the daemon will or will not exit.")
    secondsUntilExit: float | None = Field(
        None, description="Seconds left before the idle shutdown, null while busy."
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Why the daemon is staying up, e.g. ['2 gateway bindings', '1 job'].",
    )
    summary: str | None = Field(
        None,
        description="One line for humans: 'will exit in 120s' or 'will not exit: 1 job'.",
    )


class ConnectedClient(Payload):
    """One client connected to the daemon (1.26.0)."""

    clientKind: str | None = Field(default=None, description="As the client said in hello.")
    clientId: str | None = Field(
        default=None, description="The caller's own stable id; null for the other clients."
    )
    clientVersion: str | None = Field(default=None, description="As the client said in hello.")
    you: bool = Field(default=False, description="True for the connection that asked.")


class InfoResult(Payload):
    version: str = Field(description="Daemon version.")
    protocolVersion: str = Field(description="Protocol semver the daemon speaks.")
    pid: int = Field(description="Process id of the daemon.")
    port: int = Field(description="TCP port the daemon is listening on (127.0.0.1 only).")
    startedAt: str = Field(description="UTC ISO-8601 timestamp of daemon start.")
    home: str = Field(description="Resolved SNOWPEA_HOME directory.")
    counters: dict[str, int] = Field(
        default_factory=dict,
        description="Lifecycle counters: sessions, jobs, gateway_bindings, named_agents.",
    )
    lifecycle: LifecycleStatus | None = Field(
        None, description="Idle-shutdown status, omitted by older daemons."
    )
    restartRequired: bool = Field(
        default=False,
        description=(
            "True once system.update finished; the daemon runs the old code until restarted."
        ),
    )
    platform: str | None = Field(
        None,
        description=(
            "The daemon machine's sys.platform (darwin, linux, win32), for engine and "
            "permission guidance that depends on where the core runs."
        ),
    )
    clients: list[ConnectedClient] = Field(
        default_factory=list,
        description=(
            "Every authenticated connection, the caller included (1.26.0): an app that "
            "would restart a shared daemon checks that no other client is using it."
        ),
    )
    settingsDegraded: list[str] = Field(
        default_factory=list,
        description=(
            "1.55.0: top-level sections of settings.json that did not validate when it was "
            "read and are NOT what the file says (empty: none). 'security' runs fail-closed "
            "(the last valid lists, or the default lists with unknownCode 'ask'); 'web' keeps "
            "remote access off; any other section is at its defaults. The file as it was is "
            "kept beside it as settings.json.invalid. '*': the whole file could not be read, "
            "and settings.set is refused (invalid_params, details.reason "
            "'settings_unreadable') until it is fixed and reloaded. Show it and offer to fix "
            "the file."
        ),
    )


class HealthResult(Payload):
    status: Literal["ok"] = Field(default="ok", description="Always 'ok' when the daemon answers.")


class CheckUpdateParams(Payload):
    force: bool = Field(
        default=False, description="Ignore the 24h cache and ask the network right now."
    )


class CheckUpdateResult(Payload):
    """What ``system.checkUpdate`` knows; never an error frame (CORE-update)."""

    current: str = Field(description="Version of the running daemon (snowpea_core.__version__).")
    latest: str = Field(description="Newest version found; equal to current when nothing is known.")
    available: bool = Field(description="True when latest is strictly newer than current.")
    channel: UpdateChannel = Field(description="Where the answer came from: 'pypi' or 'git'.")
    source: str = Field(description="What an installer would be handed to get 'latest'.")
    releaseUrl: str | None = Field(
        default=None, description="Human page for the release, when one exists."
    )
    checkedAt: str = Field(description="UTC ISO-8601 timestamp of the answer.")
    cached: bool = Field(
        default=False, description="True when this came from $SNOWPEA_HOME/update-check.json."
    )
    error: str | None = Field(
        default=None,
        description="Why the check could not complete; available is false whenever it is set.",
    )


class UpdateResult(Payload):
    """What ``system.update`` started."""

    started: bool = Field(description="True when the upgrade subprocess was spawned.")
    command: str = Field(
        description="The command line that runs, or that has to be run by hand."
    )
    log: str = Field(description="Absolute path of the file the upgrade writes its output to.")
    error: str | None = Field(
        default=None, description="Why nothing was started; null on the happy path."
    )
    selfRestart: bool = Field(
        default=False,
        description=(
            "True when the daemon stops and comes back by itself on the same port "
            "(Windows, uv-installed core): do not restart or spawn a core, wait for "
            "daemon.json to drop 'restarting' and name a healthy daemon (1.26.0)."
        ),
    )


# --------------------------------------------------------------------------
# session.*
# --------------------------------------------------------------------------


class Attachment(Payload):
    """A file, image, inline text or web page sent along with a prompt.

    Exactly one of ``path``, ``data`` and ``text`` carries the content.  The
    daemon sniffs the real media type from the bytes, so ``mimeType`` is a
    hint it may overrule; anything over 20MB is refused with
    ``invalid_params``.  A ``page`` (1.6.0) carries ``url`` plus optional
    ``title``, ``selection`` and ``snapshot``; it reaches the model as
    delimited, untrusted page content.
    """

    kind: Literal["file", "image", "text", "page"] = Field(
        default="file", description="Attachment flavour."
    )
    url: str | None = Field(default=None, description="Page address, for 'page'.")
    title: str | None = Field(default=None, description="Page title, for 'page'.")
    selection: str | None = Field(
        default=None, description="Text the user selected on the page, for 'page'."
    )
    snapshot: str | None = Field(
        default=None, description="Page text or accessibility snapshot, for 'page'."
    )
    name: str | None = Field(
        default=None, description="Display name; defaults to the file's basename."
    )
    path: str | None = Field(default=None, description="Absolute path, for 'file' and 'image'.")
    data: str | None = Field(
        default=None, description="Base64 (or data-URI) content, for a pasted image."
    )
    mimeType: str | None = Field(default=None, description="Media type when known.")
    size: int | None = Field(default=None, description="Byte size the client measured.")
    text: str | None = Field(default=None, description="Inline content, for 'text'.")


class SessionCreateParams(Payload):
    workdir: str = Field(description="Absolute path the session operates in.")
    mode: Mode | None = Field(
        default=None,
        description=(
            "Starting mode; defaults to the project setting (defaultMode, else 'accept'). "
            "A mode wider than that default (plan < accept < auto) needs a person (1.54.0): "
            "a local person, a remote web login, or a browser with actor 'person'; else "
            "jarvis_person_only with details.reason 'mode_raise'."
        ),
    )
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "A browser (clientVersion 0.3.0+) sets 'person' when a click created the session "
            "with a mode wider than the default (1.54.0)."
        ),
    )
    provider: str | None = Field(
        default=None, description="Chat provider vendor; defaults to configured."
    )
    model: str | None = Field(
        default=None, description="Model id; defaults to the provider's default."
    )
    agent: str | None = Field(default=None, description="Named agent whose persona to load.")
    effort: EffortLevel | None = Field(
        default=None, description="Reasoning effort for this session; null follows the settings."
    )
    maxConcurrent: int | None = Field(
        default=None, description="Override for concurrent subagents."
    )
    originSurface: str | None = Field(
        None,
        description=(
            "Surface that owns approvals for this session (TUI, gateway, ...). The server "
            "decides what is recorded (1.55.0): a browser connection's session is always "
            "'browser'; a remote web login keeps one of ide/desktop/tui/cli/web/app and "
            "otherwise gets its connection's surface id; a local person's value is kept; the "
            "core's own surfaces (jarvis-task, jarvis, scheduler, skill, connector, gateway:*, "
            "agent:*) are invalid_params for every caller. The creating connection's clientId "
            "is recorded with the session: a browser's own session is one it hosts or created."
        ),
    )
    denyExec: bool | None = Field(
        default=None,
        description="When true, refuse exec-tagged tools without prompting (headless CI).",
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Whose host tools this session sees: a connection's clientId (or surface "
            "id). Default: the creating connection (1.6.0)."
        ),
    )
    browserMemory: bool | None = Field(
        default=None,
        description=(
            "Recall also searches pages the browser ingested. Default: on when a "
            "browser client (clientKind 'browser') creates the session (1.7.0)."
        ),
    )
    memory: bool | None = Field(
        default=None,
        description=(
            "Long-term memory for this session only (1.27.0): false means no recall "
            "block, no digest, no auto-remember and save_memory/search_memory answer "
            "tool_inactive; true turns it on although memory.enabled is off. Omit to "
            "follow the setting. Kept with the session across a daemon restart (1.31.0); "
            "a delegated child follows its root conversation's."
        ),
    )
    siteMemory: bool | None = Field(
        default=None,
        description=(
            "Site memory for this session only (1.27.0): false makes site.get/put/mark/"
            "markPart called with this sessionId answer tool_inactive; true allows them "
            "although browser.siteMemory is off. Omit to follow the setting. Kept with "
            "the session across a daemon restart and followed by its delegated children "
            "(1.31.0); false does not stop host.turnContext (1.35.0): the request says "
            "siteMemory false, so the host leaves its site-memory part out."
        ),
    )
    locale: str | None = Field(
        default=None,
        description=(
            "The client's UI language as a BCP 47 tag, e.g. 'ko-KR' (1.7.0). With "
            "agent.replyLanguage 'auto', text the core writes for this session (/init, "
            "/ralph and delegated briefs) is in this language before any guess from "
            "what the user typed. Ignored when not a language tag."
        ),
    )


class SessionAttachParams(Payload):
    sessionId: str = Field(description="Session this connection becomes the origin of (1.6.0).")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Re-point the session's host tools to this clientId (or surface id). It must "
            "name an open connection whose clientKind is 'browser'. Persisted; emits "
            "sessions.changed (reason 'host')."
        ),
    )
    locale: str | None = Field(
        default=None,
        description=(
            "The client's UI language as a BCP 47 tag (1.7.0), as on session.create. "
            "Kept in memory only, so a client re-sends it after a daemon restart; "
            "omitted leaves the session's current locale."
        ),
    )


class SessionSetAgentParams(Payload):
    sessionId: str = Field(description="Session to switch.")
    agent: str | None = Field(
        default=None,
        description=(
            "Agent definition to run as from the next model call: its prompt (persona "
            "layer), tools allowlist ('*' = all) and max_tool_rounds. null clears it."
        ),
    )
    guardTurn: bool = Field(
        default=False,
        description=(
            "Guard the running turn (1.24.0): from now on every call that is not a read "
            "(read-only web tools included) and every mcp__ call is asked with forceAsk "
            "semantics — plan still denies; no mode, allowlist or cached answer allows it; "
            "approval.ask answers come back as 'once'. It covers sessions delegated from this "
            "one (also ones started later), a steer, and turns the agent queues for itself, "
            "and ends when the person's own next prompt starts a turn. No turn running: no "
            "effect."
        ),
    )


class SessionSetAgentResult(Payload):
    sessionId: str = Field(description="The session.")
    agent: str | None = Field(default=None, description="Agent now in force.")
    guarded: bool = Field(
        default=False, description="True when a running turn is now guarded (guardTurn)."
    )


class SessionSetBrowserProviderParams(Payload):
    sessionId: str = Field(description="Session whose browser tools to point (addendum 9).")
    provider: Literal["host", "local"] = Field(
        description=(
            "'host': only the Snowpea browser (the default for browser sessions; with no "
            "browser attached, browser tools fail with host_unavailable). 'local': the "
            "user explicitly allows core's own browser for this session only."
        )
    )


class SessionSetBrowserProviderResult(Payload):
    sessionId: str = Field(description="The session.")
    browserProvider: Literal["host", "local"] = Field(description="What it uses now.")


class SessionAttachResult(Payload):
    sessionId: str = Field(description="The attached session.")
    hostTools: list[str] = Field(
        default_factory=list, description="Host tools the session now sees."
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description="The session's hostToolsFrom after the call; older cores omit it.",
    )


class SessionEventsParams(Payload):
    sessionId: str = Field(description="Session whose saved events to read.")
    afterSeq: int = Field(default=0, ge=0, description="Only events with a greater seq.")
    limit: int = Field(
        default=500, ge=1, le=5000, description="At most this many events, oldest first."
    )


class SessionEventsResult(Payload):
    sessionId: str = Field(description="The session.")
    events: list[SessionEvent] = Field(
        default_factory=list, description="Saved events after afterSeq, in seq order."
    )
    more: bool = Field(
        default=False, description="True when events past the last one returned exist."
    )
    lastSeq: int = Field(
        default=0,
        description=(
            "The session's newest saved seq when the call was answered; with more "
            "false, the events returned reach it."
        ),
    )


class TurnOutcomeParams(Payload):
    sessionId: str = Field(
        description=(
            "The browser client's own session: one it created or whose hostToolsFrom it "
            "is; once the session is closed, only its hostToolsFrom."
        )
    )
    turnId: str = Field(min_length=1, max_length=200, description="The turn judged.")
    outcome: TurnOutcomeValue = Field(description="ok | failed | corrected | abandoned.")
    by: TurnOutcomeBy = Field(description="user, host or judge.")
    reason: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Why, at most 200 characters. Refused when it holds what memory.write "
            "refuses (a phone number, a card number, a credential, ...)."
        ),
    )


class TurnOutcomeResult(Payload):
    seq: int = Field(description="The seq of the stored turn.outcome event.")


class SessionToolContentParams(Payload):
    sessionId: str = Field(description="Session the tool ran in.")
    callId: str = Field(description="tool.result callId whose content to fetch.")


class SessionToolContentResult(Payload):
    callId: str = Field(description="The call.")
    content: list[ToolContentBlock] = Field(
        default_factory=list, description="The content blocks, images with their bytes."
    )


class SessionArtifact(Payload):
    path: str = Field(description="Absolute path.")
    name: str = Field(description="Path relative to artifacts/.")
    size: int = Field(description="Bytes.")
    modifiedAt: str = Field(description="UTC ISO-8601 time of the last change.")
    mimeType: str | None = Field(default=None, description="Guessed from the name.")


class SessionArtifactsResult(Payload):
    workspaceDir: str = Field(description="The session workspace.")
    artifacts: list[SessionArtifact] = Field(
        default_factory=list, description="Files under artifacts/, newest first."
    )


class SessionAttachment(Payload):
    """A file the user attached in this session, as attached (1.23.0)."""

    name: str = Field(description="Sanitized original filename.")
    path: str = Field(
        description=(
            "Absolute path of the original bytes: under $SNOWPEA_HOME/attachments/<session>/"
            "files/ for an upload, or the user's own file when they attached one by path."
        )
    )
    mime: str = Field(description="Sniffed media type.")
    size: int = Field(description="Bytes.")
    messageId: str | None = Field(
        default=None, description="The turn whose prompt brought it (its turnId)."
    )
    byPath: bool = Field(
        default=False,
        description=(
            "True for a file the user attached by path, recorded where it is rather than "
            "copied: it can be anywhere on disk, so a host should not act on it unasked. "
            "False for bytes the user uploaded, kept under attachments/<session>/files/."
        ),
    )


class SessionAttachmentsResult(Payload):
    attachments: list[SessionAttachment] = Field(
        default_factory=list, description="This session's attachments, oldest first."
    )


class SessionRenameParams(Payload):
    sessionId: str = Field(description="Session to rename.")
    title: str = Field(description="New title; empty clears it (1.7.0).")


class SessionsChangedNotification(Payload):
    """Broadcast to every authenticated connection when the session list moves."""

    reason: str = Field(description="create, prompt, turn, renamed, close, ...")
    sessionId: str = Field(description="Session that changed.")
    kind: SessionKind | None = Field(
        default=None,
        description=(
            "As SessionSummary.kind: what opened the session (chat, agent, a delegated "
            "child, a routine run), so a client can tell a child from a new chat."
        ),
    )
    parentSessionId: str | None = Field(
        default=None,
        description="As SessionSummary.parentSessionId: the delegating session; null for a root.",
    )
    status: str | None = Field(default=None, description="Its status now, when known.")
    title: str | None = Field(default=None, description="Its title, when it has one.")
    hostToolsFrom: str | None = Field(
        default=None,
        description="The session's owning client (addendum 8); null for unhosted sessions.",
    )
    browserProvider: Literal["host", "local"] | None = Field(
        default=None,
        description=(
            "A browser session's browser: 'host' (Snowpea browser only) or 'local' "
            "(the user opted in to core's own browser); null for non-browser sessions."
        ),
    )
    role: Literal["jarvis"] | None = Field(
        default=None,
        description="As SessionSummary.role (1.53.0): 'jarvis' for the Jarvis session.",
    )
    jarvis: SessionJarvisState | None = Field(
        default=None,
        description="As SessionSummary.jarvis (1.53.0); null while Jarvis mode is off.",
    )
    jarvisOn: bool | None = Field(
        default=None,
        description="As SessionSummary.jarvisOn (1.56.0, capability jarvisSessionSwitch).",
    )
    jarvisLocked: JarvisSwitchLock | None = Field(
        default=None,
        description="As SessionSummary.jarvisLocked (1.56.0, capability jarvisSessionSwitch).",
    )


class UsageSummaryParams(Payload):
    since: str | None = Field(default=None, description="UTC ISO start (inclusive).")
    until: str | None = Field(default=None, description="UTC ISO end (exclusive).")
    groupBy: Literal["provider", "model", "session", "day"] = Field(
        default="day", description="How to group the totals."
    )


class UsageRow(Payload):
    key: str = Field(description="Group value: vendor, vendor:model, session id or YYYY-MM-DD.")
    inputTokens: int = Field(default=0, description="Prompt tokens.")
    outputTokens: int = Field(default=0, description="Completion tokens.")
    calls: int = Field(default=0, description="Provider calls counted.")


class UsageSummaryResult(Payload):
    groupBy: str = Field(description="The grouping used.")
    rows: list[UsageRow] = Field(default_factory=list, description="Totals, largest first.")
    inputTokens: int = Field(default=0, description="Sum over all rows.")
    outputTokens: int = Field(default=0, description="Sum over all rows.")


class SessionNoticeParams(Payload):
    sessionId: str = Field(description="Session to tell.")
    text: str = Field(
        description=(
            "Something that happened outside a tool call (a popup opened, a download "
            "finished). Reaches the model as a [system] line before its next call; "
            "it starts no turn (1.7.0)."
        )
    )


class SessionSteerParams(Payload):
    sessionId: str = Field(description="Session with a running turn.")
    text: str = Field(
        description=(
            "User message injected at the running turn's next tool-round boundary; "
            "with no turn running it starts one like session.prompt (1.6.0). Text that "
            "starts with '/' once trimmed is refused with steer_slash_command (1.50.0)."
        )
    )


class SessionContinueParams(Payload):
    sessionId: str = Field(description="A chat session the caller may drive.")
    reason: str = Field(
        default="handback", description="Why the session continues, e.g. 'handback'."
    )
    note: str = Field(description="The [system] line the model sees, e.g. 'the tab is back'.")
    idempotencyKey: str | None = Field(
        default=None, description="The same key twice: the second call is a no-op."
    )


class SessionContinueResult(Payload):
    started: bool = Field(description="True when a new system-initiated turn started.")
    turnId: str | None = Field(default=None, description="The turn that started, if any.")
    duplicate: bool | None = Field(
        default=None, description="True when idempotencyKey was already used."
    )
    queued: bool | None = Field(
        default=None,
        description="True when a turn was running and the note joins its next model call.",
    )


class SessionSteerResult(Payload):
    ok: bool = Field(default=True, description="True when accepted.")
    started: bool = Field(
        default=False, description="True when no turn was running and a new one started."
    )


class SessionCreateResult(Payload):
    sessionId: str = Field(description="Id of the new session.")
    workspaceDir: str | None = Field(
        default=None,
        description="Session workspace with tmp/ and artifacts/ (1.7.0).",
    )
    delegation: bool = Field(
        default=False,
        description=(
            "Whether the session starts in delegation mode (the lead hands the work "
            "to its team): the session pin, else agents.delegateByDefault."
        ),
    )
    role: Literal["jarvis"] | None = Field(
        default=None,
        description="As SessionSummary.role (1.53.0): 'jarvis' for the Jarvis session.",
    )
    jarvis: SessionJarvisState | None = Field(
        default=None,
        description="As SessionSummary.jarvis (1.53.0); null while Jarvis mode is off.",
    )
    jarvisOn: bool | None = Field(
        default=None,
        description="As SessionSummary.jarvisOn (1.56.0, capability jarvisSessionSwitch).",
    )
    jarvisLocked: JarvisSwitchLock | None = Field(
        default=None,
        description="As SessionSummary.jarvisLocked (1.56.0, capability jarvisSessionSwitch).",
    )


class SessionEvent(Payload):
    """One entry of a session's ordered event log."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number; resume replays after it.")
    kind: str = Field(description="Event kind; see sessionEventKinds in the schema dump.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")
    turnId: str | None = Field(
        default=None,
        description="Turn the event happened in (1.44.0); null outside a turn and before 1.44.",
    )
    step: int | None = Field(
        default=None,
        description=(
            "Model call within the turn, from 1 (1.44.0); 0 before the turn's first call, "
            "null outside a turn and before 1.44."
        ),
    )


class SessionResumeParams(Payload):
    sessionId: str = Field(description="Session to resume.")
    afterSeq: int | None = Field(default=None, description="Replay only events with a greater seq.")
    locale: str | None = Field(
        default=None,
        description=(
            "The client's UI language as a BCP 47 tag (1.7.0), as on session.create. "
            "Kept in memory only, so a client re-sends it after a daemon restart; "
            "omitted leaves the session's current locale."
        ),
    )


class SessionOrigin(Payload):
    """The connection that answers a session's approvals and questions (1.26.0).

    A client that re-attaches sessions on reconnect checks this first and
    leaves alone a session another live client (an IDE continuing the chat)
    already owns.
    """

    clientKind: str | None = Field(
        default=None, description="'browser', 'ide', 'tui', ... as the client said in hello."
    )
    clientId: str | None = Field(
        default=None,
        description=(
            "The client's stable id from hello — only when it is the caller's own "
            "(mine); other clients' ids are not handed out."
        ),
    )
    live: bool = Field(description="False once that connection has closed.")
    mine: bool = Field(
        default=False,
        description="True when the caller is that connection, or has the same clientId.",
    )


class SessionResumeResult(Payload):
    sessionId: str = Field(description="Session that was resumed.")
    mode: Mode | None = Field(
        default=None,
        description=(
            "The session's permission mode as restored, so a surface that resumes "
            "adopts it instead of keeping the mode it launched with."
        ),
    )
    autoAfterPlan: bool = Field(
        default=False,
        description="Plan-then-auto as restored with the mode (1.32.0).",
    )
    provider: str | None = Field(default=None, description="Chat provider vendor in use.")
    model: str | None = Field(default=None, description="Model id in use.")
    effort: str | None = Field(default=None, description="Reasoning effort pinned on the session.")
    effectiveEffort: EffortLevel | None = Field(
        default=None,
        description=(
            "The effort in force (1.29.0): the pin, or what the settings give, clamped to "
            "what the model offers; null when the model has no effort setting or the "
            "session is closed. 'effort' stays the session's own pin."
        ),
    )
    effortSource: EffortSource | None = Field(
        default=None, description="Rule that decided effectiveEffort (1.29.0)."
    )
    efforts: list[EffortLevel] | None = Field(
        default=None,
        description="Tiers the session's model offers; [] means none (1.29.0).",
    )
    delegation: bool = Field(
        default=False,
        description="Whether delegation mode is in force: the session pin, else the default.",
    )
    events: list[SessionEvent] = Field(
        default_factory=list, description="Missed events in seq order."
    )
    origin: SessionOrigin | None = Field(
        default=None,
        description="Who owns the session's approvals after this resume (1.26.0).",
    )


class SessionSummary(Payload):
    """One row of ``session.list``."""

    sessionId: str = Field(description="Session id.")
    workdir: str = Field(description="Absolute working directory.")
    mode: Mode = Field(description="Current permission mode.")
    autoAfterPlan: bool = Field(
        default=False,
        description=(
            "PLAN mode with every approval answered yes, switching to auto once the "
            "plan is done (1.32.0); false outside plan mode."
        ),
    )
    provider: str | None = Field(default=None, description="Chat provider vendor in use.")
    model: str | None = Field(default=None, description="Model id in use.")
    originSurface: str | None = Field(default=None, description="Surface that owns approvals.")
    createdAt: str = Field(description="UTC ISO-8601 creation timestamp.")
    seq: int = Field(default=0, description="Sequence number of the latest event.")
    contextUsed: int = Field(
        default=0, description="Tokens the session's current prompt occupies (CORE-context)."
    )
    contextWindow: int | None = Field(
        default=None,
        description=(
            "Context window of the session's model; never null from 1.42.0 (null only "
            "before the session has reported one)."
        ),
    )
    contextWindowSource: WindowSource | None = Field(
        default=None, description="Where contextWindow came from (1.42.0)."
    )
    lastPrompt: str | None = Field(
        default=None,
        description=(
            "Latest prompt a person typed (a message.user whose origin is human, or one from "
            "before 1.53.0 without an origin); goal rounds, hand-backs, scheduled and "
            "delegation prompts never replace it (1.53.0). Falls back to the newest typed "
            "/command for a session that only ever ran commands."
        ),
    )
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Reasoning effort pinned to this session, or null when it follows "
            "agent.effortBy / agent.effort."
        ),
    )
    effectiveEffort: EffortLevel | None = Field(
        default=None,
        description=(
            "The effort in force (1.29.0): the pin, or what the settings give, clamped to "
            "what the model offers; null when the model has no effort setting or the "
            "session is closed. 'effort' stays the session's own pin."
        ),
    )
    effortSource: EffortSource | None = Field(
        default=None, description="Rule that decided effectiveEffort (1.29.0)."
    )
    efforts: list[EffortLevel] | None = Field(
        default=None,
        description="Tiers the session's model offers; [] means none (1.29.0).",
    )
    kind: SessionKind = Field(
        default="chat",
        description=(
            "What opened the session: a human (chat), a scheduled job, a spawned "
            "subagent, or a persistent named agent (CORE-session-kind)."
        ),
    )
    parentSessionId: str | None = Field(
        default=None,
        description="Session that caused this one: the thread that scheduled the job, "
        "or the parent that spawned the subagent.",
    )
    jobId: str | None = Field(
        default=None, description="Scheduled job this run belongs to; null for other kinds."
    )
    agent: str | None = Field(
        default=None, description="Named agent the session belongs to, when it has one."
    )
    running: bool = Field(
        default=False,
        description=(
            "True while the daemon has a turn in flight for this session. Always false "
            "for a stored row, which by definition has no live turn — a client should "
            "trust this rather than infer a running turn from a replay that ends on "
            "turn.started (CORE-dangling-turns)."
        ),
    )
    workspaceDir: str | None = Field(
        default=None,
        description="Session workspace with tmp/ and artifacts/ (1.7.0).",
    )
    title: str | None = Field(default=None, description="Short label (auto or renamed).")
    status: Literal[
        "idle", "running", "awaiting_approval", "awaiting_question", "error"
    ] = Field(default="idle", description="What the session is doing now (1.7.0).")
    lastActivityAt: str | None = Field(
        default=None,
        description=(
            "UTC ISO time of the last activity: while a turn runs, its latest stored event "
            "(tracked in memory); otherwise the last prompt or turn end."
        ),
    )
    turnStartedAt: str | None = Field(
        default=None, description="UTC ISO start of the running turn."
    )
    pendingApprovals: int = Field(default=0, description="Approvals waiting on a person.")
    pendingQuestions: int = Field(default=0, description="Questions waiting on a person.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Client whose host tools the session uses (a browser profile's clientId); "
            "null for IDE, TUI and CLI sessions (addendum 8)."
        ),
    )
    browserProvider: Literal["host", "local"] | None = Field(
        default=None,
        description=(
            "A browser session's browser: 'host' (Snowpea browser only) or 'local' "
            "(the user opted in to core's own browser); null for non-browser sessions."
        ),
    )
    workspaceId: str | None = Field(
        default=None, description="Workspace the conversation's folder belongs to (1.47.0)."
    )
    pinned: bool = Field(default=False, description="Pinned to the top of its workspace (1.47.0).")
    archived: bool = Field(default=False, description="Archived; hidden by default (1.47.0).")
    lastUserAt: str | None = Field(
        default=None,
        description=(
            "UTC ISO time of the latest prompt a person sent (a message.user event); null "
            "when there is none. Goal rounds and other system turns do not move it (1.50.0); "
            "since 1.53.0 only a message.user whose origin is human (or has no origin) does."
        ),
    )
    forkedFrom: str | None = Field(
        default=None,
        description=(
            "Session this one was forked from by session.fork; null otherwise (1.50.0). "
            "A fork keeps its source's title unchanged (1.53.0)."
        ),
    )
    role: Literal["jarvis"] | None = Field(
        default=None,
        description=(
            "'jarvis' for the daemon's Jarvis session (1.53.0), a kind='agent' session a "
            "client without capability 'jarvis' shows as an ordinary named agent; null "
            "for every other session."
        ),
    )
    jarvis: SessionJarvisState | None = Field(
        default=None,
        description=(
            "How Jarvis stands towards this session (1.53.0): exposed, delegation level "
            "and pause; null while Jarvis mode is off and on the Jarvis session itself."
        ),
    )
    jarvisOn: bool | None = Field(
        default=None,
        description=(
            "The session's Jarvis switch (1.56.0, capability jarvisSessionSwitch): true when "
            "Jarvis may see, read, steer and decide for it (within its grants); false "
            "(the default for a new session, jarvis.sessions.default) when, for Jarvis, it "
            "does not exist. Changed with jarvis.session.set. Kept while Jarvis mode is off. "
            "Null for a sub-agent row and from a daemon without the capability."
        ),
    )
    jarvisLocked: JarvisSwitchLock | None = Field(
        default=None,
        description=(
            "Why the switch cannot be changed (1.56.0): project_hidden (its project has "
            "jarvis.expose off: hidden whatever the switch says), jarvis_session (Jarvis's "
            "own session, its children or task workers: always on), browser_hidden (a "
            "browser-host session while jarvis.exposeBrowser is off). Null when it can."
        ),
    )
    forkedAtTurnId: str | None = Field(
        default=None,
        description="Turn of forkedFrom the fork was taken at; null for a non-fork (1.50.0).",
    )
    origin: SessionOrigin | None = Field(
        default=None,
        description=(
            "The connection that owns approvals now; null when none ever attached "
            "in this daemon run (saved rows, schedules) (1.26.0)."
        ),
    )
    machine: MachineInfo | None = Field(
        default=None,
        description=(
            "The machine whose daemon listed the row (1.40.0): a client that caches rows "
            "from several daemons matches them by machine.id."
        ),
    )


class SessionCompactParams(Payload):
    """``session.compact`` — summarise the conversation and replace it."""

    sessionId: str = Field(description="Session whose history to compact.")
    instructions: str | None = Field(
        default=None,
        description="Extra guidance for the summary, e.g. 'keep the API design decisions'.",
    )


class SessionCompactResult(Payload):
    before: int = Field(default=0, description="Estimated tokens the history held before.")
    after: int = Field(default=0, description="Estimated tokens the history holds now.")
    summaryChars: int = Field(default=0, description="Length of the summary in characters.")


class SessionListResult(Payload):
    sessions: list[SessionSummary] = Field(default_factory=list, description="Every live session.")
    complete: bool = Field(
        default=False,
        description=(
            "True when the list is all there is for what was asked (1.27.0): the "
            "daemon's boot has restored the sessions it reopens, and with includeClosed "
            "the saved ones were read. Without includeClosed it covers live sessions "
            "only, which after a restart is usually none; ask with includeClosed "
            "before forgetting a closed chat. False (or absent, from an older daemon) means "
            "an empty or short list proves nothing, so a client must not forget its "
            "own records by it."
        ),
    )


class SessionListParams(Payload):
    includeClosed: bool = Field(
        default=False,
        description=(
            "Include persisted closed sessions. Since 1.55.0 their rows carry running false, "
            "status 'idle', title (the stored title; for a scheduled run without one, the "
            "job's title or the first line of its task), lastActivityAt (time of the last "
            "stored event), lastUserAt and seq, like live rows."
        ),
    )
    workdir: str | None = Field(default=None, description="Only sessions rooted here.")
    kinds: list[str] | None = Field(
        default=None,
        description=(
            "Only sessions of these kinds; omit for every kind. A surface that shows "
            "human threads asks for [\"chat\"] (CORE-session-kind)."
        ),
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Only sessions owned by this client (a browser profile's clientId); omit "
            "for every session (addendum 8)."
        ),
    )
    includeUnhosted: bool = Field(
        default=False,
        description=(
            "With hostToolsFrom: also return sessions with no host (IDE, TUI, CLI), "
            "which a browser shows in every profile."
        ),
    )


class SessionDeleteParams(Payload):
    sessionId: str | None = Field(default=None, description="Delete one saved session.")
    workdir: str | None = Field(default=None, description="Delete saved sessions rooted here.")
    all: bool = Field(default=False, description="Delete saved sessions from every directory.")
    deleteWorkspace: bool = Field(
        default=True,
        description=(
            "Also delete each session's workspace ($SNOWPEA_HOME/sessions/<date>_<id>: "
            "tmp/, artifacts/, downloads) — it can hold page text the user deleted the "
            "chat to get rid of. false keeps it (1.7.0)."
        ),
    )


class SessionDeleteResult(Payload):
    deleted: int = 0


class SessionDeleteOutcome(Payload):
    ok: bool = Field(default=True, description="True when the call succeeded.")
    deleted: int = Field(default=0, description="Saved sessions removed (0 for an unknown id).")


class SessionIdParams(Payload):
    sessionId: str = Field(description="Target session.")


class SessionPromptParams(Payload):
    """Start a turn (or queue/steer one while a turn runs).

    Events, in order: ``turn.started {turnId, text}`` then ``message.user
    {text, attachments, refs, steered: false}`` for the prompt, then the model's
    output. A prompt folded into a running turn (``whenBusy: "steer"``) emits
    ``message.user {steered: true}`` and ``turn.dequeued {reason: "steered"}``;
    a queued one emits ``turn.queued`` first and the pair above when it starts.
    """

    sessionId: str = Field(description="Session to prompt.")
    text: str = Field(description="User text; a leading '/' is parsed as a slash command.")
    attachments: list[Attachment] | None = Field(
        default=None, description="Files or images to include."
    )
    whenBusy: Literal["queue", "steer"] | None = Field(
        default=None,
        description=(
            "If a turn is running: 'queue' waits for it, 'steer' folds this prompt "
            "into it at the next tool round. Default: the agent.busy setting (1.7.0)."
        ),
    )


class FileCompleteEntry(Payload):
    path: str = Field(
        description="Path relative to the workdir; directories end with '/'."
    )
    kind: Literal["file", "dir"] = Field(description="Whether the entry is a file or directory.")
    size: int | None = Field(default=None, description="Byte size for files.")


class FileCompleteParams(Payload):
    sessionId: str | None = Field(
        default=None, description="Session whose workdir to complete in."
    )
    workdir: str | None = Field(
        default=None, description="Workdir to complete in when no session is set."
    )
    query: str = Field(default="", description="Partial path to match.")
    limit: int | None = Field(
        default=None, description="Maximum entries to return (default 30, max 200)."
    )


class FileCompleteResult(Payload):
    entries: list[FileCompleteEntry] = Field(
        default_factory=list, description="Matching paths, best first."
    )
    truncated: bool = Field(
        default=False, description="True when more matches exist than returned."
    )


class TurnResult(Payload):
    turnId: str = Field(
        description=(
            "Id of the started turn; turn.done carries it back. Since 1.55.0, for an instant "
            "slash command sent while a turn runs, the command's id ('c-…'): no turn starts "
            "and command.result carries it back instead (besideTurn true)."
        )
    )
    besideTurn: bool | None = Field(
        default=None,
        description=(
            "1.55.0: true when the slash command ran beside the running turn (an instant "
            "command); absent otherwise."
        ),
    )


class SessionSetModeParams(Payload):
    sessionId: str = Field(description="Session to change.")
    mode: Mode = Field(
        description=(
            "New permission mode. Raising it (plan < accept < auto) needs a person (1.54.0, "
            "security review 4 H2): a local person, a remote web login, or a browser for a "
            "session it hosts (or opened) with actor 'person'; never a connector. Else "
            "jarvis_person_only with details.reason 'mode_raise'. Lowering is free."
        )
    )
    actor: Literal["person"] | None = Field(
        default=None,
        description="A browser (0.3.0+) sets 'person' when a click raised the mode (1.54.0).",
    )
    autoAfterPlan: bool | None = Field(
        default=None,
        description=(
            "With mode 'plan' (1.32.0): true keeps planning but answers every approval "
            "'allow' (a deny stays a deny), and set_mode then offers auto first; false "
            "clears it. Omitted keeps it while staying in plan and clears it when plan "
            "mode is entered afresh. Any other mode always clears it."
        ),
    )


class SessionSetModeResult(Payload):
    mode: Mode = Field(description="Mode now in effect.")
    autoAfterPlan: bool = Field(
        default=False, description="Whether plan-then-auto is now in force (1.32.0)."
    )


class SessionSetModelParams(Payload):
    """``session.setModel`` — pin one session to a model (CORE-model-assignment)."""

    sessionId: str = Field(description="Session to pin.")
    model: str | None = Field(
        default=None,
        description=(
            "A models.profiles id, a 'vendor:model' pair, or a bare vendor name. "
            "Null or 'inherit' clears the pin and lets the configured routing decide."
        ),
    )


class SessionSetModelResult(Payload):
    provider: str | None = Field(default=None, description="Vendor now in effect.")
    model: str | None = Field(default=None, description="Model id now in effect.")
    pinned: bool = Field(default=False, description="False when the pin was cleared.")


# --------------------------------------------------------------------------
# audio.*
# --------------------------------------------------------------------------


class AudioCapabilitiesResult(Payload):
    """What voice I/O can do on the daemon's machine right now.

    Every field can be off; ``reasons`` says why, so a client can tell the
    user what to install instead of failing silently (CORE-multimodal).
    """

    stt: str | None = Field(
        default=None, description="Transcription backend in use, or null when there is none."
    )
    tts: bool = Field(default=False, description="True when speech synthesis is available.")
    ttsProvider: str | None = Field(default=None, description="Speech backend in use.")
    sttPinned: bool = Field(
        default=False,
        description=(
            "True when audio.stt.provider names an engine. False means unset, which "
            "means voice input is off: there is no fallback chain."
        ),
    )
    ttsPinned: bool = Field(
        default=False, description="True when audio.tts.provider names an engine."
    )
    sttEffective: str | None = Field(
        default=None,
        description="The engine actually in use, or null when none is pinned or it is missing.",
    )
    sttLanguage: str = Field(
        default="auto",
        description=(
            "'auto' (the engine detects, or the reply language decides) or the BCP-47 "
            "tag audio.stt.language forces."
        ),
    )
    sttLanguageSource: str = Field(
        default="auto",
        description=(
            "Which rung decided it: setting | reply | detect. 'detect' means the "
            "engine works it out itself and nothing had to choose."
        ),
    )
    ttsEffective: str | None = Field(
        default=None, description="The speech engine actually in use, or null."
    )
    voice: str | None = Field(default=None, description="Configured voice, when one is set.")
    record: bool = Field(default=False, description="True when the microphone can be recorded.")
    play: bool = Field(default=False, description="True when the daemon can play audio itself.")
    autoSpeak: bool = Field(
        default=False, description="True when replies are spoken without being asked."
    )
    sttProviders: list[str] = Field(
        default_factory=list, description="Every usable transcription backend, preferred first."
    )
    ttsProviders: list[str] = Field(
        default_factory=list, description="Every usable speech backend, preferred first."
    )
    players: list[str] = Field(default_factory=list, description="Audio players found on PATH.")
    recorders: list[str] = Field(default_factory=list, description="Recorders found on PATH.")
    reasons: dict[str, str] = Field(
        default_factory=dict, description="Per-capability explanation of why it is off."
    )


class AudioTranscribeParams(Payload):
    """Turn recorded audio into text.  Give either ``path`` or ``data``."""

    path: str | None = Field(
        default=None,
        description=(
            "Audio file on the daemon's machine. From a remote web login, a browser or a "
            "connector (1.55.0): only a recording or an attachment of a session (a file "
            "under the daemon's audio or attachments folder, as audio.record.stop answers); "
            "any other path is outside_root. Inline data is not affected."
        ),
    )
    data: str | None = Field(default=None, description="Base64 (or data-URI) audio.")
    mime: str | None = Field(default=None, description="Media type of the audio, e.g. audio/wav.")
    language: str | None = Field(default=None, description="BCP-47 hint for the backend.")
    sessionId: str | None = Field(default=None, description="Session the audio belongs to.")


class AudioTranscribeResult(Payload):
    text: str = Field(description="What the backend heard.")
    provider: str = Field(description="Backend that produced the transcript.")


class AudioVoice(Payload):
    """One voice a speech engine can speak with."""

    id: str = Field(description="Voice id, as audio.tts.voices stores it.")
    label: str = Field(description="Display name.")
    language: str = Field(
        default="*",
        description=(
            "BCP-47 tag, or '*' for a voice that works in every language the "
            "engine supports (Supertonic's presets are '*': one multilingual model)."
        ),
    )
    gender: str | None = Field(default=None, description="male / female, when known.")
    installed: bool = Field(
        default=True,
        description="False when choosing it downloads something first (piper voices).",
    )
    sizeBytes: int | None = Field(
        default=None, description="Roughly what the download weighs, when it is one."
    )
    sample: bool = Field(
        default=True, description="True when a preview can be synthesised here and now."
    )


class AudioVoicesParams(Payload):
    """List the voices one speech engine offers."""

    engine: str = Field(description="Engine id, e.g. supertonic, piper, edge-tts.")
    languages: list[str] = Field(
        default_factory=list,
        description=(
            "Filter a large catalogue to these BCP-47 tags; empty uses ko and en "
            "plus the session's reply language. Ignored by engines with few voices."
        ),
    )


class AudioVoicesResult(Payload):
    voices: list[AudioVoice] = Field(
        default_factory=list, description="Installed voices first, then by language and id."
    )


class AudioInstallParams(Payload):
    """Install one local voice engine, or one of its voices."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    engine: str = Field(
        description=(
            "Engine id from the setup catalog: faster-whisper (or local-whisper), "
            "piper, edge-tts. A system package (espeak-ng, say, powershell) answers "
            "ok=false with a hint instead."
        )
    )
    voice: str | None = Field(
        default=None,
        description=(
            "Install one of the engine's voices rather than the engine itself, e.g. "
            "piper's ko_KR-kss-medium. Same staged progress; installing a voice "
            "does not select it."
        ),
    )
    warmup: bool = Field(
        default=False,
        description=(
            "When true, download first-run assets only — Supertonic's ONNX models — "
            "with staged progress, without reinstalling the package."
        ),
    )


class AudioInstallResult(Payload):
    """What ``audio.install`` did, log and all."""

    ok: bool = Field(description="True when the engine is installed and now detected.")
    engine: str = Field(description="Engine that was attempted, after id normalisation.")
    voice: str | None = Field(
        default=None,
        description="The voice that was installed, when the request named one.",
    )
    log: str = Field(
        default="",
        description="Tail of the installer's combined output, newest last; may be empty.",
    )
    hint: str | None = Field(
        default=None,
        description=(
            "What to do instead, when ok is false: the platform's own install command "
            "for a system package, or why the attempt could not run."
        ),
    )


class AudioSpeakParams(Payload):
    """Synthesise speech; the client plays it unless ``play`` is set."""

    text: str = Field(description="What to say.")
    sessionId: str | None = Field(default=None, description="Session the audio belongs to.")
    voice: str | None = Field(default=None, description="Voice id; defaults to the setting.")
    provider: str | None = Field(
        default=None,
        description=(
            "Speech backend to use for this call only. When set, the pinned voice-out "
            "engine is not required — settings screens use this to preview before saving."
        ),
    )
    language: str | None = Field(
        default=None,
        description="BCP-47 language for voice selection; defaults to the stt language setting.",
    )
    play: bool = Field(
        default=False, description="Play on the daemon's machine instead of returning only a path."
    )


class AudioSpeakResult(Payload):
    path: str = Field(description="Audio file the speech was written to.")
    mime: str = Field(description="Media type of that file.")
    provider: str = Field(description="Backend that synthesised it.")
    voice: str | None = Field(default=None, description="Voice that was used.")
    played: bool = Field(default=False, description="True when the daemon played it.")


class AudioRecordStartParams(Payload):
    sessionId: str | None = Field(default=None, description="Session the recording belongs to.")


class AudioRecordStopParams(Payload):
    sessionId: str | None = Field(default=None, description="Session that is recording.")
    transcribe: bool = Field(
        default=False, description="Also transcribe the recording and return its text."
    )


class AudioRecordResult(Payload):
    path: str = Field(description="Wav file being written, or the finished recording.")
    recording: bool = Field(description="True while capture is still running.")
    mime: str = Field(default="audio/wav", description="Media type of the recording.")
    text: str | None = Field(
        default=None, description="Transcript, when 'stop' was asked to transcribe."
    )
    provider: str | None = Field(default=None, description="Backend that transcribed it.")


# --------------------------------------------------------------------------
# command.* / tool.*
# --------------------------------------------------------------------------


class OptionalSessionParams(Payload):
    sessionId: str | None = Field(
        None, description="Scope the listing to one session; omit for the global set."
    )


class CommandInfo(Payload):
    """One slash command."""

    name: str = Field(description="Command name without the leading slash.")
    summary: str = Field(description="One-line description shown in /help.")
    argsSchema: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema for the argument string."
    )
    source: CommandSource = Field(default="builtin", description="Where the command came from.")
    format: ItemFormat = Field(default="snowpea", description=_FORMAT_DESCRIPTION)


class CommandListResult(Payload):
    commands: list[CommandInfo] = Field(
        default_factory=list, description="Available slash commands."
    )


class CommandRunParams(Payload):
    sessionId: str = Field(description="Session to run the command in.")
    name: str = Field(description="Command name without the leading slash.")
    args: str = Field(default="", description="Raw argument string, as typed after the name.")
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "Browser marker (security review 3): a browser 0.3.0+ must send 'person' to run "
            "a command that persists a marker-required setting (commands/persists.py); "
            "ignored for other kinds."
        ),
    )
    whenBusy: Literal["queue", "steer"] | None = Field(
        default=None,
        description=(
            "1.55.0: while a turn runs, a command of kind 'turn' waits like a prompt "
            "(turn.queued); 'steer' cannot fold a command into the running request, so it "
            "queues too. An 'instant' command runs beside the turn (command.result)."
        ),
    )


class ToolInfo(Payload):
    """One registered tool."""

    name: str = Field(description="Tool name as the model calls it.")
    category: str = Field(description="Grouping used by the UI.")
    permissionTag: PermissionTag = Field(description="Permission class checked against the mode.")
    state: ToolState = Field(
        default="active", description="Inactive tools are hidden from the model."
    )
    source: str = Field(default="builtin", description="builtin, skill, plugin or MCP server name.")
    server: str = Field(
        default="",
        description="MCP server this tool came from; empty for everything else (M14 §3).",
    )
    description: str = Field(default="", description="Text shown to the model.")
    provider: str = Field(
        default="",
        description=(
            "Backing provider for tools that have one, e.g. the web-search provider id; "
            'reads "configured → answering" when the configured one cannot run.'
        ),
    )
    reason: str = Field(
        default="",
        description="Why an inactive tool is inactive, e.g. \"lsp.enabled is false\".",
    )
    deferred: bool = Field(
        default=False,
        description=(
            "True when the model is told the tool's name but not its schema until it "
            "calls tool_search; the tool is still listed and still callable."
        ),
    )


class ToolListResult(Payload):
    tools: list[ToolInfo] = Field(default_factory=list, description="Registered tools.")


#: A helper name inside a host tool: an identifier, dotted or not (``page.goto``).
#: It is quoted to the model, so it carries nothing but a name (review 6, L-4).
HELPER_NAME_PATTERN = r"^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$"


class HostToolSpec(Payload):
    """One tool a client runs itself (``tool.register``, 1.6.0)."""

    name: str = Field(
        description=(
            "Tool name, [A-Za-z][A-Za-z0-9_-]{0,63}. May not collide with a daemon tool "
            "except the built-in browser_* tools, which it shadows for the sessions "
            "that see this connection's tools."
        )
    )
    description: str = Field(
        max_length=65536,
        description="What the model reads about the tool (at most 65536 characters).",
    )
    inputSchema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description="JSON Schema of the arguments.",
    )
    permission: PermissionTag = Field(description="Permission class checked against the mode.")
    category: str | None = Field(default=None, description='UI grouping; defaults to "host".')
    kind: Literal["act", "read"] | None = Field(
        default=None,
        description=(
            "1.69.0 (capability hostToolKind): 'act' when a call can change something "
            "outside the conversation (a click, a form fill, a repl that acts on a page) "
            "even though permission stays read because the host gates it itself; 'read' "
            "when it only looks. Absent: unknown, judged from permission (a repl or a "
            "browser-category host tool counts as acting). The answer-now note never "
            "fires in a turn that called an acting tool."
        ),
    )
    timeoutMs: int | None = Field(
        default=None,
        description=(
            "How long tool.invoke may stay silent; default 120000. tool.progress restarts "
            "it, and it is paused while the session waits on an approval or a question."
        ),
    )
    parallel: Literal["safe", "never"] = Field(
        default="never",
        description=(
            "'safe': several calls of it in one model response may run at once, like the "
            "built-in readers (1.18.0). Only for permission read or network; 'never' (the "
            "default) keeps it in order."
        ),
    )
    deferred: bool = Field(
        default=False,
        description=(
            "Name it in the prompt and send its schema only after tool_search loads it, "
            "when the daemon has tools.agentDeferred on and the session's provider keeps a "
            "fixed tool list (1.21.0). Calling it by name still works."
        ),
    )
    agents: list[str] | None = Field(
        default=None,
        description=(
            "Only sessions running as one of these agent definitions see it (1.24.0); "
            "null: every session of this host."
        ),
    )
    descriptionBy: (
        dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=65536)]] | None
    ) = Field(
        default=None,
        max_length=50,
        description=(
            "A description for the sessions of one agent definition (1.35.0), by agent "
            "name: a tool a coding agent uses a little can carry a short manual while "
            "the browsing agent keeps the long one in description. Other agents read "
            "description. At most 50 entries, names up to 64 characters, each text up to "
            "65536."
        ),
    )
    look: bool = Field(
        default=False,
        description=(
            "A successful call shows the rendered page (a snapshot or screenshot), which "
            "verify-on-stop counts as looking at a UI change (1.29.0). A result can also "
            "say so itself with meta look: true; repl code calling snapshot(, "
            "annotatedScreenshot(, snapshotTabs(, display( or .screenshot( counts too."
        ),
    )
    helpers: list[Annotated[str, Field(max_length=64, pattern=HELPER_NAME_PATTERN)]] | None = Field(
        default=None,
        max_length=200,
        description=(
            "1.55.0: names that exist inside this tool, not as tools (a REPL's globals such "
            "as snapshot or click). A call to one of them as a tool, or a tool_search for "
            "it, is answered '<name> is not a tool: it is a helper inside <tool>; call it "
            "in that tool's code'. Each is an identifier, dotted or not (page.goto): "
            "letters, digits, _ and $, up to 64 characters; anything else is invalid_params."
        ),
    )
    descriptionShort: str | None = Field(
        default=None,
        max_length=4000,
        description=(
            "1.55.0: a short description the model gets in the tool schema instead of "
            "description; core adds one line pointing at tool_help. description stays the "
            "full manual, which tool_help returns."
        ),
    )
    sections: (
        dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=65536)]] | None
    ) = Field(
        default=None,
        max_length=50,
        description=(
            "1.55.0: named parts of the manual (name -> text, names up to 64 chars, at most "
            "50; a text is kept up to 20000 characters and refused over 65536). "
            "tool_help {tool, section} returns one; tool_help {tool} lists them. A "
            "failed result whose meta carries helpSection: '<name>' gets that section "
            "appended, once per session."
        ),
    )


class ToolRegisterParams(Payload):
    tools: list[HostToolSpec] = Field(
        description="Tools to add or replace for this connection; all-or-nothing."
    )
    capabilities: list[str] | None = Field(
        default=None,
        description=(
            "What this connection answers besides its tools (1.31.0): 'turnContext' "
            "opts in to host.turnContext for the sessions whose host tools it serves "
            "(a browser host only); 'turnContextCompaction' (1.37.0) also asks it again "
            "after each compaction. A list replaces the connection's earlier one; "
            "omitted keeps it. Unknown names are ignored."
        ),
    )


class ToolRegisterResult(Payload):
    registered: list[str] = Field(default_factory=list, description="Names now registered.")


class ToolUnregisterParams(Payload):
    names: list[str] = Field(description="This connection's tools to remove.")


class ToolUnregisterResult(Payload):
    removed: list[str] = Field(default_factory=list, description="Names actually removed.")


class ToolContentBlock(Payload):
    """One block of a rich host-tool result."""

    type: Literal["text", "image"] = Field(description="Block kind.")
    text: str | None = Field(default=None, description="Text, for 'text'.")
    mediaType: str | None = Field(default=None, description="e.g. image/png, for 'image'.")
    data: str | None = Field(default=None, description="Base64 bytes, for 'image'.")
    contentRef: str | None = Field(
        default=None,
        description=(
            "In a tool.result event, an image is sent as a reference instead of its "
            "bytes; fetch them with session.toolContent (1.7.0)."
        ),
    )
    ephemeral: bool | None = Field(
        default=None,
        description=(
            "For 'image' (1.37.0): a picture taken only to look at (a screenshot). "
            "The model sees it in the next request; after that it stays in the "
            "conversation as a one-line text stub, so the requests after it share a "
            "prefix and a long session does not carry old pictures."
        ),
    )


class UserTurn(Payload):
    """The person's own words behind a turn (``tool.invoke`` ``userTurn``)."""

    messageId: str = Field(description="The turn the message started.")
    text: str = Field(description="What the user wrote, as they wrote it.")
    steers: list[str] = Field(
        default_factory=list,
        description="Messages the user sent into the turn while it ran, in order.",
    )
    current: bool = Field(
        default=True,
        description=(
            "True when this message started the root's running turn. False when the "
            "agent (queue_command, a delegation's announce turn, a queued /ralplan), a "
            "schedule, another gateway group member or the system started it: the text "
            "is then the user's last message, not a request made in this turn."
        ),
    )


TurnOrigin = Literal["human", "agent", "member", "schedule", "system", "jarvis"]


class JobRunContext(Payload):
    """The scheduled run behind a turn (``tool.invoke`` ``job``, 1.23.0)."""

    id: str = Field(description="The job.")
    runId: str = Field(description="Unique per firing; a host acts on one at most once.")
    scheduledAt: str = Field(description="UTC ISO-8601 occurrence this run is for.")
    trigger: Literal["schedule", "runNow"] = Field(
        description="'schedule' for a firing of its schedule, 'runNow' for job.runNow."
    )
    createdVia: Literal["rpc", "tool"] = Field(
        description=(
            "'tool' when an agent's schedule_create or a /schedule command made it; 'rpc' "
            "when a client holding the daemon token called job.schedule. Not proof that a "
            "person made it: anchor trust in the host's own record of the job."
        )
    )
    ownerClientId: str | None = Field(
        default=None, description="The browser clientId that owns a job with meta."
    )
    fingerprint: str = Field(
        description=(
            "'sha256:' over the canonical JSON of {task, spec, agent, mode, workdir, "
            "hostToolsFrom, hostWaitSec, catchUpWindowMinutes, channel, meta}: any change "
            "to the job changes it. It does not cover the named agent's definition or "
            "memory, the workdir's files, or global settings (approval rules, providers, "
            "models). Compare it with the value job.schedule "
            "returned; do not recompute it."
        )
    )
    meta: dict[str, Any] | None = Field(
        default=None, description="The opaque meta its owner stored, as stored."
    )


class ToolInvokeModel(Payload):
    """The model a session's ``tool.invoke`` serves (1.55.0)."""

    canSee: bool = Field(
        description=(
            "True when an image in the result reaches the model as an image (the model "
            "takes image input, or is a local server's model not known to be blind)."
        )
    )
    contextWindow: int | None = Field(
        default=None, description="The model's context window in tokens; null when unknown."
    )
    localStyle: bool = Field(
        description=(
            "True for a llama.cpp / vLLM / Ollama-style server: one fixed tool list per "
            "session and a prefix cache that any change to earlier messages invalidates."
        )
    )


class ToolInvokeRequest(Payload):
    """Server -> client: run one of the client's host tools."""

    sessionId: str = Field(description="Session whose turn called the tool.")
    turnId: str = Field(default="", description="Turn the call belongs to.")
    callId: str = Field(description="Id of the tool call; tool.progress refers to it.")
    name: str = Field(description="Host tool name.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments from the model.")
    mode: Mode = Field(
        default="accept",
        description=(
            "The session's mode. A code-running tool registered as 'read' must refuse "
            "mutations in plan mode itself and escalate the rest with approval.ask."
        ),
    )
    workspaceDir: str = Field(
        default="",
        description="The session workspace (tmp/, artifacts/); the workdir when it has none.",
    )
    workdir: str = Field(
        default="",
        description="The session's project folder, absolute (Projects, 1.7.0).",
    )
    parentSessionId: str | None = Field(
        default=None,
        description=(
            "The session that spawned this one (a subagent's parent); null for a root "
            "session. A browser host shares the parent's agent tabs with it."
        ),
    )
    userTurn: UserTurn | None = Field(
        default=None,
        description=(
            "The root conversation's latest message the user typed, trusted (1.22.0); "
            "'current' says whether it started the running turn. For a delegated child it "
            "is the root's, never the parent agent's task text; only the session's own "
            "person's words count (not an agent's, a schedule's or another group member's), "
            "and null until the person has typed one."
        ),
    )
    origin: TurnOrigin | None = Field(
        default=None,
        description=(
            "Who started the root conversation's running turn (1.23.0): the session's own "
            "person, the agent, a schedule, another gateway group member or the system. A "
            "delegated child reports its root's."
        ),
    )
    job: JobRunContext | None = Field(
        default=None,
        description=(
            "The scheduled run behind the root's running turn (1.23.0); set only when "
            "origin is 'schedule'. A delegated child reports its root's."
        ),
    )
    guard: str | None = Field(
        default=None,
        description=(
            "'agent-switch' while session.setAgent {guardTurn} guards this session or one it "
            "was delegated from (1.25.0): a host asks before its own acting tools, whatever "
            "tag they were registered with."
        ),
    )
    attachments: list[SessionAttachment] = Field(
        default_factory=list,
        description=(
            "Files the user attached in the root conversation (1.23.0), as "
            "session.attachments lists them: the paths a host may hand to a page upload."
        ),
    )
    model: ToolInvokeModel | None = Field(
        default=None,
        description=(
            "1.55.0: the session's current model, so a host can shape what it returns "
            "(a screenshot only for a model that can see, a shorter snapshot for a small "
            "window). Absent from older daemons."
        ),
    )


class ToolInvokeResult(Payload):
    ok: bool = Field(description="False reports the call as a tool error.")
    output: str = Field(default="", description="Text result the model reads.")
    error: str | None = Field(default=None, description="Error text when ok is false.")
    content: list[ToolContentBlock] | None = Field(
        default=None,
        description=(
            "Rich result blocks. Images reach vision models as image input; "
            "text blocks are appended to output."
        ),
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Extra facts. meta.sensitive=true keeps the output out of the session "
            "store ([redacted]), memory and compaction summaries. meta.kind='snapshot' with "
            "meta.tab marks the result as that tab's snapshot (addendum 23); since 1.55.0 "
            "this also covers its image blocks, whatever text the result carries, so a newer "
            "view of the tab stubs older ones in a long request. meta.helpSection (1.55.0) "
            "on a failed result names a section of the tool's manual to append, once per "
            "session."
        ),
    )


class HostTurnContextRequest(Payload):
    """Server -> client, at turn start: a note for the turn's first model call (1.31.0).

    Sent once per turn, only to the connection that serves the session's host
    tools and declared ``tool.register {capabilities: ["turnContext"]}``, and
    even while site memory is off for the root conversation (``siteMemory``
    says which, 1.35.0).  Core waits 300 ms at most; a late, failed or missing
    answer means no note.
    """

    sessionId: str = Field(description="Session whose turn is starting.")
    rootSessionId: str = Field(
        description="Its root conversation (the parent chain's top; the session itself for a root)."
    )
    turnId: str = Field(description="The turn that is starting.")
    siteMemory: bool = Field(
        default=True,
        description=(
            "Whether site memory is on for the root conversation (1.35.0). When false, leave "
            "out the part of the note that comes from site memory; a note about the host's "
            "own settings still goes."
        ),
    )
    prompt: str = Field(
        default="",
        description=(
            "The turn's user text, at most 4000 characters. Use it to pick sites; "
            "keep none of it."
        ),
    )
    reason: Literal["turn", "compaction"] = Field(
        default="turn",
        description=(
            "Why core asks (1.37.0). 'turn': a turn is starting. 'compaction': the "
            "conversation was just summarised, and the summary drops the earlier notes; "
            "answer again with what the work needs now. Core sends 'compaction' only to a "
            "connection that declared the capability 'turnContextCompaction', with the "
            "user's latest words in 'prompt' and a 1 s deadline."
        ),
    )


class HostTurnContextResult(Payload):
    text: str = Field(
        default="",
        description=(
            "Site memory for the first call, or '' for nothing. Core keeps at most 4000 "
            "characters (cut at a line boundary), drops control characters but newlines, "
            "and adds it to the turn's user message as an untrusted <host-context> block. "
            "After a compaction (reason 'compaction') it is kept with the summary instead "
            "and the summariser never reads it."
        ),
    )
    coversSkills: list[str] | None = Field(
        default=None,
        description=(
            "Names of skills this note already carries (1.37.0). Core does not "
            "auto-inject them on the turn this answer belongs to (a 'compaction' answer: "
            "the next turn), and does not count them as injected, so a later turn without "
            "this cover can still bring them in. At most 16 names, not counted in the "
            "4000-character limit."
        ),
    )


class ToolCancelNotification(Payload):
    """Server -> client notification: stop a running tool.invoke (the turn was interrupted).

    The daemon does not wait for an answer; the call already ended as an
    interrupted tool error.
    """

    sessionId: str = Field(description="Session whose turn was interrupted.")
    callId: str = Field(description="The tool.invoke callId to stop.")


class ToolProgressParams(Payload):
    """Client -> server notification during a tool.invoke."""

    callId: str = Field(description="The tool.invoke callId this progress belongs to.")
    message: str = Field(
        default="",
        max_length=65536,
        description=(
            "Progress text; re-emitted as session.event tool.progress. Any tool.progress, "
            "an empty keepalive included, restarts the call's deadline (addendum 15). At "
            "most 65536 characters; only the connection the call was sent to may report."
        ),
    )
    state: Literal["running", "waiting"] | None = Field(
        default=None,
        description=(
            "1.55.0: 'waiting' = the call is waiting on something outside it (a page "
            "loading, a download); 'running' = it is working again. Forwarded on the "
            "session event tool.progress as state, so a tool card can say so. Restarts "
            "the deadline like any progress."
        ),
    )
    note: str | None = Field(
        default=None,
        max_length=200,
        description="1.55.0: what it waits for, one short line ('waiting for the page').",
    )


# --------------------------------------------------------------------------
# approval.* / permission.*
# --------------------------------------------------------------------------


#: Fields 1.57.0 (m21) adds to ``ApprovalRemember`` and ``AllowlistPattern``:
#: left out of the JSON while null, so a plain command's ``remember`` and an
#: ordinary allowlist row stay exactly what 0.2.89 sent.
_M21_OPTIONAL: tuple[str, ...] = ("exactLine", "kind", "display", "script", "scripts")


def _omit_null_m21(data: Any) -> Any:
    if isinstance(data, dict):
        for key in _M21_OPTIONAL:
            if key in data and data[key] is None:
                del data[key]
    return data


class ScriptRef(Payload):
    """A script file as a script grant holds it (1.57.0)."""

    path: str = Field(description="Absolute path of the file.")
    sha256: str = Field(description="Hex SHA-256 of its bytes when the person allowed it.")


class UncheckedWhy(Payload):
    """One part of a call snowpea could not check before it runs (1.57.0, m21)."""

    part: str = Field(description="The simple command it is about, as written (clipped).")
    file: str | None = Field(
        default=None, description="The script file it is in; absent when on the line itself.")
    line: int | None = Field(default=None, description="1-based line in that file, when known.")
    reason: str = Field(
        description="The reason id (unknown-path, dynamic-program, env-code:NAME, "
        "snowpea-cli, script-missing, code:…).")
    text: str = Field(description="Why, in plain words, in the session's language.")


class ApprovalJobRef(Payload):
    """The scheduled job a request's run belongs to (1.68.0, ``jobApprovals``)."""

    id: str = Field(description="The job's id (job.list jobId).")
    title: str | None = Field(
        default=None, description="Its short name, else the start of its task."
    )


class ApprovalBlocked(Payload):
    """What a blocked call that is put to the person hit (1.61.0)."""

    by: str = Field(
        description="What blocked it: 'commands:<preset>', 'paths:<preset>', 'custom' (your "
        "own pattern or path) or 'security' (snowpea's own controls or the security lists).")
    kind: str | None = Field(
        default=None,
        description="'command' (a blocked command), 'path' (a protected path), 'control' "
        "(snowpea's own control files) or 'settings' (the security lists themselves).")
    why: str = Field(description="Why, in plain words, in the session's language.")
    match: str | None = Field(
        default=None, description="What matched: the command text or the path.")


class ApprovalRemember(Payload):
    """What a remembered answer to a shell approval would allow (1.34.0, ``shellRemember``)."""

    @model_serializer(mode="wrap")
    def _wire(self, handler: Any) -> Any:
        return _omit_null_m21(handler(self))

    pattern: str = Field(
        description=(
            "The allowlist regex a 'project' or 'always' answer stores, e.g. "
            "'^git status( .*)?$': the program, its subcommand for subcommand tools, then "
            "any arguments."
        )
    )
    display: str = Field(description="The same rule for people, e.g. 'git status …'.")
    note: str | None = Field(
        default=None,
        description=(
            "A warning to show with the rule, or null: 'scripts in this project decide "
            "what runs' for go run/test, cargo run/test/build, make <target>, npm/pnpm/"
            "yarn/bun run <script> (and test/start), uv/poetry/pdm run, pytest, gradle "
            "and mvn. Such rules are offered for the project only."
        ),
    )
    scopes: list[Literal["project", "always"]] = Field(
        description=(
            "The remembered answers the card may offer: ['project'], or "
            "['project', 'always'] when the rule cannot write or run anything whatever "
            "arguments follow. An 'always' answer to a ['project'] offer is stored for the "
            "project. [] for an exact compound line (exactLine): nothing is stored."
        )
    )
    exactLine: bool | None = Field(
        default=None,
        description=(
            "1.57.0: true for a compound line (one holding ; & | < > ` $( or a newline): "
            "'allow for this session' remembers only this exact line (whitespace "
            "normalised) run in this folder, for this session, in memory; 'project'/"
            "'always' answers apply as 'session'. pattern is then the escaped line, "
            "display the line itself. Null for every other request: a plain command's "
            "session answer is unchanged."
        ),
    )
    kind: Literal["script"] | None = Field(
        default=None,
        description=(
            "1.57.0: 'script' on a person-only request that asks only because of what the "
            "content of script files it runs does (a script grant): a person's allow "
            "('session', or 'project' to keep it; 'always' is kept for the project) is "
            "remembered for this exact command (display) and these files as they are now "
            "(scripts), and asked again, saying the script changed, as soon as one "
            "changes. Never offered to, nor stored from, a remote login, a connector, "
            "Jarvis or a messenger. Null otherwise."
        ),
    )
    script: ScriptRef | None = Field(
        default=None, description="1.57.0, kind 'script': the script the command runs."
    )
    scripts: list[ScriptRef] | None = Field(
        default=None,
        description=(
            "1.57.0, kind 'script': every file whose content the grant holds to (the "
            "script, files it imports, the package.json or Makefile it runs a line of)."
        ),
    )


class ApprovalRequest(Payload):
    """A tool call waiting for a human decision."""

    requestId: str = Field(description="Id to answer with approval.respond.")
    sessionId: str = Field(description="Session whose turn is blocked.")
    tool: str = Field(description="Tool the model wants to run.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments it wants to use.")
    risk: str = Field(default="low", description="Risk hint for the UI.")
    timeoutSec: int = Field(
        default=300,
        description=(
            "Seconds before the request is dropped as unanswered; 0 means no limit "
            "(1.35.0: a TUI or desktop session waits for its person)."
        ),
    )
    scopeHint: ApprovalScope = Field(default="once", description="Scope the UI should preselect.")
    note: str = Field(
        default="",
        description=(
            "Extra warning shown with the prompt, e.g. \"modifies snowpea configuration\"."
        ),
    )
    site: str | None = Field(
        default=None,
        description="Origin (scheme://host) a 'site' scope would store the answer for (1.6.0).",
    )
    guard: str | None = Field(
        default=None,
        description=(
            "Why this is asked whatever the mode (1.24.0): 'agent-switch' for a call in the "
            "rest of a turn session.setAgent {guardTurn} guarded. Such an answer is never "
            "remembered."
        ),
    )
    diff: str | None = Field(
        default=None,
        description=(
            "For patch and write_file: the unified diff the call would write, computed "
            "without writing (1.29.0). Null for other tools, for a call that would fail, "
            "or when the diff is over 200 KB."
        ),
    )
    reasonCode: str | None = Field(
        default=None,
        description=(
            "Why a call is asked by what it touches (1.30.0): 'config' (snowpea settings), "
            "'secret' (a credential or key file), 'outside_project' (a write outside the "
            "session's project). 1.61.0, with blocked: 'blocked_command' (a command on the "
            "block list, the person's own patterns included), 'blocked_path' (a protected "
            "path) or 'blocked_control' (snowpea's own control files or the security lists "
            "themselves). Null for an ordinary ask."
        ),
    )
    paths: list[str] | None = Field(
        default=None,
        description=(
            "With reasonCode: the resolved absolute paths concerned (1.30.0)."
        ),
    )
    folder: str | None = Field(
        default=None,
        description=(
            "For 'outside_project': the folder a 'project' or 'always' answer allows "
            "writes into from then on (kept with the owner's global rules, never in the "
            "project), and a 'session' answer for this session (1.30.0). Null when that "
            "folder would be too wide (a filesystem root, the home folder, or above the "
            "project): every answer then covers this call only."
        ),
    )
    remember: ApprovalRemember | None = Field(
        default=None,
        description=(
            "For a shell command (1.34.0, capability 'shellRemember'): the rule a 'project' "
            "or 'always' answer would store. Null when only 'once' (or 'session') can be "
            "given: a compound or complex line (; && || | & backticks $ redirects, "
            "heredocs, an env-var prefix, unquoted subshells or globs), a program named by "
            "a path or with non-ASCII letters, an argument that runs a command "
            "(--upload-pack, -c, --exec, rg --pre, …), sudo/su/doas, rm/dd/chmod/kill and "
            "the like, shells, interpreters and editors, network tools, npx-style package "
            "runners, git push/reset/clean/rm/fetch/pull/clone and other history rewrites, "
            "onceOnly args, an agent-switch guard, any reasonCode, and every non-shell "
            "call. Core keeps a 'project'/'always' answer to a shell request with a null "
            "remember to 'once'. Stored shell rules only ever match one such plain command."
        ),
    )
    externalTerminal: ExternalTerminalAsk | None = Field(
        default=None,
        description=(
            "Set when the prompt was read off the person's tmux pane (1.56.0, externalTmux): "
            "the CLI asked in its terminal, not on a wire. An answer is sent there as the "
            "CLI's own key, and only while the same prompt is still on screen; tool/args are "
            "the pending call the CLI's transcript names, or tool 'terminal.prompt' with "
            "args {question} (then personOnly)."
        ),
    )
    external: ExternalRef | None = Field(
        default=None,
        description=(
            "Set when the call comes from an external CLI session snowpea hosts (1.53.0); "
            "sessionId is then 'ext:<cli>:<id>' and the answer goes back to that CLI. Such "
            "a request is never sent as approval.request to a connection: it waits in the "
            "shared queue and is announced as approval.pending (with route) to every "
            "authenticated surface, listed by approval.list and answered with "
            "approval.respond."
        ),
    )
    route: ApprovalRoute | None = Field(
        default=None,
        description=(
            "Who the delegation engine routed it to while Jarvis mode is on (1.53.0): "
            "person (wait for a human, as before), jarvis (the Jarvis session decides; a "
            "person may still answer first) or auto (allowed by the engine, shown for the "
            "record). Null when Jarvis mode is off."
        ),
    )
    personOnly: bool | None = Field(
        default=None,
        description=(
            "True on a request only a person may answer (1.53.0): Jarvis's delegation_set "
            "proposal (tool 'delegation_set', args {scope, target, level, allow, expiresAt, "
            "note}, from the Jarvis session). Answered with approval.respond {decision, scope: "
            "'once'}: 'allow' only from a person's connection (else jarvis_person_only); "
            "'deny' from anyone who sees it. 1.61.0: also a blocked call put to the person "
            "(blocked); a local browser 0.4.0+ may allow a security ask (blocked or reason "
            "set) of its own session with actor 'person' and personConfirm 'window' "
            "(capability browserPersonApprovals)."
        ),
    )
    argsEscaped: bool | None = Field(
        default=None,
        description=(
            "True when the call's arguments held characters a surface would strip or that "
            "change how text reads (C0/C1 controls, escape sequences, NUL, bidi and invisible "
            "format characters) (1.53.0). args then shows them written out (a backslash "
            "escape such as x9d or u202e); what runs is the raw text. Such a request is never "
            "remembered or cached, and the delegation engine reports it in every guarded "
            "class (rule exec:opaque:control-chars)."
        ),
    )
    neverDelegable: bool | None = Field(
        default=None,
        description=(
            "True when only a person may decide this request (1.54.0): it is personOnly, or "
            "the delegation engine classifies it 'never' (snowpea's own settings and "
            "secrets, the snowpea CLI, delegation changes, …), or the security lists "
            "cannot check it. Clients label it \"always asks you\". Null otherwise; additive "
            "and informational: core enforces it whatever the client shows."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Why core put this request to the person whatever the mode (1.54.0): 'cannot be "
            "checked against your block list' when the security lists are set and part of "
            "the call cannot be seen (a program only the shell knows, eval of a variable, a "
            "pipe into a shell, a path only the shell knows). Such a request is personOnly, "
            "routed to the person, never cached or remembered. Null otherwise. 1.55.0 "
            "(security review 6) adds two values: 'acts on snowpea's own controls' (the "
            "line runs snowpea's own CLI or code, or changes snowpea's own hooks, plugins, "
            "skills, commands or agents: asked in every mode, whatever the lists and "
            "settings say) and 'cannot tell what this code changes' (only code the check "
            "cannot resolve is in the way; security.unknownCode: 'allow' switches this one "
            "off). note carries the sentence for the person."
        ),
    )
    uncheckedWhy: list[UncheckedWhy] | None = Field(
        default=None,
        description=(
            "With reason (1.57.0, m21): what snowpea could not check, part by part — which "
            "simple command of the line, in which script file and line when the cause is "
            "inside a script, the reason id and a sentence for the person in the session's "
            "language. Null on an ordinary ask. Additive and informational."
        ),
    )
    blocked: ApprovalBlocked | None = Field(
        default=None,
        description=(
            "1.61.0: the call is blocked (on the block list, a protected path, snowpea's own "
            "controls or the security lists) and core puts it to the person instead of "
            "refusing it outright. Such a request is personOnly and neverDelegable, routed to "
            "the person, never cached or remembered: an allow applies to this call only "
            "(scope 'once'). reason is then 'on your block list', 'touches a protected "
            "folder' or \"touches snowpea's own controls\" and note carries the sentence. Not "
            "asked (refused with security_blocked as before) in a run with nobody present "
            "(a scheduled job, a Jarvis task) and for a read of the daemon's token or address "
            "or of saved logins (credentials, MCP logins, external CLI logins). Null otherwise."
        ),
    )
    job: ApprovalJobRef | None = Field(
        default=None,
        description=(
            "1.68.0 (jobApprovals): the request comes from a run of this scheduled job (a "
            "routine), with nobody watching: it is in the shared queue (approval.pending to "
            "every surface, the messenger's private chat when one is bound) and waits "
            "settings scheduler.approvalTimeoutSec (or the job's approvalTimeoutSec). The card "
            "may offer scope 'task': remembered for this job across its runs (JobInfo.grants). "
            "Null otherwise."
        ),
    )


class ApprovalAskParams(Payload):
    """Client -> server, during a tool.invoke: escalate one action (1.6.0).

    The host asks before an action it judges riskier than the tool's own tag
    (a click on "Pay"). The answer goes through the normal pipeline: mode
    matrix, allowlist (tool + site), then the session's approver; a timeout
    denies.

    The ``approval.request`` the approver receives carries ``tool``, the
    host's ``args`` with ``detail`` nested under ``args.detail`` and the site
    under ``args.site``, ``note`` = ``reason``, ``site`` and
    ``scopeHint: "site"`` (when a site is known). Example:
    ``{"tool": "repl", "args": {"detail": {"origin": "https://shop.example",
    "element": "button#submit", "action": "click"}, "site":
    "https://shop.example"}, "note": "The code clicks 'Submit order'", "site":
    "https://shop.example", "scopeHint": "site"}``.
    """

    sessionId: str = Field(description="Session whose tool call is running.")
    callId: str | None = Field(default=None, description="The tool.invoke callId, if any.")
    tool: str = Field(description="Host tool asking; the allowlist is keyed by it.")
    permission: PermissionTag = Field(description="Permission class of the escalated action.")
    reason: str = Field(description="What will happen, shown to the person approving.")
    args: dict[str, Any] = Field(default_factory=dict, description="Action details to show.")
    site: str | None = Field(
        default=None,
        description=(
            "Origin of the page, e.g. https://github.com; derived from detail.origin or "
            "args.url if omitted."
        ),
    )
    risk: str | None = Field(
        default=None,
        description=(
            "Risk hint shown with the approval; default from the permission. 'payment' "
            "also means: the allowlist is never consulted and the answer is never "
            "stored beyond this one call (scope is coerced to 'once')."
        ),
    )
    forceAsk: bool = Field(
        default=False,
        description=(
            "Always ask a person: the mode matrix may only deny (plan still denies write, "
            "send and config; a read is asked) and "
            "never auto-allows, not even in auto mode. The allowlist still applies unless "
            "risk is 'payment'. For any client's destructive or irreversible actions."
        ),
    )
    detail: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the running code is about to do, e.g. {origin, element, action}; shown "
            "with the approval and merged into its args."
        ),
    )
    reasonCode: str | None = Field(
        default=None,
        description=(
            "1.56.0: the host's reason code for the ask, also accepted inside args "
            "(args.reasonCode wins). One starting with 'payment' (payment.step, ...) is "
            "always the person's: Jarvis and the engine never decide it."
        ),
    )


class ApprovalAskResult(Payload):
    decision: Decision = Field(description="allow or deny.")
    scope: ApprovalScope = Field(
        default="once",
        description=(
            "Scope the answer applies to; always 'once' for payment. 'project' only "
            "for project-safe actions (repl.files, a localDevServer repl.navigate); "
            "'always' for a page action only for repl.tabs, repl.clipboard and (1.13.0) "
            "repl.read and repl.attach, others are kept to 'once'. For "
            "args.onceOnly (1.12.0) the scope the person picked, which core never stores: "
            "the host may keep it on its side."
        ),
    )
    by: str = Field(
        default="",
        description=(
            "Who decided: 'user' (a person answered), 'allowlist', 'mode' (the mode "
            "matrix), 'readOnly' (a read-only delegation refuses changes and never "
            "asks; 1.14.0), or 'timeout' / 'interrupted' (denied without an answer)."
        ),
    )


class ApprovalListResult(Payload):
    requests: list[ApprovalRequest] = Field(
        default_factory=list, description="Approvals still pending."
    )


class ApprovalRespondParams(Payload):
    requestId: str = Field(description="Request being answered.")
    decision: Decision = Field(description="allow runs the tool, deny ends the turn.")
    scope: ApprovalScope = Field(
        default="once",
        description=(
            "How long the decision applies. From a remote web login (and a connector) an "
            "allow never stores a rule (1.55.0): 'project', 'always' and 'site' are applied "
            "as 'session'; the result's scope and approval.resolved say what applied."
        ),
    )
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "A browser host (clientKind 'browser', clientVersion 0.3.0 or later) must set "
            "'person' (a click) to allow with a scope wider than 'once' (security review 3); "
            "ignored for other kinds. 1.61.0: with personConfirm 'window', part of what lets a "
            "browser 0.4.0+ allow a personOnly security ask (see personConfirm)."
        ),
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description=(
            "1.61.0 (browserPersonApprovals): as ApprovalAnswer.personConfirm — 'window' when "
            "the person confirmed in the browser's own native window. With actor 'person', a "
            "local browser 0.4.0+ may allow a personOnly security ask of its own session "
            "(once); else refused with jarvis_person_only, details.reason browser_*."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "Why the human refused; quoted back to the model as the tool's refusal "
            "so the next turn can answer it (M15b \u00a71)."
        ),
    )
    reauth: str | None = Field(
        default=None,
        description=(
            "1.56.0: the remote sign-in password again, to allow Jarvis's delegation_set "
            "proposal from a remote web login while settings jarvis.delegation.remoteGrant is "
            "on (session or project scope, no guarded classes, at most 24 h). Checked like a "
            "sign-in and counted with its failures; never stored, logged or echoed. Ignored on "
            "every other answer."
        ),
    )
    grantExpiresAt: str | None = Field(
        default=None,
        description=(
            "1.56.0, with reauth: when the grant ends (UTC ISO). It may only shorten the "
            "proposal; a remote grant ends within 24 h whatever is given (default: 24 h, or "
            "the proposal's expiry when sooner)."
        ),
    )


class ApprovalAnswer(Payload):
    """Result of the server-initiated ``approval.request``."""

    decision: Decision = Field(description="The human's decision.")
    scope: ApprovalScope = Field(default="once", description="How long the decision applies.")
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "A browser host (clientKind 'browser', clientVersion 0.3.0 or later) sets "
            "'person' when the answer came from a click; without it an allow wider than "
            "'once' counts as 'once' (security review 3). Ignored for other kinds."
        ),
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description=(
            "1.61.0 (browserPersonApprovals): a browser host 0.4.0 or later sets 'window' when "
            "the person confirmed this answer in the browser's own native window (not a page "
            "click). With actor 'person' it is what lets a local browser allow a personOnly "
            "security ask (blocked, or reason set) of a session it owns, hosts or asked (a "
            "subagent's by its parents); the allow applies once (a script grant keeps its "
            "scope). Without it, from a browser before 0.4.0, from a remote browser, for "
            "another session or for any other personOnly request (delegation_set, a mode "
            "raise, a schedule, tmux.adopt, an external CLI's, a Jarvis-routed one) the allow "
            "is refused with jarvis_person_only and details.reason browser_unconfirmed, "
            "browser_legacy, browser_remote, browser_not_owner or browser_not_allowed "
            "(RequestRefusal.reason). Ignored for other kinds."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "Why the human refused; quoted back to the model as the tool's refusal "
            "so the next turn can answer it (M15b \u00a71)."
        ),
    )


# --------------------------------------------------------------------------
# question.*  (the ``ask_user`` tool)
# --------------------------------------------------------------------------


class QuestionOption(Payload):
    """One answer the agent offers; a surface draws it as a row to pick."""

    label: str = Field(description="What the row says, and what comes back in selected[].")
    description: str = Field(default="", description="One dim line under the label.")
    preview: str = Field(
        default="",
        description="Monospace block beside the list, for an option easier shown than told.",
    )


class QuestionItem(Payload):
    """One question of a batch; a surface draws it as one tab."""

    header: str = Field(
        default="", description="Short chip naming the question, e.g. \"Auth method\"."
    )
    question: str = Field(description="The question, including why the answer matters.")
    options: list[QuestionOption] = Field(
        default_factory=list, description="Closed set of answers; empty means free text."
    )
    multi: bool = Field(default=False, description="More than one option may be picked.")
    allowOther: bool = Field(
        default=True, description="Offer a free-text '기타 / Other' row alongside the options."
    )
    secret: bool = Field(
        default=False,
        description=(
            "The free-text answer is a credential. A surface MUST mask it while it is "
            "typed, MUST NOT echo it into the transcript, and MUST NOT log it. Set for "
            "API keys and tokens; a URL or a project id is asked in the clear."
        ),
    )


class QuestionRequest(Payload):
    """Questions from the agent waiting on a human answer.

    The transport mirrors ``ApprovalRequest`` on purpose: same id field, same
    timeout field, same server->client call and the same pending/resolved
    notifications, so a client that already renders approvals has nothing new
    to learn.

    The whole batch arrives at once, and one answer set goes back.  That is
    what lets a surface show the questions as tabs the user can walk back
    through and change their mind in before submitting; asking them one
    blocking request at a time would make an earlier answer unreachable the
    moment it was given.  A client with no tabs to draw — a messenger — is
    free to ask them one after another itself and answer once at the end.
    """

    requestId: str = Field(description="Id to answer with question.respond.")
    sessionId: str = Field(description="Session whose turn is blocked.")
    questions: list[QuestionItem] = Field(
        default_factory=list, description="The questions, in the order they were asked."
    )
    timeoutSec: int = Field(
        default=600,
        description=(
            "Seconds before the batch gives up; 0 means no limit (1.35.0: a TUI or "
            "desktop session waits for its person)."
        ),
    )
    external: ExternalRef | None = Field(
        default=None,
        description="Set when an external CLI session asks (1.53.0); see ApprovalRequest.",
    )
    route: ApprovalRoute | None = Field(
        default=None,
        description=(
            "person or jarvis while Jarvis mode is on (1.53.0); null when it is off. A "
            "question is never 'auto'."
        ),
    )
    personOnly: bool | None = Field(
        default=None,
        description=(
            "True when only a person may answer (1.54.0, security review 4 C1): the answer "
            "changes the session's permissions or raises its mode (set_mode, the plan-exit "
            "and plan-review pickers). Routed to the person; Jarvis's question_answer is "
            "refused (jarvis_person_only); a browser or connector may not answer it. Null "
            "otherwise."
        ),
    )
    kind: Literal["mode"] | None = Field(
        default=None,
        description=(
            "What a personOnly question decides (1.54.0): 'mode' for a mode picker. Null for "
            "an ordinary question."
        ),
    )
    neverDelegable: bool | None = Field(
        default=None,
        description=(
            "True when only a person may decide this question (1.54.0): it is personOnly. "
            "Clients label it \"always asks you\". Null otherwise."
        ),
    )


class QuestionListResult(Payload):
    requests: list[QuestionRequest] = Field(
        default_factory=list, description="Questions still waiting for an answer."
    )


class QuestionAnswerItem(Payload):
    """One question's answer.  Both fields empty means it was not answered."""

    selected: list[str] = Field(
        default_factory=list, description="Labels the human picked, in the order offered."
    )
    text: str | None = Field(default=None, description="Free text, for 'Other' or no options.")


class QuestionRespondParams(Payload):
    requestId: str = Field(description="Batch being answered.")
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "A browser host sets 'person' when a click answered another session's question "
            "(security review 3); a browser or connector otherwise answers only its own "
            "sessions' questions (not_found). Ignored for other kinds."
        ),
    )
    answers: list[QuestionAnswerItem] = Field(
        default_factory=list,
        description="One entry per question, in question order; empty declines the batch.",
    )


class QuestionAnswer(Payload):
    """Result of the server-initiated ``question.request``.

    An empty list means the human declined the whole batch: Esc in the TUI, or
    a client that has no way to ask.
    """

    answers: list[QuestionAnswerItem] = Field(
        default_factory=list, description="One entry per question, in question order."
    )


class AllowlistAddParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    pattern: str = Field(description="Glob or command prefix promoted from ask to allow.")
    scope: AllowlistScope = Field(default="project", description="Where the pattern is stored.")
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute folder of the project to use (its .snowpea/settings.json). Default: "
            "the caller's most recent session's workdir (1.7.0)."
        ),
    )


class AllowlistAddResult(Payload):
    patternId: str = Field(description="Id used to remove the pattern later.")


class AllowlistListParams(Payload):
    scope: AllowlistScope | None = Field(default=None, description="Filter by scope; omit for all.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Only this browser profile's global entries (plus project entries); omit "
            "for every entry (addendum 8)."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute folder of the project to use (its .snowpea/settings.json). Default: "
            "the caller's most recent session's workdir (1.7.0)."
        ),
    )


class AllowlistPattern(Payload):
    """One stored allowlist entry."""

    @model_serializer(mode="wrap")
    def _wire(self, handler: Any) -> Any:
        return _omit_null_m21(handler(self))

    patternId: str = Field(description="Stable id of the pattern.")
    pattern: str = Field(description="The glob or command prefix.")
    scope: AllowlistScope = Field(description="Where it is stored.")
    tool: str | None = Field(
        default=None,
        description=(
            "Tool or dotted action the entry targets (e.g. 'repl.upload'); null for a "
            "shell command pattern (1.7.0)."
        ),
    )
    origin: str | None = Field(
        default=None,
        description="Site the entry is limited to (a 'site'-scoped approval), else null.",
    )
    createdAt: str | None = Field(
        default=None, description="UTC ISO time it was stored; null for older entries."
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "Browser profile (clientId) a global entry belongs to: stored from an "
            "approval in that profile's session and matched only there. Null for "
            "unscoped entries (IDE/CLI, project) (addendum 8)."
        ),
    )
    reasonCode: str | None = Field(
        default=None,
        description=(
            "approval.ask args.reasonCode the entry is limited to, e.g. 'localDevServer' "
            "for a project rule on a loopback dev server origin; null for an entry that "
            "covers any call (1.7.0)."
        ),
    )
    kind: Literal["script"] | None = Field(
        default=None,
        description=(
            "1.57.0: 'script' for a script grant: the exact command in pattern (e.g. "
            "'bash bin/run.sh') may run unasked in this project while the files in "
            "scripts are unchanged. tool names the tool; scope is 'project' (kept in the "
            "owner's settings, never in the project's file). Removed with "
            "permission.allowlist.remove like any entry. Null for a rule."
        ),
    )
    display: str | None = Field(
        default=None,
        description=(
            "1.57.0, kind 'script': the grant for people, e.g. 'bash bin/run.sh (while "
            "bin/run.sh is unchanged)'."
        ),
    )
    script: ScriptRef | None = Field(
        default=None, description="1.57.0, kind 'script': the script the command runs."
    )
    scripts: list[ScriptRef] | None = Field(
        default=None,
        description="1.57.0, kind 'script': every file whose content the grant holds to.",
    )


class AllowlistListResult(Payload):
    patterns: list[AllowlistPattern] = Field(
        default_factory=list, description="Stored allowlist entries."
    )


class AllowlistRemoveParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    patternId: str = Field(description="Pattern to delete.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "When given, a global entry is removed only if it belongs to this browser "
            "profile (addendum 8)."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute folder of the project to use (its .snowpea/settings.json). Default: "
            "the caller's most recent session's workdir (1.7.0)."
        ),
    )


# --------------------------------------------------------------------------
# provider.* / backend.*
# --------------------------------------------------------------------------


class HiddenModelUse(Payload):
    """A hidden model that something still uses (1.60.0, capability hiddenModels)."""

    model: str = Field(description="The hidden model id.")
    usedBy: list[str] = Field(
        default_factory=list,
        description=(
            "Who uses it: 'default' (models.default, or the vendor's own default model), "
            "'category:<slot>' (models.categories), 'agent:<name>' (agents.models) and "
            "'session:<sessionId>' (a session whose turn is running on it)."
        ),
    )


class ProviderInfo(Payload):
    """One chat provider."""

    vendor: str = Field(description="Vendor key, e.g. 'anthropic'.")
    label: str = Field(default="", description="Human-readable vendor name for pickers.")
    defaultModel: str = Field(default="", description="Model used when the caller names none.")
    models: list[str] = Field(default_factory=list, description="Model ids this vendor offers.")
    configured: bool = Field(default=False, description="True when credentials are present.")
    default: bool = Field(default=False, description="True for the vendor used when none is named.")
    authMethods: list[str] = Field(
        default_factory=lambda: ["api_key"],
        description=(
            "Login flows the vendor supports: 'api_key' everywhere, plus provider-specific "
            "device-code, PKCE, Google ADC, or direct OAuth-token authentication."
        ),
    )
    authStatus: AuthStatus = Field(
        default="unconfigured",
        description=(
            "State of the stored credential: 'unconfigured', 'active', or 'expired' when an "
            "OAuth session is past its expiry and needs refreshing or a new login."
        ),
    )
    preset: str = Field(
        default="",
        description=(
            "Preset this vendor follows: 'local' for the built-in local vendor and for every "
            "named OpenAI-compatible server, otherwise the vendor's own id."
        ),
    )
    custom: bool = Field(
        default=False,
        description="True for a named OpenAI-compatible server the user added, not a built-in.",
    )
    baseUrl: str = Field(
        default="",
        description=(
            "Configured endpoint for a local-style vendor. Empty for hosted vendors "
            "whose URL is fixed by the preset."
        ),
    )
    defaultBaseUrl: str = Field(
        default="",
        description=(
            "The preset's own endpoint, used when no base_url is configured (e.g. "
            "https://api.meta.ai/v1); empty for SDK-driven vendors. A settings form shows "
            "it as the default so the field reads as optional."
        ),
    )
    variant: str = Field(
        default="",
        description=(
            "Server software hint for a local-style vendor: vllm, ollama, lmstudio, "
            "or generic. Empty for hosted vendors."
        ),
    )
    supportsEffort: bool = Field(
        default=False,
        description=(
            "True when this vendor accepts a reasoning-effort setting. False for a local-style "
            "server unless its block sets effort_param: true."
        ),
    )
    hiddenModels: list[str] = Field(
        default_factory=list,
        description=(
            "1.60.0 (capability hiddenModels): settings providers.<vendor>.hiddenModels, the "
            "model ids the person removed from this vendor's pickers (exact ids as the vendor "
            "lists them; 'openai/gpt-4o' on OpenRouter is its own id). They are left out of "
            "models here, of provider.models and of every picker; provider.model.unhide "
            "brings one back."
        ),
    )
    hiddenInUse: list[HiddenModelUse] = Field(
        default_factory=list,
        description=(
            "1.60.0: hidden models something still uses. Hiding never rewrites a setting or "
            "stops a run: these keep working, and a surface shows a warning."
        ),
    )


class ProviderListResult(Payload):
    providers: list[ProviderInfo] = Field(default_factory=list, description="Known chat providers.")


class ProviderModelsParams(Payload):
    vendor: str | None = Field(
        default=None, description="Vendor to query; defaults to the configured one."
    )
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Extra model ids (hand-typed, or no longer listed) to include in contextWindows "
            "(1.42.0). The vendor's configured model and the models.default / "
            "models.categories.* that point at this vendor are always included."
        ),
    )


class TrajectoryRow(Payload):
    """One row of a session's Trajectory (1.45.0, capability 'trajectory')."""

    kind: Literal[
        "turn", "user", "message", "tool", "subagent", "compacted", "context", "system"
    ] = Field(description="What the row is.")
    seq: int = Field(description="Event the row comes from; the key to merge updates by.")
    ts: str = Field(description="When it started.")
    turnId: str | None = Field(default=None, description="Turn it belongs to.")
    step: int | None = Field(default=None, description="Model call within the turn.")
    title: str = Field(default="", description="One line for the row.")
    text: str | None = Field(default=None, description="Text or result preview.")
    endSeq: int | None = Field(default=None, description="Event that finished it (result, done).")
    endTs: str | None = Field(default=None, description="When it finished.")
    ok: bool | None = Field(default=None, description="Whether it succeeded, once finished.")
    data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Kind-specific facts: message — outcome, ttftMs, decodeMs, totalMs, inputTokens, "
            "cachedInputTokens, outputTokens, reasoningChars, toolCalls; tool — name, callId, "
            "paths; subagent — agentId, childSessionId, reason, rounds; turn — initiator, "
            "reason, outcome; compacted — before, after, summaryChars, auto, kept; user — "
            "steered, and origin {kind, surface} from message.user (1.53.0). "
            "Since 1.51.0 every context, system and compacted row has data.kind, a stable "
            "slug, plus that row's values: summary (chars); continued (hookKind, count, "
            "limit); host_note (chars); forked (fromSessionId, atTurnId); error (code); "
            "provider_retry (attempt, max — null for unreachable, reason, delaySec, "
            "waitLeftSec, error); model_changed (provider, model); mode_changed (mode); "
            "goal (goalId, phase, round, maxRounds, blockedCode, cleared); compaction "
            "(before, after, summaryChars, auto, kept). Titles stay for older clients."
        ),
    )


class ExtensionInfo(Payload):
    """An extension in $SNOWPEA_HOME/extensions (1.49.0, capability 'extensions')."""

    name: str = Field(description="Plugin name.")
    path: str = Field(description="Its folder.")
    description: str = Field(default="", description="From plugin.json.")
    version: str = Field(default="", description="From plugin.json.")
    enabled: bool = Field(default=True, description="settings.extensions.<name>.enabled.")
    state: Literal["loaded", "unloaded", "disabled", "error", "incompatible"] = Field(
        description="What happened when it was last loaded."
    )
    error: str | None = Field(default=None, description="Why it is not loaded, when it failed.")
    effects: int = Field(default=0, description="Registrations it holds (tools, sections, …).")
    configSchema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of its config, for a settings form."
    )
    config: dict[str, Any] = Field(default_factory=dict, description="Its current config.")


class ExtensionListResult(Payload):
    extensions: list[ExtensionInfo] = Field(default_factory=list, description="Installed plugins.")


class ExtensionNameParams(Payload):
    name: str = Field(description="Plugin name.")


class ExtensionConfigureParams(Payload):
    name: str = Field(description="Plugin name.")
    config: dict[str, Any] = Field(
        description="New config; checked against configSchema, saved, then the plugin reloads."
    )


class ExtensionResult(Payload):
    extension: ExtensionInfo = Field(description="The plugin now.")


class LineageNode(Payload):
    """A session in a delegation tree (1.48.0, capability 'lineage')."""

    sessionId: str = Field(description="The session.")
    title: str | None = Field(default=None, description="Its title, when it has one.")
    agent: str | None = Field(default=None, description="The agent it runs as.")
    kind: str = Field(default="chat", description="chat, subagent, job, …")
    createdAt: str = Field(default="", description="UTC ISO-8601.")
    running: bool = Field(default=False, description="A turn is running in it now.")
    children: list[LineageNode] = Field(
        default_factory=list, description="Sessions it delegated to."
    )


class LineageAncestor(LineageNode):
    """A breadcrumb of ``session.lineage`` (1.50.0: forks are followed too)."""

    kind: Literal["fork", "delegation"] = Field(  # type: ignore[assignment]
        default="delegation",
        description=(
            "How the session below this one in the chain came from it: forked from it "
            "(session.fork) or delegated by it (1.50.0; before, the session kind)."
        ),
    )
    sessionKind: str = Field(
        default="chat", description="The ancestor's own session kind: chat, subagent, job, …"
    )


class LineageFork(Payload):
    """A direct fork of the session (1.50.0)."""

    sessionId: str = Field(description="The fork.")
    title: str | None = Field(default=None, description="Its title; null when untitled.")
    atTurnId: str | None = Field(default=None, description="Turn of this session it was forked at.")
    createdAt: str = Field(default="", description="UTC ISO-8601.")


class SessionLineageParams(Payload):
    sessionId: str = Field(description="Session whose lineage is read.")


class SessionLineageResult(Payload):
    ancestors: list[LineageAncestor] = Field(
        default_factory=list,
        description=(
            "From the root down to the session's parent, following both delegation "
            "(parentSessionId) and fork (forkedFrom) links (1.50.0)."
        ),
    )
    descendants: list[LineageNode] = Field(
        default_factory=list, description="Sessions it delegated to, as a tree."
    )
    forks: list[LineageFork] = Field(
        default_factory=list, description="Direct forks of this session, oldest first (1.50.0)."
    )


class WorkspaceInfo(Payload):
    """A project folder the sidebar groups conversations by (1.47.0, capability 'workspaces')."""

    id: str = Field(description="Workspace id.")
    path: str = Field(
        description="Canonical folder path; sessions whose workdir resolves here belong."
    )
    title: str = Field(description="Name shown in the sidebar.")
    position: int = Field(default=0, description="Order in the sidebar, 0 first.")
    createdAt: str = Field(description="UTC ISO-8601.")
    sessions: int = Field(default=0, description="Conversations in it, archived ones not counted.")


class WorkspaceListResult(Payload):
    workspaces: list[WorkspaceInfo] = Field(default_factory=list, description="In sidebar order.")


class WorkspaceAddParams(Payload):
    path: str = Field(description="Folder to add; an existing workspace for it is returned.")
    title: str | None = Field(default=None, description="Name; the folder's name when omitted.")


class WorkspaceRenameParams(Payload):
    id: str = Field(description="Workspace to rename.")
    title: str = Field(description="New name.")


class WorkspaceIdParams(Payload):
    id: str = Field(description="Workspace id.")


class WorkspaceReorderParams(Payload):
    ids: list[str] = Field(description="Workspace ids in the order wanted; the rest follow.")


class WorkspaceResult(Payload):
    workspace: WorkspaceInfo | None = Field(default=None, description="The workspace now.")


class SessionFlagParams(Payload):
    sessionId: str = Field(description="Conversation to flag.")
    value: bool = Field(description="True to pin (or archive), false to undo.")
    stop: bool = Field(
        default=False,
        description="session.archive only: stop a running turn first instead of refusing.",
    )


class SessionForkParams(Payload):
    sessionId: str = Field(description="Session to fork (open or closed).")
    atTurnId: str | None = Field(
        default=None,
        description=(
            "Last turn to keep; omitted keeps everything up to the latest finished turn. "
            "A turn before the session's last compaction cannot be cut at."
        ),
    )


class SessionForkResult(Payload):
    sessionId: str = Field(description="The new session.")
    fromSessionId: str = Field(description="The session it was forked from.")
    atTurnId: str | None = Field(default=None, description="Last inherited turn.")
    historyLen: int = Field(default=0, description="Messages the new session starts with.")


class SessionTrajectoryParams(Payload):
    sessionId: str = Field(description="Session to read (open or closed).")
    afterSeq: int = Field(
        default=0,
        description=(
            "Only rows that started or finished after this seq; pass the last lastSeq to "
            "get what changed. Rows are keyed by seq: replace a row you have with a newer one."
        ),
    )
    limit: int = Field(default=2000, description="At most this many rows, the newest first kept.")


class SessionTrajectoryResult(Payload):
    rows: list[TrajectoryRow] = Field(default_factory=list, description="Rows in seq order.")
    lastSeq: int = Field(default=0, description="Highest event seq folded; the next afterSeq.")
    more: bool = Field(default=False, description="True when older rows were left out by limit.")


class GoalGetParams(Payload):
    sessionId: str = Field(description="Session whose goal is read.")


class GoalSetParams(Payload):
    sessionId: str = Field(description="Session the goal belongs to; replaces any goal it had.")
    objective: str = Field(description="What has to be true when the goal is complete.")
    maxRounds: int | None = Field(
        default=None, description="Round cap (default 20, at most 256)."
    )
    start: bool = Field(
        default=True, description="Start the first round now when the session is idle."
    )


class GoalControlParams(Payload):
    sessionId: str = Field(description="Session whose goal is paused, resumed or cleared.")
    extraRounds: int = Field(
        default=0, description="goal.resume only: rounds added to the cap."
    )


class GoalResult(Payload):
    goal: GoalView | None = Field(default=None, description="The goal now; null when none.")


class ProviderVisionResetParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    vendor: str | None = Field(
        default=None, description="Vendor to reset; defaults to the configured one."
    )
    model: str | None = Field(
        default=None, description=(
            "Model whose learned 'cannot see images' answer is "
            "forgotten; null: the vendor's current model."
        )
    )


class ProviderVisionResetResult(Payload):
    vendor: str = Field(description="Vendor that was reset.")
    model: str = Field(description="Model that was reset.")
    forgotten: bool = Field(description=(
        "True when something had been learned and is now "
        "forgotten."
    ))


class ProviderModelsResult(Payload):
    vendor: str = Field(description="Vendor the listing came from.")
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Model ids the vendor's endpoint reports. Whatever the source, models[0] is "
            "the recommended default (a ChatGPT account's first-priority model, the "
            "newest chat model for an API key), so a picker can take current or models[0]."
        ),
    )
    current: str = Field(default="", description="Model this vendor uses today.")
    source: str = Field(
        default="live",
        description=(
            "Which rung answered: live (the vendor's endpoint), settings "
            "(providers.<vendor>.models), cache (the last good listing) or "
            "curated (this build's list, merged with models.dev)."
        ),
    )
    detail: str = Field(
        default="", description="One line naming the source, for a picker to show."
    )
    error: str | None = Field(
        default=None,
        description=(
            "Why the live listing could not be used, when source is not 'live' (1.17.0); "
            "absent when the vendor answered or publishes no listing."
        ),
    )
    vision: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Which of the listed models can be sent images, when that is known. A model "
            "missing from this map is unknown rather than text-only, so a picker draws no "
            "badge for it instead of a negative one."
        ),
    )
    contextWindows: dict[str, ModelWindow] = Field(
        default_factory=dict,
        description=(
            "One record per listed model (1.42.0): its context window in tokens (never "
            "unknown), where that came from, and any bounds requests taught us. Covers the "
            "listed models, the configured one, models.default / models.categories.* that "
            "point at this vendor, and any ids passed in params.models. A model whose "
            "source is 'assumed' is worth a look."
        ),
    )


class SessionSetEffortParams(Payload):
    """``session.setEffort`` — pin how hard this session may think."""

    sessionId: str = Field(description="Session to pin.")
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "One of 'low', 'medium', 'high', 'max'. Null clears the pin and lets "
            "agent.effortBy / agent.effort decide again."
        ),
    )


class SessionSetEffortResult(Payload):
    """What the session will now use, and which rule decided it."""

    sessionId: str = Field(description="Session that was pinned.")
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Effective reasoning effort after the change, clamped to what the model "
            "offers; null when the model has no effort setting."
        ),
    )
    efforts: list[EffortLevel] | None = Field(
        default=None,
        description="Tiers the session's model offers; [] means it has no effort setting.",
    )
    effortSource: EffortSource = Field(
        description="Rule that decided it: the session pin, a model or vendor rule, or the default."
    )
    pinned: EffortLevel | None = Field(
        default=None, description="The session's own pin; null when it follows the settings."
    )


#: Who started a call from a browser host (security review 3, browser-team design):
#: ``person`` when the snowpea browser's own Settings page made it under a user
#: gesture.  Only a ``clientKind: "browser"`` connection's calls are judged by it.
Actor = Literal["person"]
ACTOR_DESCRIPTION = (
    "Set to 'person' by a browser host (clientKind 'browser') only for a call its own "
    "Settings page made under a user gesture (1.53.0). Without it a browser host's "
    "settings.set may change only its runtime keys (browser.siteMemory, "
    "browser.agentsMdUpdate) and provider.configure / mcp.add|update|remove are "
    "refused (jarvis_person_only). Ignored for every other connection kind."
)


class ProviderConfigureParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    vendor: str = Field(description="Vendor to configure.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Vendor-specific settings, including credentials."
    )


class ProviderRemoveParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    vendor: str = Field(
        description=(
            "Provider to forget: its credentials, its model profiles and the agent "
            "assignments that used them."
        )
    )


class ProviderModelParams(Payload):
    """``provider.model.remove`` / ``provider.model.unhide`` (1.60.0, capability hiddenModels)."""

    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "Browser marker: a browser connection needs actor 'person' and clientVersion "
            "0.3.0 or later (a legacy browser is refused)."
        ),
    )
    vendor: str = Field(description="Provider whose list the model belongs to.")
    model: str = Field(description="Exact model id as provider.models lists it.")


class ProviderModelRemoveResult(Payload):
    action: Literal["unlisted", "hidden"] = Field(
        description=(
            "'unlisted': the id was hand-added (providers.<vendor>.models or .oauth_models) "
            "and was taken off that list; 'hidden': it was added to "
            "providers.<vendor>.hiddenModels."
        )
    )


class ProviderLoginWebParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    vendor: str = Field(description="Vendor to log into.")
    method: str = Field(
        description=(
            "Login flow to start: 'browser_pkce' or 'google_oauth' (browser), 'device_code' or "
            "'google_adc' (headless), 'oauth_pkce' (OpenRouter). 'web' or an empty value picks "
            "the best flow this machine can complete."
        )
    )


class ProviderLoginWebResult(Payload):
    """Answered as soon as the flow has something to show the user; the wait
    for approval continues in the background and is reported via
    ``provider.loginProgress`` notifications."""

    ok: bool = Field(default=True, description="True when the call succeeded.")
    status: LoginWebStatus = Field(
        default="await_user",
        description=(
            "'await_user' once userCode/verificationUri are ready and polling has "
            "started in the background; 'done' or 'failed' if the flow finished "
            "synchronously before this response was sent."
        ),
    )
    userCode: str | None = Field(
        default=None, description="Short code the user types in, for device-code flows."
    )
    verificationUri: str | None = Field(
        default=None, description="URL to open to approve the login."
    )
    verificationUriComplete: str | None = Field(
        default=None, description="verificationUri with the code already embedded, when known."
    )
    expiresInSec: float | None = Field(
        default=None, description="Seconds until the code or session expires, when known."
    )


# --------------------------------------------------------------------------
# checkpoint.*
# --------------------------------------------------------------------------


class CheckpointFile(Payload):
    """One file entry in a checkpoint manifest."""

    path: str = Field(description="Path relative to the session workdir.")
    status: CheckpointFileStatus = Field(description="How the turn changed the file.")
    beforeSha: str | None = Field(
        default=None,
        description="Content-addressed blob sha before the turn, null for created files.",
    )
    afterSha: str | None = Field(
        default=None,
        description="Content-addressed blob sha after the turn, null for deleted files.",
    )
    size: int = Field(default=0, description="Size in bytes of the saved before-state.")
    source: CheckpointFileSource = Field(description="'tool' or 'shell'.")
    restorable: bool = Field(
        default=True, description="False when the daemon cannot safely restore this file."
    )
    skipped: CheckpointSkipped | None = Field(
        default=None, description="Why bytes were not captured, null when captured."
    )


class CheckpointInfo(Payload):
    """A per-turn or restore checkpoint manifest."""

    id: str = Field(description="Checkpoint id.")
    turnId: str | None = Field(
        default=None,
        description="Turn id this checkpoint belongs to, when different clients need it.",
    )
    sessionId: str = Field(description="Session this checkpoint belongs to.")
    workdir: str = Field(description="Workdir the paths are relative to.")
    createdAt: str = Field(description="UTC ISO-8601 timestamp.")
    prompt: str = Field(default="", description="Prompt text, truncated to 200 chars.")
    kind: CheckpointKind = Field(
        description="'turn' for agent work, 'restore' for undoable restore."
    )
    files: list[CheckpointFile] = Field(default_factory=list, description="Files in the manifest.")


class CheckpointListParams(Payload):
    sessionId: str = Field(description="Session whose checkpoints are listed.")


class CheckpointListResult(Payload):
    checkpoints: list[CheckpointInfo] = Field(
        default_factory=list, description="Checkpoints, newest first."
    )


class CheckpointDiffParams(Payload):
    sessionId: str = Field(description="Session whose checkpoint is inspected.")
    id: str = Field(description="Checkpoint id.")
    paths: list[str] | None = Field(
        default=None, description="Optional subset of relative paths to diff."
    )
    through: bool = Field(
        default=False,
        description=(
            "True means restore to before this turn, considering this and all later "
            "turns in the session."
        ),
    )


class CheckpointDiffFile(Payload):
    path: str = Field(description="Relative file path.")
    patch: str = Field(description="Unified diff from current disk content to the before-state.")
    binary: bool = Field(default=False, description="True when no text patch can be shown.")
    changedSince: bool = Field(
        default=False,
        description="True when current disk content differs from the last recorded afterSha.",
    )
    restorable: bool = Field(default=True, description="False when restore cannot apply this file.")


class CheckpointDiffResult(Payload):
    files: list[CheckpointDiffFile] = Field(default_factory=list, description="Per-file diffs.")


class CheckpointRestoreParams(CheckpointDiffParams):
    force: bool = Field(
        default=False, description="Restore even when a file changed since capture."
    )
    dryRun: bool = Field(
        default=False, description="Report what would happen without writing files."
    )


class CheckpointRestoreSkipped(Payload):
    path: str = Field(description="Relative file path.")
    reason: CheckpointRestoreSkipReason = Field(description="Why the file was not restored.")


class CheckpointRestoreResult(Payload):
    restored: list[str] = Field(default_factory=list, description="Paths restored.")
    skipped: list[CheckpointRestoreSkipped] = Field(
        default_factory=list, description="Paths that were not restored and why."
    )
    checkpointId: str | None = Field(
        default=None, description="Restore checkpoint id, null for dry-run or no writes."
    )


class CheckpointDeleteParams(Payload):
    sessionId: str = Field(description="Session whose checkpoints are deleted.")
    id: str | None = Field(
        default=None, description="Checkpoint to delete; omitted deletes the whole session set."
    )


# --------------------------------------------------------------------------
# agent.* / team.*
# --------------------------------------------------------------------------


class AgentInfo(Payload):
    """One named agent."""

    name: str = Field(description="Agent name used by agent.spawn.")
    description: str = Field(default="", description="What the agent is for.")
    channel: str | None = Field(default=None, description="Gateway channel bound to the agent.")
    source: str = Field(default="user", description="Where the definition came from.")
    format: ItemFormat = Field(default="snowpea", description=_FORMAT_DESCRIPTION)
    kind: str = Field(
        default="definition",
        description=(
            "definition = an agents/<name>.md file, subagent = a running child, "
            "named = a persistent named instance, team = the active project team "
            "(a label only - it is not spawnable)."
        ),
    )
    path: str | None = Field(default=None, description="Definition file, when there is one.")
    status: str | None = Field(
        default=None, description="For kind='subagent': queued, running, done or error."
    )
    task: str | None = Field(
        default=None, description="For kind='subagent': the task it was given."
    )
    agentId: str | None = Field(
        default=None, description="For kind='subagent': the id its subagent.* events carry."
    )
    sessionId: str | None = Field(default=None, description="Session the agent runs in.")
    parentSessionId: str | None = Field(
        default=None, description="For kind='subagent': the session that delegated the task."
    )
    namespace: str | None = Field(
        default=None,
        description="For kind='named': the agent's memory namespace, agent:<name>.",
    )
    channels: list[str] = Field(
        default_factory=list,
        description="For kind='named': every gateway channel bound to the agent.",
    )
    bindings: list[str] = Field(
        default_factory=list,
        description="For kind='named': the gateway binding ids serving those channels.",
    )
    jobs: list[str] = Field(
        default_factory=list,
        description="For kind='named': ids of the scheduled jobs that run as this agent.",
    )
    active: bool | None = Field(
        default=None,
        description=(
            "For kind='team': true for the project's active team. Exactly one team "
            "row is active, or none when the project has chosen no team."
        ),
    )
    agents: list[str] = Field(
        default_factory=list,
        description="For kind='team': its member agent names, in roster order.",
    )
    stages: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "For kind='team': which member fills each stage of `/team \"<task>\"` — "
            "keys explore, plan, implement, test, review, and a stage nobody fills "
            "is absent. Empty when the team has no implementer and so cannot run "
            "the pipeline at all."
        ),
    )
    hasGuide: bool | None = Field(
        default=None,
        description="For kind='team': true when a team guide file exists for this team.",
    )


class AgentListResult(Payload):
    agents: list[AgentInfo] = Field(default_factory=list, description="Defined named agents.")


class AgentCreateParams(Payload):
    description: str = Field(description="Natural-language brief the daemon turns into an agent.")
    named: bool = Field(
        default=False,
        description=(
            "Also register a persistent named instance: its own session, the memory "
            "namespace agent:<name>, and rows that survive a daemon restart."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Name for the agent; when omitted the daemon takes the generated one.",
    )


class AgentCreateResult(Payload):
    name: str = Field(description="Name assigned to the new agent.")
    path: str | None = Field(default=None, description="Where the definition was written.")


class AgentSpawnParams(Payload):
    name: str = Field(description="Agent to run.")
    task: str = Field(description="Task handed to the agent.")
    sessionId: str | None = Field(
        default=None, description="Parent session, when spawned from one."
    )
    model: str | None = Field(
        default=None,
        description=(
            "Run this one spawn on a specific model: a models.profiles id, a "
            "'vendor:model' pair, or a bare vendor. Outranks the agent's own "
            "assignment; null uses the configured routing."
        ),
    )


class AgentSpawnResult(Payload):
    agentId: str = Field(description="Id correlating the subagent.* events.")


class AgentBindChannelParams(Payload):
    name: str = Field(description="Agent to bind.")
    channel: str = Field(description="Gateway channel that will reach the agent.")
    credentialsRef: str | None = Field(
        default=None,
        description=(
            "Credential entry the platform account uses; defaults to the platform name, "
            "e.g. 'telegram' for channel 'telegram:123'."
        ),
    )


class AgentDeleteParams(Payload):
    name: str = Field(description="Agent to delete.")


class TeamStartParams(Payload):
    sessionId: str = Field(description="Session the team works under.")
    n: int = Field(description="Number of workers to run in parallel.")
    task: str = Field(description="Task the team splits between workers.")


class TeamStartResult(Payload):
    teamId: str = Field(description="Id to poll with team.status.")


class TeamStatusParams(Payload):
    teamId: str = Field(
        default="", description="Team to inspect; empty means the most recent one."
    )


class TeamTask(Payload):
    """One unit of work inside a team run."""

    taskId: str = Field(description="Task id, stable for the run.")
    title: str = Field(description="Short task description.")
    status: TeamTaskState = Field(default="queued", description="Current state.")
    assignee: str | None = Field(default=None, description="Worker that owns the task.")
    agentN: int | None = Field(
        default=None, description="1-based worker index that owns the task."
    )
    retries: int = Field(default=0, description="How many times a merge conflict re-queued it.")
    branch: str = Field(default="", description="Branch the worker commits the task on.")
    note: str = Field(default="", description="Why the task is in this state.")
    dependsOn: list[str] = Field(
        default_factory=list, description="Task ids that must merge before this one runs."
    )
    conflictHunks: str = Field(
        default="", description="Conflicted diff captured before git merge --abort."
    )
    conflictSummary: str = Field(
        default="", description="One line naming the conflicted files."
    )


class TeamStatusResult(Payload):
    teamId: str = Field(description="Team that was inspected.")
    state: Literal["running", "done", "failed", "interrupted"] = Field(
        default="running", description="Overall state."
    )
    tasks: list[TeamTask] = Field(default_factory=list, description="Task board contents.")
    workers: int = Field(default=0, description="How many workers the run was started with.")
    task: str = Field(default="", description="The task the team was given.")
    worktrees: list[str] = Field(
        default_factory=list, description="Worker worktrees that exist right now."
    )


class TeamRoutingRuleInfo(Payload):
    when: str = Field(description="When to delegate to this agent.")
    agent: str = Field(description="Agent name to delegate to.")
    known: bool = Field(
        default=True,
        description="False when the agent is unknown or outside the team roster.",
    )


class TeamGuideInfo(Payload):
    team: str = Field(description="Team name this guide applies to.")
    description: str = Field(default="", description="One-line summary from the guide frontmatter.")
    persona: str = Field(default="", description="Persona and house rules from the guide body.")
    routing: list[TeamRoutingRuleInfo] = Field(
        default_factory=list, description="Routing rules from the guide."
    )
    source: Literal["project", "global"] = Field(description="Where the guide was read from.")
    path: str | None = Field(default=None, description="Absolute path to the guide file.")


class TeamGuideWorkdirParams(Payload):
    workdir: str | None = Field(default=None, description="Project directory.")
    sessionId: str | None = Field(
        default=None, description="Session whose workdir to use when workdir is omitted."
    )


class TeamGuideGetParams(TeamGuideWorkdirParams):
    team: str | None = Field(
        default=None,
        description="Team to read; omit to use the effective team for this session.",
    )


class TeamGuideGetResult(Payload):
    guide: TeamGuideInfo | None = Field(default=None, description="The guide, if one exists.")
    effectiveTeam: str = Field(description="Team name the guide lookup used.")


class TeamGuideRoutingInput(Payload):
    when: str = Field(description="When to delegate to this agent.")
    agent: str = Field(description="Agent name to delegate to.")


class TeamGuideSetParams(TeamGuideWorkdirParams):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    team: str = Field(description="Team name, or 'default' for sessions with no active team.")
    scope: Literal["project", "global"] = Field(description="Where to write the guide.")
    description: str | None = Field(default=None, description="One-line summary.")
    persona: str = Field(description="Persona body to store.")
    routing: list[TeamGuideRoutingInput] = Field(
        default_factory=list, description="Routing rules to store."
    )


class TeamGuideSetResult(Payload):
    guide: TeamGuideInfo = Field(description="The guide that was written.")


class TeamGuideDeleteParams(TeamGuideWorkdirParams):
    team: str = Field(description="Team name, or 'default'.")
    scope: Literal["project", "global"] = Field(description="Which copy to delete.")


class TeamGuideListParams(TeamGuideWorkdirParams):
    pass


class TeamGuideListResult(Payload):
    guides: list[TeamGuideInfo] = Field(default_factory=list, description="Every visible guide.")


class TeamsChangedNotification(Payload):
    reason: str = Field(description="Why the guide list changed.")
    team: str | None = Field(default=None, description="Team that changed, when known.")


# --------------------------------------------------------------------------
# job.*
# --------------------------------------------------------------------------


class JobGrantInput(Payload):
    """A pre-approval a person gives a job (1.68.0, ``jobApprovals``).

    One of ``pattern`` (an allowlist regex matched against one plain shell
    command, e.g. ``^git fetch( .*)?$``) or ``line`` (one exact command line,
    whitespace normalised; the shape for a compound line); neither: the tool
    itself, for a tool that is not a shell. A catch-all pattern is refused.
    """

    tool: str = Field(default="shell", min_length=1, max_length=200,
                      description="The tool it allows ('shell' for commands).")
    pattern: str | None = Field(default=None, max_length=1000,
                                description="Allowlist regex for one plain command.")
    line: str | None = Field(default=None, max_length=4000,
                             description="One exact command line.")


class JobGrant(Payload):
    """What a job's runs may do without asking (1.68.0, ``jobApprovals``)."""

    id: str = Field(description="Grant id, for job.update removeGrants.")
    tool: str = Field(description="The tool it allows.")
    kind: Literal["rule", "line", "tool"] = Field(
        description="'rule': pattern matches one plain command; 'line': the exact line; "
        "'tool': the tool itself (not a shell)."
    )
    pattern: str | None = Field(default=None, description="kind 'rule': the regex.")
    line: str | None = Field(default=None, description="kind 'line': the line.")
    display: str = Field(description="The grant for people, e.g. 'git fetch …'.")
    by: Literal["approval", "person"] = Field(
        description="'approval': answered 'task' on a run's card; 'person': given with "
        "job.schedule / job.update."
    )
    createdAt: str | None = Field(default=None, description="UTC ISO time it was given.")


class JobWaitingApproval(Payload):
    """A run of the job waiting for a person right now (1.68.0)."""

    requestId: str = Field(description="Answer it with approval.respond.")
    sessionId: str = Field(description="The run's session.")
    tool: str = Field(description="What it wants to run.")
    summary: str = Field(description="The command, or the tool's arguments, shortened.")
    timeoutSec: int = Field(description="How long the request waits in all.")


class JobScheduleParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    grants: list[JobGrantInput] | None = Field(
        default=None,
        max_length=50,
        description=(
            "1.68.0 (jobApprovals): what every run may do without asking. Only a person: a "
            "local IDE/TUI connection, or a local browser 0.4.0+ with actor 'person' and "
            "personConfirm 'window'; a remote web login, a connector and Jarvis are refused "
            "(jarvis_person_only). The security lists still apply to what a grant covers."
        ),
    )
    approvalTimeoutSec: int | None = Field(
        default=None, ge=30, le=86400,
        description="1.68.0: how long a run's approval waits; null: scheduler.approvalTimeoutSec.",
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description="1.68.0: a browser's own confirmation window confirmed the grants.",
    )
    spec: str = Field(description="Cron expression or natural-language schedule.")
    task: str = Field(description="Prompt run on each firing.")
    title: str | None = Field(
        default=None,
        max_length=200,
        description="A short name for the job, shown instead of its task (1.33.0).",
    )
    mode: Mode | None = Field(
        default=None,
        description=(
            "Permission mode for the unattended run. Omitted (1.68.0, jobApprovals; owner "
            "decision 2026-11-19): 'auto' while settings security.enabled is false and a "
            "person makes the routine directly (a local IDE/TUI connection, or a browser with "
            "actor 'person'); else 'accept', as before. A raise still needs a person "
            "(mode_guard)."
        ),
    )
    channel: str | None = Field(
        default=None,
        description=(
            "Gateway channel that receives the output. One rule for job.schedule, /schedule "
            "and the schedule_create tool (1.55.0): a job made by a remote web login — or in "
            "a turn one prompted — is createdBy 'remote_login', reports in the app or the "
            "log only, and is never above settings web.remote.maxMode."
        ),
    )
    agent: str | None = Field(default=None, description="Named agent that runs the task.")
    workdir: str | None = Field(
        default=None, description="Working directory for the run; defaults to the daemon's home."
    )
    sessionTemplate: JobSessionTemplate | None = Field(
        default=None, description="Session setup for each run, incl. host tools (1.7.0)."
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque data for the host, at most 4 KB of JSON (1.23.0). Only a connection "
            "whose system.hello said clientKind 'browser' with a clientId may set it; the "
            "job is then owned by that clientId: only it may update, cancel, delete or run "
            "it now, see its meta, and no agent tool can change or cancel it."
        ),
    )


class JobSessionTemplate(Payload):
    """How a routine's session is set up (1.7.0)."""

    agent: str | None = Field(default=None, description="Named agent that runs the task.")
    mode: Mode | None = Field(default=None, description="Permission mode for the run.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            'Run with a connected client\'s host tools: a clientKind ("browser"), a '
            "clientId or a surface id."
        ),
    )
    hostWaitSec: int = Field(
        default=300,
        description=(
            "How long a run that has started waits for the host's tools to appear; then "
            "it fails with host_unavailable (job.event)."
        ),
    )
    catchUpWindowMinutes: int = Field(
        default=60,
        description=(
            "A firing whose host is not connected is held as waiting_for_host and starts "
            "when the host registers its tools; past this many minutes it is recorded as "
            "missed (addendum 11)."
        ),
    )


class JobScheduleResult(Payload):
    jobId: str = Field(description="Id of the scheduled job.")
    nextRunAt: str | None = Field(
        default=None, description="UTC ISO-8601 time of the first firing."
    )
    fingerprint: str | None = Field(
        default=None,
        description=(
            "For a job with meta, the fingerprint its runs' tool.invoke job will carry "
            "(1.23.0). Seal this value; do not recompute it."
        ),
    )


class JobPreviewParams(Payload):
    spec: str = Field(
        description="A schedule spec, read exactly as job.schedule reads it (1.10.0)."
    )


class JobPreviewResult(Payload):
    kind: Literal["cron", "once", "interval"] = Field(description="What the spec turned into.")
    nextRunAt: str | None = Field(
        default=None,
        description=(
            "UTC ISO-8601 time a job with this spec would first fire if saved now. A spec "
            "that would never fire is invalid_params, as for job.schedule."
        ),
    )
    normalized: str | None = Field(
        default=None,
        description=(
            "The parsed schedule: the cron expression for 'cron', 'every <n>s' for "
            "'interval', 'once at <UTC ISO>' for 'once'."
        ),
    )


class LlmSampleImage(Payload):
    """One image for ``llm.sample`` (1.55.0)."""

    mime: Literal["image/png", "image/jpeg", "image/webp", "image/gif"] = Field(
        description="The image's type."
    )
    dataBase64: str = Field(
        min_length=1, max_length=8_400_000, description="The bytes, base64 (at most 6 MB decoded)."
    )


class LlmSampleParams(Payload):
    sessionId: str | None = Field(
        default=None,
        description=(
            "Session whose provider settings and bill this call uses; its host only. "
            "Omitted (1.40.0, capability 'llmSampleHost'): a host-level call for a connection "
            "that has registered host tools -- the default model of this daemon "
            "(models.default), with 'fast', 'standard' and 'deep' choosing "
            "browser.fastModel / models.categories as for a session. No session is billed "
            "and no usage event is written."
        ),
    )
    modelCategory: Literal["fast", "default", "standard", "deep", "vision"] = Field(
        default="default",
        description=(
            "'fast' = browser.fastModel (then models.categories.fast, then the session's "
            "model); 'default' = the session's model; 'standard' (1.33.0) = "
            "models.categories.standard, then the session's model; 'deep' (1.33.0) = "
            "models.categories.deep, then .standard, then the session's model; 'vision' "
            "(1.55.0) = models.categories.vision only: with none configured the call is "
            "refused with 'unavailable', details.reason 'no_vision_model'."
        ),
    )
    images: list[LlmSampleImage] | None = Field(
        default=None,
        max_length=4,
        description=(
            "1.55.0: up to 4 images sent with the input (each at most 6 MB decoded, 12 MB "
            "together). A model known not to take images refuses the call with "
            "'unavailable', details.reason 'model_cannot_see'; use modelCategory 'vision'. "
            "An image that does not decode is 'invalid_params'."
        ),
    )
    system: str | None = Field(
        default=None, max_length=20_000, description="Instructions the host wrote."
    )
    input: str = Field(
        min_length=1,
        max_length=200_000,
        description="Page-derived text; sent as the user message, fenced and marked untrusted.",
    )
    jsonSchema: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description=(
            "JSON Schema the answer must match. With it the model is told to answer JSON "
            "only, and an answer that does not validate is 'invalid_output'."
        ),
    )
    maxTokens: int = Field(
        default=1024, ge=1, le=4096, description="Output token cap, at most 4096."
    )
    task: str | None = Field(
        default=None,
        max_length=20_000,
        description=(
            "Trusted per-call instruction from the host, placed after the fenced input, so "
            "system + page stay a prefix several samples of one page share (1.21.0)."
        ),
    )
    schemaAfterInput: bool = Field(
        default=False,
        description=(
            "Put the schema instruction after the fenced input (with task) instead of in "
            "the system message, for a schema that varies per call (1.21.0)."
        ),
    )


class LlmSampleUsage(Payload):
    inputTokens: int = 0
    outputTokens: int = 0


class LlmSampleResult(Payload):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True, populate_by_name=True)

    text: str | None = Field(default=None, description="The answer, when no schema was given.")
    data: Any = Field(
        default=None,
        alias="json",
        description="The validated JSON value, when a schema was given.",
    )
    model: str = Field(description="'vendor:model' that answered.")
    usage: LlmSampleUsage = Field(description="Tokens the call used.")


class WebSearchParams(Payload):
    query: str = Field(min_length=1, max_length=2048, description="What to search for.")
    num: int = Field(default=10, ge=1, le=20, description="How many results, 1-20 (default 10).")


class WebSearchHit(Payload):
    title: str
    url: str
    snippet: str = Field(default="", description="Empty when the provider gives none.")


class WebSearchResult(Payload):
    results: list[WebSearchHit] = Field(default_factory=list)
    provider: str = Field(description="The search provider that answered, e.g. 'ddgs'.")


class JobHeldRun(Payload):
    """An occurrence held for its host, or one that never got it (addendum 11/12)."""

    status: Literal["waiting_for_host", "missed"] = Field(description="Where it stands.")
    scheduledAt: str = Field(description="UTC ISO time the occurrence was due.")


class JobInfo(Payload):
    """One scheduled job."""

    jobId: str = Field(description="Job id.")
    spec: str = Field(description="Schedule as given.")
    task: str = Field(description="Prompt run on each firing.")
    title: str | None = Field(
        default=None, description="The job's short name, when it has one (1.33.0)."
    )
    mode: Mode = Field(default="accept", description="Permission mode for the run.")
    channel: str | None = Field(default=None, description="Channel that receives the output.")
    originSessionId: str | None = Field(
        default=None, description="TUI/chat session that also receives every result."
    )
    nextRunAt: str | None = Field(default=None, description="UTC ISO-8601 time of the next firing.")
    state: Literal["scheduled", "running", "cancelled"] = Field(
        "scheduled", description="Current job state."
    )
    kind: Literal["cron", "once", "interval"] = Field(
        "once", description="How the spec repeats: a cron rule, a one-shot, or a fixed interval."
    )
    enabled: bool = Field(True, description="False once the job is cancelled or has run out.")
    lastRunAt: str | None = Field(
        default=None, description="UTC ISO-8601 time of the last firing, null before the first."
    )
    lastStatus: Literal["ok", "error", "denied_by_timeout"] | None = Field(
        default=None, description="How the last run ended."
    )
    held: JobHeldRun | None = Field(
        default=None,
        description=(
            "The newest occurrence when it is waiting for its host or was missed "
            "(addendum 12); null otherwise."
        ),
    )
    catchUpWindowMinutes: int = Field(
        default=60, description="How long a held run waits for its host before it is missed."
    )
    createdVia: Literal["rpc", "tool"] = Field(
        default="tool",
        description="'rpc' for job.schedule, 'tool' for schedule_create or /schedule (1.23.0).",
    )
    createdBy: Literal["remote_login"] | None = Field(
        default=None,
        description=(
            "'remote_login' when a remote web login (web.remote) scheduled the job over "
            "job.schedule; null for every other job (1.55.0). Show it: the job keeps "
            "running after that login ends. web.remote.status counts them (remoteJobs)."
        ),
    )
    owned: bool = Field(
        default=False,
        description="True for a job with meta: only its owner may change, run or delete it.",
    )
    target: str | None = Field(
        default=None,
        description=(
            "What runs it when not an ordinary scheduled session: 'jarvis-task' is a Jarvis "
            "mode assistant task (1.53.0, WP8): its own worker session, result handed to "
            "Jarvis."
        ),
    )
    timezone: str | None = Field(
        default=None,
        description="IANA zone its cron clock times are read in; null is the daemon's (1.53.0).",
    )
    meta: dict[str, Any] | None = Field(
        default=None, description="The job's meta; only the owner connection sees it."
    )
    ownerClientId: str | None = Field(
        default=None, description="The owning browser clientId; only the owner sees it."
    )
    fingerprint: str | None = Field(
        default=None,
        description="The fingerprint tool.invoke job carries; only the owner sees it.",
    )
    lastError: str | None = Field(
        default=None,
        description=(
            "1.68.0 (jobApprovals): why the last run did not end 'ok' (as job.runs error). A "
            "run that waited for an approval nobody gave says 'approval needed: <tool> "
            "<command>'; null after an 'ok' run. 1.69.0 (jobMissedRuns): an occurrence "
            "missed past the catch-up window says 'missed: <reason>'."
        ),
    )
    lastSessionId: str | None = Field(
        default=None, description="1.68.0: the session the last run opened, to open it."
    )
    waitingApproval: JobWaitingApproval | None = Field(
        default=None,
        description="1.68.0: a run of it waits for a person now (the oldest such request).",
    )
    grants: list[JobGrant] = Field(
        default_factory=list,
        description="1.68.0: what its runs may do without asking; job.update removeGrants.",
    )
    approvalTimeoutSec: int | None = Field(
        default=None,
        description="1.68.0: its own approval wait; null: settings scheduler.approvalTimeoutSec.",
    )


class JobListResult(Payload):
    jobs: list[JobInfo] = Field(default_factory=list, description="Known jobs.")


class JobIdParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    jobId: str = Field(description="Target job.")


class JobDeleteParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    jobId: str = Field(description="Job to delete, with its schedule and run history.")
    deleteRuns: bool = Field(
        default=False,
        description=(
            "Also delete the sessions its runs opened, with their workspaces, as "
            "session.deleteSaved does. A run still in progress is left alone."
        ),
    )


class JobUpdateTemplate(Payload):
    """The session-template fields job.update may change; omitted ones stay."""

    catchUpWindowMinutes: int | None = Field(
        default=None, ge=0, description="New catch-up window for held runs."
    )
    hostWaitSec: int | None = Field(
        default=None, ge=0, description="New wait for a started run's host tools."
    )


class JobUpdateParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    jobId: str = Field(description="Job to change.")
    sessionTemplate: JobUpdateTemplate | None = Field(
        default=None, description="Template fields to change."
    )
    enabled: bool | None = Field(
        default=None,
        description=(
            "False pauses the job (no next firing); true resumes it from now on its "
            "schedule."
        ),
    )
    schedule: str | None = Field(
        default=None,
        description=(
            "A new schedule spec, parsed like job.schedule's spec; the next run is "
            "recomputed."
        ),
    )
    task: str | None = Field(
        default=None,
        description=(
            "A new prompt for every later run (1.33.0); it must not be blank. A job "
            "with meta gets a new fingerprint."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=200,
        description="A new short name (1.33.0); '' removes it, null or absent keeps it.",
    )
    mode: Mode | None = Field(
        default=None,
        description=(
            "1.68.0 (jobApprovals): the permission mode later runs start in. A raise (e.g. to "
            "'auto') is a person's, as at job.schedule (jarvis_person_only, mode_raise); a "
            "remote web login never above web.remote.maxMode."
        ),
    )
    addGrants: list[JobGrantInput] | None = Field(
        default=None, max_length=50,
        description="1.68.0: grants to add (as JobScheduleParams.grants; a person's only).",
    )
    removeGrants: list[str] | None = Field(
        default=None, max_length=200,
        description="1.68.0: grant ids to remove (JobInfo.grants); anyone who may update it.",
    )
    approvalTimeoutSec: int | None = Field(
        default=None, ge=0, le=86400,
        description="1.68.0: how long a run's approval waits; 0 goes back to the setting.",
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description="1.68.0: a browser's own confirmation window confirmed addGrants.",
    )


class JobRunsParams(Payload):
    """``job.runs`` — one job's run history (1.33.0)."""

    jobId: str = Field(
        description="Job whose runs to list; an unknown one is invalid_params, as in job.update."
    )
    limit: int = Field(default=50, ge=1, le=500, description="Newest runs to return.")


class JobRunInfo(Payload):
    """One occurrence of a job: a firing, a runNow, or one held for its host."""

    runId: str = Field(description="Stable id of the occurrence: '<jobId>@<scheduledAt>'.")
    scheduledAt: str = Field(description="UTC ISO time the occurrence was due.")
    startedAt: str = Field(description="UTC ISO time it was claimed.")
    endedAt: str | None = Field(
        default=None, description="UTC ISO time it finished; null while running or held."
    )
    status: str = Field(
        description=(
            "'ok', 'error' or 'denied_by_timeout' once finished; 'running', "
            "'waiting_for_host' or 'missed' otherwise."
        )
    )
    sessionId: str | None = Field(
        default=None, description="Session the run opened, when it opened one."
    )
    error: str | None = Field(
        default=None,
        description="Why a run that did not end 'ok' failed, clipped (runs recorded since 1.33.0).",
    )
    trigger: Literal["schedule", "runNow"] | None = Field(
        default=None,
        description="What started it; null for a run an older core recorded.",
    )


class JobRunsResult(Payload):
    runs: list[JobRunInfo] = Field(default_factory=list, description="Newest first.")


class JobNextRunForClientParams(Payload):
    clientId: str = Field(description="A host's clientId (a browser profile).")


class JobNextRunForClientResult(Payload):
    nextRunAt: str | None = Field(
        default=None,
        description=(
            "Earliest run that needs this client's host: a held (waiting_for_host) "
            "occurrence or the next firing of a job whose hostToolsFrom is this clientId; "
            "null when none."
        ),
    )


class JobDeleteResult(Payload):
    jobId: str = Field(description="The deleted job.")
    deletedSessions: int = Field(default=0, description="Run sessions deleted with it.")


# --------------------------------------------------------------------------
# gateway.*
# --------------------------------------------------------------------------


class GatewayBindParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    platform: str = Field(description="Chat platform key: telegram, discord or slack.")
    credentialsRef: str = Field(
        description="Name of the credential to use: a key in credentials.json or an env var."
    )
    target: dict[str, Any] = Field(
        description=(
            "What the conversation talks to: {'agent': name}, {'session': id} "
            "or {'new_session': {'workdir': path, 'mode': mode}}."
        )
    )
    channelId: str | None = Field(
        default=None, description="Restrict the binding to one chat, channel or room."
    )
    userId: str | None = Field(
        default=None, description="Platform user allowed to answer approvals from chat."
    )


class GatewayBindResult(Payload):
    bindingId: str = Field(description="Id used to unbind later.")


class GatewayBinding(Payload):
    """One live gateway attachment."""

    bindingId: str = Field(description="Binding id.")
    platform: str = Field(description="Chat platform key.")
    target: str = Field(description="Target it routes to, e.g. 'agent:ops' or 'new_session'.")
    credentialsRef: str = Field(default="", description="Credential name; never the secret.")
    channelId: str | None = Field(default=None, description="Chat it is limited to, if any.")
    userId: str | None = Field(default=None, description="User allowed to answer approvals.")
    state: Literal["active", "inactive"] = Field(
        default="active", description="Whether it is listening."
    )
    source: str = Field(
        default="manual",
        description=(
            "'manual' for a gateway.bind call, 'settings' for the catch-all binding "
            "the setup wizard's settings.gateway entry keeps in sync."
        ),
    )


class GatewaySyncResult(Payload):
    """What one ``gateway.sync`` pass changed, by platform."""

    added: list[str] = Field(
        default_factory=list, description="Platforms that started listening."
    )
    removed: list[str] = Field(
        default_factory=list, description="Platforms whose auto binding was dropped."
    )
    kept: list[str] = Field(
        default_factory=list, description="Platforms that were already listening."
    )


class GatewayListResult(Payload):
    bindings: list[GatewayBinding] = Field(
        default_factory=list, description="Live gateway bindings."
    )


class GatewayUnbindParams(Payload):
    bindingId: str = Field(description="Binding to remove.")


# --------------------------------------------------------------------------
# memory.* / skill.*
# --------------------------------------------------------------------------


#: Where a memory is kept (M5 §1b).  ``"all"`` is only valid as a filter.
MemoryScope = Literal["project", "global", "agent", "all"]


class MemorySearchParams(Payload):
    query: str = Field(description="Free-text query.")
    limit: int = Field(default=10, description="Maximum number of hits.")
    namespace: str | None = Field(
        default=None,
        description='Memory namespace to search; defaults to "default". Never crosses namespaces.',
    )


class MemoryHit(Payload):
    """One recalled memory."""

    id: str = Field(description="Memory id.")
    text: str = Field(description="Stored text.")
    tags: list[str] = Field(default_factory=list, description="Tags attached at write time.")
    score: float = Field(default=0.0, description="Relevance score; higher is closer.")
    scope: MemoryScope = Field(default="global", description="Scope the hit came from.")
    project: str = Field(
        default="", description="Project root for a project memory; empty otherwise."
    )


class MemorySearchResult(Payload):
    hits: list[MemoryHit] = Field(default_factory=list, description="Matches, best first.")


MemoryOrigin = Literal["user-said", "model", "nudge", "auto-pref"]
MemorySource = Literal["browser", "user-said", "model", "nudge", "auto-pref"]
PreferenceCategory = Literal["shopping", "output", "site"]


class MemoryWriteParams(Payload):
    text: str = Field(description="Text to remember.")
    tags: list[str] = Field(default_factory=list, description="Tags for later filtering.")
    namespace: str | None = Field(
        default=None, description='Memory namespace to write into; defaults to "default".'
    )


class MemoryWriteResult(Payload):
    id: str = Field(description="Id of the stored memory.")


class MemoryUpdateParams(Payload):
    """``memory.update`` — the person edits a memory in place (1.28.0)."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    id: str = Field(description="Memory id.")
    text: str = Field(
        min_length=1,
        description="The new text; refused like memory.write when it holds a value.",
    )


class MemoryConfirmParams(Payload):
    """``memory.confirm`` — answer a memory.candidate event (1.28.0)."""

    candidateId: str = Field(description="From the memory.candidate event.")
    accept: bool = Field(description="True saves it as a confirmed memory; false drops it.")


class MemoryConfirmResult(Payload):
    id: str | None = Field(
        default=None, description="The stored memory when accepted; None when dropped."
    )


class MemoryListParams(Payload):
    """Filter for ``memory.list`` (M5 §1b).  Everything is optional."""

    scope: MemoryScope | None = Field(
        default=None,
        description=(
            'Which scopes to list: "project", "global", "agent" or "all" (default).'
        ),
    )
    sessionId: str | None = Field(
        default=None,
        description="Session whose project and agent scopes to resolve; omit for global only.",
    )
    project: str | None = Field(
        default=None,
        description=(
            "Project root to use instead of a session's, so a CLI in a checkout can list "
            "that project's memories without opening a session."
        ),
    )
    query: str | None = Field(
        default=None, description="Free-text filter; omit to list newest first."
    )
    limit: int = Field(default=100, description="Maximum number of entries.")
    namespace: Literal["browser"] | None = Field(
        default=None,
        description=(
            '"browser" lists visited-page memories (memory.ingest) instead, newest first; '
            "scope, sessionId and project are then ignored (1.27.0)."
        ),
    )


class MemoryEntryInfo(Payload):
    """One stored memory, with the scope it is kept in."""

    id: str = Field(description="Memory id.")
    text: str = Field(description="Stored text.")
    tags: list[str] = Field(default_factory=list, description="Tags attached at write time.")
    scope: MemoryScope = Field(default="global", description="Scope this memory is kept in.")
    project: str = Field(
        default="", description="Project root for a project memory; empty otherwise."
    )
    createdAt: str = Field(default="", description="When it was written (ISO-8601, UTC).")
    sensitive: bool = Field(
        default=False,
        description=(
            "True when memory.write would now refuse this text: it holds what looks like "
            "a credential, card or long number, phone number, email address or resident "
            "registration number (1.27.0). It was saved before the check; a settings "
            "page can offer to delete it."
        ),
    )
    source: MemorySource | None = Field(
        default=None,
        description=(
            'Where it came from: "browser" for a visited-page memory (1.27.0); from 1.28.0 '
            'a user memory says who wrote it: "user-said" (memory.write), "model" '
            '(save_memory), "nudge" (the auto-remember of "remember …") or "auto-pref" '
            "(the preference extractor). memory.update keeps it and sets confirmed. None "
            "for a memory saved before 1.28.0."
        ),
    )
    sourceSession: str | None = Field(
        default=None, description="The session that wrote it, if one did (1.28.0)."
    )
    confirmed: bool = Field(
        default=False,
        description="True when the person wrote, edited or accepted it (1.28.0).",
    )
    url: str = Field(default="", description="The visited page, for a browser memory.")
    visitedAt: str = Field(
        default="", description="When the page was visited, for a browser memory, if known."
    )


class MemoryUpdateResult(Payload):
    entry: MemoryEntryInfo = Field(description="The memory after the edit.")


class MemoryListResult(Payload):
    entries: list[MemoryEntryInfo] = Field(
        default_factory=list, description="Matching memories, newest or best first."
    )


class MemoryDeleteParams(Payload):
    id: str | None = Field(default=None, description="Memory id to forget.")
    source: Literal["browser"] | None = Field(
        default=None,
        description=(
            '"browser" forgets browsing memories instead: those of url (a page, or a '
            "whole site when it has no path), those visited before `before`, or all (1.7.0)."
        ),
    )
    url: str | None = Field(default=None, description="With source: the page or site.")
    before: str | None = Field(
        default=None, description="With source: UTC ISO time; forget visits before it."
    )


class MemoryIngestItem(Payload):
    url: str = Field(description="Page address.")
    title: str = Field(default="", description="Page title.")
    text: str = Field(default="", description="Page text (first 4000 chars are kept).")
    visitedAt: str | None = Field(default=None, description="UTC ISO time of the visit.")


class MemoryIngestParams(Payload):
    items: list[MemoryIngestItem] = Field(description="Visited pages to remember.")
    sessionId: str | None = Field(default=None, description="Session they came from, if any.")


class MemoryIngestResult(Payload):
    added: int = Field(default=0, description="Pages stored.")
    skipped: int = Field(default=0, description="Pages already stored (same url and text).")
    removed: int = Field(default=0, description="For memory.delete by source: how many.")


# --------------------------------------------------------------------------
# site.* — site memory, learned per-site maps (1.9.0)
# --------------------------------------------------------------------------

#: How a stored locator finds its element.  XPath is deliberately absent: it
#: breaks on any layout change and can address text a page did not label.
SiteLocatorKind = Literal["role", "label", "text", "placeholder", "testid", "css"]
SiteActionKind = Literal["click", "fill", "select", "toggle", "submit"]
SiteMarkOutcome = Literal["ok", "stale"]
#: What a typed flow step does to its action's element (1.15.0).
SiteStepVerb = Literal["fill", "click", "select", "enter"]
SiteMarkPartOutcome = Literal["ok", "miss", "stale"]
SITE_RECIPE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
SITE_FIELD_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,23}$"
#: Attributes a recipe field may read instead of the text; never ``value``.
SITE_ATTR_PATTERN = (
    r"^(href|src|srcset|alt|title|datetime|aria-label|content|id|class|data-[a-z0-9_-]{1,40})$"
)
#: Stats counters stop here.
SITE_COUNT_MAX = 1_000_000

#: Plain names chosen by the agent: a page type and an action name.
SITE_NAME_PATTERN = r"^[a-z0-9-]{1,40}$"


class SiteLocator(Payload):
    """One way to find an element; ``role`` takes role (+ name), every other kind value."""

    by: SiteLocatorKind = Field(description="Locator kind (1.9.0). Never XPath.")
    role: str | None = Field(
        default=None, max_length=40, description="With by 'role': the ARIA role."
    )
    name: str | None = Field(
        default=None, max_length=200, description="With by 'role': the accessible name."
    )
    value: str | None = Field(
        default=None,
        max_length=200,
        description="With every other kind: the label, text, placeholder, test id or CSS.",
    )


class SiteFrame(Payload):
    """One hop to the frame an action lives in (1.16.0): no values or session ids."""

    selector: str = Field(
        min_length=1,
        max_length=160,
        description=(
            "The frame element in its parent: '#id', or iframe/frame[name|title|data-testid=\"…\"]."
        ),
    )
    origin: str = Field(
        min_length=1, max_length=200, description="The frame document's canonical http(s) origin."
    )
    path: str = Field(
        min_length=1, max_length=200, description="Its path pattern, no query; '*' = any chars."
    )


class SiteAction(Payload):
    name: str = Field(pattern=SITE_NAME_PATTERN, description="Action name, [a-z0-9-]{1,40}.")
    kind: SiteActionKind = Field(description="What the action does to its element.")
    locators: list[SiteLocator] = Field(
        min_length=1,
        max_length=6,
        description="At most 6, in the order to try; at least one is not 'css'.",
    )
    loginField: bool = Field(
        default=False,
        description="A login name or password input: locators only, never a value.",
    )
    frames: list[SiteFrame] | None = Field(
        default=None,
        min_length=1,
        max_length=4,
        description="Frames from the page down to the element, outermost first (1.16.0).",
    )
    stale: bool = Field(
        default=False,
        description=(
            "Its locators last found nothing (1.27.0): set by site.markPart part action "
            "stale, cleared by ok. The entry's own stale is separate."
        ),
    )
    missCount: int = Field(
        default=0, ge=0, description="How often site.markPart part action said miss (1.27.0)."
    )


class SiteFlowStep(Payload):
    """One replayable flow step (1.15.0): which action, never what to type."""

    verb: SiteStepVerb = Field(description="fill | click | select | enter.")
    action: str | None = Field(
        default=None,
        pattern=SITE_NAME_PATTERN,
        description=(
            "An action name of the same entry; required for fill, click and select. Never a "
            "login field for fill or select."
        ),
    )
    intent: str | None = Field(
        default=None, max_length=120, description="What the step is for, one line."
    )


class SiteFlowStats(Payload):
    ok: int = Field(default=0, ge=0, le=SITE_COUNT_MAX, strict=True)
    missed: int = Field(default=0, ge=0, le=SITE_COUNT_MAX, strict=True)
    lastRun: str | None = Field(default=None, description="UTC ISO time of the last ok run.")


class SiteFlow(Payload):
    name: str = Field(min_length=1, max_length=40, description="Flow name.")
    steps: list[Annotated[str, Field(max_length=120)] | SiteFlowStep] = Field(
        default_factory=list,
        max_length=16,
        description=(
            "At most 16: a string of 120 chars (shown, never replayed) or a typed step "
            "(1.15.0)."
        ),
    )
    expect: Literal["page", "update"] | None = Field(
        default=None,
        description="After the last step: 'page' = a new page loads, 'update' = this one changes.",
    )
    stats: SiteFlowStats | None = Field(
        default=None, description="Replay counts; site.markPart keeps them (1.15.0)."
    )


class SiteRecipeField(Payload):
    name: str = Field(
        pattern=SITE_FIELD_NAME_PATTERN, description="[A-Za-z][A-Za-z0-9_]{0,23}, unique."
    )
    selector: str = Field(
        max_length=100, description="CSS relative to the item; '' = the item itself."
    )
    attr: str | None = Field(
        default=None,
        pattern=SITE_ATTR_PATTERN,
        description="Read this attribute instead of the text (href, src, data-*…; never value).",
    )


class SiteRecipeStats(Payload):
    hits: int = Field(default=0, ge=0, le=SITE_COUNT_MAX, strict=True)
    misses: int = Field(default=0, ge=0, le=SITE_COUNT_MAX, strict=True)
    day: int | None = Field(
        default=None,
        ge=0,
        le=SITE_COUNT_MAX,
        strict=True,
        description="Days since 1970-01-01 (UTC) of the last success or save.",
    )


class SiteRecipe(Payload):
    """How to read a page's repeated structure (1.15.0): selectors only, no values."""

    name: str = Field(
        pattern=SITE_RECIPE_NAME_PATTERN,
        description="[a-z0-9][a-z0-9-]{0,31}, unique in the entry.",
    )
    intent: str | None = Field(default=None, max_length=120, description="What it reads.")
    item: str = Field(
        min_length=1, max_length=100, description="CSS of the repeated element."
    )
    fields: list[SiteRecipeField] = Field(min_length=1, max_length=12, description="1 to 12.")
    stats: SiteRecipeStats | None = Field(default=None, description="Use counts.")
    stale: bool = Field(default=False, description="Its last use found nothing.")


class SiteEntryInput(Payload):
    """What the host sends to ``site.put``: structure only, no values (1.9.0)."""

    pageType: str = Field(
        pattern=SITE_NAME_PATTERN, description="Page type, [a-z0-9-]{1,40}, e.g. 'login'."
    )
    urlPattern: str = Field(
        min_length=1,
        max_length=200,
        description="Path glob on the origin, starting with '/'; '*' = any segment chars.",
    )
    summary: str = Field(default="", max_length=300, description="What the page is.")
    landmarks: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list, max_length=20, description="At most 20 of 80 chars."
    )
    actions: list[SiteAction] = Field(
        default_factory=list, max_length=30, description="At most 30."
    )
    flows: list[SiteFlow] = Field(default_factory=list, max_length=10, description="At most 10.")
    recipes: list[SiteRecipe] = Field(
        default_factory=list, max_length=8, description="At most 8 readers (1.15.0)."
    )
    pitfalls: list[Annotated[str, Field(max_length=160)]] = Field(
        default_factory=list, max_length=10, description="At most 10 of 160 chars."
    )
    fingerprint: str | None = Field(
        default=None,
        max_length=200,
        description="Structural hash of the page's AX skeleton, e.g. 'ax1:3f9a…'.",
    )
    lastVerified: str | None = Field(
        default=None, description="UTC ISO time the host last verified it; default now."
    )


class SiteEntry(SiteEntryInput):
    """A stored entry; strings are page-derived, untrusted data (1.9.0)."""

    entryId: str = Field(description="Core-assigned id.")
    origin: str = Field(description="Origin, e.g. 'https://www.example.com'.")
    createdAt: str = Field(description="UTC ISO time of the first put.")
    lastVerified: str = Field(  # type: ignore[assignment]
        description="UTC ISO time of the last put or 'ok' mark."
    )
    successCount: int = Field(
        default=0,
        description="'ok' marks. Before 1.27.0 every put counted as one too.",
    )
    failureCount: int = Field(default=0, description="'stale' marks.")
    stale: bool = Field(default=False, description="Marked stale and not replaced since.")
    source: Literal["learned", "pack", "shared"] = Field(
        default="learned",
        description=(
            "learned (site.put), or read-only from a signed plugin pack: pack (curated) "
            "or shared (opt-in maps through the same channel) (1.28.0). A pack entry's "
            "id starts with p_; site.mark/markPart count it for site.stats and change "
            "nothing; site.put for its pageType makes a learned entry, which wins."
        ),
    )
    packVersion: str | None = Field(
        default=None, description="The pack's version, for a pack or shared entry (1.28.0)."
    )


class SiteGetParams(Payload):
    origin: str = Field(description="Origin, e.g. 'https://www.example.com'.")
    path: str | None = Field(
        default=None,
        max_length=2048,
        description="Path (and query) on the origin: only entries whose urlPattern matches.",
    )
    pageType: str | None = Field(default=None, description="Only this page type.")
    sessionId: str | None = Field(
        default=None,
        description=(
            "The session the call is made for (1.27.0); its siteMemory override from "
            "session.create applies."
        ),
    )


class SiteGetResult(Payload):
    entries: list[SiteEntry] = Field(
        default_factory=list,
        description="Most specific urlPattern first; stale entries included, flagged.",
    )


class SitePutParams(Payload):
    origin: str = Field(description="Origin the entry belongs to.")
    entry: SiteEntryInput = Field(description="The entry; upserted by (origin, pageType).")
    sessionId: str | None = Field(
        default=None,
        description=(
            "The session the call is made for (1.27.0); its siteMemory override from "
            "session.create applies."
        ),
    )


class SitePutResult(Payload):
    entryId: str = Field(description="The entry's id.")
    created: bool = Field(description="True when no entry for (origin, pageType) existed.")


class SiteMarkParams(Payload):
    origin: str = Field(description="Origin of the entry.")
    entryId: str = Field(description="The entry.")
    outcome: SiteMarkOutcome = Field(description="'ok' = verified use; 'stale' = it failed.")
    sessionId: str | None = Field(
        default=None,
        description=(
            "The session the call is made for (1.27.0); its siteMemory override from "
            "session.create applies."
        ),
    )
    detail: str | None = Field(
        default=None, max_length=200, description="For the UI and log only."
    )


class SiteMarkResult(Payload):
    entry: SiteEntry = Field(description="The entry after the mark.")


class SiteMarkPartParams(Payload):
    origin: str = Field(description="Origin of the entry.")
    entryId: str = Field(description="The entry.")
    part: Literal["flow", "recipe", "action"] = Field(
        description='Which list the part is in; "action" from 1.27.0.'
    )
    name: str = Field(
        min_length=1, max_length=40, description="The flow's, recipe's or action's name."
    )
    sessionId: str | None = Field(
        default=None,
        description=(
            "The session the call is made for (1.27.0); its siteMemory override from "
            "session.create applies."
        ),
    )
    outcome: SiteMarkPartOutcome = Field(
        description=(
            "flow: ok (stats.ok+1, lastRun) | miss (stats.missed+1). recipe: ok (hits+1, "
            "day, not stale) | miss (misses+1) | stale (misses+1, stale). action: ok (not "
            "stale) | miss (missCount+1) | stale (stale)."
        )
    )


class SiteMarkPartResult(Payload):
    entry: SiteEntry = Field(description="The entry after the count.")


class SiteStatsParams(Payload):
    origin: str | None = Field(default=None, description="Only this origin.")
    days: int = Field(default=30, ge=1, le=365, description="Days back, today included.")


class SiteOriginStats(Payload):
    """How one origin's stored maps did over the window, counted per call (1.27.0)."""

    origin: str = Field(description="Origin.")
    locateOk: int = Field(default=0, description="site.mark ok.")
    locateMiss: int = Field(default=0, description="site.mark stale.")
    runOk: int = Field(default=0, description="site.markPart flow ok.")
    runMiss: int = Field(default=0, description="site.markPart flow miss.")
    readOk: int = Field(default=0, description="site.markPart recipe ok.")
    readMiss: int = Field(default=0, description="site.markPart recipe miss or stale.")
    puts: int = Field(default=0, description="site.put.")
    staleMarks: int = Field(default=0, description="Marks that flagged an entry or recipe stale.")
    playbookPuts: int = Field(default=0, description="site.playbook.put (1.38.0).")
    playbookOk: int = Field(default=0, description="site.playbook.mark ok (1.38.0).")
    playbookMiss: int = Field(
        default=0, description="site.playbook.mark partial or failed (1.38.0)."
    )


class SiteStatsResult(Payload):
    origins: list[SiteOriginStats] = Field(
        default_factory=list, description="Origins with any count in the window, by origin."
    )


class SiteListParams(Payload):
    origin: str | None = Field(default=None, description="Only this origin.")
    limit: int = Field(default=100, ge=1, le=500, description="Most sites per page.")
    cursor: str | None = Field(default=None, description="From the previous page.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "The browser profile (its clientId). Required from a UI client; a browser "
            "client may only name its own."
        ),
    )


class SiteSummary(Payload):
    origin: str = Field(description="Origin.")
    entries: int = Field(description="Entries stored for it.")
    lastVerified: str = Field(description="Latest lastVerified of its entries.")
    stale: int = Field(description="How many of them are stale.")
    pageTypes: list[str] = Field(
        default_factory=list, description="Distinct page types of its entries, sorted (1.9.0)."
    )
    successCount: int = Field(default=0, description="Sum over its entries (1.9.0).")
    failureCount: int = Field(default=0, description="Sum over its entries (1.9.0).")
    packEntries: int = Field(
        default=0,
        description=(
            "Entries site.get adds from signed packs, those no learned entry shadows "
            "(1.28.0). An origin known only from a pack is not listed."
        ),
    )
    playbooks: int = Field(
        default=0, description="Typed playbooks stored for the origin (1.38.0)."
    )


class SiteListResult(Payload):
    sites: list[SiteSummary] = Field(default_factory=list, description="Sites, by origin.")
    cursor: str | None = Field(default=None, description="Pass back for the next page.")


class SiteDeleteParams(Payload):
    origin: str = Field(description="Origin.")
    entryId: str | None = Field(default=None, description="One entry; omitted = all of them.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "The browser profile (its clientId). Required from a UI client; a browser "
            "client may only name its own."
        ),
    )


class SiteDeleteResult(Payload):
    deleted: int = Field(description="Entries removed.")


# ---------------------------------------------------------------------------
# site playbooks (1.38.0)
# ---------------------------------------------------------------------------

SitePlaybookSlotKind = Literal["text", "html", "files", "choice", "date", "lines"]
SitePlaybookState = Literal["candidate", "verified", "trusted", "healing", "retired"]
SitePlaybookResult = Literal["ok", "partial", "failed"]
SitePlaybookEditOp = Literal[
    "rename",
    "alwaysAsk",
    "retire",
    "unretire",
    "restore",
    "acknowledge",
    "removeSegment",
    "removePitfall",
]

#: A playbook, slot or segment name.
SITE_PLAYBOOK_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
SITE_SLOT_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,31}$"
SITE_VERB_PATTERN = r"^[A-Za-z][A-Za-z0-9]{0,23}$"


class SitePlaybookSlot(Payload):
    """A parameter of the task: its name and type, never a value."""

    name: str = Field(pattern=SITE_SLOT_NAME_PATTERN, description="Slot name.")
    kind: SitePlaybookSlotKind = Field(description="What the slot holds.")
    optional: bool = Field(default=False, description="The task can run without it.")


class SitePlaybookStep(Payload):
    """One step; it names an action of the origin's entries and holds no locator."""

    do: str = Field(
        pattern=SITE_VERB_PATTERN, description="The verb: goto, click, fill, choose, ..."
    )
    action: str | None = Field(
        default=None,
        pattern=SITE_NAME_PATTERN,
        description="An action of this origin's entries. A login field is refused.",
    )
    slot: str | None = Field(default=None, description="A slot of this playbook.")
    url: str | None = Field(
        default=None, max_length=200, description="For goto: a path on the origin, '/...'."
    )
    mode: str | None = Field(default=None, pattern=SITE_VERB_PATTERN, description="E.g. replace.")
    via: str | None = Field(default=None, pattern=SITE_NAME_PATTERN, description="E.g. an action.")
    consequential: bool | str = Field(
        default=False,
        description="Publishes, sends or pays: always asks. A short word names what.",
    )
    optional: bool = Field(default=False, description="Skipped when the step has no target.")
    ask: bool = Field(default=False, description="Stops for the user's confirmation first.")

    @field_validator("consequential")
    @classmethod
    def _short_word(cls, value: bool | str) -> bool | str:
        if isinstance(value, str) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,31}", value):
            raise ValueError("a short plain word")
        return value


class SitePlaybookCheck(Payload):
    """A check from the host's fixed vocabulary, run after a segment."""

    kind: str = Field(
        pattern=SITE_VERB_PATTERN, description="present, fieldEquals, editorContains, ..."
    )
    action: str | None = Field(default=None, pattern=SITE_NAME_PATTERN)
    slot: str | None = Field(default=None)
    measure: list[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list, max_length=4
    )
    atLeast: str | None = Field(default=None, max_length=40, description='E.g. "len(images)".')


class SitePlaybookSegment(Payload):
    """The unit of replay and of fallback."""

    name: str = Field(pattern=SITE_PLAYBOOK_NAME_PATTERN)
    page: str | None = Field(default=None, max_length=200, description="A path pattern, '/...'.")
    steps: list[SitePlaybookStep] = Field(default_factory=list, max_length=16)
    checks: list[SitePlaybookCheck] = Field(default_factory=list, max_length=8)


class SitePlaybookPitfall(Payload):
    text: str = Field(min_length=1, max_length=200, description="What goes wrong. No values.")
    seen: int = Field(default=1, ge=0, le=SITE_COUNT_MAX, strict=True)
    lastVerified: str | None = Field(default=None, description="YYYY-MM-DD.")
    contradicted: int = Field(default=0, ge=0, le=SITE_COUNT_MAX, strict=True)
    source: Literal["host", "agent", "user"] | None = Field(default=None)


class SitePlaybookRun(Payload):
    """One run, counts only."""

    day: str = Field(description="YYYY-MM-DD (UTC).")
    result: SitePlaybookResult
    done: int = Field(ge=0, le=64, strict=True, description="Segments done.")
    total: int = Field(ge=0, le=64, strict=True, description="Segments in the playbook.")
    rounds: int | None = Field(default=None, ge=0, le=100_000, strict=True)


class SitePlaybookInput(Payload):
    """What the host sends to ``site.playbook.put``: structure only, no values."""

    name: str = Field(pattern=SITE_PLAYBOOK_NAME_PATTERN, description="Unique per origin.")
    goal: str = Field(min_length=1, max_length=100, description="What the task achieves.")
    intents: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Words that match a prompt to this playbook, each up to 60 characters.",
    )
    start: str | None = Field(
        default=None, max_length=200, description="Where the task begins: a path or a page type."
    )
    skills: list[str] = Field(
        default_factory=list, max_length=8, description="Skills this playbook covers."
    )
    slots: list[SitePlaybookSlot] = Field(default_factory=list, max_length=8)
    segments: list[SitePlaybookSegment] = Field(default_factory=list, max_length=8)
    pitfalls: list[SitePlaybookPitfall] = Field(default_factory=list, max_length=12)
    history: list[SitePlaybookRun] = Field(
        default_factory=list,
        max_length=10,
        description="When not empty it replaces the stored rows; when empty they stay.",
    )
    state: SitePlaybookState = Field(default="candidate")
    verifiedAt: str | None = Field(default=None, description="YYYY-MM-DD.")
    alwaysAsk: bool | None = Field(
        default=None, description="Left out, the stored value stays (the user may have set it)."
    )
    acked: bool | None = Field(
        default=None, description="The user acknowledged it. Left out, the stored value stays."
    )
    mac: str | None = Field(
        default=None, max_length=200, description="The host's MAC over the playbook; opaque."
    )

    @field_validator("intents", "skills")
    @classmethod
    def _short_items(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip() or len(item) > 64:
                raise ValueError("each item is 1 to 64 characters")
        return value


class SitePlaybook(SitePlaybookInput):
    """A stored playbook: the input, plus what core owns."""

    id: str = Field(description="pb_..., assigned by core.")
    origin: str
    version: int = Field(description="Counts up when slots or segments change.")
    prev: SitePlaybookInput | None = Field(
        default=None, description="The version before the last structural change, one deep."
    )
    alwaysAsk: bool = Field(default=False)
    acked: bool = Field(default=False)
    createdAt: str
    updatedAt: str


class SitePlaybookGetParams(Payload):
    origin: str
    name: str | None = Field(default=None, description="Only this playbook.")
    sessionId: str | None = Field(
        default=None,
        description="The session the call is made for; its siteMemory override applies.",
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description="The browser profile (its clientId). Required from a UI client.",
    )


class SitePlaybookGetResult(Payload):
    playbooks: list[SitePlaybook] = Field(default_factory=list)


class SitePlaybookPutParams(Payload):
    origin: str
    playbook: SitePlaybookInput = Field(description="Upserted by (origin, name).")
    sessionId: str | None = Field(default=None)


class SitePlaybookPutResult(Payload):
    playbook: SitePlaybook = Field(description="As stored: id, version and prev are core's.")
    created: bool


class SitePlaybookMarkParams(Payload):
    origin: str
    playbookId: str
    outcome: SitePlaybookResult
    done: int = Field(ge=0, le=64, strict=True, description="Segments done.")
    total: int = Field(ge=0, le=64, strict=True, description="Segments in the playbook.")
    rounds: int | None = Field(default=None, ge=0, le=100_000, strict=True)
    sessionId: str | None = Field(default=None)


class SitePlaybookEditParams(Payload):
    origin: str
    playbookId: str
    op: SitePlaybookEditOp
    name: str | None = Field(default=None, description="rename: the new name.")
    value: bool | None = Field(default=None, description="alwaysAsk: on or off.")
    segment: str | None = Field(default=None, description="removeSegment: the segment's name.")
    index: int | None = Field(default=None, ge=0, description="removePitfall: its index.")
    hostToolsFrom: str | None = Field(
        default=None,
        description="The browser profile (its clientId). Required from a UI client.",
    )


class SitePlaybookResultOne(Payload):
    playbook: SitePlaybook


class SitePlaybookDeleteParams(Payload):
    origin: str
    playbookId: str | None = Field(default=None, description="One playbook; omitted = all.")
    hostToolsFrom: str | None = Field(
        default=None,
        description="The browser profile (its clientId). Required from a UI client.",
    )


class SitePlaybookDeleteResult(Payload):
    deleted: int


class SkillSearchParams(Payload):
    query: str = Field(description="Free-text query over skill names and summaries.")


class SkillInfo(Payload):
    """One skill, agent, command or plugin."""

    name: str = Field(description="Skill name.")
    kind: SkillKind = Field(default="skill", description="What kind of entry this is.")
    summary: str = Field(default="", description="What the skill does.")
    source: str = Field(
        default="builtin",
        description=(
            "Where it came from: builtin, global, project, plugin:<name> for an "
            "installed entry; claude-global, claude-project or claude-plugin for one "
            "read from Claude Code; codex-global, codex-project or codex-plugin "
            "(1.52.0) for one read from Codex; or the marketplace that offered it."
        ),
    )
    format: ItemFormat = Field(default="snowpea", description=_FORMAT_DESCRIPTION)
    title: str = Field(
        default="",
        description="A plugin's display name (Codex interface.displayName, 1.52.0); may be empty.",
    )
    note: str = Field(
        default="",
        description=(
            "What of a plugin does not work here (1.52.0), e.g. 'needs Codex apps' for a "
            "Codex plugin with app connectors (.app.json): its skills load, its apps do not."
        ),
    )
    installed: bool = Field(default=False, description="True when present locally.")
    id: str = Field(default="", description="Registry id; empty for local entries.")
    installSpec: str = Field(
        default="", description="What to pass to skill.install to get this entry."
    )
    rating: float = Field(
        default=0.0, description="Average rating, when the source publishes one; 0 means unrated."
    )
    downloads: int = Field(
        default=0, description="Download count, when the source publishes one; 0 means unknown."
    )
    enabled: bool = Field(
        default=True,
        description=(
            "False for an installed skill or command settings.skills.disabled names "
            "(1.34.0): kept out of the prompt index, the slash commands and "
            "auto-injection, but still readable and removable."
        ),
    )


class SkillSourceNotIncluded(Payload):
    label: str = Field(description="Human name of the hub, e.g. 'Hermes Hub'.")
    reason: str = Field(default="", description="Why it is switched off on the registry.")


class SkillSearchResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Matching skills.")
    unavailable: list[str] = Field(
        default_factory=list,
        description=(
            "Sources that could not be reached, as '<source>: <reason>'. "
            "Empty skills with a non-empty list means offline, not no match."
        ),
    )
    notIncluded: list[SkillSourceNotIncluded] = Field(
        default_factory=list,
        description=(
            "Hubs the registry has deliberately switched off (not failures). "
            "Clients should mention them quietly, never as an outage."
        ),
    )


class SkillInstallParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    source: str = Field(description="Path, URL or registry name to install from.")
    link: bool = Field(
        default=False,
        description=(
            "Local plugin directory only: install it as a symlink instead of a copy, so "
            "edits are live after skill.reload (development). skill.remove drops the "
            "link and leaves the directory alone."
        ),
    )


class SkillListParams(Payload):
    """``skill.list`` — optionally for one project (1.33.0)."""

    workdir: str | None = Field(
        default=None,
        description=(
            "List this project's skills, agents and commands beside the global, built-in "
            "and plugin ones (1.33.0). Omitted, the project rows are those of every "
            "project the daemon has loaded."
        ),
    )


class SkillListResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Installed skills.")


class SkillRemoveParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str = Field(
        description=(
            "Entry to delete: a skill, agent or command name, or a plugin's directory or "
            "manifest name as skill.list shows it (1.33.0)."
        )
    )
    scope: SkillRemoveScope | None = Field(
        default=None,
        description=(
            "Look only here (1.33.0): 'plugin' ($SNOWPEA_HOME/plugins), 'global' "
            "($SNOWPEA_HOME skills/, agents/, commands/) or 'project' (the workdir's "
            ".snowpea, then .claude). Omitted, the copy in effect goes: the project (with "
            "a workdir), then global, then plugins, so a skill wins over a plugin of the "
            "same name. Within a scope a skill, then an agent, then a command."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute project directory; needed for scope 'project', and with no scope it "
            "puts the project's copies first (1.33.0)."
        ),
    )
    kind: SkillKind | None = Field(
        default=None,
        description=(
            "Only this kind of entry (1.33.0): a skill directory, an agent or command "
            ".md file, or a plugin."
        ),
    )


class SkillCreateParams(Payload):
    """``skill.create`` — write a new ``SKILL.md``, generated or supplied verbatim."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str = Field(description="Skill name; also its directory and the future /<name>.")
    description: str | None = Field(
        default=None,
        description=(
            "Natural-language brief. Used only when 'content' is omitted: the daemon "
            "starts the same generating turn '/skill create' runs and answers with a "
            "turnId rather than waiting for it."
        ),
    )
    content: str | None = Field(
        default=None,
        description=(
            "A complete SKILL.md body. When given, it is validated and written "
            "directly — no model turn runs."
        ),
    )
    scope: SkillScope = Field(default="project", description="Where to write the skill.")
    workdir: str = Field(
        description="Project directory the skill is written under (or read/create a session from)."
    )
    force: bool = Field(
        default=False,
        description=(
            "Overwrite an existing SKILL.md at the target; without it an existing one "
            "answers skill_exists (1.33.0), in draft mode before any turn starts."
        ),
    )
    sessionId: str | None = Field(
        default=None,
        description=(
            "Draft mode only ('content' omitted): run the generating turn on this "
            "existing session instead of creating a headless one. Must be rooted at "
            "'workdir'; invalid_params otherwise. Ignored when 'content' is given."
        ),
    )


class SkillCreateResult(Payload):
    name: str | None = Field(default=None, description="Skill name, once known.")
    path: str | None = Field(default=None, description="Where the SKILL.md was written.")
    turnId: str | None = Field(
        default=None,
        description="Set instead of name/path when generation was started as a turn.",
    )
    sessionId: str | None = Field(
        default=None,
        description=(
            "Set alongside turnId: the session the turn ran on — the caller's own "
            "'sessionId', or a new headless one created for 'workdir'."
        ),
    )


class SkillLearnParams(Payload):
    """``skill.learn`` — what ``/skill learn`` does, for a UI client (1.28.0)."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    force: bool = Field(
        default=False,
        description="Overwrite a SKILL.md already at the target; otherwise that is refused.",
    )

    sessionId: str = Field(description="The session whose history becomes the skill.")
    name: str | None = Field(
        default=None, description="Skill name; omit to let the model name it."
    )
    scope: SkillScope = Field(
        default="project",
        description=(
            '"global" writes it to the user\'s skills under the snowpea home, so every '
            'later session loads it; "project" under the session workdir\'s .snowpea/skills.'
        ),
    )


class SkillLearnResult(Payload):
    name: str = Field(description="The skill's name, also its /<name> command.")
    path: str = Field(description="Where the SKILL.md was written.")


class SkillReadParams(Payload):
    name: str = Field(description="Skill to read.")
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute project directory to resolve a project-scoped skill in; optional "
            "since 1.33.0, an empty string means none (never the daemon's directory) and "
            "a relative one is invalid_params."
        ),
    )
    scope: SkillReadScope | None = Field(
        default=None,
        description=(
            "Look only in this scope (1.33.0); 'project' needs workdir. Omitted: the "
            "project (with a workdir), then global, then any other loaded skill."
        ),
    )
    plugin: str | None = Field(
        default=None,
        description=(
            "Read this plugin's copy, by its manifest or directory name, when several "
            "plugins ship the same skill (1.33.0). Implies scope 'plugin'; any other "
            "scope with it is invalid_params."
        ),
    )
    kind: SkillEntryKind | None = Field(
        default=None,
        description=(
            "Read an agent or a command instead of a skill (1.36.0): the file is "
            "agents/<name>.md or commands/<name>.md in the same places a skill's "
            "directory would be. Omitted or 'skill', the SKILL.md as before."
        ),
    )


class SkillReadResult(Payload):
    path: str = Field(description="Where the SKILL.md was found.")
    content: str = Field(description="Its full text.")
    scope: SkillReadScope = Field(
        description=(
            "'project' or 'global', or (1.33.0) 'builtin' for a skill that ships with "
            "the engine and 'plugin' for one a plugin brings."
        )
    )
    readOnly: bool = Field(
        default=False,
        description=(
            "True when skill.write cannot save this file (1.33.0): a built-in or plugin "
            "skill, or one under Claude Code's or (1.52.0) Codex's own directories."
        ),
    )
    format: ItemFormat = Field(default="snowpea", description=_FORMAT_DESCRIPTION)


class SkillWriteParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str = Field(description="Skill to write.")
    content: str = Field(
        description=(
            "Full SKILL.md text to save. From a remote web login (1.55.0, also skill.create "
            "and skill.learn): only a global skill; the project scope is 'protected' (a "
            "control file, details.reason 'local_only'), a global agent or command, a name a "
            "built-in uses, and front matter with allowed-tools, autoInject, permission(s), "
            "hooks, model or tools are jarvis_person_only (details {reason: 'local_only', "
            "what: 'agent'|'command'|'builtin_name'|'front_matter', keys?})."
        )
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Project directory, when scope is 'project'; optional for 'global' (1.33.0), "
            "where it decides which project's skills may shadow the written copy (active). "
            "An empty string is no directory, never the daemon's; a relative one is refused."
        ),
    )
    scope: SkillScope = Field(default="project", description="Where to write the skill.")
    kind: SkillEntryKind | None = Field(
        default=None,
        description=(
            "Write an agent or a command instead of a skill (1.36.0): the file is "
            "agents/<name>.md or commands/<name>.md in the same places a skill's "
            "directory would be. Omitted or 'skill', the SKILL.md as before."
        ),
    )


class SkillWriteResult(Payload):
    ok: bool = Field(default=True, description="True when the file was written.")
    path: str = Field(description="Where the SKILL.md was written (1.33.0).")
    active: bool = Field(
        description=(
            "Whether that copy is the one in effect (1.33.0): false when a project skill "
            "or a Claude Code global skill of the same name shadows it."
        )
    )


# --------------------------------------------------------------------------
# settings.* / setup.*
# --------------------------------------------------------------------------


class SettingsGetParams(Payload):
    scope: SettingsScope = Field(
        default="global",
        description=(
            '"global" reads $SNOWPEA_HOME/settings.json; '
            '"project" reads <workdir>/.snowpea/settings.json.'
        ),
    )
    workdir: str | None = Field(
        default=None, description='Project root; required when scope is "project".'
    )


class SettingsResult(Payload):
    settings: dict[str, Any] = Field(
        description=(
            "The effective settings document. Fields named api_key, token, "
            "refresh_token or password are masked as '***'."
        )
    )


class SettingsSetParams(Payload):
    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    scope: SettingsScope = Field(
        default="global",
        description=(
            '"global" writes $SNOWPEA_HOME/settings.json; '
            '"project" writes <workdir>/.snowpea/settings.json.'
        ),
    )
    patch: dict[str, Any] = Field(description="Fields to deep-merge into the existing settings.")
    workdir: str | None = Field(
        default=None,
        description=(
            'Project root; required when scope is "project". snowpea\'s own home, a folder '
            "inside it, and a folder whose .snowpea directory is the home (with the default "
            "home: the user's home directory) are not projects (1.55.0): invalid_params with "
            "details {reason: 'snowpea_home', field: 'workdir', what: 'home'|'parent'}, for "
            "every caller and every method that writes a project's .snowpea files "
            "(settings.set, permission.allowlist.add, skill.create|write|learn, "
            "team.guide.set|delete, project.trust.set); session.create, job.schedule, "
            "external.create and a project-scope mcp.add|update|remove refuse the home and "
            "folders inside it. A remote web login writes only defaultMode, agents.* and "
            "models.* in the project scope."
        ),
    )


class SettingsReloadResult(Payload):
    """What ``system.reloadSettings`` did (CORE-settings-reload)."""

    reloaded: bool = Field(
        description=(
            "True when settings.json differed from what the daemon held and "
            "the in-memory state was rebound; false when it was already current."
        )
    )
    changedKeys: list[str] = Field(
        default_factory=list,
        description="Top-level settings sections that changed, e.g. ['providers'].",
    )
    waitingKeys: list[str] = Field(
        default_factory=list,
        description=(
            "1.55.0 (security reviews 7 and 8): settings paths that can run programs or "
            "widen access ('security', 'jarvis', 'web.remote', 'allowlist', "
            "'mcp.permissions', and every program, endpoint or daemon leaf of the settings "
            "table as a dotted path, e.g. 'mcp.servers.foo.command', "
            "'providers.openai.base_url', 'audio.tts.command') whose value in settings.json "
            "on disk differs from what the daemon holds. A change that arrives by file edit "
            "while the daemon runs is NOT applied: it waits for a local person "
            "(settings.acceptDiskChange)."
        ),
    )


class SettingsAcceptDiskChangeParams(Payload):
    """``settings.acceptDiskChange`` (1.55.0, security review 7)."""

    accept: bool = Field(
        default=True,
        description=(
            "true: apply what settings.json on disk says for the waiting sections; false: "
            "keep what the daemon holds and write it back to settings.json."
        ),
    )


class SettingsAcceptDiskChangeResult(Payload):
    """What ``settings.acceptDiskChange`` did."""

    applied: list[str] = Field(
        default_factory=list, description="Waiting sections now taken from disk."
    )
    discarded: list[str] = Field(
        default_factory=list,
        description="Waiting sections whose disk value was overwritten with the daemon's.",
    )


class SetupCatalogItem(Payload):
    """One selectable row on a setup wizard screen (``setup/catalog.py::CatalogItem``)."""

    id: str = Field(description="Stable id, e.g. a vendor or provider name.")
    label: str = Field(description="Display label.")
    tier: str = Field(description='"free", "paid" or "subscription".')
    key: str = Field(description='"no key", "key optional", "key required" or "self-hosted".')
    default: bool = Field(default=False, description="Whether this is the screen's default pick.")
    description: str = Field(default="", description="One-line description.")
    active: bool = Field(default=True, description="False for items listed but not usable yet.")
    tags: list[str] = Field(
        default_factory=list, description="Display tags, e.g. ('free · no key', 'active')."
    )
    installable: bool = Field(
        default=False,
        description=(
            "True when audio.install can obtain this row without root, so a surface "
            "may draw an Install button next to it. Only the voice rows set it."
        ),
    )
    installHint: str | None = Field(
        default=None,
        description=(
            "A command the user runs themselves, e.g. 'sudo apt install espeak-ng'. "
            "Set for a system package the daemon will not install, and as a fallback "
            "beside an Install button."
        ),
    )
    recommended: bool = Field(
        default=False,
        description=(
            "True for the one row a screen should lead with and pre-select when "
            "nothing is configured yet. At most one row per list sets it."
        ),
    )
    defaultModel: str = Field(
        default="",
        description=(
            "Model this vendor row would start a session with, resolved from the "
            "account's own catalog or models.dev rather than read off the build's "
            "preset. Only the vendor list sets it; empty elsewhere and on a "
            "self-hosted server, whose model is whatever was loaded into it."
        ),
    )
    defaultModelSource: str = Field(
        default="",
        description=(
            "Where defaultModel came from: 'from your account', 'from models.dev' "
            "or 'built-in default'. Shown beside the id so a fallback never passes "
            "for the account's own answer."
        ),
    )


class SetupItem(Payload):
    id: str = Field(description="Stable item id, e.g. 'provider', 'search', 'memory'.")
    title: str = Field(description="What the item is, for the wizard.")
    done: bool = Field(description="True when the item is satisfied.")
    defaultApplied: bool | None = Field(
        default=None, description="For optional items: true when the value is the default."
    )


class SetupStatusParams(Payload):
    profile: Literal["default", "browser"] = Field(
        default="default", description="Which setup flow is asking (1.6.0)."
    )


class SetupStatusResult(Payload):
    required: list[SetupItem] = Field(
        default_factory=list, description="Items that must be done before first use."
    )
    optional: list[SetupItem] = Field(
        default_factory=list, description="Items with a usable default."
    )
    existingInstall: bool = Field(
        default=False,
        description="True when this home already has a configured provider from an earlier setup.",
    )
    configuredProviders: list[str] = Field(
        default_factory=list, description="Vendors with credentials or a local endpoint."
    )


class SetupApplyDefaultsParams(Payload):
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    profile: Literal["default", "browser"] = Field(
        default="browser", description="Which defaults to apply."
    )


class SetupApplyDefaultsResult(Payload):
    applied: list[str] = Field(
        default_factory=list,
        description="Setting keys this call filled in because they were unset (absent or null).",
    )
    skipped: list[str] = Field(
        default_factory=list,
        description="Profile keys left alone because settings.json already holds a value.",
    )
    status: SetupStatusResult = Field(description="setup.status after applying.")


class ProviderTestParams(Payload):
    provider: str = Field(description="Vendor to test.")
    model: str | None = Field(default=None, description="Model; defaults to the vendor's default.")


class ProviderTestResult(Payload):
    ok: bool = Field(description="True when a one-line completion came back.")
    provider: str = Field(description="Vendor tested.")
    model: str = Field(default="", description="Model the request used.")
    modelEcho: str | None = Field(
        default=None, description="Model name the server reported, when it did."
    )
    latencyMs: int = Field(default=0, description="Round-trip time of the test call.")
    reply: str | None = Field(default=None, description="The model's reply, trimmed.")
    error: str | None = Field(default=None, description="Why it failed.")


class SetupCatalogResult(Payload):
    """The five Hermes-style setup screens, as data (M3 contract §5)."""

    vendors: list[SetupCatalogItem] = Field(default_factory=list, description="LLM vendors.")
    search: list[SetupCatalogItem] = Field(
        default_factory=list, description="Web-search providers, ddgs first."
    )
    browser: list[SetupCatalogItem] = Field(
        default_factory=list, description="Browser-control providers."
    )
    tools: list[SetupCatalogItem] = Field(
        default_factory=list, description="Tool categories and their default on/off state."
    )
    gateway: list[SetupCatalogItem] = Field(
        default_factory=list, description="Chat gateways (telegram, discord, slack), all off."
    )
    stt: list[SetupCatalogItem] = Field(
        default_factory=list,
        description="Speech-to-text choices; active reflects what is usable on this machine.",
    )
    tts: list[SetupCatalogItem] = Field(
        default_factory=list,
        description="Text-to-speech choices; active reflects what is usable on this machine.",
    )


# --------------------------------------------------------------------------
# session.event kinds
# --------------------------------------------------------------------------


class UserAttachment(Payload):
    """A file that came with a prompt, as the transcript names it."""

    kind: Literal["file", "image", "text", "page"] = Field(
        default="file", description="Attachment flavour."
    )
    name: str = Field(default="", description="Display name shown under the prompt.")
    url: str | None = Field(default=None, description="Page address, for 'page' (1.7.0).")
    title: str | None = Field(default=None, description="Page title, for 'page' (1.7.0).")


class PromptExpansion(Payload):
    """What a slash command put in front of the model in place of the typed line."""

    kind: Literal["skill", "command"] = Field(
        default="skill",
        description="'skill' for a skill command, 'command' for a built-in such as /init.",
    )
    name: str = Field(description="The skill or command that ran, e.g. `ralph` or `init`.")
    text: str = Field(description="The full instruction the model received.")


class MessageUser(Payload):
    """The prompt that opened a turn, at the moment it joined the history.

    The history has always held it, but ``session.resume`` replays the event
    log — so without this event a resumed transcript shows the answers and none
    of the questions.  Chat gateways forward ``message.done`` and deliberately
    ignore this kind: the person in the chat wrote the prompt themselves.
    """

    kind: Literal["message.user"] = "message.user"
    text: str = Field(description="Prompt text as the user typed it.")
    attachments: list[UserAttachment] = Field(
        default_factory=list, description="Files sent along with the prompt."
    )
    refs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Resolved @ references (path, kind, lines, truncated).",
    )
    steered: bool = Field(
        default=False,
        description=(
            "True when this prompt was queued behind a running turn and then "
            "folded into that same turn before its next model call."
        ),
    )
    expansion: PromptExpansion | None = Field(
        default=None,
        description=(
            "Set when `text` is a skill command: the skill body the model was "
            "given instead. Surfaces show it folded, like a tool call, not inline."
        ),
    )
    origin: MessageOrigin | None = Field(
        default=None,
        description=(
            "Whose words these are (1.53.0, capability 'messageOrigin'). Only kind 'human' "
            "moves lastPrompt/lastUserAt and pauses Jarvis in a managed session. Null on "
            "events stored before 1.53.0; read those as human. A Jarvis wake (the turn's "
            "turn.started reason 'jarvis-intake') is kind 'system', surface null: core's "
            "text, verbatim with its 'untrusted' fences, never the person's."
        ),
    )


class MessageDelta(Payload):
    """Streaming assistant text."""

    kind: Literal["message.delta"] = "message.delta"
    text: str = Field(description="Text fragment to append to the current message.")


class MessageReasoning(Payload):
    """Hidden reasoning the model streamed before it wrote anything.

    The text never joins the transcript or the history — it exists so a
    surface can say the model is thinking, and how much of the output budget
    the thinking has already taken (CORE-reasoning-budget).
    """

    kind: Literal["message.reasoning"] = "message.reasoning"
    text: str = Field("", description="Reasoning fragment; not part of the answer.")
    chars: int = Field(0, description="Characters of reasoning so far in this turn.")


class MessageDone(Payload):
    """A completed message."""

    kind: Literal["message.done"] = "message.done"
    text: str = Field(description="Full message text.")
    role: Literal["assistant", "user", "system"] = Field(
        "assistant", description="Who produced the message."
    )
    messageKind: Literal["", "interrupted", "plan"] = Field(
        default="",
        serialization_alias="kind",
        validation_alias="messageKind",
        description=(
            "Optional message classification inside the message.done payload, "
            "for example 'interrupted' for the post-stop system note, or 'plan' "
            "(1.32.0) for the plan set_mode puts up for review just before its "
            "question; it arrives while that set_mode call is still running."
        ),
    )
    truncated: bool = Field(
        False, description="The answer still hit the output limit and is incomplete."
    )
    continuations: int = Field(
        0, description="How many times the turn was resumed after hitting the output limit."
    )
    reasoningFallback: bool | None = Field(
        default=None,
        description=(
            "1.70.0 (capability reasoningOnlyFallback): true when the model wrote its "
            "answer only in its reasoning, also after being asked again, and this text is "
            "that reasoning shown under a marker line instead of an empty_response error. "
            "Absent otherwise."
        ),
    )


class ToolCallEvent(Payload):
    """The model asked to run a tool."""

    kind: Literal["tool.call"] = "tool.call"
    callId: str = Field(description="Id pairing this call with its tool.result.")
    name: str = Field(description="Tool being called.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments supplied.")


class ToolResultEvent(Payload):
    """A tool finished."""

    kind: Literal["tool.result"] = "tool.result"
    callId: str = Field(description="Id of the matching tool.call.")
    name: str = Field(description="Tool that ran.")
    ok: bool = Field(description="False when the tool failed.")
    output: str = Field(default="", description="Output handed back to the model.")
    error: str | None = Field(default=None, description="Failure detail when ok is false.")
    content: list[ToolContentBlock] | None = Field(
        default=None,
        description=(
            "A host tool's content blocks: text inline, images as contentRef "
            "(session.toolContent). Absent for sensitive results (1.7.0)."
        ),
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Host-tool facts for surfaces (e.g. pages for page preview cards); not shown "
            "to the model. Forwarded as the host gave it, also for sensitive results; "
            "dropped above 16 KB (addendum 13)."
        ),
    )


class ToolProgress(Payload):
    """Output a still-running tool has produced so far (IDE-PROGRESS D2).

    Advisory and additive: the chunks let a surface tail a long command, but
    ``tool.result`` remains the authoritative record of what the tool
    returned.  A surface that ignores this kind loses nothing but the tail.
    Chunks are coalesced (at most ~4 KB each, flushed a few times a second)
    and the stream is capped; the cap is announced by one final progress with
    ``truncated`` set and no ``chunk``.
    """

    kind: Literal["tool.progress"] = "tool.progress"
    callId: str = Field(description="Id of the tool.call this output belongs to.")
    name: str = Field(description="Tool that is running.")
    stream: Literal["stdout", "stderr"] = Field(
        default="stdout", description="Which stream the chunk came from."
    )
    chunk: str = Field(default="", description="Raw output fragment, in order.")
    seq: int = Field(
        default=0, description="0-based index of this progress within the call."
    )
    truncated: bool = Field(
        default=False,
        description="True on the final progress when the byte cap stopped the tail.",
    )
    state: Literal["running", "waiting"] | None = Field(
        default=None,
        description=(
            "1.55.0, host tools: set when the host reported a state with tool.progress "
            "(chunk is then usually empty): 'waiting' shows the card as waiting (with "
            "note), 'running' clears it. Absent on ordinary output chunks."
        ),
    )
    note: str | None = Field(
        default=None,
        description="1.55.0: the host's line for the state, e.g. 'waiting for the page'.",
    )


class TodoItem(Payload):
    id: str = Field(description="Stable id within the list.")
    content: str = Field(description="What the step is.")
    status: Literal["pending", "in_progress", "completed"] = Field(description="Progress.")


class TodosUpdated(Payload):
    """The agent changed its task list (``write_todos``, 1.7.0)."""

    kind: Literal["todos.updated"] = "todos.updated"
    todos: list[TodoItem] = Field(default_factory=list, description="The whole list, in order.")


class CommandProgressStory(Payload):
    id: str = Field(description="Story (or subtask) id, e.g. S1.")
    title: str = Field(description="What the story delivers.")
    status: Literal["pass", "fail", "pending"] = Field(description="Where the story stands.")
    note: str = Field(default="", description="Why it passed or failed; empty while pending.")


class CommandProgress(Payload):
    """A long-running workflow command reports its plan and progress (1.7.0).

    Sent by ``/ralph`` and ``/ultrawork`` once the plan is built (iteration 0),
    after each iteration, and once more with ``outcome`` when the command ends.
    The plain-text progress messages are still sent; this is the same state in
    a shape a surface can render without parsing them.
    """

    kind: Literal["command.progress"] = "command.progress"
    command: Literal["ralph", "ultrawork"] = Field(description="Command reporting.")
    iteration: int = Field(default=0, description="0 for the plan, then 1, 2, ….")
    stories: list[CommandProgressStory] = Field(
        default_factory=list, description="Every story, in plan order."
    )
    outcome: str | None = Field(
        default=None,
        description=(
            "Set on the last event only: ralph 'complete', 'rejected', 'stopped', "
            "'max_iterations' or 'error'; ultrawork 'complete' or 'partial'."
        ),
    )


class CommandResultEvent(Payload):
    """An instant command ran beside a running turn (1.55.0, ``commandsBesideTurn``).

    Sent instead of ``turn.started`` / ``message.done`` / ``turn.done`` when a
    command of kind ``instant`` (``commands/kinds.py``: reads and safe session
    settings such as /model, /effort, /mode, /help) arrives while a turn runs.
    The running turn keeps its id, its ``session.list`` status and its
    interrupt; nothing is added to the conversation history.  Other events the
    command causes (``mode.changed``, ``model.changed`` …) still go out.  With
    no turn running the command is a turn of its own, as before.
    """

    kind: Literal["command.result"] = "command.result"
    commandId: str = Field(
        description="Id of this command run (what session.prompt / command.run returned)."
    )
    command: str = Field(description="The line as typed, e.g. '/model gpt-5'.")
    ok: bool = Field(description="False when the command failed or was unknown.")
    text: str = Field(default="", description="What the command said (its replies, joined).")
    appliesFrom: Literal["now", "next_tool_call", "next_request", "next_turn"] | None = Field(
        default=None,
        description=(
            "For a setting change: when the running turn sees it (next_tool_call for the "
            "mode, rules and vision model; next_request for MCP servers; next_turn for the "
            "model, effort, busy handling and delegation). Null for a read."
        ),
    )
    runningTurnId: str | None = Field(
        default=None, description="The turn that was running and still is."
    )


class PlanUpdated(Payload):
    """The project's current plan was saved or one of its steps changed (1.8.0).

    The plan lives at ``<workdir>/.snowpea/plans/current.md`` (state beside it
    in ``current.json``); ``/ralph``, ``/team`` and ``/ultrawork`` run it when
    asked to just carry on, and mark its steps as their work passes.
    """

    kind: Literal["plan.updated"] = "plan.updated"
    title: str = Field(description="The plan's title.")
    status: Literal["active", "done", "archived"] = Field(description="Where the plan stands.")
    done: int = Field(default=0, description="Steps marked done.")
    total: int = Field(default=0, description="Steps in the plan.")
    next: str | None = Field(
        default=None, description="The next pending step as 'S3 <title>'; null when none is left."
    )
    path: str = Field(description="The plan file, relative to the workdir.")


class DiffEvent(Payload):
    """A file was edited."""

    kind: Literal["diff"] = "diff"
    path: str = Field(description="File that changed.")
    patch: str = Field(description="Unified diff of the change.")
    callId: str | None = Field(
        default=None, description="The tool call that made the change (1.29.0)."
    )


class CheckpointUpdated(Payload):
    """A turn checkpoint was created or updated."""

    kind: Literal["checkpoint.updated"] = "checkpoint.updated"
    checkpoint: CheckpointInfo = Field(description="The whole checkpoint manifest so far.")


class CheckpointRestored(Payload):
    """A restore checkpoint operation finished."""

    kind: Literal["checkpoint.restored"] = "checkpoint.restored"
    checkpointId: str | None = Field(
        default=None, description="Undo checkpoint for the restore, null when none was written."
    )
    restored: list[str] = Field(default_factory=list, description="Paths restored.")
    skipped: list[CheckpointRestoreSkipped] = Field(
        default_factory=list, description="Paths skipped and their reasons."
    )


#: Lifecycle of one delegated subagent run (M7 contract §3).
SubagentStatus = Literal["queued", "running", "done", "error", "interrupted"]


class SubagentSpawn(Payload):
    """A subagent started."""

    kind: Literal["subagent.spawn"] = "subagent.spawn"
    agentId: str = Field(description="Id correlating this subagent's events.")
    name: str = Field(default="", description="Named agent that was spawned.")
    task: str = Field(default="", description="Task it was given.")
    title: str = Field(
        default="",
        description=(
            "One-line label for the delegation, written by the delegating model in "
            "the user's language; empty when it wrote none."
        ),
    )
    status: SubagentStatus = Field(
        default="queued", description="State at spawn: queued until a concurrency slot frees up."
    )
    sessionId: str | None = Field(
        default=None, description="The subagent's own session, once it has one."
    )


class SubagentUsage(Payload):
    """Tokens one subagent consumed."""

    inputTokens: int = Field(default=0, description="Prompt tokens the subagent used.")
    outputTokens: int = Field(default=0, description="Completion tokens the subagent used.")


class SubagentUpdate(Payload):
    """Progress from a running subagent."""

    kind: Literal["subagent.update"] = "subagent.update"
    agentId: str = Field(description="Subagent reporting progress.")
    title: str = Field(
        default="",
        description=(
            "One-line label for the delegation, written by the delegating model in "
            "the user's language; empty when it wrote none."
        ),
    )
    status: SubagentStatus = Field(default="running", description="Lifecycle state.")
    text: str = Field(default="", description="Human-readable progress text.")
    lastText: str = Field(default="", description="Most recent text the subagent produced.")
    name: str = Field(default="", description="Named agent that is running, when there is one.")
    sessionId: str | None = Field(default=None, description="The subagent's own session.")
    usage: SubagentUsage | None = Field(
        default=None,
        description=(
            "Tokens the subagent has used so far, cumulative; sent with every update "
            "so a surface can show a long delegation is still moving."
        ),
    )



class SubagentDone(Payload):
    """A subagent finished."""

    kind: Literal["subagent.done"] = "subagent.done"
    agentId: str = Field(description="Subagent that finished.")
    ok: bool = Field(default=True, description="False when it failed.")
    title: str = Field(
        default="",
        description=(
            "One-line label for the delegation, written by the delegating model in "
            "the user's language; empty when it wrote none."
        ),
    )
    result: str = Field(default="", description="Final report.")
    status: SubagentStatus = Field(
        default="done", description="Terminal state: done, error or interrupted."
    )
    summary: str = Field(default="", description="The subagent's final answer.")
    usage: SubagentUsage = Field(
        default_factory=SubagentUsage, description="Tokens the subagent consumed."
    )
    name: str = Field(default="", description="Named agent that ran, when there was one.")
    sessionId: str | None = Field(default=None, description="The subagent's own session.")
    reason: str = Field(
        default="complete",
        description=(
            "Why it ended: complete, budget, timeout, error, interrupted or denied."
        ),
    )
    rounds: int = Field(default=0, description="Tool rounds used by the subagent.")
    budget: int = Field(default=0, description="Tool-round budget cap.")
    deniedCalls: int = Field(
        default=0, description="How many child tool calls were denied in this run."
    )
    deniedTools: list[str] = Field(
        default_factory=list, description="Unique names of tools whose calls were denied."
    )


class TeamTaskUpdate(Payload):
    """A team task changed state."""

    kind: Literal["team.task.update"] = "team.task.update"
    teamId: str = Field(description="Team the task belongs to.")
    taskId: str = Field(description="Task that changed.")
    status: TeamTaskState = Field(default="queued", description="New state.")
    assignee: str | None = Field(default=None, description="Worker that owns the task.")
    agentN: int | None = Field(
        default=None, description="1-based worker index that owns the task."
    )
    retries: int = Field(default=0, description="How many times a merge conflict re-queued it.")


#: Who kept a turn going (``hook.continue.kind``, 1.51.0).
HookContinueKind = Literal[
    "unfinished_work",
    "verify_gate",
    "stop_hook",
    "provider_retry",
    "overflow",
    "handback",
    "budget",
    "goal",
    "other",
]


class HookContinue(Payload):
    """Something kept the turn going instead of letting it end."""

    kind: Literal["hook.continue"] = "hook.continue"
    reason: str = Field(
        description=(
            "What it said, in English (the instruction or the notice); clients that localise "
            "build their own line from 'kind', 'count' and 'limit' (1.51.0)."
        )
    )
    count: int | None = Field(
        default=None,
        description=(
            "The producer's own count so far this turn: Stop-hook continuations (stop_hook), "
            "unfinished-work continuations, verify-gate nudges, the retry attempt "
            "(provider_retry), budget continuations (budget), empty-answer nudges (other). "
            "Before 1.51.0 always the Stop-hook count (0 for other producers)."
        ),
    )
    hookKind: HookContinueKind = Field(
        default="other",
        serialization_alias="kind",
        validation_alias="hookKind",
        description=(
            "Who kept the turn going, inside the payload as 'kind' (1.51.0; the way "
            "message.done carries its 'kind'): unfinished_work (the answer listed work it "
            "could still do), verify_gate (no fresh check after edits, open to-dos, a "
            "deliverable in tmp/), stop_hook (a Stop hook), provider_retry (a failed model "
            "call is tried again), overflow (the context overflowed and is summarised), "
            "handback (a delegated child handed back), budget (the round budget's probe or "
            "automatic continuation), goal (a goal round), other (anything else: an empty "
            "answer nudged, a plugin's turn.stopping listener)."
        ),
    )
    limit: int | None = Field(
        default=None,
        description=(
            "The cap 'count' runs up to, when there is one (1.51.0): unfinished-work "
            "continuations (agent.unfinished_continue), verify-gate nudges, Stop-hook "
            "continuations, agent.provider_retries (null for a retry of an unreachable "
            "server, whose limit is time), agent.auto_budget_continuations, 1 for overflow."
        ),
    )


class ProviderWaiting(Payload):
    """The model server has sent nothing for a while (1.37.0)."""

    kind: Literal["provider.waiting"] = "provider.waiting"
    sinceSec: int = Field(description="Seconds of silence so far.")
    phase: Literal["first_token", "idle"] = Field(
        description="'first_token' before the call's first event, 'idle' between events."
    )


class SessionForked(Payload):
    """This session was forked from another; everything before it is inherited (1.46.0)."""

    kind: Literal["session.forked"] = "session.forked"
    fromSessionId: str = Field(description="Session the history came from.")
    atTurnId: str | None = Field(default=None, description="Last inherited turn.")
    historyLen: int = Field(default=0, description="Messages inherited.")


class StepStarted(Payload):
    """A model call of the turn begins (1.44.0, capability 'steps')."""

    kind: Literal["step.started"] = "step.started"
    turnId: str | None = Field(default=None, description="Turn the call belongs to.")
    step: int = Field(description="Model call number within the turn, from 1.")
    provider: str | None = Field(default=None, description="Vendor called.")
    model: str | None = Field(default=None, description="Model called.")


class StepDone(Payload):
    """A model call of the turn ended, with its timing and usage (1.44.0)."""

    kind: Literal["step.done"] = "step.done"
    turnId: str | None = Field(default=None, description="Turn the call belongs to.")
    step: int = Field(description="Model call number within the turn, from 1.")
    outcome: str = Field(
        description="end_turn, tool_use, max_tokens, interrupted, error, … as the call ended."
    )
    ttftMs: int = Field(default=0, description="Time to the first streamed token, ms.")
    decodeMs: int = Field(default=0, description="Time from the first token to the end, ms.")
    totalMs: int = Field(default=0, description="Whole call, ms.")
    inputTokens: int = Field(default=0, description="Prompt tokens the provider reported.")
    cachedInputTokens: int | None = Field(default=None, description="Of those, served from cache.")
    outputTokens: int = Field(default=0, description="Completion tokens reported.")
    reasoningTokens: int = Field(default=0, description="Thinking tokens (estimated when unsplit).")
    error: str | None = Field(default=None, description="Why the call failed, when it did.")
    errorCode: str | None = Field(
        default=None,
        description=(
            "The failure's code (1.51.0), the one the turn's 'error' event carries: the "
            "provider's ProviderError code (internal, timeout, invalid_params, auth_expired, "
            "image_rejected, model_not_configured, not_found, …), 'internal' for anything "
            "else, 'interrupted' for a call Stop cut short; null for a call that ended "
            "normally."
        ),
    )
    text: str = Field(
        default="",
        description=(
            "Everything the call wrote, settled (1.44.0): the same text its message.delta "
            "events streamed, stored once. An errored or interrupted call carries what "
            "streamed before it stopped (1.50.0)."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "Everything the call thought, settled, as message.reasoning streamed; partial "
            "for an errored or interrupted call (1.50.0)."
        ),
    )
    toolCalls: int = Field(default=0, description="Tool calls the call asked for.")


#: Why a goal is blocked (``blockedReason.code``, a closed set since 1.51.0).
GoalBlockedCode = Literal[
    "round-limit", "needs-input", "no-credentials", "no-access", "external", "other"
]


class GoalBlockedReason(Payload):
    code: GoalBlockedCode = Field(
        description=(
            "Condition code, a closed set (1.51.0): 'round-limit' — the round cap was reached "
            "(set by the core); the rest come from the model's goal_update: 'needs-input' — "
            "a decision, an answer or an approval from the person; 'no-credentials' — a key, "
            "a login or a token is missing or refused; 'no-access' — a permission, a file or "
            "a resource it may not reach; 'external' — a service, a tool or the environment "
            "outside its control is down or missing; 'other' — anything else (a free-form "
            "code the model gave is mapped onto this set, 'other' when nothing fits). "
            "Clients localise by this code."
        )
    )
    message: str = Field(
        default="",
        description=(
            "What is needed to go on, in English as the model (or the core, for round-limit) "
            "wrote it: a fallback to show when a client has no text for 'code'. The core "
            "does not translate it."
        ),
    )


class GoalView(Payload):
    """A session's goal (1.43.0, capability 'goals')."""

    id: str = Field(description="Goal id.")
    revision: int = Field(description="Bumped on every change; goal_update must name it.")
    objective: str = Field(description="What the goal is.")
    phase: Literal["active", "paused", "blocked", "complete"] = Field(
        description="active runs rounds while armed; paused, blocked and complete do not."
    )
    maxRounds: int = Field(description="Round cap; reaching it blocks with 'round-limit'.")
    rounds: int = Field(default=0, description="Rounds started so far.")
    armed: bool = Field(
        default=False,
        description="Rounds start by themselves only while armed; never after a restart or resume.",
    )
    blockedReason: GoalBlockedReason | None = Field(default=None, description="Set when blocked.")
    evidence: str | None = Field(default=None, description="What showed it complete.")
    createdAt: str = Field(description="UTC ISO-8601.")
    updatedAt: str = Field(description="UTC ISO-8601.")


class GoalChanged(Payload):
    """The session's goal changed or was cleared (1.43.0)."""

    kind: Literal["goal.changed"] = "goal.changed"
    goal: GoalView | None = Field(default=None, description="The goal now; null once cleared.")


class VisionUnavailable(Payload):
    """A picture reached a model that cannot see it and nothing described it (1.41.0).

    Capability ``visionNotice``.  At most once per turn per ``reason``.  The
    model itself is told through a short text note in place of the image; this
    event is for the surface, which can offer "set a vision model".
    """

    kind: Literal["vision.unavailable"] = "vision.unavailable"
    reason: Literal["no_vision_model", "describe_failed"] = Field(
        description=(
            "'no_vision_model': the session's model cannot see images and "
            "models.categories.vision is not set (or does not resolve to a model that can). "
            "'describe_failed': the vision slot is set but did not describe the image "
            "(see 'error')."
        )
    )
    source: Literal["attachment", "tool"] = Field(
        description=(
            "'attachment': the user's message; 'tool': a tool result "
            "(view_image, a screenshot)."
        )
    )
    provider: str | None = Field(default=None, description="The session's provider.")
    model: str | None = Field(default=None, description="The session's model that could not see.")
    visionModel: str | None = Field(
        default=None,
        description=(
            "models.categories.vision as configured ('vendor:model' or a "
            "profile id); null if unset."
        ),
    )
    count: int = Field(default=1, description="How many images went unseen in this report.")
    error: str | None = Field(
        default=None, description=(
            "Short text of why the vision slot failed ('describe_failed' "
            "only)."
        )
    )


class ProviderRetry(Payload):
    """A model call failed transiently and will be tried again (1.37.0)."""

    kind: Literal["provider.retry"] = "provider.retry"
    attempt: int = Field(
        description=(
            "The retry about to run, 1-based; for 'unreachable', the retries of the "
            "unreachable server so far in this call (1.51.0; before, the next charged retry)."
        )
    )
    max: int | None = Field(
        default=None,
        description=(
            "Retries allowed (agent.provider_retries); null for an 'unreachable' retry, "
            "whose limit is time (see waitLeftSec), not a count (1.51.0)."
        ),
    )
    delaySec: float = Field(description="Seconds before it runs.")
    error: str = Field(description="Short class of the failure, e.g. ReadTimeout.")
    reason: Literal["unreachable", "silent", "transient"] = Field(
        default="transient",
        description=(
            "Why (1.51.0): 'unreachable' — the call never reached the server (DNS, refused, "
            "unreachable, connect timeout) and is retried until "
            "agent.provider_connect_wait_sec has passed; 'silent' — the server sent no first "
            "token, or nothing between events, for agent.provider_first_token_sec / "
            "provider_idle_sec; 'transient' — anything else worth another try (429, 5xx, a "
            "reset connection, a read timeout)."
        ),
    )
    waitLeftSec: int | None = Field(
        default=None,
        description=(
            "Seconds left of the agent.provider_connect_wait_sec budget for an 'unreachable' "
            "retry; null for the others (1.51.0)."
        ),
    )


class AgentChanged(Payload):
    """The agent a human session runs as changed (addendum 17)."""

    kind: Literal["agent.changed"] = "agent.changed"
    agent: str | None = Field(
        default=None,
        description="Agent definition now in force (prompt, tools, rounds); null for none.",
    )


class ModeChanged(Payload):
    """The session's permission mode changed."""

    kind: Literal["mode.changed"] = "mode.changed"
    mode: Mode = Field(description="Mode now in effect.")
    delegation: bool | None = Field(
        default=None,
        description=(
            "Whether delegation mode is now in force, sent when /delegation changes it; "
            "absent when only the permission mode moved."
        ),
    )
    autoAfterPlan: bool | None = Field(
        default=None,
        description=(
            "Whether plan-then-auto is in force after this change (1.32.0); absent from "
            "an event that only reports delegation."
        ),
    )


class UsageEvent(Payload):
    """Token usage for the turn."""

    kind: Literal["usage"] = "usage"
    inputTokens: int = Field(default=0, description="Prompt tokens consumed.")
    outputTokens: int = Field(default=0, description="Completion tokens produced.")
    provider: str | None = Field(default=None, description="Vendor that served the call (1.7.0).")
    model: str | None = Field(default=None, description="Model that served the call (1.7.0).")
    cachedInputTokens: int | None = Field(
        default=None,
        description=(
            "Of inputTokens, how many the server answered from its prompt cache, when it "
            "reports that (1.20.0)."
        ),
    )
    timings: dict[str, float] | None = Field(
        default=None,
        description=(
            "llama.cpp's own timings when the server sends them (1.25.0): prompt_n, "
            "prompt_ms, cache_n, predicted_n, predicted_ms. A first token much later than "
            "prompt_ms was spent waiting for a slot."
        ),
    )


WindowSource = Literal["settings", "server", "error", "catalog", "learned", "assumed"]
"""Where a context window came from (1.42.0), in the order they are tried."""


class WindowBounds(Payload):
    """What requests taught us about a model's window (1.42.0)."""

    lower: int | None = Field(
        default=None, description="Largest prompt, in tokens, that went through."
    )
    upper: int | None = Field(
        default=None, description="Smallest prompt, in tokens, refused for its length."
    )


class ModelWindow(Payload):
    """A model's context window as ``provider.models`` reports it (1.42.0)."""

    contextWindow: int = Field(description="Window in tokens; never unknown.")
    contextWindowSource: WindowSource = Field(description="Where contextWindow came from.")
    contextWindowBounds: WindowBounds | None = Field(
        default=None, description="Learned bounds; absent until a request taught us any."
    )
    contextWindowScope: Literal["model", "vendor"] | None = Field(
        default=None,
        description=(
            "Which setting won when the source is 'settings': 'model' is "
            "providers.<v>.context_windows[<model>], 'vendor' is providers.<v>.context_window."
        ),
    )


class ContextEvent(Payload):
    """How full the model's context window is (CORE-context).

    Emitted after every turn and after every compaction, so a surface can show
    ``used / window`` without asking.  ``used`` is the size of the prompt that
    was (or would be) sent — system prompt, history and tool results — not a
    running total of the session's tokens.
    """

    kind: Literal["context"] = "context"
    used: int = Field(default=0, description="Tokens the current prompt occupies.")
    window: int | None = Field(
        default=None, description="Context window of the model in tokens; null when unknown."
    )
    percent: float | None = Field(
        default=None, description="used/window as a percentage, null when the window is unknown."
    )
    estimated: bool = Field(
        default=True,
        description="True while 'used' is a local estimate; false once the provider reported it.",
    )
    model: str | None = Field(default=None, description="Model the window belongs to.")
    provider: str | None = Field(default=None, description="Vendor serving that model.")
    contextWindowSource: WindowSource | None = Field(
        default=None,
        description=(
            "Where 'window' came from (1.42.0): settings, server, error (named by a "
            "refusal), catalog, learned (moved by what requests taught us) or assumed "
            "(131072, nothing named one). 'window' itself is never null from 1.42.0."
        ),
    )
    autoCompactAt: int | None = Field(
        default=None,
        description=(
            "1.55.0: the 'used' value at which the next turn compacts first "
            "(context.autoCompactPercent of the window, less the room kept for the "
            "reply); null when auto-compaction is off or the window is unknown. Many "
            "short messages can also trigger it earlier (the history's message cap)."
        ),
    )


#: Why a compaction ran (1.55.0): ``overflow`` = the model server refused the
#: request for its length mid-turn; ``auto`` = the threshold before a turn;
#: ``manual`` = ``session.compact`` or ``/compact``.
CompactionReason = Literal["overflow", "auto", "manual"]


class CompactionEvent(Payload):
    """The conversation was summarised and replaced (CORE-context)."""

    kind: Literal["compaction"] = "compaction"
    before: int = Field(default=0, description="Estimated tokens the history held before.")
    after: int = Field(default=0, description="Estimated tokens the history holds now.")
    summaryChars: int = Field(default=0, description="Length of the summary in characters.")
    auto: bool = Field(
        default=False,
        description="True when core started it (reason auto or overflow), false for manual.",
    )
    kept: int = Field(default=0, description="Messages kept verbatim after the summary.")
    reason: CompactionReason = Field(
        default="manual",
        description=(
            "1.55.0: overflow (the server refused the request for its length; the turn "
            "retries once after it), auto (the threshold, before a turn) or manual."
        ),
    )


class CompactionStarted(Payload):
    """Compaction is about to run (IDE-PROGRESS D3).

    ``compaction`` is published after the fact, as a transcript divider, which
    left a surface unable to say *Compacting…* while it happened — and unable
    to notice an automatic compaction at all.  This announces the work;
    ``compaction`` still reports the outcome.
    """

    kind: Literal["compaction.started"] = "compaction.started"
    reason: CompactionReason = Field(
        default="manual",
        description=(
            "auto = the auto-compaction threshold triggered it; overflow (1.55.0) = the "
            "server refused the request for its length; manual = asked for."
        ),
    )
    before: int = Field(default=0, description="Estimated tokens the history holds now.")


class ErrorEvent(Payload):
    """Something went wrong inside a turn."""

    kind: Literal["error"] = "error"
    code: str = Field(description="One of the protocol error codes.")
    message: str = Field(description="Human-readable detail.")
    details: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Machine-readable facts for the code (1.55.0); absent when the code has none. "
            "hook_failed: {plugin, hook (the hook's command line), event (PreToolUse, "
            "PostToolUse or Stop), reason (one line), exitCode? (absent when it could not "
            "start or timed out), timedOut? (true when it timed out)}. "
            "context_window_too_small: {windowTokens, neededTokens}. "
            "project_untrusted (1.62.0, browserProjectTrust): {workdir, scopes (the inactive "
            "files' scopes), changed (paths new or changed since the trust), browserScopes "
            "(of scopes, those a browser may grant), canTrustHere (browserScopes is not "
            "empty: a local browser 0.4.0+ that owns the session may trust the folder itself "
            "with project.trust.set), browserText (the line for such a browser), "
            "legacyBrowserText (the line for a browser that cannot)}; message stays the "
            "TUI/IDE line."
        ),
    )


class AudioSpoken(Payload):
    """A reply was synthesised, and played here when ``played`` is true.

    Emitted when ``audio.tts.autoSpeak`` is on, so a surface can show that the
    daemon is talking — and can play the file itself when the daemon has no
    player (CORE-multimodal).
    """

    kind: Literal["audio.spoken"] = "audio.spoken"
    path: str = Field(description="Audio file the speech was written to.")
    mime: str = Field(default="audio/mpeg", description="Media type of that file.")
    provider: str = Field(default="", description="Backend that synthesised it.")
    played: bool = Field(default=False, description="True when the daemon played it.")
    voice: str | None = Field(default=None, description="Voice that was used.")
    utterance: str = Field(
        default="reply",
        description=(
            "reply = the turn's answer; ack = the opening acknowledgement the agent "
            "writes before its first tool call. A surface may label or skip an ack; "
            "one that does not know the field treats everything as a reply."
        ),
    )


class ModelChanged(Payload):
    """The session's provider/model changed (``/model``, ``session.setModel``).

    The HUD is fed once by ``session/ready`` and had no way to learn about a
    later pin, so it showed a stale model for the rest of the session
    (CORE-model-assignment B-P2-3).
    """

    kind: Literal["model.changed"] = "model.changed"
    provider: str | None = Field(default=None, description="Vendor now in effect.")
    model: str | None = Field(default=None, description="Model id now in effect.")
    effort: EffortLevel | None = Field(
        default=None, description="Effective reasoning effort for the session (CORE-effort)."
    )
    effortSource: EffortSource | None = Field(
        default=None,
        description="Rule that decided the effort: 'session', 'model', 'vendor' or 'default'.",
    )
    efforts: list[EffortLevel] | None = Field(
        default=None,
        description=(
            "Tiers this model offers, weakest first; [] means it has no effort setting "
            "(effort is then null). E.g. Qwen3.8 Flash Next offers low, medium, high."
        ),
    )


class TurnStarted(Payload):
    """A turn actually began running (IDE-PROGRESS D1).

    Emitted once per turn at the moment the loop takes it up — after any wait
    in the prompt queue and before the first ``message.delta`` — so a surface
    can start its clock from when the turn truly began rather than from when
    it first overheard one.  ``queued`` is true when the turn had waited.
    """

    kind: Literal["turn.started"] = "turn.started"
    turnId: str = Field(description="Turn that is now running.")
    prompt: str | None = Field(
        default=None, description="The prompt that opened it; null when there is none."
    )
    queued: bool = Field(
        default=False, description="True when this turn waited in the prompt queue first."
    )
    initiator: Literal["user", "system"] = Field(
        default="user",
        description=(
            "Who started the turn (1.8.0). 'system' = session.continue: the input is a "
            "[system] note, no message.user is emitted, and surfaces show a status line "
            "instead of a user bubble. Exception (1.53.0): a Jarvis wake (reason "
            "'jarvis-intake') emits its text as message.user with origin {kind: 'system', "
            "surface: null} right after this event; prompt stays null."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Why a system turn started, e.g. 'handback' (1.8.0), 'goal' (1.50.0), "
            "'jarvis-intake' (a Jarvis wake, 1.53.0)."
        ),
    )
    goalRound: int | None = Field(
        default=None,
        description=(
            "A goal round's number, from 1 (reason 'goal', 1.50.0); absent on every other "
            "turn."
        ),
    )
    goalId: str | None = Field(
        default=None, description="The goal the round works on (1.50.0); absent otherwise."
    )


class TurnQueued(Payload):
    """A prompt arrived while a turn was running and was put in the FIFO.

    Surfaces use it to confirm the prompt was accepted and to show how many
    are waiting; ``turn.dequeued`` closes the pair (CORE-fixes-v017 R5).
    """

    kind: Literal["turn.queued"] = "turn.queued"
    turnId: str = Field(description="Turn id assigned to the queued prompt.")
    position: int = Field(
        description="1-based place in the queue behind the running turn."
    )
    queued: int = Field(description="Prompts waiting in the queue after this one was added.")
    text: str | None = Field(
        default=None,
        description=(
            "The queued prompt's own text, cut to 300 characters, so a surface that did "
            "not send it (another client, a reconnect) can still list it. Absent from "
            "older cores: show a placeholder then."
        ),
    )
    attachments: int | None = Field(
        default=None, description="How many files or images the queued prompt carries."
    )


class TurnDequeued(Payload):
    """A queued prompt left the queue - it started, was dropped, or was steered.

    ``reason="dropped"`` is emitted for every prompt flushed by
    ``session.interrupt``, so a surface can clear its queue indicator.
    """

    kind: Literal["turn.dequeued"] = "turn.dequeued"
    turnId: str = Field(description="Turn id that left the queue.")
    reason: QueuedTurnReason = Field(
        default="started",
        description=(
            "started = it is now running, dropped = it was discarded, "
            "steered = it was merged into the running turn."
        ),
    )
    queued: int = Field(default=0, description="Prompts still waiting after this one left.")


class TurnDone(Payload):
    """A turn ended, for any reason."""

    kind: Literal["turn.done"] = "turn.done"
    turnId: str = Field(description="Turn that ended.")
    reason: TurnReason = Field(
        default="complete",
        description=(
            "Why the turn ended. budget = the tool-round budget ran out; the turn "
            "reported what it had done before ending. stalled = consecutive rounds "
            "made no new tool or file progress. empty_response = the model ended with no "
            "text and no tool call, again after one nudge (1.23.0); an error event with "
            "code empty_response precedes it."
        ),
    )
    synthetic: bool = Field(
        default=False,
        description=(
            "True when the daemon wrote this event itself to close a turn a crash "
            "left open, rather than the turn reporting its own end (CORE-dangling-turns). "
            "The turn produced no further output after the events already stored."
        ),
    )
    historyLen: int | None = Field(
        default=None,
        description="Stored history length when the turn ended (1.46.0); where a fork cuts.",
    )
    compactions: int | None = Field(
        default=None,
        description="Compactions so far when the turn ended (1.46.0); a later one moves the cut.",
    )


TurnOutcomeValue = Literal["ok", "failed", "corrected", "abandoned"]
TurnOutcomeBy = Literal["user", "host", "judge"]


class TurnOutcomeEvent(Payload):
    """Whether a turn did what was asked, as a browser client judged it (1.27.0).

    Recorded as it was sent; core does not act on it.  A turn may get more
    than one: an ``ok`` the user later corrects is followed by ``corrected``.
    """

    kind: Literal["turn.outcome"] = "turn.outcome"
    turnId: str = Field(description="The turn judged.")
    outcome: TurnOutcomeValue = Field(description="ok | failed | corrected | abandoned.")
    by: TurnOutcomeBy = Field(description="user (a 👍/👎), host, or judge (a detector).")
    reason: str | None = Field(default=None, description="Why, at most 200 characters.")


class ContextHostEvent(Payload):
    """A host note (``host.turnContext``) joined this turn's user message (1.31.0).

    For evals: tells a turn that had the note from one that did not.  Never
    carries the text.
    """

    kind: Literal["context.host"] = "context.host"
    turnId: str = Field(description="The turn the note was added to.")
    chars: int = Field(default=0, description="Characters of host text kept, after the cap.")


class MemoryCandidateEvent(Payload):
    """A preference the extractor would remember, waiting for the person (1.28.0).

    Sent with ``memory.autoExtract: "suggest"`` after a completed turn of the
    person's own words; nothing is stored until ``memory.confirm`` accepts it.
    Candidates live in the daemon's memory only and are gone after a restart.
    """

    kind: Literal["memory.candidate"] = "memory.candidate"
    candidateId: str = Field(description="Pass to memory.confirm.")
    text: str = Field(description="The preference as it would be stored.")
    category: PreferenceCategory = Field(description="shopping | output | site.")


class LspDiagnostics(Payload):
    """A language server published diagnostics for a file in this session.

    Carries only the counts: a surface wants a badge, and the text of every
    diagnostic already reaches the model through the tool result.
    """

    kind: Literal["lsp.diagnostics"] = "lsp.diagnostics"
    path: str = Field(description="File the diagnostics are about.")
    count: int = Field(default=0, description="Diagnostics of every severity.")
    errors: int = Field(default=0, description="How many of them are errors.")
    warnings: int = Field(default=0, description="How many of them are warnings.")
    callId: str | None = Field(
        default=None,
        description="The tool call whose edit or check produced them, when one did (1.29.0).",
    )


class LoopSuspected(Payload):
    """The same tool call keeps running with the same arguments (CORE-repeat-guard).

    Advisory: the call still ran.  A surface can show the turn is going in
    circles, and the model gets the same sentence appended to its tool result.
    """

    kind: Literal["loop.suspected"] = "loop.suspected"
    tool: str = Field(description="Tool whose call repeated.")
    count: int = Field(default=0, description="Repeats seen in the last 20 tool calls.")


class JobDone(Payload):
    """A scheduled job this session created finished successfully.

    Sent to the *originating* thread (``job.originSessionId``), not to the
    unattended session the run itself used, so the thread that asked for the
    job can offer "open s-xxxx" (CORE-session-kind).
    """

    kind: Literal["job.done"] = "job.done"
    jobId: str = Field(description="Job that ran.")
    sessionId: str | None = Field(default=None, description="Session the run used.")
    status: str = Field(default="ok", description="Job status as the scheduler recorded it.")
    text: str = Field(default="", description="What the run reported.")


class JobFailed(Payload):
    """A scheduled job this session created ended without a usable answer."""

    kind: Literal["job.failed"] = "job.failed"
    jobId: str = Field(description="Job that ran.")
    sessionId: str | None = Field(default=None, description="Session the run used.")
    status: str = Field(default="error", description="Job status as the scheduler recorded it.")
    text: str = Field(default="", description="What the run reported, or the error.")


class ApprovalAsked(Payload):
    """An approval was put to a person (0.2.91, stored for the conversation's record).

    What the card showed and where it was sent, so an export can say which
    approvals were asked; ``approval.answered`` with the same ``requestId``
    says how it ended.  A call tagged ``secret`` or ``config`` keeps its
    arguments out (``summary`` "arguments withheld", ``argsWithheld`` true).
    """

    kind: Literal["approval.asked"] = "approval.asked"
    requestId: str = Field(description="The approval request.")
    tool: str = Field(description="The tool the call was for.")
    risk: str | None = Field(default=None, description="low | medium | high, as on the card.")
    summary: str = Field(
        default="",
        description=(
            "What the card showed of the call, at most 240 characters: the command line, "
            "the path or URL, else the arguments as JSON; 'arguments withheld' for a "
            "secret or config call."
        ),
    )
    argsWithheld: bool = Field(default=False, description="True when the arguments were kept out.")
    reasonCode: str | None = Field(default=None, description="ApprovalRequest.reasonCode.")
    unchecked: bool = Field(
        default=False,
        description="True when the card said snowpea could not check the call (uncheckedWhy).",
    )
    personOnly: bool = Field(default=False, description="Only a person could answer it.")
    route: str | None = Field(default=None, description="ApprovalRequest.route when it was asked.")
    askedConn: str = Field(
        description=(
            "Where it was asked: the person's surface (ide, desktop, app, tui, cli, web, "
            "person), remote (a remote web login), browser, connector, or none (the shared "
            "queue: every surface could answer)."
        )
    )


class ApprovalAnswered(Payload):
    """How an asked approval ended (0.2.91; follows ``approval.asked``)."""

    kind: Literal["approval.answered"] = "approval.answered"
    requestId: str = Field(description="The approval request.")
    tool: str = Field(description="The tool the call was for.")
    decision: Literal["allow", "deny"] = Field(description="allow | deny.")
    scope: str | None = Field(default=None, description="The scope applied; null for a deny.")
    by: str = Field(
        description=(
            "Who or what ended it: origin (the asked surface's person), origin-timeout (that "
            "surface's own card timed out), origin-no-answer, origin-unreachable, client "
            "(approval.respond from a surface), gateway:<platform>:<user>, jarvis, "
            "jarvis:auto, timeout, interrupted, security, error."
        )
    )
    reason: str | None = Field(
        default=None, description="The reason given or recorded, at most 300 characters."
    )
    waitedMs: int = Field(default=0, description="Milliseconds from asked to answered.")


class QuestionAskedItem(Payload):
    """One question of a ``question.asked`` batch."""

    header: str | None = Field(default=None, description="The tab title.")
    question: str = Field(description="The question.")
    options: list[str] = Field(default_factory=list, description="The option labels.")
    secret: bool = Field(default=False, description="Its answer is never stored.")


class QuestionAsked(Payload):
    """Questions were put to a person (0.2.91, stored for the conversation's record)."""

    kind: Literal["question.asked"] = "question.asked"
    requestId: str = Field(description="The question request.")
    questions: list[QuestionAskedItem] = Field(default_factory=list, description="The batch.")
    personOnly: bool = Field(default=False, description="Only a person could answer it.")
    questionKind: str | None = Field(default=None, description="QuestionRequest.kind ('mode').")
    route: str | None = Field(default=None, description="QuestionRequest.route when asked.")
    askedConn: str = Field(description="As approval.asked.askedConn.")


class QuestionAnsweredItem(Payload):
    """One answer of a ``question.answered`` batch."""

    selected: list[str] = Field(default_factory=list, description="Picked option labels.")
    text: str | None = Field(default=None, description="Typed text; null when withheld.")
    withheld: bool = Field(default=False, description="A secret question: nothing kept.")
    declined: bool = Field(default=False, description="Declined (Esc, or no way to ask).")
    timedOut: bool = Field(default=False, description="Nobody answered.")


class QuestionAnswered(Payload):
    """How asked questions ended (0.2.91; follows ``question.asked``)."""

    kind: Literal["question.answered"] = "question.answered"
    requestId: str = Field(description="The question request.")
    by: str = Field(
        description=(
            "Who or what ended it: origin, origin-timeout, origin-unreachable, client, "
            "jarvis, timeout, interrupted, error, unknown."
        )
    )
    answers: list[QuestionAnsweredItem] = Field(
        default_factory=list, description="One per question."
    )
    waitedMs: int = Field(default=0, description="Milliseconds from asked to answered.")


SessionEventPayload = Annotated[
    MessageUser
    | MessageDelta
    | MessageReasoning
    | MessageDone
    | ToolCallEvent
    | ToolResultEvent
    | ToolProgress
    | DiffEvent
    | TodosUpdated
    | CommandProgress
    | CommandResultEvent
    | PlanUpdated
    | CheckpointUpdated
    | CheckpointRestored
    | SubagentSpawn
    | SubagentUpdate
    | SubagentDone
    | TeamTaskUpdate
    | ModeChanged
    | AgentChanged
    | HookContinue
    | ProviderWaiting
    | ProviderRetry
    | VisionUnavailable
    | GoalChanged
    | StepStarted
    | SessionForked
    | StepDone
    | ModelChanged
    | UsageEvent
    | ContextEvent
    | CompactionEvent
    | CompactionStarted
    | ErrorEvent
    | AudioSpoken
    | TurnStarted
    | TurnQueued
    | TurnDequeued
    | TurnDone
    | LspDiagnostics
    | LoopSuspected
    | JobDone
    | JobFailed
    | ApprovalAsked
    | ApprovalAnswered
    | QuestionAsked
    | QuestionAnswered,
    Field(discriminator="kind"),
]

#: ``session.event`` payload model per ``kind`` (contract §1).
SESSION_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "message.user": MessageUser,
    "message.delta": MessageDelta,
    "message.reasoning": MessageReasoning,
    "message.done": MessageDone,
    "tool.call": ToolCallEvent,
    "tool.result": ToolResultEvent,
    "tool.progress": ToolProgress,
    "diff": DiffEvent,
    "todos.updated": TodosUpdated,
    "command.progress": CommandProgress,
    "command.result": CommandResultEvent,
    "plan.updated": PlanUpdated,
    "checkpoint.updated": CheckpointUpdated,
    "checkpoint.restored": CheckpointRestored,
    "subagent.spawn": SubagentSpawn,
    "subagent.update": SubagentUpdate,
    "subagent.done": SubagentDone,
    "team.task.update": TeamTaskUpdate,
    "mode.changed": ModeChanged,
    "agent.changed": AgentChanged,
    "hook.continue": HookContinue,
    "provider.waiting": ProviderWaiting,
    "provider.retry": ProviderRetry,
    "vision.unavailable": VisionUnavailable,
    "goal.changed": GoalChanged,
    "step.started": StepStarted,
    "session.forked": SessionForked,
    "step.done": StepDone,
    "model.changed": ModelChanged,
    "usage": UsageEvent,
    "context": ContextEvent,
    "compaction": CompactionEvent,
    "compaction.started": CompactionStarted,
    "error": ErrorEvent,
    "audio.spoken": AudioSpoken,
    "turn.started": TurnStarted,
    "turn.queued": TurnQueued,
    "turn.dequeued": TurnDequeued,
    "turn.done": TurnDone,
    "turn.outcome": TurnOutcomeEvent,
    "memory.candidate": MemoryCandidateEvent,
    "context.host": ContextHostEvent,
    "lsp.diagnostics": LspDiagnostics,
    "loop.suspected": LoopSuspected,
    "job.done": JobDone,
    "job.failed": JobFailed,
    "approval.asked": ApprovalAsked,
    "approval.answered": ApprovalAnswered,
    "question.asked": QuestionAsked,
    "question.answered": QuestionAnswered,
}

SESSION_EVENT_KINDS: tuple[str, ...] = tuple(SESSION_EVENT_MODELS)


class SessionEventNotification(Payload):
    """Server notification carrying one session event."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number.")
    kind: str = Field(description="Event kind; see sessionEventKinds for the payload schema.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")
    hostToolsFrom: str | None = Field(
        default=None,
        description="The session's owning client (addendum 8); null for unhosted sessions.",
    )


class SessionEventKindEnvelope(Payload):
    """Typed view of a ``session.event`` payload, discriminated by ``kind``."""

    event: SessionEventPayload


class ApprovalResolvedNotification(Payload):
    """An approval was answered elsewhere; stop showing it."""

    requestId: str = Field(description="Request that was resolved.")
    decision: Decision | None = Field(
        default=None,
        description=(
            "The decision that was recorded; null only with outcome 'withdrawn' (1.53.0)."
        ),
    )
    by: str = Field(
        description=(
            "Surface or user that answered. Since 1.53.0 also 'jarvis' (the Jarvis session "
            "decided within its delegation), 'jarvis:auto' (the delegation engine "
            "allowed a safe request without a model call) and 'rerouted' (with outcome "
            "'withdrawn')."
        )
    )
    outcome: Literal["withdrawn"] | None = Field(
        default=None,
        description=(
            "'withdrawn' (1.53.0, security review 3): the request is no longer this "
            "connection's to show — sent only to a browser connection when a request it "
            "showed was routed to Jarvis. Nothing was decided; stop showing it. If it is "
            "routed back to the person the browser gets approval.pending again."
        ),
    )
    scope: ApprovalScope | None = Field(
        default=None,
        description=(
            "The scope actually applied (1.54.0): once, session, project, always or site — "
            "after core's rules (a browser's unmarked widening reply, a person-only request, "
            "a shell line with no safe rule are kept to 'once'). Null for a deny and with "
            "outcome 'withdrawn'."
        ),
    )


class ApprovalRespondResult(Ok):
    """``approval.respond``'s result (1.54.0: the applied scope)."""

    scope: ApprovalScope | None = Field(
        default=None,
        description=(
            "The scope that applies to this answer (as ApprovalResolvedNotification.scope); "
            "null for a deny or when the request had already been answered."
        ),
    )


class RequestRefusal(Payload):
    """Why the asking connection's own reply to a request did not count (1.55.0)."""

    code: str = Field(description="The error code the reply earned: 'jarvis_person_only'.")
    reason: str = Field(
        description=(
            "As the RPC error's details.reason: 'person_only', or 'mode_raise' for a "
            "question of kind 'mode'. 1.61.0 (browserPersonApprovals), a browser's allow of a "
            "person-only approval: 'browser_unconfirmed' (not actor 'person' with "
            "personConfirm 'window'), 'browser_legacy' (clientVersion before 0.4.0), "
            "'browser_remote' (a remote web login or a peer that is not this machine), "
            "'browser_not_owner' (not a session it owns, hosts or opened, nor its asker; a "
            "subagent's counts by its parents) or 'browser_not_allowed' (not a security ask: "
            "delegation_set, a mode raise, a schedule, tmux.adopt, an external CLI's or a "
            "Jarvis-routed request)."
        )
    )
    message: str = Field(description="Human-readable detail.")


class ApprovalPendingNotification(Payload):
    """An unattended approval is waiting; any authenticated surface may answer.

    Since 1.55.0 also sent to the asking connection alone, with ``refused``,
    when its reply to an ``approval.request`` for a ``personOnly`` request was
    not a person's (a browser reply without ``actor: "person"``, or from a
    browser that does not host the session): the request stays open, the card
    should come back, and a person answers it with ``approval.respond``.
    """

    request: ApprovalRequest = Field(description="The request now in the shared queue.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "The session's owning client (addendum 8). A browser profile shows only "
            "its own cards and those with null."
        ),
    )
    refused: RequestRefusal | None = Field(
        default=None,
        description=(
            "1.55.0: set only on the re-announcement to the asking connection after its "
            "reply was refused; the request is still open."
        ),
    )


class QuestionResolvedNotification(Payload):
    """A question was answered elsewhere; stop showing it."""

    requestId: str = Field(description="Question that was resolved.")
    by: str = Field(
        description=(
            "Surface or user that answered. Since 1.53.0 also 'jarvis' (the Jarvis session "
            "answered within its delegation) and 'rerouted' (with outcome 'withdrawn')."
        )
    )
    outcome: Literal["withdrawn"] | None = Field(
        default=None,
        description=(
            "'withdrawn' (1.53.0, security review 3): sent only to a browser connection when "
            "a question it showed was routed to Jarvis; nothing was answered."
        ),
    )


#: Why a waiting request's ``route`` changed (``approval.route.changed`` /
#: ``question.route.changed``, 1.53.0).
RouteChangeReason = Literal[
    "jarvis_enabled",
    "jarvis_disabled",
    "paused",
    "resumed",
    "budget",
    "declined",
    "refused",
    "session_on",
    "session_off",
]


class _RouteChanged(Payload):
    requestId: str = Field(description="The waiting request.")
    sessionId: str = Field(
        description="Its session; 'ext:<cli>:<id>' for an external session's approval."
    )
    route: ApprovalRoute | None = Field(
        description="The new route; null when Jarvis mode went off (no routing)."
    )
    reason: RouteChangeReason = Field(
        description=(
            "jarvis_enabled (it was waiting when Jarvis mode came on and has just been "
            "routed), jarvis_disabled (routing ended), paused (the person took the session "
            "back or typed in it: Jarvis's requests there are the person's), resumed (handed "
            "back, or the quiet time ran out: routed again), budget (Jarvis's allowance ran "
            "out), declined (Jarvis's turn ended without deciding it), refused (the engine "
            "refused Jarvis's or its own decision, e.g. on a re-check). 1.56.0 "
            "(jarvisSessionSwitch): session_on (the session's Jarvis switch was turned on "
            "while it waited: routed as if it had just been asked), session_off (the switch "
            "was turned off: route null, the person's as with Jarvis mode off)."
        )
    )


class ApprovalRouteChangedNotification(_RouteChanged):
    """A waiting approval's ``route`` changed (1.53.0, capability ``jarvisDelegation``).

    Sent to the audience of the original request: the asking connection alone
    for an interactive approval (``approval.request``), every authenticated
    surface for one in the shared queue (``approval.pending``).  ``approval.list``
    returns the new route from then on.
    """

    hostToolsFrom: str | None = Field(
        default=None, description="As ApprovalPendingNotification.hostToolsFrom."
    )


class QuestionRouteChangedNotification(_RouteChanged):
    """A waiting question's ``route`` changed (1.53.0, capability ``jarvisDelegation``).

    Daemon-wide, like ``question.pending`` (the asking connection included);
    ``question.list`` returns the new route from then on.
    """


class QuestionPendingNotification(Payload):
    """A question is waiting; any authenticated surface may see it coming.

    Since 1.55.0 also sent to the asking connection alone, with ``refused``,
    when its reply to a ``question.request`` for a ``personOnly`` question was
    not a person's: the question stays open with the same ``requestId``, the
    card should come back, and a person answers it with ``question.respond``.
    """

    request: QuestionRequest = Field(description="The question now in the shared queue.")
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "The session's owning client (addendum 8). A browser profile shows only "
            "its own cards and those with null."
        ),
    )
    refused: RequestRefusal | None = Field(
        default=None,
        description=(
            "1.55.0: set only on the re-announcement to the asking connection after its "
            "reply was refused; the question is still open."
        ),
    )


class CommandsChangedNotification(Payload):
    """The slash-command table changed; re-read it (``skill.reload``, install)."""

    commands: list[CommandInfo] = Field(
        default_factory=list, description="The command table as it stands now."
    )
    reason: str = Field(default="reload", description="Why the table changed.")


class AudioInstallProgressNotification(Payload):
    """Progress from a running ``audio.install``.

    Session-less and broadcast: an install belongs to the daemon, not to a
    conversation, and any attached surface may be showing it.

    A log line alone cannot say how far along a 400MB download is, so every
    event names its **stage** and its place in the sequence. A surface draws a
    one-line bar from ``stage``/``step``/``steps``/``percent`` and the log tail
    from ``line``; either may be absent, and a client that only knows ``line``
    keeps working.
    """

    engine: str = Field(description="Engine being installed, always the bare id.")
    voice: str = Field(
        default="",
        description=(
            "Set when a *voice* of that engine is installing, e.g. ko_KR-kss-medium; "
            "empty for the engine itself. A surface keys its progress row on the "
            "pair, so an engine install and a voice install never share a row."
        ),
    )
    line: str = Field(default="", description="One line of the installer's output.")
    stage: str = Field(
        default="",
        description=(
            "resolve | download | extract | verify | install | check. "
            "'install' is the package manager, 'verify' is the checksum, "
            "'check' is the detection that runs afterwards."
        ),
    )
    step: int = Field(default=0, description="1-based place of this stage in the sequence.")
    steps: int = Field(default=0, description="How many stages this engine has in total.")
    percent: float | None = Field(
        default=None, description="0-100 within the stage, when it can be known."
    )
    bytesDone: int | None = Field(default=None, description="Bytes transferred so far.")
    bytesTotal: int | None = Field(
        default=None, description="Total bytes, from Content-Length when the server sends one."
    )


class JobEventNotification(Payload):
    """Progress from a scheduled job."""

    jobId: str = Field(description="Job the event belongs to.")
    kind: Literal[
        "started",
        "finished",
        "failed",
        "denied",
        "deleted",
        "updated",
        "waiting_for_host",
        "missed",
        "notify",
        "approval_needed",
    ] = Field(
        description=(
            "Where the run got to. 'notify' (1.23.0) follows the finish of a job with "
            "meta, payload {status, sessionId, text}: its host shows the user a notification. "
            "'approval_needed' (1.68.0, jobApprovals): a run waits for a person, payload "
            "{requestId, sessionId, tool, summary, title, timeoutSec}: show a notification "
            "that names the job; the request itself is in approval.pending. 'missed' "
            "payload {scheduledAt}; 1.69.0 (jobMissedRuns) adds reason ('daemon_down': "
            "the daemon started past the catch-up window; 'host_absent': the browser host "
            "never came), error, title and nextRunAt, and a daemon that started too late "
            "now sends it too (with a job.runs row of status 'missed')."
        )
    )
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")


class UpdateProgressNotification(Payload):
    """Progress of the upgrade ``system.update`` started."""

    phase: UpdatePhase = Field(description="Where the upgrade got to.")
    message: str = Field(default="", description="One line for humans.")
    selfRestart: bool = Field(
        default=False,
        description=(
            "On 'done': the daemon restarts by itself, so the client waits for it "
            "instead of restarting the core (1.26.0)."
        ),
    )


class GatewayEventNotification(Payload):
    """Activity on a gateway binding."""

    bindingId: str = Field(description="Binding the event belongs to.")
    kind: str = Field(description="Event kind, e.g. 'message' or 'error'.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")


class ProviderLoginProgressNotification(Payload):
    """Progress of a ``provider.loginWeb`` flow, from the device code or PKCE
    URL being ready through to the token being stored."""

    vendor: str = Field(description="Vendor being logged into.")
    method: str = Field(description="Login flow in progress, e.g. 'device_code' or 'oauth_pkce'.")
    phase: LoginProgressPhase = Field(description="Where the login got to.")
    userCode: str | None = Field(
        default=None, description="Short code the user types in, for device-code flows."
    )
    verificationUri: str | None = Field(
        default=None, description="URL to open to approve the login."
    )
    verificationUriComplete: str | None = Field(
        default=None, description="verificationUri with the code already embedded, when known."
    )
    message: str | None = Field(default=None, description="One line for humans.")
    expiresInSec: float | None = Field(
        default=None, description="Seconds until the code or session expires, when known."
    )


class SecuritySwitchState(Payload):
    """The security master switch (1.58.0, capability ``securityMasterSwitch``)."""

    enabled: bool = Field(
        default=True,
        description=(
            "security.enabled: false while the security checks are off — the blocked-command "
            "and protected-path lists (presets, custom, project additions) are not applied "
            "and code snowpea cannot read ahead runs as under unknownCode 'allow'. The stored "
            "lists and unknownCode are kept, so switching it on restores them. snowpea's own "
            "controls and the mode rules apply either way. 1.66.0 (capability "
            "'securityDefaultOff'): off by default — true only when settings.json says "
            "security.enabled: true; while off, reads of the daemon's token and address, "
            "saved logins and other CLIs' logins are not blocked either and follow the mode "
            "rules like any other read (a 'secret' read: asked in plan and accept, not in "
            "auto). The default of this field stays true: a core before 1.58.0 omits it and "
            "always checked."
        ),
    )
    byDefault: bool = Field(
        default=False,
        description=(
            "1.66.0 (capability 'securityDefaultOff'): true while off only because nobody "
            "turned the checks off — no record of a switch to off (disabledBy and "
            "disabledAt are then null). The default state, not a problem: a surface shows "
            "it neutrally. False while on, and while off by someone's choice."
        ),
    )
    disabledBy: str | None = Field(
        default=None,
        description=(
            "While off: the client kind of the connection that turned it off ('desktop', "
            "'tui', 'ide', 'browser', ...), as recorded in "
            "$SNOWPEA_HOME/security/switch.json. A hand edit of settings.json reads "
            "'settings.json' (found at the daemon's start) or 'settings.json (accepted by "
            "<kind>)' (settings.acceptDiskChange). Null while on or when not recorded."
        ),
    )
    disabledAt: str | None = Field(
        default=None, description="While off: when it was turned off (ISO 8601 UTC), or null."
    )


class SettingsChangedNotification(Payload):
    """The daemon reloaded settings from disk (CORE-settings-reload).

    Broadcast to every authenticated connection so a surface can re-read the
    parts it caches instead of showing a stale model or mode.
    """

    scope: SettingsScope = Field(
        default="global",
        description='Which document was reloaded; "global" for $SNOWPEA_HOME/settings.json.',
    )
    keys: list[str] = Field(
        default_factory=list,
        description="Top-level settings sections that changed, e.g. ['providers'].",
    )
    waitingKeys: list[str] = Field(
        default_factory=list,
        description=(
            "1.55.0 (security reviews 7 and 8): the settings paths that can run programs "
            "or widen access ('security', 'jarvis', 'web.remote', 'allowlist', "
            "'mcp.permissions', or a dotted program/endpoint/daemon leaf such as "
            "'mcp.servers.foo.command' or 'providers.openai.base_url') when settings.json "
            "was edited on disk while the daemon runs and that path differs: the edit is "
            "not applied until a local person accepts it (settings.acceptDiskChange). Sent "
            "with keys possibly empty; an empty list after a non-empty one means nothing "
            "waits any more."
        ),
    )
    security: SecuritySwitchState | None = Field(
        default=None,
        description=(
            "1.58.0 (securityMasterSwitch): the security master switch as it is now, sent "
            "whenever keys holds 'security' (a change of security.enabled included), so "
            "every surface can show that security checks are off and who turned them off. "
            "Absent otherwise."
        ),
    )


# --------------------------------------------------------------------------
# lsp.*
# --------------------------------------------------------------------------


class LspServerStatus(Payload):
    """One language server the daemon has started, or tried to (M13 §4)."""

    id: str = Field(description="Server id, e.g. 'pyright' or 'gopls'.")
    root: str = Field(description="Project root the server was started in.")
    state: LspServerState = Field(description="starting, ready, broken or stopped.")
    languageId: str = Field(
        default="", description="LSP language id the server's first extension maps to."
    )
    pid: int | None = Field(default=None, description="Process id while it is running.")
    reason: str | None = Field(
        default=None,
        description=(
            "Why it is not ready (1.29.0): 'not installed', 'not for this project', "
            "'exited: code N', 'initialize failed: …', 'idle' (stopped by the idle sweep; "
            "the next edit restarts it). Null while it is fine."
        ),
    )


class LspStatusResult(Payload):
    """``lsp.status`` — every server, for the TUI's `lsp` segment and `/lsp`."""

    servers: list[LspServerStatus] = Field(
        default_factory=list, description="One row per (server, root) pair."
    )


class LspToolchainOffer(Payload):
    """A toolchain ``lsp.install`` can install when asked to (1.68.0, lspToolchainInstall)."""

    name: str = Field(description="Toolchain name: 'dotnet-sdk'.")
    sizeMb: int | None = Field(
        default=None, description="Download size in MB (an estimate when unknown)."
    )
    source: str | None = Field(
        default=None,
        description="Where it comes from: the archive's https URL on Microsoft's host.",
    )
    version: str | None = Field(
        default=None, description="The version that would be installed, when known."
    )


class LspCatalogEntry(Payload):
    """One registered language server, whether or not it has ever started."""

    id: str = Field(description="Server id, e.g. 'pyright' or 'gopls'.")
    languageIds: list[str] = Field(
        default_factory=list,
        description="LSP language ids this server's extensions map to.",
    )
    extensions: list[str] = Field(
        default_factory=list,
        description="Extensions or whole filenames this server claims.",
    )
    installable: bool = Field(
        description="True when lsp.install or lsp.autoInstall could obtain it "
        "(npm/pip/go/dotnet/rustup); False means it must already be on PATH."
    )
    installHint: str | None = Field(
        default=None, description="A command that installs it manually, e.g. 'pip install ty'."
    )
    disabled: bool = Field(
        description="True when settings (lsp.disabled, a default-off id, or an "
        "explicit lsp.servers override) keep this server from starting."
    )
    available: bool = Field(
        default=False,
        description=(
            "True when its binary is found now, on PATH or under $SNOWPEA_HOME/lsp "
            "(1.29.0). A rustup proxy without the component does not count."
        ),
    )
    path: str | None = Field(default=None, description="The binary found, when available.")
    managed: bool = Field(
        default=False, description="True when that binary is under $SNOWPEA_HOME/lsp."
    )
    needs: str | None = Field(
        default=None,
        description=(
            "While it is not available: what must be on PATH first, e.g. 'npm', 'go', "
            "'dotnet', or for a PATH-only server the toolchain or command that provides "
            "it ('gem install ruby-lsp'). Null when lsp.install can do it as is. "
            "1.68.0: '.NET SDK' for a .NET server while no SDK is found (dotnet on PATH "
            "with the runtime only counts as none)."
        ),
    )
    reason: Literal["needs_toolchain"] | None = Field(
        default=None,
        description=(
            "1.68.0 (lspInstallReasons): 'needs_toolchain' while it is not available and "
            "its installer's toolchain is missing (see toolchain, hint, url). Null otherwise."
        ),
    )
    toolchain: str | None = Field(
        default=None,
        description=(
            "1.68.0 (lspInstallReasons), with reason needs_toolchain: 'dotnet-sdk', 'go', "
            "'rustup', 'node' or 'python'."
        ),
    )
    hint: str | None = Field(
        default=None,
        description="1.68.0 (lspInstallReasons): one English line on how to get the toolchain.",
    )
    url: str | None = Field(
        default=None,
        description="1.68.0 (lspInstallReasons): the toolchain's official download page.",
    )
    toolchainInstallable: bool | None = Field(
        default=None,
        description=(
            "1.68.0 (lspToolchainInstall), with reason needs_toolchain: true when "
            "lsp.install {withToolchain: true} can install that toolchain itself "
            "(dotnet-sdk today). Also set (true) with toolchainInstalled."
        ),
    )
    toolchainInstalled: bool | None = Field(
        default=None,
        description=(
            "1.68.0 (lspToolchainInstall): for a server whose installer uses a toolchain "
            "core can install (toolchain, e.g. 'dotnet-sdk', is then set too): true while "
            "core's own copy is installed (lsp.toolchain.remove deletes it), false while "
            "it is not. Null for other servers. A system-wide SDK does not count."
        ),
    )
    toolchainVersion: str | None = Field(
        default=None,
        description="1.68.0: with toolchainInstalled true, the version of core's own copy.",
    )
    toolchainSizeMb: int | None = Field(
        default=None,
        description=(
            "1.68.0: with toolchainInstalled true, the MB core's own copy takes on disk "
            "(recorded at install; null for a copy installed before that)."
        ),
    )
    needsToolchain: LspToolchainOffer | None = Field(
        default=None,
        description=(
            "1.68.0 (lspToolchainInstall), with reason needs_toolchain and "
            "toolchainInstallable: what lsp.install {withToolchain: true} would download, "
            "as LspInstallItem.needsToolchain, so a client can offer the server together "
            "with the toolchain in one click. sizeMb is an estimate and version null when "
            "the release metadata could not be read in time."
        ),
    )


class LspCatalogResult(Payload):
    """``lsp.catalog`` — every registered server, for settings UIs (M13 §4)."""

    servers: list[LspCatalogEntry] = Field(
        default_factory=list, description="One row per registered server id."
    )


class LspInstallParams(Payload):
    """``lsp.install`` — the user asked for servers to be installed (1.29.0)."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    ids: list[str] | None = Field(
        default=None,
        description=(
            "Server ids to install. Null: every installable server that is enabled and "
            "not available. Runs even with lsp.autoInstall false: the request is the consent."
        ),
    )
    withToolchain: bool = Field(
        default=False,
        description=(
            "1.68.0 (lspToolchainInstall): also install a missing toolchain the result "
            "offered as needsToolchain (the .NET SDK, from Microsoft, sha512-verified, "
            "into a per-user directory core owns), then the server. A person's only: a "
            "local IDE/TUI/CLI, or a local browser 0.4.0+ with actor 'person' and "
            "personConfirm 'window'; a remote web login, a connector, Jarvis or an "
            "in-process caller is refused (jarvis_person_only)."
        ),
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description=(
            "1.68.0: a browser's 'window' — its person confirmed the toolchain download in "
            "the browser's own window (size and source shown). Needed with withToolchain."
        ),
    )




class LspInstallItem(Payload):
    id: str = Field(description="Server id.")
    ok: bool = Field(description="True when the server's binary is there now.")
    path: str | None = Field(default=None, description="The binary, when ok.")
    error: str | None = Field(default=None, description="Why not, when not ok.")
    installed: bool = Field(
        default=False,
        description="True when an installer ran (ok or not); false when it was already there.",
    )
    reason: Literal["needs_toolchain", "npm_failed", "toolchain_failed"] | None = Field(
        default=None,
        description=(
            "1.68.0 (lspInstallReasons), when not ok: 'needs_toolchain' (toolchain, hint, "
            "url; also when dotnet exits 2147516571 sdk-not-found), 'npm_failed' (npmCode, "
            "detail), 'toolchain_failed' (toolchainError: the withToolchain install failed). "
            "Null for any other failure."
        ),
    )
    toolchain: str | None = Field(
        default=None, description="1.68.0: as LspCatalogEntry.toolchain."
    )
    hint: str | None = Field(default=None, description="1.68.0: as LspCatalogEntry.hint.")
    url: str | None = Field(default=None, description="1.68.0: as LspCatalogEntry.url.")
    toolchainInstallable: bool | None = Field(
        default=None, description="1.68.0: as LspCatalogEntry.toolchainInstallable."
    )
    npmCode: str | None = Field(
        default=None,
        description="1.68.0, with npm_failed: npm's error code, e.g. 'ERESOLVE', 'EACCES', "
        "'ENOTFOUND', 'E404'; null when npm printed none.",
    )
    detail: str | None = Field(
        default=None,
        description="1.68.0, with npm_failed: npm's last error lines (not the log-file "
        "line), at most 800 characters.",
    )
    needsToolchain: LspToolchainOffer | None = Field(
        default=None,
        description=(
            "1.68.0 (lspToolchainInstall), with needs_toolchain: what lsp.install "
            "{withToolchain: true} would download — show it in the confirm. Absent when "
            "core cannot install that toolchain."
        ),
    )
    toolchainError: str | None = Field(
        default=None,
        description=(
            "1.68.0, with toolchain_failed: busy, cancelled, network, metadata, source, "
            "checksum, disk_full, archive, io or unsupported."
        ),
    )


class LspInstallResult(Payload):
    results: list[LspInstallItem] = Field(default_factory=list, description="One per id.")


class LspInstallProgressNotification(Payload):
    """``lsp.install.progress`` — one line or step of a running ``lsp.install``.

    Session-less and broadcast, like ``audio.install.progress``.
    """

    id: str = Field(description="Server being installed.")
    stage: Literal["start", "output", "done"] = Field(
        description="start (the installer runs), output (a line it printed), done."
    )
    line: str | None = Field(default=None, description="The output line, or the command.")
    ok: bool | None = Field(default=None, description="For done: whether it worked.")
    error: str | None = Field(default=None, description="For done and not ok: why.")
    phase: Literal["resolve", "download", "verify", "extract", "toolchain", "server"] | None = (
        Field(
            default=None,
            description=(
                "1.68.0 (lspToolchainInstall): resolve, download, verify, extract (a "
                "toolchain install, stage 'output'), toolchain (its end), server (the "
                "server's own installer). Absent from older cores."
            ),
        )
    )
    percent: int | None = Field(
        default=None, description="1.68.0: 0-100 while a toolchain downloads, else null."
    )
    toolchain: str | None = Field(
        default=None, description="1.68.0: the toolchain being installed, e.g. 'dotnet-sdk'."
    )


class LspToolchainParams(Payload):
    """``lsp.toolchain.remove`` / ``lsp.toolchain.cancel`` (1.68.0, lspToolchainInstall)."""

    name: str = Field(description="Toolchain name: 'dotnet-sdk'.")
    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker: see ActorParams."
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description="A browser's 'window' (needed for remove, as for withToolchain).",
    )


class LspToolchainRemoveResult(Payload):
    name: str = Field(description="Toolchain name.")
    removed: bool = Field(
        description="True when core's own copy was there and is gone (its downloads too)."
    )


class LspToolchainCancelResult(Payload):
    name: str = Field(description="Toolchain name.")
    cancelled: bool = Field(
        description="True when an install was running and was stopped; the lsp.install "
        "that ran it answers toolchain_failed / cancelled."
    )


class LspRestartParams(Payload):
    """``lsp.restart`` — the UI's retry button (1.29.0)."""

    id: str | None = Field(
        default=None, description="Server id; null restarts every server and re-probes."
    )


class LspRestartResult(Payload):
    restarted: list[str] = Field(
        default_factory=list,
        description="Ids stopped or forgotten; each starts afresh on its next edit.",
    )


class LspChangedNotification(Payload):
    """``lsp.changed`` — a language server moved, or the set may have (1.29.0).

    With ``id``: that (server, root) entered ``state``. With ``id`` null: an
    install, restart or settings change; re-read ``lsp.catalog`` and ``lsp.status``.
    """

    id: str | None = Field(default=None, description="Server id, or null for 'everything'.")
    root: str | None = Field(default=None, description="Project root of that server.")
    state: LspServerState | None = Field(default=None, description="Its new state.")
    reason: str | None = Field(default=None, description="As LspServerStatus.reason.")


# --------------------------------------------------------------------------
# mcp.*  (M14 contract §3)
# --------------------------------------------------------------------------


class McpToolInfo(Payload):
    """One tool an MCP server exposes."""

    name: str = Field(description="Tool name as the server reports it, without the mcp__ prefix.")
    description: str = Field(default="", description="One-line description from the server.")


class McpAuthInfo(Payload):
    """OAuth standing of one MCP server (1.34.0, capability ``mcpOAuth``).

    Tokens never travel: this says only whether one is stored and usable.
    """

    required: bool = Field(
        default=False,
        description=(
            "True when the server asked for authorization (answered 401) or a sign-in "
            "is stored for it."
        ),
    )
    state: McpAuthState = Field(
        default="none",
        description=(
            "ok (tokens stored for this URL), needs_auth (the server answered 401 and a "
            "refresh did not help: call mcp.authorize) or none."
        ),
    )
    clientId: str | None = Field(
        default=None, description="The entry's configured oauth.clientId, when it has one."
    )


class McpOAuthFields(Payload):
    """``oauth`` of a ``.mcp.json`` entry (1.34.0)."""

    clientId: str | None = Field(
        default=None,
        description=(
            "Client ID registered with the server's authorization server; needed when it "
            "offers no dynamic client registration (RFC 7591)."
        ),
    )
    clientSecret: str | None = Field(
        default=None,
        description=(
            "Secret of a confidential client; write it as ${NAME} to keep it in "
            "credentials.json. Never reported back."
        ),
    )
    scope: str | None = Field(
        default=None,
        description="Scopes to ask for, space separated; default: what the server advertises.",
    )


class McpServerInfo(Payload):
    """One configured MCP server, from whichever scope declares it."""

    name: str = Field(description="Server name; the key under mcpServers.")
    scope: McpScope = Field(description="project, global, plugin or settings.")
    transport: McpTransport = Field(description="stdio, http or sse.")
    command: str | None = Field(default=None, description="Executable, for a stdio server.")
    args: list[str] = Field(default_factory=list, description="Arguments, argv style.")
    url: str | None = Field(default=None, description="Endpoint, for an http or sse server.")
    envKeys: list[str] = Field(
        default_factory=list,
        description="Names of the environment variables set for the server; never their values.",
    )
    headerKeys: list[str] = Field(
        default_factory=list,
        description="Names of the HTTP headers sent to the server; never their values.",
    )
    cwd: str | None = Field(default=None, description="Working directory for a stdio server.")
    permission: PermissionTag = Field(
        default="network", description="Permission tag every tool of this server is judged by."
    )
    disabled: bool = Field(
        default=False, description="True when the entry is kept but never started."
    )
    state: McpState = Field(default="stopped", description="stopped, starting, ready or error.")
    error: str | None = Field(default=None, description="Why the last start failed.")
    toolCount: int = Field(default=0, description="Tools the server contributed after filtering.")
    tools: list[McpToolInfo] = Field(
        default_factory=list, description="The tools themselves; filled once the server is ready."
    )
    plugin: str | None = Field(
        default=None, description="Plugin that brings a plugin-scoped entry."
    )
    timeoutSec: float | None = Field(default=None, description="Startup cap override, in seconds.")
    toolTimeoutSec: float | None = Field(
        default=None, description="Per-call cap override, in seconds."
    )
    toolsInclude: list[str] = Field(
        default_factory=list, description="Only these tools are registered, when set."
    )
    toolsExclude: list[str] = Field(
        default_factory=list, description="These tools are never registered."
    )
    shadowed: bool = Field(
        default=False,
        description=(
            "True for a declaration another scope overrides under the same name (a "
            "global entry hidden by a project one, 1.33.0): listed so a client knows "
            "the name is taken there too, but never started."
        ),
    )
    auth: McpAuthInfo = Field(
        default_factory=McpAuthInfo,
        description=(
            "OAuth standing (1.34.0): needs_auth when the server answered 401, so a "
            "client can offer a sign-in instead of showing a generic error."
        ),
    )


class McpListParams(Payload):
    """``mcp.list`` — the workdir decides which project file is read."""

    sessionId: str | None = Field(default=None, description="Session whose workdir to read.")
    workdir: str | None = Field(
        default=None, description="Project directory; defaults to the session's, then the daemon's."
    )
    includeShadowed: bool = Field(
        default=False,
        description=(
            "Also list each declaration another scope overrides under the same name, "
            "marked shadowed: true (1.33.0). Off by default, so a client keyed by name "
            "keeps one row per server."
        ),
    )


class McpListResult(Payload):
    servers: list[McpServerInfo] = Field(
        default_factory=list,
        description=(
            "One row per configured server, project entries winning; with "
            "includeShadowed, each declaration that loses to another scope follows its "
            "winner with shadowed: true (1.33.0)."
        ),
    )


class McpEntryFields(Payload):
    """The entry keys ``mcp.add`` and ``mcp.update`` share (M14 §1b)."""

    type: McpTransport | None = Field(
        default=None, description="Force a transport instead of inferring it."
    )
    command: str | None = Field(default=None, description="Executable for a stdio server.")
    args: list[str] | None = Field(
        default=None, description="Arguments, argv style; never a shell string."
    )
    env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Environment for the child process. mcp.update, and mcp.test on a saved "
            "server, merge it key by key into the saved env (1.33.0); envRemove deletes keys."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Endpoint for an http or sse server. From a remote web login (1.55.0): a NEW "
            "url server may be added in the global scope (the project .mcp.json is a control "
            "file: protected, details.reason local_only, for mcp.add, update and remove), "
            "without any ${VAR} in its fields (jarvis_person_only, "
            "details {reason: 'local_only', what: 'variable'}); the url, headers and oauth "
            "of a SAVED server are a local person's to change (what: 'target') — in mcp.add "
            "with replace, mcp.update, mcp.test with overrides and settings.set "
            "mcp.servers.*. An entry a remote login adds is saved with savedBy: "
            "'remote_login' and never expands a credentials.json value. No entry expands a "
            "SNOWPEA_* environment variable."
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "HTTP headers sent with every request. mcp.update, and mcp.test on a saved "
            "server, merge them key by key into the saved headers (1.33.0); headersRemove "
            "deletes keys."
        ),
    )
    cwd: str | None = Field(default=None, description="Working directory for a stdio server.")
    disabled: bool | None = Field(
        default=None, description="Keep the entry but never start the server."
    )
    timeoutSec: float | None = Field(default=None, description="Startup cap, in seconds.")
    toolTimeoutSec: float | None = Field(default=None, description="Per-call cap, in seconds.")
    toolsInclude: list[str] | None = Field(
        default=None,
        description=(
            "Register only these tools of the server. [] clears the filter; null or "
            "absent leaves it as saved."
        ),
    )
    toolsExclude: list[str] | None = Field(
        default=None,
        description=(
            "Never register these tools of the server. [] clears the filter; null or "
            "absent leaves it as saved."
        ),
    )
    permission: PermissionTag | None = Field(
        default=None, description="Permission tag for the server's tools; stored in settings."
    )
    oauth: McpOAuthFields | None = Field(
        default=None,
        description=(
            "OAuth client settings for an http/sse server (1.34.0); mcp.update merges them "
            "key by key."
        ),
    )


class McpAddParams(McpEntryFields):
    """``mcp.add`` — write one entry into a project or global ``.mcp.json``."""

    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    name: str = Field(description="Server name; ^[a-zA-Z0-9_-]{1,64}$.")
    scope: McpWritableScope = Field(
        default="global",
        description=(
            "Which file to write: $SNOWPEA_HOME/.mcp.json (global, the default since "
            "1.23.0, so every project sees it) or the project's .mcp.json."
        ),
    )
    workdir: str | None = Field(default=None, description="Project directory for scope=project.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")
    preset: str | None = Field(
        default=None, description="Catalog id copied before the explicit fields are applied."
    )
    test: bool = Field(
        default=True, description="Probe the server before saving; nothing is written if it fails."
    )
    acceptFindings: bool = Field(
        default=False,
        description=(
            "Add the entry despite the security findings mcp_unsafe reported (1.33.0). "
            "Does not let it overwrite a name already declared in the file."
        ),
    )
    replace: bool = Field(
        default=False,
        description=(
            "Overwrite an entry already declared under this name in the target file "
            "instead of answering mcp_exists (1.33.0). Does not accept security findings."
        ),
    )
    force: bool = Field(
        default=False,
        description="acceptFindings and replace together, as before 1.33.0.",
    )


class McpAddResult(Payload):
    ok: bool = Field(default=True, description="True when the entry was written.")
    path: str = Field(description="File the entry was written to.")
    state: McpState = Field(default="stopped", description="State of the server after the write.")
    tools: list[McpToolInfo] = Field(
        default_factory=list, description="Tools the probe found, so a client can offer a picker."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Security findings that --force accepted."
    )
    error: str | None = Field(default=None, description="Why the server did not start.")
    auth: McpAuthInfo = Field(
        default_factory=McpAuthInfo,
        description=(
            "OAuth standing (1.34.0). A probe that met 401 still writes the entry, with "
            "state needs_auth, so the client can go on to mcp.authorize."
        ),
    )


class McpRemoveParams(Payload):
    """``mcp.remove`` — drop one entry; the runtime follows whichever copy now wins.

    Removing a copy another scope shadows leaves the running server alone;
    removing the winner starts the copy it shadowed, or stops the server when
    there is none (1.33.0).
    """

    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    name: str = Field(description="Server name.")
    scope: McpWritableScope | None = Field(
        default=None, description="Which file to rewrite; omitted, the one that declares it."
    )
    workdir: str | None = Field(default=None, description="Project directory for scope=project.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")


class McpUpdateParams(Payload):
    """``mcp.update`` — merge a patch into an existing entry.

    The server is restarted only when the copy in effect changed: editing a copy
    another scope shadows writes the file and nothing else (1.33.0).
    """

    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    name: str = Field(description="Server name.")
    scope: McpWritableScope | None = Field(
        default=None, description="Which file to rewrite; omitted, the one that declares it."
    )
    workdir: str | None = Field(default=None, description="Project directory for scope=project.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")
    patch: McpEntryFields = Field(
        default_factory=McpEntryFields,
        description=(
            "Keys to change; anything absent or null is kept. env and headers merge key "
            "by key (1.33.0), except that a new url with no headers drops the saved "
            "headers; toolsInclude/toolsExclude [] clears that filter."
        ),
    )
    acceptFindings: bool = Field(
        default=False,
        description=(
            "Save the entry although the patch brings in a security finding; without "
            "it that is mcp_unsafe, as in mcp.add (1.33.0)."
        ),
    )
    force: bool = Field(default=False, description="Same as acceptFindings here (1.33.0).")
    envRemove: list[str] | None = Field(
        default=None,
        description="Environment variables to delete from the saved entry (1.33.0).",
    )
    headersRemove: list[str] | None = Field(
        default=None, description="HTTP headers to delete from the saved entry (1.33.0)."
    )
    unset: list[McpUnsetKey] | None = Field(
        default=None,
        description=(
            "Optional entry keys to clear, e.g. ['cwd', 'timeoutSec'] (1.33.0). Applied "
            "after the patch, as envRemove and headersRemove are, so a key named here is "
            "gone even if the patch also set it."
        ),
    )


class McpTestParams(McpEntryFields):
    """``mcp.test`` — probe a saved server or an unsaved draft."""

    actor: Actor | None = Field(default=None, description=ACTOR_DESCRIPTION)
    name: str | None = Field(default=None, description="Saved server to probe.")
    envRemove: list[str] | None = Field(
        default=None,
        description=(
            "Probe without these saved environment variables, as mcp.update would (1.33.0)."
        ),
    )
    headersRemove: list[str] | None = Field(
        default=None,
        description="Probe without these saved HTTP headers, as mcp.update would (1.33.0).",
    )
    unset: list[McpUnsetKey] | None = Field(
        default=None,
        description="Probe with these optional keys cleared, as mcp.update would (1.33.0).",
    )
    scope: McpScope | None = Field(
        default=None,
        description=(
            "Scope of the saved server. Given, that scope's declaration is the one probed "
            "or merged onto, not whichever wins; when it has none the draft is probed on "
            "its own (1.33.0)."
        ),
    )
    workdir: str | None = Field(default=None, description="Project directory for scope=project.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")


class McpTestResult(Payload):
    ok: bool = Field(description="True when the server answered tools/list.")
    state: McpState = Field(description="ready when the probe succeeded, error otherwise.")
    tools: list[McpToolInfo] = Field(default_factory=list, description="What the server exposes.")
    error: str | None = Field(default=None, description="Spawn or protocol error, verbatim.")
    elapsedMs: int = Field(default=0, description="How long the probe took.")
    auth: McpAuthInfo = Field(
        default_factory=McpAuthInfo,
        description="OAuth standing the probe found: needs_auth when it was refused with 401.",
    )


class McpReloadParams(Payload):
    """``mcp.reload`` — restart one server, or every configured one."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str | None = Field(
        default=None, description="Server to restart; all of them when absent."
    )
    workdir: str | None = Field(default=None, description="Project directory to rediscover from.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")


class McpReloadResult(Payload):
    ok: bool = Field(default=True, description="True when the reload ran.")
    servers: list[str] = Field(default_factory=list, description="Servers that were restarted.")


class McpCatalogEntry(Payload):
    """One curated server a client can offer as a starting point (M14 §3)."""

    id: str = Field(description="Catalog id, e.g. 'github'.")
    label: str = Field(description="Human name.")
    description: str = Field(default="", description="What the server does.")
    transport: McpTransport = Field(description="stdio, http or sse.")
    entry: dict[str, Any] = Field(
        default_factory=dict, description="The .mcp.json entry this preset writes."
    )
    needs: list[str] = Field(
        default_factory=list,
        description=(
            "Variables the user must supply. The entry refers to them as ${NAME} (1.34.0), "
            "so the value can live in credentials.json or the daemon's environment, or a "
            "client may write it into the entry instead."
        ),
    )
    homepage: str = Field(default="", description="Where the server is documented.")
    auth: McpCatalogAuth = Field(
        default="none",
        description=(
            "How the server authenticates (1.34.0): oauth (sign in with mcp.authorize "
            "after adding it), token (a key listed in needs) or none."
        ),
    )


class McpCatalogResult(Payload):
    entries: list[McpCatalogEntry] = Field(
        default_factory=list, description="Curated servers, in display order."
    )


class McpChangedNotification(Payload):
    """``mcp.changed`` — one server moved, or was added, updated or removed."""

    name: str = Field(description="Server name.")
    scope: McpScope = Field(default="project", description="Scope the server is declared in.")
    state: McpState = Field(description="stopped, starting, ready or error.")
    toolCount: int = Field(default=0, description="Tools the server currently contributes.")
    error: str | None = Field(default=None, description="Why the server is in the error state.")
    removed: bool = Field(default=False, description="True when the entry itself is gone.")
    auth: McpAuthInfo | None = Field(
        default=None, description="OAuth standing after the transition (1.34.0)."
    )


class McpAuthorizeParams(Payload):
    """``mcp.authorize`` — start an OAuth sign-in for an http/sse server (1.34.0)."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str = Field(description="Server name.")
    workdir: str | None = Field(default=None, description="Project directory that declares it.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")
    scope: McpWritableScope | None = Field(
        default=None,
        description=(
            "Which declaration signs in: the global or the project entry of this name. "
            "Omitted, the one in effect; but when a project or plugin entry shadows a "
            "global one with another URL, that is invalid_params and scope is required, "
            "so a cloned project cannot collect the sign-in meant for the global server."
        ),
    )
    oauthScope: str | None = Field(
        default=None,
        description=(
            "OAuth scopes to ask for; default: the entry's oauth.scope, then the server's "
            "WWW-Authenticate scope, then its advertised scopes_supported."
        ),
    )
    redirectUri: str | None = Field(
        default=None,
        description=(
            "Redirect URI the host catches itself (its own tab) and hands back with "
            "mcp.authorize.complete: http on 127.0.0.1, localhost or [::1] (any port), "
            "https, or a private-use scheme; never javascript:, data:, file: or a "
            "fragment (invalid_params). Omitted, the daemon listens on "
            "http://127.0.0.1:<random port>/callback and completes on its own."
        ),
    )


class McpAuthorizeResult(Payload):
    url: str = Field(description="Consent URL to open in a browser.")
    state: str = Field(description="Opaque flow id; mcp.authorize.complete takes it back.")
    expiresAt: str = Field(description="When the flow stops waiting, ISO-8601 UTC.")
    redirectUri: str = Field(
        description="Redirect URI the consent URL carries: the given one or the loopback listener."
    )
    serverUrl: str = Field(
        description="URL of the entry signing in, for the host to show before opening url."
    )
    resource: str = Field(
        description=(
            "Canonical resource (RFC 8707) the token will be for; the host can check it "
            "matches serverUrl before opening url."
        )
    )
    scope: McpScope = Field(description="Scope of the declaration signing in.")


class McpAuthorizeCompleteParams(Payload):
    """``mcp.authorize.complete`` — hand over the code the host's redirect caught (1.34.0)."""

    state: str = Field(description="The state mcp.authorize returned (the redirect's ?state=).")
    code: str | None = Field(default=None, description="The redirect's ?code=.")
    error: str | None = Field(
        default=None, description="The redirect's ?error=, when the user or server refused."
    )
    iss: str | None = Field(
        default=None,
        description=(
            "The redirect's ?iss= (RFC 9207). Checked against the authorization server's "
            "issuer; required when it advertises authorization_response_iss_parameter_supported."
        ),
    )


class McpAuthorizeCompleteResult(Payload):
    ok: bool = Field(default=True, description="True when the tokens were stored.")
    name: str = Field(description="Server the sign-in was for.")
    state: McpState = Field(
        default="stopped", description="Server state after the restart with the new token."
    )


class McpDeauthorizeParams(Payload):
    """``mcp.deauthorize`` — forget a server's OAuth tokens and client (1.34.0)."""

    actor: Literal["person"] | None = Field(
        default=None, description="Browser marker (security review 3): see ActorParams."
    )
    name: str = Field(description="Server name.")
    workdir: str | None = Field(default=None, description="Project directory that declares it.")
    sessionId: str | None = Field(default=None, description="Session whose workdir to use.")
    scope: McpWritableScope | None = Field(
        default=None,
        description=(
            "Forget the sign-in of this scope's declaration; omitted, the one in effect, "
            "or every record of the name when none is declared."
        ),
    )


class McpDeauthorizeResult(Payload):
    ok: bool = Field(default=True, description="True when the call ran.")
    removed: bool = Field(default=False, description="True when a stored sign-in was deleted.")


class McpAuthDoneNotification(Payload):
    """``mcp.auth.done`` — an ``mcp.authorize`` flow finished, either way (1.34.0)."""

    name: str = Field(description="Server name.")
    ok: bool = Field(description="True when tokens were stored and the server restarted.")
    error: str | None = Field(
        default=None,
        description=(
            "Why it failed: refused, timed out, the exchange failed, or 'cancelled' when "
            "the connection that started it closed."
        ),
    )
    state: str = Field(
        description=(
            "The flow's state, as mcp.authorize returned it; a flow another authorize "
            "replaced reports nothing, so a host matches on this."
        )
    )
    scope: McpScope = Field(description="Scope of the declaration that signed in.")


# --------------------------------------------------------------------------
# fs.* (1.40.0, capability 'fs')
# --------------------------------------------------------------------------

FsType = Literal["file", "dir", "other"]
#: Largest read or write one fs call carries.
FS_MAX_BYTES = 8 * 1024 * 1024
#: Largest listing one fs.list / fs.glob call returns.
FS_MAX_ENTRIES = 20000


class FsPathParams(Payload):
    sessionId: str = Field(
        description=(
            "Session whose roots bound the call: its workdir, its workspaceDir and its root "
            "conversation's attachments dir. A relative path is relative to the workdir."
        )
    )
    path: str = Field(description="File or directory; absolute, or relative to the workdir.")


class FsStatResult(Payload):
    path: str = Field(description="The path as resolved (symlinks in the directories followed).")
    type: FsType = Field(
        description=(
            "'file', 'dir' or 'other'. A symbolic link reports the type of its target when "
            "that target lies inside a root, else 'other'."
        )
    )
    size: int = Field(description="Bytes (the link itself for a symbolic link).")
    mtimeMs: float = Field(description="Last change, milliseconds since the epoch.")
    nlink: int = Field(description="Hard link count.")
    isLink: bool = Field(description="True when the last component is a symbolic link.")


class FsListParams(FsPathParams):
    depth: int = Field(
        default=1, ge=1, le=32, description="1 lists the directory itself; more recurses."
    )
    maxEntries: int = Field(
        default=2000, ge=1, le=FS_MAX_ENTRIES, description="Stop after this many entries."
    )


class FsEntry(Payload):
    path: str = Field(description="Path relative to the listed directory, with '/'.")
    type: FsType = Field(description="As in fs.stat.")
    size: int = Field(description="Bytes.")
    mtimeMs: float = Field(description="Last change, milliseconds since the epoch.")
    nlink: int = Field(description="Hard link count.")
    isLink: bool = Field(description="True for a symbolic link (never followed while listing).")


class FsListResult(Payload):
    path: str = Field(description="The directory as resolved.")
    entries: list[FsEntry] = Field(
        default_factory=list, description="Sorted by path, directories before their children."
    )
    truncated: bool = Field(default=False, description="True when maxEntries cut the listing.")


class FsGlobParams(Payload):
    sessionId: str = Field(description="Session whose roots bound the call (see fs.stat).")
    path: str = Field(
        default=".", description="Directory the pattern starts from; default the workdir."
    )
    pattern: str = Field(
        description=(
            "Relative glob: '*', '?', '[..]' within a name and a segment '**' for any number of "
            "directories, as in 'src/**' followed by '/*.ts'. No '..' and not absolute."
        )
    )
    maxEntries: int = Field(
        default=2000, ge=1, le=FS_MAX_ENTRIES, description="Stop after this many matches."
    )


class FsReadParams(FsPathParams):
    offset: int = Field(default=0, ge=0, description="First byte to read.")
    length: int | None = Field(
        default=None, ge=0, description="Bytes to read; omitted reads to the end of the file."
    )
    maxBytes: int = Field(
        default=FS_MAX_BYTES,
        ge=1,
        description=(
            "Refuse (too_large) a read of more than this many bytes; at most 8 MiB, a larger "
            "value is capped."
        ),
    )


class FsReadResult(Payload):
    dataBase64: str = Field(description="The bytes read, base64.")
    size: int = Field(description="Size of the whole file.")
    offset: int = Field(description="Where the bytes start.")
    length: int = Field(description="How many bytes came back.")
    mtimeMs: float = Field(description="Last change, milliseconds since the epoch.")
    eof: bool = Field(description="True when the bytes reach the end of the file.")


FsWriteMode = Literal["create", "overwrite"]


class FsWriteParams(FsPathParams):
    dataBase64: str = Field(description="The whole new content, base64; at most 8 MiB decoded.")
    mode: FsWriteMode = Field(
        description=(
            "'create' fails with 'exists' when the path is there; 'overwrite' replaces a "
            "regular file (a symbolic link or a file with other hard links is refused)."
        )
    )


class FsWriteResult(Payload):
    path: str = Field(description="The path written, as resolved.")
    size: int = Field(description="Bytes written.")
    mtimeMs: float = Field(description="Last change, milliseconds since the epoch.")
    created: bool = Field(description="True when the file did not exist before.")


class FsHashResult(Payload):
    sha256: str = Field(description="Hex SHA-256 of the whole file, read in a stream.")
    size: int = Field(description="Bytes.")
    mtimeMs: float = Field(description="Last change, milliseconds since the epoch.")


# --------------------------------------------------------------------------
# approval.evaluate (1.40.0, capability 'approvalEvaluate')
# --------------------------------------------------------------------------

ApprovalVerdict = Literal["allow", "ask", "deny"]


class ApprovalEvaluateParams(Payload):
    tool: str = Field(description="Tool the action belongs to; the allowlist is keyed by it.")
    permission: PermissionTag = Field(description="Permission class of the action.")
    site: str | None = Field(
        default=None, description="Origin of the page (https://host); matched against site rules."
    )
    mode: Mode = Field(
        default="accept",
        description="The permission mode the action would run in; the mode matrix is consulted.",
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="The action's arguments (a shell command line, a url), for rules on them.",
    )
    risk: str | None = Field(
        default=None,
        description="'payment' (or args.onceOnly): the allowlist is never consulted.",
    )


class ApprovalEvaluateResult(Payload):
    decision: ApprovalVerdict = Field(
        description="'allow', 'deny', or 'ask' (a person has to decide)."
    )
    by: Literal["mode", "allowlist", "none"] = Field(
        description="What decided: the mode matrix, a saved rule of this profile, or neither."
    )


# --------------------------------------------------------------------------
# M19 Jarvis mode (beta, 1.53.0): jarvis.*, external.*, mcp.preset.*
#
# Declared in 1.53.0 so every work package builds against one contract.  Until
# a package lands its methods answer ``not_implemented`` and its capability
# stays out of ``system.hello`` (see :data:`PENDING_CAPABILITIES`).  Design:
# docs/design/m19-jarvis-mode.md, Part 3.
# --------------------------------------------------------------------------


class JarvisBudget(Payload):
    """Jarvis's wake and token allowance (1.53.0); when exhausted everything goes to the person."""

    wakesPerHour: int = Field(description="jarvis.budget.wakesPerHour.")
    wakesThisHour: int = Field(default=0, description="Wakes in the current rolling hour.")
    dailyTokens: int = Field(description="jarvis.budget.dailyTokens; 0 means no cap.")
    tokensToday: int = Field(default=0, description="Tokens the Jarvis session used today (local).")
    exhausted: bool = Field(
        default=False,
        description="True while either limit is reached: nothing is routed to Jarvis.",
    )


class JarvisPause(Payload):
    """One session whose delegation is paused because the person stepped in (1.53.0)."""

    sessionId: str | None = Field(default=None, description="snowpea session, or null.")
    external: ExternalRef | None = Field(default=None, description="External session, or null.")
    project: str | None = Field(default=None, description="Project root of the session.")
    reason: Literal["typing", "takeover"] = Field(
        description=(
            "typing: the person sent input (returns after jarvis.quietMinutes without any); "
            "takeover: jarvis.takeover (returns at its limit or on jarvis.handback)."
        )
    )
    until: str | None = Field(
        default=None,
        description="UTC ISO time authority returns; null for a takeover without a limit.",
    )


class JarvisStatusResult(Payload):
    """``jarvis.status`` (1.53.0, capability ``jarvis``)."""

    enabled: bool = Field(description="settings jarvis.enabled.")
    running: bool = Field(
        default=False,
        description=(
            "True when the Jarvis service is up and watching sessions (enabled and started)."
        ),
    )
    sessionId: str | None = Field(
        default=None,
        description=(
            "The Jarvis session (SessionSummary.role 'jarvis'); null before it was first created."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Model profile Jarvis runs on (jarvis.model); null follows models.default.",
    )
    folder: str = Field(
        description="Jarvis's working folder (jarvis.folder, expanded); its only write root."
    )
    delegationDefault: JarvisLevel = Field(
        default="staged", description="jarvis.delegation.default: the level with no grant."
    )
    budget: JarvisBudget | None = Field(
        default=None, description="Allowance; null while not running."
    )
    paused: list[JarvisPause] = Field(
        default_factory=list, description="Sessions where the person has taken back authority."
    )
    hiddenProjects: list[str] = Field(
        default_factory=list, description="Project roots with jarvis.expose off."
    )
    problems: list[str] = Field(
        default_factory=list,
        description="Why it is not running as configured, e.g. an unknown model profile.",
    )


class JarvisEnableParams(Payload):
    model: str | None = Field(
        default=None, description="Also set jarvis.model (a models.profiles id) before starting."
    )
    folder: str | None = Field(
        default=None, description="Also set jarvis.folder before starting; created if missing."
    )


class JarvisOverviewParams(Payload):
    project: str | None = Field(default=None, description="Only this project root.")
    includeIdle: bool = Field(
        default=True, description="False leaves out idle rows with nothing pending."
    )
    includeExternal: bool = Field(default=True, description="Include external CLI sessions.")
    limit: int = Field(default=200, ge=1, le=1000, description="Most rows, newest activity first.")


class JarvisOverviewRow(Payload):
    """One supervised session (1.53.0), kept by the intake without any model call."""

    sessionId: str | None = Field(
        default=None, description="snowpea session; null for an external one."
    )
    external: ExternalRef | None = Field(
        default=None, description="External session; null for snowpea."
    )
    project: str | None = Field(default=None, description="Project root it works in.")
    title: str | None = Field(default=None, description="Session title.")
    status: Literal["idle", "running", "awaiting_approval", "awaiting_question", "error"] = Field(
        description="SessionSummary.status, or the external status."
    )
    host: ExternalHost | None = Field(
        default=None, description="For an external session: where its process lives."
    )
    mode: ExternalSessionMode | None = Field(
        default=None,
        description="For an external session snowpea runs (host snowpea or tmux): its mode.",
    )
    nativeMode: str | None = Field(
        default=None,
        description="For such a session: the CLI's own mode it runs in (1.56.0), as the row's.",
    )
    controlled: bool | None = Field(
        default=None,
        description=(
            "For an external session: as ExternalSessionRow.controlled. False (unrestricted, "
            "agy with approvalPolicy auto, cursor outside readonly): snowpea cannot gate what "
            "it runs and Jarvis's guarded-class rules do not apply inside it."
        ),
    )
    approvals: ExternalApprovals | None = Field(
        default=None,
        description=(
            "For an external session: as ExternalSessionRow.approvals ('person' for a tmux "
            "pane: its prompts are answered in the terminal, never by Jarvis)."
        ),
    )
    pendingApprovals: int = Field(default=0, description="Approvals waiting (any route).")
    pendingQuestions: int = Field(default=0, description="Questions waiting (any route).")
    waitingOnPerson: int = Field(
        default=0, description="Of those, how many are routed to the person."
    )
    level: JarvisLevel | None = Field(
        default=None, description="Delegation in force for it; null when not exposed."
    )
    exposed: bool = Field(
        default=True,
        description=(
            "False for a browser-host session while jarvis.exposeBrowser is off: listed with "
            "its status only; Jarvis cannot read its transcript or instruct it."
        ),
    )
    pausedUntil: str | None = Field(default=None, description="As SessionJarvisState.pausedUntil.")
    takenOver: bool = Field(default=False, description="As SessionJarvisState.takenOver.")
    levelScope: Literal["session", "project", "global", "default"] | None = Field(
        default=None, description="As SessionJarvisState.levelScope."
    )
    grantId: str | None = Field(default=None, description="As SessionJarvisState.grantId.")
    lastActivityAt: str | None = Field(
        default=None, description="UTC ISO time of its last activity."
    )
    digest: str = Field(
        default="",
        description="One line: what it did last or is waiting for (at most 200 characters).",
    )
    hostToolsFrom: str | None = Field(
        default=None,
        description=(
            "For a snowpea session: as SessionSummary.hostToolsFrom (1.54.0) — a browser "
            "profile's clientId when it runs on a browser host's tools, null for IDE, TUI "
            "and CLI sessions."
        ),
    )
    originSurface: str | None = Field(
        default=None,
        description=(
            "For a snowpea session: as SessionSummary.originSurface (1.54.0): the surface "
            "that owns its approvals ('gateway:<platform>:<chat>' for a messenger session, "
            "'scheduler', 'jarvis-task', a client's surface id, …)."
        ),
    )
    securityListsApply: bool | None = Field(
        default=None,
        description="For an external session: as ExternalSessionRow.securityListsApply (1.54.0).",
    )
    terminal: TerminalInfo | None = Field(
        default=None,
        description=(
            "1.73.0 (terminalSessions): a terminal row (sessionId and external null, unless "
            "the terminal runs that external session): the terminal as terminal.list shows it."
        ),
    )


class JarvisOverviewResult(Payload):
    rows: list[JarvisOverviewRow] = Field(
        default_factory=list, description="Newest activity first."
    )
    generatedAt: str = Field(description="UTC ISO time the table was taken.")
    hiddenCount: int = Field(
        default=0,
        description=(
            "1.56.0 (jarvisSessionSwitch): how many open sessions (snowpea and the external "
            "ones snowpea has open) are not shared with Jarvis — switch off, or locked "
            "hidden — so a client can say 'N sessions are not shared with Jarvis'. The rows "
            "list only sessions whose switch is on."
        ),
    )


class JarvisSessionSetParams(Payload):
    """``jarvis.session.set`` (1.56.0, capability ``jarvisSessionSwitch``)."""

    sessionId: str | None = Field(default=None, description="A snowpea session.")
    external: ExternalRef | None = Field(
        default=None, description="An external CLI session (exactly one of the two)."
    )
    enabled: bool = Field(description="On: Jarvis sees and may supervise it; off: it does not.")


class JarvisSessionSetResult(Payload):
    """The session's switch after the change."""

    sessionId: str | None = Field(default=None, description="As given.")
    external: ExternalRef | None = Field(default=None, description="As given.")
    enabled: bool = Field(description="The switch now (jarvisOn).")
    jarvisLocked: JarvisSwitchLock | None = Field(
        default=None,
        description="As SessionSummary.jarvisLocked; never set when the call succeeded.",
    )
    session: SessionSummary | None = Field(
        default=None,
        description="The snowpea session's row after the change, when it is open; else null.",
    )


class JarvisSessionChangedNotification(Payload):
    """``jarvis.session.changed`` (1.56.0): a session's Jarvis switch changed.

    Daemon-wide to people (like every ``jarvis.*`` event); clients re-read
    ``jarvis.overview`` and the session's row.
    """

    sessionId: str | None = Field(default=None, description="The snowpea session.")
    external: ExternalRef | None = Field(default=None, description="Or the external session.")
    enabled: bool = Field(description="The switch now.")
    by: Literal["person", "grant", "jarvis"] = Field(
        description=(
            "person (jarvis.session.set), grant (a session grant the person confirmed "
            "turned it on), jarvis (a session Jarvis created starts on)."
        )
    )


class JarvisDecision(Payload):
    """One ledger entry (1.53.0): a decision on behalf of a supervised session."""

    id: str = Field(description="Ledger id.")
    ts: str = Field(description="UTC ISO time it was decided.")
    sessionId: str | None = Field(default=None, description="snowpea session it was for.")
    external: ExternalRef | None = Field(default=None, description="External session it was for.")
    project: str | None = Field(default=None, description="Project root of that session.")
    kind: Literal[
        "approval", "question", "continue", "prompt", "interrupt", "escalate", "unrestricted"
    ] = Field(
        description=(
            "approval or question answered; continue (a stopped or incomplete turn was "
            "resumed); prompt or interrupt sent to the session; escalate (handed to the "
            "person); unrestricted (an external session was created, opened or switched to "
            "mode 'unrestricted' or, since 1.56.0, another personOnly native mode named in "
            "summary — always decidedBy 'person', with external and project)."
        )
    )
    importance: JarvisImportance = Field(
        default="low", description="How prominently clients show it (badge / OS / messenger)."
    )
    requestId: str | None = Field(default=None, description="Approval or question id, when one.")
    tool: str | None = Field(default=None, description="Tool of an approval.")
    summary: str = Field(
        description="What was decided, one short line (fits an OS notification, ~120 chars)."
    )
    decision: str | None = Field(
        default=None, description="'allow' or 'deny' for an approval; null for other kinds."
    )
    decidedBy: Literal["jarvis", "person", "auto"] = Field(
        description=(
            "jarvis (the model, within the engine's options), person, or auto (the engine alone)."
        )
    )
    classification: JarvisClassification | None = Field(
        default=None,
        description=(
            "Engine verdict for an approval. 'never' means never delegable: always the "
            "person's to answer (they may still allow it), not blocked."
        ),
    )
    guardedClass: GuardedClass | None = Field(
        default=None, description="With classification 'guarded'."
    )
    rule: str | None = Field(
        default=None,
        description="The engine rule that matched, e.g. 'exec:git-push' or 'tag:read'.",
    )
    readOnly: bool | None = Field(
        default=None,
        description=(
            "True on a 'risky' approval the engine judged read-only but would not allow "
            "alone (rule 'exec:read-like', 1.53.0): a read-only git form at the project's "
            "top level, an inspection from an external CLI's shell, or one with a glob, a "
            "recursive or symlink-following option or an expansion. Jarvis decides these at "
            "either level, once."
        ),
    )
    grantId: str | None = Field(default=None, description="Grant that allowed it, when one did.")
    reason: str = Field(default="", description="Why, as Jarvis or the engine stated it.")


class JarvisLedgerParams(Payload):
    sessionId: str | None = Field(default=None, description="Only decisions for this session.")
    # Spelled in ApprovalRoute's order: the generated artifacts then read the same on every Python.
    decidedBy: Literal["person", "jarvis", "auto"] | None = Field(
        default=None, description="Only decisions by this decider."
    )
    before: str | None = Field(
        default=None,
        description="Cursor from a previous page's nextBefore; omitted starts at the newest.",
    )
    limit: int = Field(default=50, ge=1, le=500, description="Entries per page.")


class JarvisLedgerResult(Payload):
    entries: list[JarvisDecision] = Field(default_factory=list, description="Newest first.")
    nextBefore: str | None = Field(
        default=None, description="Cursor for the next page; null at the end."
    )


JarvisScope = Literal["global", "project", "session"]


class JarvisGrant(Payload):
    """One delegation grant (1.53.0), kept in $SNOWPEA_HOME/jarvis/delegation.db, not settings."""

    id: str = Field(description="Grant id.")
    scope: JarvisScope = Field(description="global, project or session; the most specific wins.")
    target: str | None = Field(
        default=None,
        description=(
            "Project root for 'project'; session id (or 'ext:<cli>:<id>') for 'session'; null for "
            "'global'."
        ),
    )
    level: JarvisLevel = Field(description="staged or full.")
    allow: list[GuardedClass] = Field(
        default_factory=list,
        description=(
            "Guarded classes Jarvis may decide under this grant; never on a global grant in v1."
        ),
    )
    grantedBy: str = Field(
        description="Surface of the person who confirmed it ('ide', 'tui', 'web', 'telegram', ...)."
    )
    proposedBy: Literal["person", "jarvis"] = Field(
        default="person", description="jarvis when it came from Jarvis's delegation_set proposal."
    )
    grantedAt: str = Field(description="UTC ISO time.")
    expiresAt: str | None = Field(
        default=None,
        description=(
            "UTC ISO expiry; null means until revoked (a session grant also ends with its "
            "session)."
        ),
    )
    note: str = Field(default="", description="Free text the person or proposal gave.")
    remoteLogin: bool = Field(
        default=False,
        description=(
            "1.56.0: given from a remote web login (re-authenticated with the remote password, "
            "settings jarvis.delegation.remoteGrant on): session or project, no guarded "
            "classes, ends within 24 h."
        ),
    )


class JarvisDelegationListParams(Payload):
    scope: JarvisScope | None = Field(default=None, description="Only this scope.")
    target: str | None = Field(
        default=None, description="Only grants for this project root or session."
    )
    includeExpired: bool = Field(default=False, description="Also list expired and revoked grants.")


class JarvisDelegationListResult(Payload):
    grants: list[JarvisGrant] = Field(default_factory=list, description="Newest first.")
    default: JarvisLevel = Field(default="staged", description="jarvis.delegation.default.")


class JarvisDelegationSetParams(Payload):
    """Create or replace the grant for one scope and target.

    A client connection calling this IS the person: the change applies at once.
    The same change proposed by Jarvis (its ``delegation_set`` tool) never comes
    through here; it raises a person-only approval showing the exact scope, and
    nothing Jarvis reads can answer it.
    """

    scope: JarvisScope = Field(description="global, project or session.")
    target: str | None = Field(
        default=None,
        description="Required for project (a root) and session (an id); omitted for global.",
    )
    level: JarvisLevel = Field(description="staged or full.")
    allow: list[GuardedClass] = Field(
        default_factory=list,
        description="Guarded classes to hand over; refused (invalid_params) on a global grant.",
    )
    expiresAt: str | None = Field(
        default=None,
        description=(
            "UTC ISO expiry. Omitted: a session grant lasts while the session does, a project "
            "grant 24 h."
        ),
    )
    note: str = Field(default="", description="Shown in the grants list and the ledger.")
    reauth: str | None = Field(
        default=None,
        description=(
            "1.56.0: the remote sign-in password again. From a remote web login a grant needs "
            "settings jarvis.delegation.remoteGrant on (a local person's switch) and this: "
            "scope session or project, no allow, expiresAt within 24 h (default 24 h). Checked "
            "like a sign-in and counted with its failures (details.reason reauth_required, "
            "reauth_failed, reauth_rate_limited, reauth_unavailable for a login made with the "
            "access link); never stored, logged or echoed. Ignored from a local connection."
        ),
    )


class JarvisDelegationSetResult(Payload):
    grant: JarvisGrant = Field(description="The grant now in force.")
    replaced: str | None = Field(default=None, description="Id of the grant it replaced, if any.")


class JarvisDelegationRevokeParams(Payload):
    id: str = Field(description="Grant to revoke.")


class JarvisDelegationRevokeResult(Payload):
    revoked: bool = Field(description="False when the grant was already gone or expired.")


class JarvisSessionTarget(Payload):
    """A supervised session: exactly one of sessionId and external."""

    sessionId: str | None = Field(default=None, description="snowpea session.")
    external: ExternalRef | None = Field(default=None, description="External session.")


class JarvisTakeoverParams(JarvisSessionTarget):
    minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
        description="Hand back automatically after this long; omitted holds until jarvis.handback.",
    )


class JarvisPauseResult(Payload):
    state: SessionJarvisState = Field(description="The session's Jarvis state after the call.")


JarvisBriefingSection = Literal["sessions", "waiting", "todos", "mail", "calendar"]


class JarvisBriefing(Payload):
    """Jarvis's periodic briefing (1.53.0); off until the person asks for it."""

    enabled: bool = Field(default=False, description="jarvis.briefing.enabled.")
    schedule: str = Field(default="0 9 * * 1-5", description="Cron expression, daemon-local time.")
    instructions: str = Field(
        default="", description="What to cover, as the person said it (adjusted in conversation)."
    )
    timezone: str | None = Field(
        default=None, description="IANA zone the schedule is read in; null = the daemon's."
    )
    sections: list[JarvisBriefingSection] = Field(
        default_factory=lambda: ["sessions", "waiting", "todos", "mail", "calendar"],
        description=(
            "What it covers; mail and calendar only count while a mail or calendar preset "
            "is connected."
        ),
    )
    jobId: str | None = Field(
        default=None, description="Scheduler job that runs it, while enabled."
    )
    nextRunAt: str | None = Field(default=None, description="UTC ISO time of the next run.")


class JarvisBriefingSetParams(Payload):
    enabled: bool | None = Field(default=None, description="Omitted keeps the current value.")
    schedule: str | None = Field(default=None, description="Cron expression; omitted keeps it.")
    instructions: str | None = Field(default=None, description="Omitted keeps it.")
    timezone: str | None = Field(
        default=None, description="IANA zone; '' clears it (daemon-local); omitted keeps it."
    )
    sections: list[JarvisBriefingSection] | None = Field(
        default=None, description="Omitted keeps them."
    )


class JarvisStateNotification(Payload):
    """Daemon-wide (1.53.0): Jarvis mode changed; sent to every authenticated connection."""

    reason: Literal[
        "enabled", "disabled", "started", "stopped", "budget", "paused", "resumed", "settings"
    ] = Field(description="What changed.")
    status: JarvisStatusResult = Field(description="jarvis.status after the change.")


class JarvisDecisionNotification(Payload):
    """Daemon-wide (1.53.0): one new ledger entry, for 'Decided by Jarvis' panels."""

    entry: JarvisDecision = Field(description="The decision.")


class JarvisEscalationNotification(Payload):
    """Daemon-wide (1.53.0): something needs the person; also posted to the messenger."""

    id: str = Field(description="Escalation id (repeats of the same need keep it).")
    ts: str = Field(description="UTC ISO time.")
    sessionId: str | None = Field(default=None, description="snowpea session concerned.")
    external: ExternalRef | None = Field(default=None, description="External session concerned.")
    project: str | None = Field(default=None, description="Its project root.")
    title: str | None = Field(default=None, description="Its title.")
    reason: Literal[
        "guarded", "risky", "never", "question", "finished", "error", "stalled", "budget", "jarvis"
    ] = Field(
        description=(
            "guarded/risky/never: an approval the engine routed to the person ('never': never "
            "delegable, the person may still allow it); question: a "
            "question routed to the person; finished/error/stalled: a turn ended that way in a "
            "fully delegated session; budget: Jarvis is out of allowance; jarvis: Jarvis chose "
            "to ask the person."
        )
    )
    summary: str = Field(
        description="One short line for the person (fits an OS notification, ~120 chars)."
    )
    importance: JarvisImportance = Field(
        default="high",
        description=(
            "How prominently to show it: in-app badge always, OS notification from normal "
            "(jarvis.notify.os), messenger from high (jarvis.notify.messenger)."
        ),
    )
    requestKind: Literal["approval", "question"] | None = Field(
        default=None, description="Kind of the waiting request, when one."
    )
    requestId: str | None = Field(
        default=None, description="Its id, to answer with approval.respond / question.respond."
    )
    guardedClass: GuardedClass | None = Field(default=None, description="With reason 'guarded'.")
    kind: Literal["tamper"] | None = Field(
        default=None,
        description=(
            "1.55.0 (additive): 'tamper' when something outside snowpea changed Jarvis's "
            "delegation rules on disk and snowpea kept its own (reason stays 'error'); a "
            "machine-readable marker so clients need not match the wording. Absent from the "
            "event otherwise (null in jarvis.escalations.list rows)."
        ),
    )


class JarvisEscalationsListParams(Payload):
    """``jarvis.escalations.list`` (1.53.0): the open escalations, newest first."""

    before: str | None = Field(
        default=None,
        description="Cursor from a previous page's nextBefore; omitted starts at the newest.",
    )
    limit: int = Field(default=50, ge=1, le=500, description="Escalations per page.")


class JarvisEscalationsListResult(Payload):
    escalations: list[JarvisEscalationNotification] = Field(
        default_factory=list,
        description=(
            "Open escalations that are not a waiting approval or question (finished, error, "
            "stalled, budget, jarvis), as they were sent; kept in $SNOWPEA_HOME/jarvis/ so "
            "they survive a client reload and a daemon restart until dismissed."
        ),
    )
    nextBefore: str | None = Field(
        default=None, description="Cursor for the next page; null at the end."
    )


class JarvisEscalationsDismissParams(Payload):
    """``jarvis.escalations.dismiss`` (1.53.0)."""

    ids: list[str] = Field(default_factory=list, description="Escalation ids to dismiss.")
    all: bool = Field(default=False, description="Dismiss every open escalation.")


class JarvisEscalationsDismissResult(Payload):
    dismissed: list[str] = Field(
        default_factory=list, description="Ids that were open and are now dismissed."
    )


class JarvisEscalationsDismissedNotification(Payload):
    """Daemon-wide (1.53.0): escalations a client dismissed, so others drop them too."""

    ids: list[str] = Field(description="Dismissed escalation ids.")


class JarvisAccessNotification(Payload):
    """Daemon-wide (1.53.0): Jarvis read or instructed a session (an audit line).

    Sent for every Jarvis tool call that reads a session's transcript or acts on
    it; the session's own surfaces (its origin connection, a browser host for a
    browser-owned session) show it as an audit line.  Also kept in the ledger as
    an ``access`` record.
    """

    id: str = Field(description="Ledger record id.")
    ts: str = Field(description="UTC ISO time.")
    sessionId: str = Field(
        description=(
            "Session Jarvis read or instructed; 'ext:<cli>:<id>' for an external session "
            "(see external)."
        )
    )
    external: ExternalRef | None = Field(
        default=None, description="The external session, when it was one."
    )
    project: str | None = Field(default=None, description="Its project root.")
    hostToolsFrom: str | None = Field(
        default=None,
        description="The session's owning client (browser host), when it has one.",
    )
    action: Literal[
        "read", "prompt", "steer", "interrupt", "create", "approval", "question"
    ] = Field(
        description=(
            "read (transcript or digest), prompt, steer, interrupt, create (a new session), "
            "approval or question (answered one)."
        )
    )
    summary: str = Field(default="", description="One short line: what was read or sent.")


class JarvisDelegationChangedNotification(Payload):
    """Daemon-wide (1.53.0): a grant was set, revoked or expired."""

    reason: Literal["set", "revoked", "expired"] = Field(description="What happened.")
    grant: JarvisGrant = Field(description="The grant as it is now.")


class JarvisProject(Payload):
    """What a project is called in conversation (1.53.0), kept in the Jarvis store."""

    root: str = Field(description="Absolute project root.")
    name: str = Field(
        description="Display name: as renamed, else the workspace title, else the folder name."
    )
    aliases: list[str] = Field(
        default_factory=list, description="Other names Jarvis and clients accept for it."
    )
    exposed: bool = Field(default=True, description="Project setting jarvis.expose.")
    named: bool = Field(
        default=False, description="True when the name was set with jarvis.project.rename."
    )


class JarvisProjectListResult(Payload):
    projects: list[JarvisProject] = Field(
        default_factory=list, description="Every project snowpea knows, by name."
    )


class JarvisProjectRenameParams(Payload):
    root: str = Field(description="Project root.")
    name: str | None = Field(
        default=None, description="New display name; null or empty returns to the default."
    )


class JarvisProjectAliasParams(Payload):
    root: str = Field(description="Project root.")
    add: list[str] = Field(default_factory=list, description="Aliases to add.")
    remove: list[str] = Field(default_factory=list, description="Aliases to remove.")


class JarvisProjectResult(Payload):
    project: JarvisProject = Field(description="The project after the change.")


# --------------------------------------------------------------------------
# M37 terminal sessions, CLI hook integrations, web tunnel (1.73.0)
# --------------------------------------------------------------------------
# Additive only: every method sits behind a capability (``PENDING_CAPABILITIES``
# until its work package lands) and older clients never see any of it.  Design:
# docs/design/m37-terminal-sessions.md section 2.13.

TerminalProfileId = Literal["shell", "snowpea", "claude", "codex", "agy", "cursor"]
TerminalState = Literal["idle", "working", "done", "blocked", "error"]
TerminalStateKind = Literal["permission", "question", "auth"]
TerminalStatusSource = Literal["osc", "screen", "hook", "rules", "none"]
TerminalAttachMode = Literal["interactive", "observe"]
IntegrationCli = Literal["claude", "codex", "cursor", "agy"]
IntegrationState = Literal["not-installed", "current", "outdated", "modified"]


class TerminalOption(Payload):
    """One permission option of a profile (a CLI flag set), e.g. claude 'bypass'."""

    id: str = Field(description="Option id, e.g. 'acceptEdits', 'bypass', 'yolo'.")
    label: str = Field(description="Short label for a checkbox.")
    args: list[str] = Field(default_factory=list, description="argv appended for this option.")
    dangerous: bool = Field(
        default=False,
        description=(
            "Skips the CLI's permission prompts or sandbox. Needs terminal.create "
            "confirmDangerous from a human connection."
        ),
    )


class TerminalProfile(Payload):
    id: TerminalProfileId = Field(description="Profile id.")
    label: str = Field(description="Display name.")
    installed: bool = Field(description="The CLI (or shell) was found on this machine.")
    path: str | None = Field(default=None, description="Resolved executable, when installed.")
    resumable: bool = Field(default=False, description="It can start from an existing session id.")
    integration: IntegrationCli | None = Field(
        default=None, description="Hook integration id of this CLI, if it has one."
    )
    options: list[TerminalOption] = Field(
        default_factory=list, description="Permission options the launch dialog offers."
    )
    dangerousRefusal: Literal["max_mode", "terminal_locked", "person_only"] | None = Field(
        default=None,
        description=(
            "Why this connection cannot use dangerous options now: max_mode (web.remote.maxMode "
            "is below auto), terminal_locked (tunnel connection, web.tunnel.unlock first) or "
            "person_only (not a person's local connection: a remote web login, a browser, "
            "Jarvis). null: allowed (still needs confirmDangerous). Set only on profiles "
            "that have a dangerous option."
        ),
    )


class TerminalProfilesResult(Payload):
    profiles: list[TerminalProfile] = Field(default_factory=list)


class TerminalStatus(Payload):
    state: TerminalState = Field(description="What the program in the terminal is doing.")
    kind: TerminalStateKind | None = Field(
        default=None, description="Why it is blocked (permission, question, auth)."
    )
    turn: int = Field(default=0, description="Incremented when the root record leaves working.")
    stuck: bool = Field(default=False, description="Set on entering blocked or error.")
    seen: bool = Field(
        default=True, description="A person has looked since the last done (done/blocked rows)."
    )
    source: TerminalStatusSource = Field(
        default="none", description="osc (7501), screen (rules or a visible blocker), hook, rules."
    )


class TerminalExternalRef(Payload):
    cli: ExternalCli = Field(description="The external CLI running in the terminal.")
    sessionId: str | None = Field(default=None, description="Its own session id, once known.")


class TerminalInfo(Payload):
    terminalId: str = Field(description="Terminal id (stable across daemon restarts).")
    profile: TerminalProfileId = Field(description="Profile it was started with.")
    title: str = Field(default="", description="Program title (OSC 0/2) or the profile label.")
    cwd: str = Field(default="", description="Working folder.")
    cols: int = Field(default=160)
    rows: int = Field(default=48)
    status: TerminalStatus = Field(description="Current status.")
    alive: bool = Field(description="The holder and its program are running.")
    pid: int | None = Field(default=None, description="The program's pid, when alive.")
    exitCode: int | None = Field(default=None, description="Exit code once it ended.")
    parent: str | None = Field(default=None, description="Who started it, e.g. 'jarvis'.")
    labels: dict[str, str] = Field(default_factory=dict)
    external: TerminalExternalRef | None = Field(default=None)
    snowpeaSessionId: str | None = Field(
        default=None, description="The snowpea session a 'snowpea' profile terminal is attached to."
    )
    options: list[str] = Field(default_factory=list, description="Option ids it was started with.")
    dangerous: bool = Field(default=False, description="Started with a dangerous option.")
    createdAt: str = Field(default="", description="ISO time.")
    lost: bool = Field(
        default=False,
        description=(
            "The holder is gone with no exit record (a reboot): only the entry is left; "
            "an external CLI terminal can be resumed from its session id."
        ),
    )


class TerminalListParams(Payload):
    parent: str | None = Field(default=None, description="Only terminals started by this parent.")
    aliveOnly: bool = Field(default=False)


class TerminalListResult(Payload):
    terminals: list[TerminalInfo] = Field(default_factory=list)


class TerminalCreateParams(Payload):
    profile: TerminalProfileId = Field(description="Which profile to start.")
    cwd: str | None = Field(
        default=None, description="A registered project root; with noProject: the workspace."
    )
    noProject: bool = Field(default=False, description="Use the session workspace, no project.")
    options: list[str] = Field(default_factory=list, description="Ids from terminal.profiles.")
    confirmDangerous: bool = Field(
        default=False,
        description=(
            "Required for a dangerous option; human connections only, set after the person "
            "confirmed. Refused from Jarvis or any tool."
        ),
    )
    title: str | None = Field(default=None)
    cols: int | None = Field(default=None, ge=10, le=1000)
    rows: int | None = Field(default=None, ge=2, le=1000)
    prompt: str | None = Field(default=None, description="First input, through the input gate.")
    resumeSessionId: str | None = Field(
        default=None, description="The CLI's own session id to resume (claude/codex/agy/cursor)."
    )
    snowpeaSessionId: str | None = Field(
        default=None, description="Profile 'snowpea': the session to attach to."
    )
    labels: dict[str, str] = Field(default_factory=dict)
    parent: str | None = Field(default=None, description="Who starts it (Jarvis sets 'jarvis').")
    trust: bool = Field(
        default=False,
        description=(
            "As ExternalOpenParams.trust, for a CLI profile: the person's one-time "
            "confirmation that external CLIs may run in this project (else "
            "external_unavailable with details.reason 'untrusted_project')."
        ),
    )


class TerminalCreateResult(Payload):
    terminal: TerminalInfo


class TerminalIdParams(Payload):
    terminalId: str = Field(description="Terminal id.")


class TerminalAttachParams(Payload):
    terminalId: str
    mode: TerminalAttachMode = Field(
        default="interactive", description="observe never resizes and never writes."
    )
    cols: int | None = Field(default=None, ge=10, le=1000)
    rows: int | None = Field(default=None, ge=2, le=1000)


class TerminalAttachResult(Payload):
    repaint: str = Field(description="base64 ANSI that rebuilds the screen in a fresh viewer.")
    seq: int = Field(description="Sequence of the last byte included; terminal.data follows.")
    cols: int
    rows: int
    status: TerminalStatus
    title: str = Field(default="")
    info: TerminalInfo


class TerminalWriteParams(Payload):
    terminalId: str
    data: str = Field(description="base64 bytes for the PTY.")
    human: bool = Field(default=True, description="Typed by a person (updates the input gate).")


class TerminalResizeParams(Payload):
    terminalId: str
    cols: int = Field(ge=10, le=1000)
    rows: int = Field(ge=2, le=1000)


class TerminalAckParams(Payload):
    terminalId: str
    bytes: int = Field(ge=0, description="Bytes the viewer has processed (replenishes credit).")


class TerminalReadParams(Payload):
    terminalId: str
    lines: int = Field(default=60, ge=1, le=400, description="Screen plus this many history lines.")


class TerminalReadResult(Payload):
    text: str = Field(description="Plain text, no ANSI.")
    status: TerminalStatus
    title: str = Field(default="")
    alive: bool = Field(default=True)
    exitCode: int | None = Field(default=None)


class TerminalSendParams(Payload):
    terminalId: str
    text: str
    submit: bool = Field(default=True, description="Press the profile's submit key after pasting.")
    force: bool = Field(default=False, description="Send into a blocked terminal; not Jarvis.")


class TerminalSendResult(Payload):
    accepted: bool = Field(description="The program went to working within 5 s.")
    sent: bool = Field(default=True)
    reason: str | None = Field(default=None)


class TerminalKeysParams(Payload):
    terminalId: str
    keys: list[str] = Field(min_length=1, max_length=64, description="Named keys, e.g. 'Enter'.")


class TerminalWaitParams(Payload):
    terminalId: str
    timeout: float = Field(default=60, gt=0, le=180)
    output: str | None = Field(default=None, description="End when this text appears.")


class TerminalWaitResult(Payload):
    reason: Literal["left_working", "exit", "match", "timeout", "blocked"]
    status: TerminalStatus
    text: str = Field(default="")


class TerminalCloseParams(Payload):
    terminalId: str
    force: bool = Field(default=False, description="Skip the HUP step.")


class TerminalUnlockParams(Payload):
    password: str = Field(description="The web password, entered again.")


class TerminalUnlockResult(Payload):
    unlocked: bool
    viaTunnel: bool = Field(default=False, description="This connection came over the tunnel.")


class TerminalReportParams(Payload):
    """Hook report; callable only with the per-terminal token (never by Jarvis or a browser)."""

    terminalId: str
    token: str = Field(description="SNOWPEA_TERMINAL_TOKEN of that terminal.")
    event: str = Field(description="The CLI's hook event name.")
    state: TerminalState | None = None
    kind: TerminalStateKind | None = Field(
        default=None, description="With state 'blocked': permission, question or auth."
    )
    sessionId: str | None = None
    transcriptPath: str | None = None
    message: str | None = None


class TerminalDataEvent(Payload):
    """To the attached viewer connection only; never broadcast.

    ``seq`` counts the terminal's output bytes up to and including this chunk (the
    same count as ``TerminalAttachResult.seq``): a chunk follows on when
    ``seq - len(data)`` equals the previous ``seq``.  A repaint carries the count
    the repainted screen stands for.
    """

    terminalId: str
    seq: int = Field(description="Output bytes so far, through this chunk.")
    data: str = Field(default="", description="base64 PTY output.")
    repaint: bool = Field(
        default=False, description="data is a full repaint (the viewer fell too far behind)."
    )


class TerminalStatusEvent(Payload):
    terminalId: str
    status: TerminalStatus


class TerminalCreatedEvent(Payload):
    terminal: TerminalInfo


class TerminalExitEvent(Payload):
    terminalId: str
    exitCode: int | None = None
    signal: str | None = None


class TerminalTitleEvent(Payload):
    terminalId: str
    title: str


class TerminalClosedEvent(Payload):
    terminalId: str


class IntegrationFile(Payload):
    path: str = Field(description="A file the integration edits.")
    exists: bool = Field(default=False)


class IntegrationRow(Payload):
    cli: IntegrationCli
    label: str
    state: IntegrationState = Field(description="not-installed, current, outdated or modified.")
    cliInstalled: bool = Field(default=False, description="The CLI itself is on this machine.")
    version: str | None = Field(default=None, description="Installed hook version, if any.")
    files: list[IntegrationFile] = Field(default_factory=list)
    detail: str | None = Field(default=None, description="Why modified/outdated, one line.")


class IntegrationListResult(Payload):
    integrations: list[IntegrationRow] = Field(default_factory=list)


class IntegrationActionParams(Payload):
    cli: IntegrationCli | Literal["all"]


class IntegrationActionResult(Payload):
    integrations: list[IntegrationRow] = Field(default_factory=list)
    changedFiles: list[str] = Field(default_factory=list)


class IntegrationChangedEvent(Payload):
    integrations: list[IntegrationRow] = Field(default_factory=list)


class WebTunnelStatus(Payload):
    """web.tunnel.* result (1.73.0, webTunnel).

    Errors are ``unavailable`` with ``details.reason`` one of: remote_disabled, no_password,
    not_listening, bad_listen_host, no_binary, no_token, no_hostname, bad_provider,
    install_unpinned, checksum_mismatch, install_failed, terminal_locked
    (``details.unlock`` = "web.tunnel.unlock"), tunnel_dangerous.
    """

    state: Literal["off", "starting", "running", "restarting", "error"] = Field(default="off")
    provider: str = Field(default="off", description="off, cloudflare-quick, cloudflare or ngrok.")
    url: str | None = Field(default=None, description="The public URL once running.")
    error: str | None = Field(default=None)
    binary: str | None = Field(default=None, description="Resolved tunnel program path, if found.")
    installable: bool = Field(
        default=False, description="web.tunnel.install has a pinned download for this platform."
    )
    installCommand: str | None = Field(
        default=None, description="Shown instead when no checksum is pinned for the platform."
    )
    tokenSet: bool | None = Field(
        default=None, description="A token for the provider is stored (null: none needed)."
    )
    restarts: int = Field(default=0, description="Restarts since it was started.")


class WebTunnelInstallResult(WebTunnelStatus):
    installedPath: str = Field(default="", description="Where the binary was put.")


class TunnelStartParams(Payload):
    provider: Literal["cloudflare-quick", "cloudflare", "ngrok"] | None = Field(
        default=None, description="Override web.remote.tunnel.provider for this start."
    )
    save: bool = Field(default=False, description="Also store it as the provider setting.")


class TunnelStopParams(Payload):
    save: bool = Field(default=False, description="Also store provider 'off' / autoStart false.")


class TunnelSetTokenParams(Payload):
    provider: Literal["cloudflare", "ngrok"]
    token: str = Field(description="Never echoed; dropped from validation error details.")


class TunnelInstallParams(Payload):
    provider: str | None = Field(default=None, description="Default: the configured provider.")


class TunnelUnlockParams(Payload):
    password: str


class TunnelUnlockResult(Payload):
    unlocked: bool = True


class TunnelChangedEvent(Payload):
    state: Literal["off", "starting", "running", "restarting", "error"]
    provider: str = "off"
    url: str | None = None
    error: str | None = None


# -- external.* -------------------------------------------------------------


class ExternalCliCapabilities(Payload):
    streaming: bool = Field(description="Replies stream as message.delta while the CLI writes.")
    approvals: Literal["bridged", "degraded"] = Field(
        description=(
            "bridged: the CLI's permission prompts become snowpea approvals; degraded: edits "
            "are auto-allowed and everything else is blocked (Cursor)."
        )
    )
    liveView: bool = Field(
        description="A session open in another terminal can be followed live (else on refresh)."
    )
    tmux: bool = Field(
        default=False,
        description=(
            "external.create {transport: 'tmux'} works: Claude Code only, on Linux/macOS with "
            "tmux installed (beta)."
        ),
    )
    modes: list[ExternalSessionMode] = Field(
        default_factory=list,
        description=(
            "Modes it offers. agy has no 'readonly' (its plan mode still writes files); "
            "cursor offers readonly (its default), default (edits and sandboxed shell with "
            "no prompt: approvals 'degraded') and unrestricted."
        ),
    )
    steer: bool = Field(
        default=False,
        description=(
            "external.send during a running turn adds the text to that turn (Codex "
            "turn/steer) instead of answering session_busy; the same turnId comes back."
        ),
    )


class ExternalNativeMode(Payload):
    """One of a CLI's own modes, as the CLI names it (1.56.0, capability externalNativeModes).

    Discovery decides which a CLI offers (its installed --help). Clients show
    ``label`` as given plus a one-line description built from asks/writes; the words
    "read-only" only for writes 'none'.
    """

    id: str = Field(
        description=(
            "The CLI's own id: claude default|acceptEdits|plan|auto|dontAsk|bypassPermissions; "
            "codex untrusted|read-only|default|full-access; agy default|accept-edits|auto|plan|"
            "dangerously-skip-permissions; cursor ask|plan|agent|yolo."
        )
    )
    label: str = Field(description="The CLI's own name for it, short, not translated.")
    asks: ExternalNativeAsks = Field(
        description=(
            "Does the CLI stop and ask the person: always (whenever a permission is needed), "
            "sometimes (it proceeds alone and asks when unsure), never."
        )
    )
    writes: ExternalNativeWrites = Field(
        description=(
            "What it may write without asking first or after the person allowed it: none, "
            "plan (only its plan or notes file), workspace (the project), anywhere."
        )
    )
    approvals: ExternalNativeApprovals = Field(
        description="bridged: what it still asks becomes a snowpea approval; none: nothing is."
    )
    personOnly: bool = Field(
        description=(
            "Only a person may choose it, with the same gate and ledger entry as "
            "'unrestricted' (a remote web login may not); Jarvis may only ask the person."
        )
    )
    legacy: ExternalSessionMode = Field(
        description="What an old client sees in 'mode' for a session in this mode."
    )
    terminal: Literal["slash", "keys", "picker"] | None = Field(
        default=None,
        description=(
            "How a session in the person's tmux pane (externalTmux) is switched to it: slash "
            "(the CLI's slash command), keys (its mode key, Shift+Tab, until the pane shows "
            "it), picker (a slash command that opens a list, navigated entry by entry); null: "
            "not from snowpea there (grey it out)."
        ),
    )


class ExternalCliInfo(Payload):
    """One external agent CLI on the daemon host (1.53.0)."""

    id: ExternalCli = Field(description="claude, codex, agy or cursor.")
    title: str = Field(description="Display name, e.g. 'Claude Code'.")
    installed: bool = Field(
        description="An executable was found (jarvis.external.clis.<id>.path or PATH)."
    )
    enabled: bool = Field(default=True, description="jarvis.external.clis.<id>.enabled.")
    version: str | None = Field(default=None, description="Its --version, when it answered.")
    path: str | None = Field(
        default=None, description="Absolute executable path pinned at discovery."
    )
    capabilities: ExternalCliCapabilities | None = Field(
        default=None, description="What works with this version; null when not installed."
    )
    approvalPolicy: Literal["asks", "auto"] | None = Field(
        default=None,
        description=(
            "agy only: what the CLI's own settings say, which snowpea cannot change per "
            "session — asks (prompts are bridged) or auto (it runs without asking; edit work "
            "then needs mode 'unrestricted', confirmed by the person). Null for the others."
        ),
    )
    defaultMode: ExternalSessionMode = Field(
        default="default",
        description=(
            "jarvis.external.clis.<id>.defaultMode: the mode a new or continued session gets "
            "unless one is chosen ('readonly' for cursor). Set only by the person; never "
            "'unrestricted' (that mode comes only from an explicit mode in the person's request)."
        ),
    )
    pendingPath: str | None = Field(
        default=None,
        description=(
            "The resolved real path of a jarvis.external.clis.<id>.path override the person has "
            "not confirmed yet: it is not run (not even --version) and the CLI shows as not "
            "installed until external.clis.confirmPath {cli, path: pendingPath}. Null when there "
            "is no override or it is confirmed."
        ),
    )
    modes: list[ExternalSessionMode] = Field(
        default_factory=list,
        description=(
            "Modes this CLI offers, also when it is not installed (as capabilities.modes): "
            "claude and codex all three, agy default|unrestricted, cursor all three."
        ),
    )
    nativeModes: list[ExternalNativeMode] = Field(
        default_factory=list,
        description=(
            "The CLI's own modes (1.56.0), in the CLI's order: what its installed version "
            "offers (discovery decides; a mode its version lacks is not listed). Before "
            "discovery or when not installed, the modes every supported version has."
        ),
    )
    defaultNativeMode: str = Field(
        default="default",
        description=(
            "The native mode id a new or continued session gets unless one is chosen "
            "(defaultMode's mode; never a personOnly one)."
        ),
    )
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Model ids external.create {model} accepts, as the CLI lists them without "
            "spending tokens. Filled in the background: empty until the CLI answered (then "
            "external.clis.changed goes out); empty when it cannot list them."
        ),
    )
    modelsLoaded: bool = Field(
        default=False,
        description=(
            "True once the CLI answered the model listing (models may still be empty: it "
            "lists none); false while it has not answered yet, or the CLI is not usable."
        ),
    )
    defaultModel: str | None = Field(
        default=None,
        description=(
            "1.67.0 (externalModelSwitch): the entry of models the CLI uses when none is "
            "chosen, when it says (Codex); null when unknown."
        ),
    )
    problems: list[str] = Field(
        default_factory=list,
        description="Why something does not work, e.g. 'not logged in', 'unsupported version 0.9'.",
    )


class ExternalClisResult(Payload):
    clis: list[ExternalCliInfo] = Field(
        default_factory=list, description="All four, in a fixed order."
    )


class ExternalClisConfirmPathParams(Payload):
    """The person confirms a CLI path override (security review H2)."""

    cli: ExternalCli = Field(description="Which CLI.")
    path: str = Field(
        description=(
            "The resolved path the person saw (ExternalCliInfo.pendingPath); it must still be "
            "what jarvis.external.clis.<cli>.path resolves to, else invalid_params."
        )
    )


class ExternalTmux(Payload):
    """Where a ``host: tmux`` session runs, for 'tmux attach -t <session>' (1.53.0)."""

    session: str = Field(description="tmux session name.")
    pane: str = Field(description="tmux pane id, e.g. '%3'.")


class ExternalTerminalPrompt(Payload):
    """A permission prompt waiting in the person's tmux pane (1.56.0, externalTmux)."""

    recognised: bool = Field(
        description=(
            "true: snowpea read it with the CLI's own pattern and asked it as an approval "
            "(ApprovalRequest.externalTerminal); false: something waits in the terminal that "
            "snowpea does not recognise — nothing is answered for it, the person answers there "
            "(external.terminalSnapshot shows the pane)."
        )
    )
    text: str | None = Field(
        default=None,
        description="The prompt as shown (recognised only): plain, clipped, untrusted text.",
    )


class ExternalTerminal(Payload):
    """Where a session open in the person's terminal runs (1.56.0, capability externalTmux).

    Set only while snowpea can drive that terminal (the tmux bridge): the pane was found
    from the CLI process's own terminal, never from pane or transcript text.
    """

    kind: Literal["tmux", "pty"] = Field(
        description=(
            "tmux: a pane of the person's tmux. pty (1.73.0, externalPty): a terminal session "
            "snowpea runs itself (terminalId); pane, session and window are then empty."
        )
    )
    pane: str = Field(default="", description="tmux pane id, e.g. '%12' (kind tmux).")
    session: str = Field(default="", description="tmux session name (kind tmux).")
    window: str = Field(default="", description="tmux window index (kind tmux).")
    terminalId: str | None = Field(default=None, description="kind pty: the terminal's id.")
    status: TerminalStatus | None = Field(default=None, description="kind pty: its status.")
    modeState: Literal["confirmed", "unconfirmed", "unknown"] = Field(
        default="unknown",
        description=(
            "confirmed: the pane shows nativeMode; unconfirmed: snowpea switched it but the "
            "pane did not show the new mode; unknown: the pane shows no mode snowpea "
            "recognises (mode and nativeMode are then null)."
        ),
    )
    prompt: ExternalTerminalPrompt | None = Field(
        default=None, description="A permission prompt waiting in the pane, or null."
    )


class ExternalTerminalAsk(Payload):
    """ApprovalRequest.externalTerminal (1.56.0): a prompt read off a tmux pane."""

    pane: str = Field(description="tmux pane id the answer key is sent to.")
    text: str = Field(description="The prompt as shown: plain, clipped, untrusted text.")


class ExternalTerminalSnapshotResult(Payload):
    """external.terminalSnapshot (1.56.0)."""

    ref: ExternalRef = Field(description="The session.")
    pane: str = Field(description="tmux pane id it was read from.")
    text: str = Field(
        description=(
            "The pane's visible text, plain (control characters removed) and clipped to "
            "the last 8000 characters. Untrusted: show it, never act on it."
        )
    )
    capturedAt: str = Field(description="UTC ISO time.")


class ExternalSessionRow(Payload):
    """One external session (1.53.0); never a row of session.list."""

    ref: ExternalRef = Field(description="Which CLI and its session id.")
    title: str | None = Field(default=None, description="Title the CLI keeps, or the first prompt.")
    cwd: str = Field(description="Working directory the CLI recorded.")
    project: str | None = Field(
        default=None, description="Registered project root containing cwd; null when none."
    )
    updatedAt: str | None = Field(default=None, description="UTC ISO time of its last entry.")
    status: ExternalStatus = Field(description="idle, running or error.")
    host: ExternalHost = Field(
        default="none",
        description=(
            "Where its live process is: none (snowpea can continue it), terminal (view only "
            "until that process exits), tmux (view, input into the same pane) or snowpea."
        ),
    )
    canSend: bool = Field(
        default=True,
        description="external.send is accepted now: host none, snowpea, or tmux (pane running).",
    )
    approvals: ExternalApprovals = Field(
        default="none", description="How its permission prompts are answered right now."
    )
    tmux: ExternalTmux | None = Field(default=None, description="With host 'tmux': where.")
    mode: ExternalSessionMode | None = Field(
        default=None,
        description="How snowpea runs it (host 'snowpea' or 'tmux'); null when it runs elsewhere.",
    )
    nativeMode: str | None = Field(
        default=None,
        description=(
            "The CLI's own mode it runs in (1.56.0, an ExternalNativeMode id of its CLI); "
            "'mode' is that mode's legacy bucket. Null when it runs elsewhere."
        ),
    )
    terminal: ExternalTerminal | None = Field(
        default=None,
        description=(
            "The person's tmux pane snowpea drives this session through (1.56.0, "
            "externalTmux), host 'tmux'; null otherwise. A terminal that is not tmux stays "
            "host 'terminal', view only."
        ),
    )
    controllable: Literal["tmux"] | None = Field(
        default=None,
        description=(
            "'tmux' when snowpea can type into the person's terminal (external.send, "
            "external.interrupt, external.setMode through the tmux bridge); null otherwise."
        ),
    )
    controlled: bool = Field(
        default=True,
        description=(
            "False when snowpea cannot gate what it does: mode 'unrestricted', cursor outside "
            "'readonly', agy with approvalPolicy 'auto'. Clients warn; Jarvis's guarded-class "
            "rules do not apply inside it."
        ),
    )
    preview: str = Field(default="", description="Last message, one line.")
    model: str | None = Field(
        default=None,
        description=(
            "Model it last used, when recorded. Since 1.67.0 (externalModelSwitch) the model "
            "chosen with external.create {model} or external.setModel wins: what its next "
            "turn uses (null after setModel {model: null}: the CLI's default)."
        ),
    )
    securityListsApply: bool | None = Field(
        default=None,
        description=(
            "Whether the security lists can reach what it runs (1.54.0): null while both "
            "lists are empty; false when snowpea cannot see its calls (mode unrestricted or "
            "otherwise uncontrolled, prompts answered in a tmux pane, running in another "
            "terminal), so a UI can warn that blocked commands and protected folders do not "
            "apply inside it."
        ),
    )
    jarvisOn: bool | None = Field(
        default=None,
        description=(
            "The session's Jarvis switch, keyed by its ref (1.56.0, capability "
            "jarvisSessionSwitch): as SessionSummary.jarvisOn; false by default."
        ),
    )
    jarvisLocked: JarvisSwitchLock | None = Field(
        default=None, description="As SessionSummary.jarvisLocked (project_hidden only)."
    )


class ExternalListParams(Payload):
    """Sessions are loaded by working directory: one folder's, or every exposed project's."""

    clis: list[ExternalCli] | None = Field(
        default=None, description="Only these CLIs; omitted lists all four."
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Project root whose sessions to list (the CLI's recorded cwd at or under it). "
            "Omitted: the workdir of sessionId; one of cwd, sessionId, allProjects is required."
        ),
    )
    sessionId: str | None = Field(
        default=None, description="snowpea session whose workdir stands in for cwd."
    )
    allProjects: bool = Field(
        default=False,
        description="List across every exposed project (the Jarvis overview); cwd is ignored.",
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Most rows, newest first.")
    before: str | None = Field(default=None, description="Cursor from nextBefore.")


class ExternalListResult(Payload):
    sessions: list[ExternalSessionRow] = Field(default_factory=list, description="Newest first.")
    nextBefore: str | None = Field(default=None, description="Cursor for the next page.")


class ExternalTimelineEvent(Payload):
    """One timeline entry of an external session, shaped like a session event (1.53.0)."""

    seq: int = Field(description="Monotonic per external session, assigned by snowpea.")
    kind: str = Field(
        description=(
            "A session event kind (message.user, message.delta, tool.call, diff, turn.done, ...)."
        )
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="The same payload that kind has in session.event."
    )
    ts: str = Field(description="UTC ISO time.")
    turnId: str | None = Field(default=None, description="Turn it belongs to, when known.")


class ExternalOpenParams(Payload):
    """Open (subscribe to) a session; a host 'none' one is continued under snowpea."""

    ref: ExternalRef = Field(description="Session to open.")
    limit: int = Field(default=200, ge=1, le=2000, description="Newest timeline entries to return.")
    mode: ExternalSessionMode | None = Field(
        default=None,
        description=(
            "Mode when this continues a host 'none' session; omitted uses the CLI's "
            "defaultMode. 'unrestricted' only from a human connection (else jarvis_person_only)."
        ),
    )
    nativeMode: str | None = Field(
        default=None,
        description=(
            "The CLI's own mode id (1.56.0, from external.clis nativeModes); wins over mode. "
            "Unknown or not offered by this CLI: invalid_params. A personOnly mode only from "
            "a human connection (else jarvis_person_only), with a ledger entry."
        ),
    )
    transport: Literal["structured", "tmux", "pty"] | None = Field(
        default=None,
        description=(
            "'pty' (1.73.0, externalPty): run the CLI as a real terminal session "
            "(ExternalTerminal.kind 'pty', terminalId); a client that omits it keeps the "
            "structured transport. "
            "'tmux' on a Claude Code session the person runs in a tmux pane of their own "
            "(listed as host 'terminal'): adopt that pane, so snowpea can type into it (host "
            "'tmux'). The call waits for a person-only approval (tool 'tmux.adopt', "
            "personOnly, args {cli, sessionId, tmux: {session, pane}, cwd}) the person "
            "answers on any of their surfaces (approval.pending); Jarvis, the engine and "
            "unattended rules cannot. Declined or unreachable: the session stays view only. "
            "'tmux' on a host 'none' session starts it in a new tmux pane. Omitted or "
            "'structured': as before."
        ),
    )
    trust: bool = Field(
        default=False,
        description=(
            "The person's one-time confirmation that external CLIs may run in this project "
            "(they load the project's own settings and hooks). Needed only when starting a "
            "CLI in a project no person trusted yet: without it external_unavailable with "
            "details.reason 'untrusted_project' (show the confirmation, retry with trust: "
            "true). Only a person's local connection can trust; anyone else gets details.reason "
            "'untrusted_project_person_only'."
        ),
    )
    options: list[str] = Field(
        default_factory=list,
        description=(
            "1.73.0 (externalPty, transport 'pty'): permission option ids from terminal.profiles "
            "for this CLI. Dangerous ones need confirmDangerous."
        ),
    )
    confirmDangerous: bool = Field(
        default=False,
        description=(
            "1.73.0: the person confirmed the dangerous options; human connections only, "
            "refused from Jarvis or any tool."
        ),
    )


class ExternalOpenResult(Payload):
    session: ExternalSessionRow = Field(description="The session as it is now.")
    events: list[ExternalTimelineEvent] = Field(
        default_factory=list, description="Newest page of the timeline, oldest first."
    )
    hasMore: bool = Field(default=False, description="Older entries exist (external.read).")
    readOnly: bool = Field(
        default=False,
        description=(
            "True while host is 'terminal' (or 'tmux' before spike S5 confirms input): view "
            "only, external.send refused. Opening a host 'none' session continues it under "
            "snowpea, so it comes back with host 'snowpea'."
        ),
    )


class ExternalReadParams(Payload):
    ref: ExternalRef = Field(description="Session to read.")
    beforeSeq: int | None = Field(
        default=None, description="Entries older than this; omitted is the newest."
    )
    limit: int = Field(default=200, ge=1, le=2000, description="Entries per page.")


class ExternalReadResult(Payload):
    events: list[ExternalTimelineEvent] = Field(default_factory=list, description="Oldest first.")
    hasMore: bool = Field(default=False, description="Older entries exist.")


class ExternalSendParams(Payload):
    ref: ExternalRef = Field(description="Session to talk to.")
    text: str = Field(min_length=1, description="Prompt text; attachments are not supported in v1.")
    trust: bool = Field(
        default=False,
        description=(
            "As ExternalOpenParams.trust (1.54.0): continuing a host 'none' session starts the "
            "CLI, so in a project no person trusted yet the send answers external_unavailable "
            "with details.reason 'untrusted_project' (show the confirmation, retry with trust: "
            "true) or 'untrusted_project_person_only'."
        ),
    )


class ExternalSendResult(Payload):
    turnId: str = Field(description="Turn id its turn.started/turn.done events carry.")
    via: Literal["tmux"] | None = Field(
        default=None,
        description=(
            "'tmux' (1.56.0): typed into the person's tmux pane. The turn ends when the CLI "
            "was busy and is idle again, or after about 20 seconds when it never looked busy "
            "(agy: once delivered)."
        ),
    )


class ExternalRefParams(Payload):
    ref: ExternalRef = Field(description="The external session.")


class ExternalCreateParams(Payload):
    cli: ExternalCli = Field(description="Which CLI.")
    project: str = Field(description="A registered project root; anything else is refused.")
    model: str | None = Field(
        default=None, description="Model id for the CLI; validated, never a flag."
    )
    mode: ExternalSessionMode | None = Field(
        default=None,
        description=(
            "default, readonly or unrestricted; omitted uses the CLI's defaultMode. "
            "'unrestricted' only from a human connection (else jarvis_person_only); Jarvis "
            "asks the person through a person-only confirmation instead."
        ),
    )
    nativeMode: str | None = Field(
        default=None,
        description="As ExternalOpenParams.nativeMode (1.56.0): wins over mode.",
    )
    transport: Literal["structured", "tmux", "pty"] = Field(
        default="structured",
        description=(
            "pty (1.73.0, externalPty): a real terminal session; see ExternalOpenParams. "
            "structured (default): the daemon runs it over the CLI's own protocol (host "
            "'snowpea'; the only transport Jarvis-managed sessions use). tmux: in a new tmux "
            "pane (host 'tmux'), only once spike S5 confirms it and the host has tmux; else "
            "external_unavailable."
        ),
    )
    trust: bool = Field(
        default=False, description="As ExternalOpenParams.trust (untrusted_project)."
    )
    options: list[str] = Field(
        default_factory=list,
        description=(
            "1.73.0 (externalPty, transport 'pty'): permission option ids from terminal.profiles "
            "for this CLI. Dangerous ones need confirmDangerous."
        ),
    )
    confirmDangerous: bool = Field(
        default=False,
        description=(
            "1.73.0: the person confirmed the dangerous options; human connections only, "
            "refused from Jarvis or any tool."
        ),
    )


class ExternalSetModeParams(Payload):
    """Switch a session snowpea runs to another mode (1.53.0); takes effect from its next turn."""

    ref: ExternalRef = Field(description="The external session (host 'snowpea' or 'tmux').")
    mode: ExternalSessionMode | None = Field(
        default=None,
        description=(
            "default, readonly or unrestricted. 'unrestricted' only from a human connection "
            "(IDE, web, TUI, or the bound approver in a messenger); every such switch is a "
            "ledger entry. Optional since 1.56.0 when nativeMode is given; one of the two is "
            "required (else invalid_params)."
        ),
    )
    nativeMode: str | None = Field(
        default=None,
        description=(
            "As ExternalOpenParams.nativeMode (1.56.0): wins over mode; a personOnly mode "
            "only from a human connection, and every such switch is a ledger entry."
        ),
    )


class ExternalSetModelParams(Payload):
    """Choose the model a session uses from its next turn (1.67.0, externalModelSwitch)."""

    ref: ExternalRef = Field(description="The external session.")
    model: str | None = Field(
        description=(
            "A model id from external.clis models (Claude Code also takes a full model id "
            "besides its aliases); validated, never a flag. null: the CLI's default. A model "
            "the CLI does not list: invalid_params."
        )
    )


class ExternalSetModelResult(Payload):
    ref: ExternalRef = Field(description="The session.")
    model: str | None = Field(description="The model chosen; null for the CLI's default.")
    appliesNextTurn: bool | None = Field(
        default=None,
        description="true when a turn is running: it finishes on the old model.",
    )


class ExternalCreateResult(Payload):
    session: ExternalSessionRow = Field(description="The new session (already opened).")


class ExternalGetResult(Payload):
    session: ExternalSessionRow = Field(description="The session as it is now.")


class ExternalClisChangedNotification(Payload):
    """Daemon-wide (1.53.0): a row of external.clis changed (e.g. its models arrived)."""

    clis: list[ExternalCliInfo] = Field(
        default_factory=list, description="All four, as external.clis would answer now."
    )


class ExternalEventNotification(Payload):
    """One timeline entry of an opened external session (1.53.0).

    Sent only to the connections subscribed to that ref — those that called
    external.open, external.create or external.send on it, until external.unwatch,
    external.close or the connection closes — like session.event to subscribers.
    """

    ref: ExternalRef = Field(description="Session it belongs to.")
    seq: int = Field(description="Its own monotonic sequence.")
    kind: str = Field(description="A session event kind.")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Same payload as in session.event."
    )
    ts: str = Field(description="UTC ISO time.")
    turnId: str | None = Field(default=None, description="Turn it belongs to, when known.")


class ExternalStateNotification(Payload):
    """Daemon-wide (1.53.0): an external session's status or host changed.

    E.g. host terminal -> none when the person's terminal exits (the session is
    available again), none -> snowpea on external.open.
    """

    ref: ExternalRef = Field(description="Which session.")
    status: ExternalStatus = Field(description="Status now.")
    previous: ExternalStatus | None = Field(default=None, description="Status before.")
    host: ExternalHost = Field(description="Where its live process is now.")
    previousHost: ExternalHost | None = Field(default=None, description="Host before.")
    canSend: bool = Field(description="As ExternalSessionRow.canSend, now.")
    approvals: ExternalApprovals = Field(description="As ExternalSessionRow.approvals, now.")
    tmux: ExternalTmux | None = Field(default=None, description="With host 'tmux': where.")
    mode: ExternalSessionMode | None = Field(
        default=None, description="As ExternalSessionRow.mode, now."
    )
    nativeMode: str | None = Field(
        default=None, description="As ExternalSessionRow.nativeMode, now (1.56.0)."
    )
    terminal: ExternalTerminal | None = Field(
        default=None, description="As ExternalSessionRow.terminal, now (1.56.0)."
    )
    controllable: Literal["tmux"] | None = Field(
        default=None, description="As ExternalSessionRow.controllable, now (1.56.0)."
    )
    controlled: bool = Field(default=True, description="As ExternalSessionRow.controlled, now.")
    title: str | None = Field(default=None, description="Title, when known.")
    model: str | None = Field(
        default=None,
        description="As ExternalSessionRow.model, now (1.67.0, externalModelSwitch).",
    )
    error: str | None = Field(default=None, description="With status 'error': why.")


# -- mcp.preset.* -------------------------------------------------------------


class McpPresetInput(Payload):
    name: str = Field(
        description="Variable the entry refers to as ${NAME} (as McpCatalogEntry.needs)."
    )
    label: str = Field(description="Human label.")
    secret: bool = Field(default=False, description="Stored in credentials.json, never echoed.")
    required: bool = Field(default=True, description="mcp.preset.apply fails without it.")
    placeholder: str = Field(default="", description="Example value.")


class McpPresetRecipe(Payload):
    id: str = Field(description="Recipe id, unique within the preset.")
    title: str = Field(description="e.g. 'Mail digest at 08:00'.")
    schedule: str = Field(description="Cron expression, daemon-local time.")
    prompt: str = Field(
        description="Task the isolated jarvis-task worker runs; Jarvis receives only its digest."
    )
    defaultEnabled: bool = Field(default=False, description="Preselected in the apply dialog.")


class McpPresetServer(Payload):
    """One server of a preset (a preset may be several: Gmail and Calendar are two)."""

    suffix: str = Field(description="Appended to the preset's server name ('' = the name alone).")
    title: str = Field(description="Human name.")
    kind: str = Field(description="'mail', 'calendar' or 'mail+calendar'.")
    transport: McpTransport = Field(description="stdio, http or sse.")
    auth: McpCatalogAuth = Field(description="As McpPresetInfo.auth, for this server.")
    signIn: Literal["oauth", "deviceCode", "none"] = Field(
        default="none",
        description=(
            "How the person signs in: oauth (mcp.authorize), deviceCode (a command run once "
            "on the daemon's machine, see loginCommand) or none."
        ),
    )
    loginCommand: str | None = Field(default=None, description="With deviceCode: the command.")
    entry: dict[str, Any] = Field(default_factory=dict, description="The .mcp.json entry template.")
    readTools: list[str] = Field(default_factory=list, description="Tagged 'read'.")
    sendTools: list[str] = Field(default_factory=list, description="Known send/modify tools.")


class McpPresetSignIn(Payload):
    server: str = Field(description="Configured server name.")
    method: Literal["oauth", "deviceCode", "none"] = Field(description="How to sign in.")
    command: str | None = Field(default=None, description="With deviceCode: run this once.")
    note: str = Field(default="", description="What the person still has to do.")


class McpPresetInfo(Payload):
    """A one-step assistant setup on top of an MCP catalog entry (1.53.0)."""

    id: str = Field(
        description=(
            "Preset id. v1 ships 'google' (Gmail and Google Calendar) and 'microsoft365' "
            "(Outlook mail and calendar)."
        )
    )
    title: str = Field(description="Human name.")
    description: str = Field(default="", description="What it sets up.")
    category: Literal["mail", "calendar", "todo", "other"] = Field(description="Grouping.")
    auth: McpCatalogAuth = Field(
        description=(
            "As McpCatalogEntry.auth: oauth (mcp.authorize after apply), token (an input) or none."
        )
    )
    transport: McpTransport = Field(description="stdio, http or sse.")
    entry: dict[str, Any] = Field(
        default_factory=dict, description="The .mcp.json entry it writes."
    )
    inputs: list[McpPresetInput] = Field(
        default_factory=list, description="Values the person supplies."
    )
    readTools: list[str] = Field(default_factory=list, description="Tools that only read.")
    sendTools: list[str] = Field(
        default_factory=list,
        description="Tools that send or change things; tagged 'send' (guarded secret_send).",
    )
    recipes: list[McpPresetRecipe] = Field(default_factory=list, description="Schedule templates.")
    homepage: str = Field(default="", description="Where the server is documented.")
    applied: bool = Field(default=False, description="A server from this preset is configured.")
    servers: list[McpPresetServer] = Field(
        default_factory=list,
        description=(
            "The servers it writes (1.53.0, WP8). entry, transport, auth, readTools and "
            "sendTools above describe the first one / the union."
        ),
    )
    appliedServers: list[str] = Field(
        default_factory=list, description="Names of the servers it configured, once applied."
    )


class McpPresetListParams(Payload):
    locale: str | None = Field(
        default=None,
        description=(
            "Language of the human texts (title, description, input labels, server and recipe "
            "titles): 'en' (default) or 'ko'; any other value answers in English. Ids, tool "
            "names, entries and recipe prompts are never translated."
        ),
    )


class McpPresetListResult(Payload):
    presets: list[McpPresetInfo] = Field(default_factory=list, description="In display order.")
    locale: str = Field(default="en", description="The language the texts are in.")


class McpPresetCheckParams(Payload):
    id: str = Field(description="An applied preset.")


class McpPresetServerCheck(Payload):
    server: str = Field(description="Configured server name.")
    connected: bool = Field(description="Its MCP connection is up.")
    authenticated: bool | None = Field(
        default=None,
        description=(
            "Whether the sign-in holds: OAuth servers by the stored sign-in, a device-code "
            "server (Microsoft 365) by asking its read-only verify tool; null when the "
            "server offers no way to tell (or is not connected to ask)."
        ),
    )
    account: str | None = Field(
        default=None,
        description=(
            "The signed-in account, masked (e.g. 'j***@example.com'); never a token or a "
            "full account id."
        ),
    )
    detail: str = Field(default="", description="One line for the person.")


class McpPresetCheckResult(Payload):
    id: str = Field(description="The preset.")
    applied: bool = Field(description="False when the preset is not applied (servers empty).")
    servers: list[McpPresetServerCheck] = Field(default_factory=list)


class McpPresetRemoveParams(Payload):
    id: str = Field(description="Preset to remove.")


class McpPresetRemoveResult(Payload):
    removed: bool = Field(description="False when the preset was not applied.")
    servers: list[str] = Field(default_factory=list, description="Servers put back or removed.")
    jobs: list[str] = Field(default_factory=list, description="Recipe jobs deleted.")
    notice: str | None = Field(
        default=None,
        description=(
            "What the person still has to do themselves, e.g. a device-code server's own "
            "sign-in that snowpea cannot clear (show it). Null when nothing is left."
        ),
    )


class McpPresetApplyParams(Payload):
    id: str = Field(description="Preset to apply.")
    inputs: dict[str, str] = Field(
        default_factory=dict, description="Values for its inputs, by name."
    )
    recipes: list[str] = Field(
        default_factory=list, description="Recipe ids to schedule as jarvis-task jobs."
    )
    scope: McpWritableScope = Field(
        default="global", description="Where the server entry is written."
    )
    workdir: str | None = Field(default=None, description="Project folder for scope 'project'.")
    name: str | None = Field(default=None, description="Server name; default the preset id.")


class McpPresetApplyResult(Payload):
    server: str = Field(description="Name of the first configured MCP server.")
    add: McpAddResult = Field(description="What mcp.add reported for the first entry.")
    servers: list[str] = Field(
        default_factory=list, description="Every server it configured (WP8), in order."
    )
    adds: list[McpAddResult] = Field(
        default_factory=list, description="mcp.add's report for each, in the same order."
    )
    signIn: list[McpPresetSignIn] = Field(
        default_factory=list, description="What still needs a sign-in, per server."
    )
    already: bool = Field(
        default=False, description="Nothing changed: the preset was already applied the same way."
    )
    jobs: list[str] = Field(
        default_factory=list, description="Scheduler job ids created for recipes."
    )
    needsAuth: bool = Field(default=False, description="Call mcp.authorize next (auth 'oauth').")


# --------------------------------------------------------------------------
# remote web access (1.54.0, capability webRemoteControl)
# --------------------------------------------------------------------------


class WebRemoteLeft(Payload):
    """What remote web logins left on this computer that keeps acting (1.55.0)."""

    jobs: int = Field(
        default=0, description="Scheduled jobs with createdBy 'remote_login' (as remoteJobs)."
    )
    mcpServers: int = Field(
        default=0,
        description=(
            "MCP server entries marked savedBy 'remote_login' (mcp.add, mcp.update, "
            "settings.set mcp.servers), in the global .mcp.json and settings mcp.servers."
        ),
    )
    skills: int = Field(
        default=0,
        description=(
            "Global skills a remote web login saved (skill.write, skill.create, skill.learn, "
            "/skill create|learn). Memory entries are not counted (they carry no such mark)."
        ),
    )


class WebRemoteStatus(Payload):
    """The external web listener as the daemon runs it, and its settings."""

    enabled: bool = Field(description="web.remote.enabled: the listener opens at daemon start.")
    listening: bool = Field(description="The external listener is open now.")
    host: str = Field(description="Bound address (or the configured one when not listening).")
    port: int = Field(description="Bound port (or the configured one).")
    username: str = Field(default="", description="Login name; empty in access-link mode.")
    passwordSet: bool = Field(description="A password is stored (scrypt hash, credentials.json).")
    mode: Literal["token", "password"] = Field(
        description=(
            "'token': the access link (a web access token in ?t=, traded for a cookie on the "
            "first request) opens the page; 'password': a username and password are set and "
            "the login page is required — the link alone is not enough."
        )
    )
    listenUrls: list[str] = Field(
        default_factory=list,
        description="Addresses other machines open (http://<lan-ip>:<port>/web/).",
    )
    accessUrl: str | None = Field(
        default=None,
        description=(
            "The access link with its token (token mode, listening). Treat it like a password: "
            "anyone with it drives this machine; rotate it with web.remote.rotateToken."
        ),
    )
    sessions: int = Field(default=0, description="Open browser sessions through the listener.")
    needsRestart: bool = Field(
        default=False,
        description=(
            "settings.json says otherwise than what runs (enabled/host/port/username): the "
            "next daemon start will differ. web.remote.listen applies at once and can save."
        ),
    )
    plainHttp: bool = Field(default=True, description="The listener speaks plain HTTP.")
    warning: str | None = Field(default=None, description="The plain-HTTP warning to show.")
    error: str | None = Field(default=None, description="Why it is not listening, if it should.")
    turnsRunning: int = Field(
        default=0, description="Turns running now (a stop or restart would cut their watchers)."
    )
    remoteJobs: int = Field(
        default=0,
        description=(
            "Scheduled jobs a remote web login created and that still exist (job.list rows "
            "with createdBy 'remote_login', 1.55.0): they keep running after the login ends."
        ),
    )
    maxMode: Literal["plan", "accept", "auto"] = Field(
        default="auto",
        description=(
            "settings web.remote.maxMode (1.55.0): the widest mode a remote web login may "
            "use. It cannot raise a session or a job above it, nor create, prompt, steer or "
            "run one already above it: jarvis_person_only with details {reason: 'mode_raise', "
            "max: <this value>}. 'auto' is no limit. A local person changes it with "
            "settings.set; it applies at once. It stops unattended runs, not the holder of "
            "the link, who still answers each approval."
        ),
    )
    remoteLeft: WebRemoteLeft = Field(
        default_factory=WebRemoteLeft,
        description=(
            "1.55.0: what remote web logins left that keeps acting after the login ends: "
            "jobs, MCP servers and skills marked as theirs. The marks are inside files the "
            "agent's shell could rewrite: a count, not a guarantee."
        ),
    )
    settingsDegraded: list[str] = Field(
        default_factory=list,
        description="1.55.0: as system.info settingsDegraded.",
    )


class WebRemoteListenParams(Payload):
    host: str = Field(description="Address to bind (0.0.0.0, a LAN address, ::).")
    port: int = Field(
        description=(
            "Port 0-65535; 0 picks a free one. Failures (1.55.0) carry details {field: "
            "'host'|'port'|'username', reason: 'in_use'|'bad_address'|'permission'|"
            "'out_of_range'|'required'}: in_use and permission answer 'unavailable', the "
            "others 'invalid_params'. A failed move keeps the listener where it was."
        )
    )
    username: str | None = Field(
        default=None,
        description="Login name for password mode; omitted keeps the configured one.",
    )
    save: bool = Field(
        default=False,
        description="Also write web.remote.{enabled,host,port,username} for the next start.",
    )


class WebRemoteStopParams(Payload):
    save: bool = Field(default=False, description="Also set web.remote.enabled false.")


class WebRemoteSetPasswordParams(Payload):
    password: str = Field(min_length=8, max_length=1024, description="At least 8 characters.")
    username: str | None = Field(
        default=None, description="Set web.remote.username too (saved)."
    )


class WebRemoteTokenResult(Payload):
    accessUrl: str | None = Field(
        default=None, description="The new access link (when listening in token mode)."
    )


# --------------------------------------------------------------------------
# project trust (1.55.0, capability projectTrust; security review 5, C1)
# --------------------------------------------------------------------------

ProjectTrustScope = Literal[
    "hooks", "mcp", "commands", "agents", "prompts", "settings", "mode", "externalCli"
]
ProjectTrustFileState = Literal["active", "untrusted", "new", "changed"]
ProjectTrustNotPinnedReason = Literal[
    "computed", "project-script", "package-runner", "module", "missing", "too-large",
    "too-many", "unreadable", "outside-project", "symlink",
]


class ProjectTrustCommand(Payload):
    """How a command is shown on the trust card (1.55.0, security review 6)."""

    label: str = Field(default="", description="The hook's event or the server's name.")
    head: str = Field(
        description="The whole command when it is short; else its first 120 characters."
    )
    tail: str = Field(
        default="",
        description=(
            "Empty for a short command; else its last 60 characters. A card shows head, "
            "an ellipsis and tail: the end of a command is never hidden."
        ),
    )
    length: int = Field(description="The command's full length in characters.")


class ProjectTrustParams(Payload):
    workdir: str = Field(description="A folder of the project (its real path is the key).")


class ProjectTrustSetParams(Payload):
    workdir: str = Field(description="A folder of the project (its real path is the key).")
    scopes: list[ProjectTrustScope] | None = Field(
        default=None,
        description=(
            "Scopes to grant (added to what it has). Omitted: hooks, mcp, commands, agents, "
            "prompts, settings — not mode (defaultMode: auto from the project file) and not "
            "externalCli, which the person names explicitly. Every control file is pinned "
            "as digests says it is."
        ),
    )
    digests: dict[str, str] = Field(
        description=(
            "Required (1.55.0, security review 6): project.trust.status's digests object, "
            "sent back exactly as it was returned — {path relative to the project: "
            "'sha256:<hex>'} for every control file, every project file one of them runs "
            "and the project settings file. What is pinned is what the person was shown: "
            "when the project differs now (a digest differs, a file is on disk that is not "
            "in digests, a file in digests is gone) nothing is trusted and the call fails "
            "with trust_changed, details {changed: [path], added: [path], missing: [path]}. "
            "Read the status again, show it, and send the new digests. {} for a project "
            "that brings nothing."
        ),
    )
    actor: Literal["person"] | None = Field(
        default=None,
        description=(
            "1.62.0 (browserProjectTrust): a browser host sets 'person'; with personConfirm "
            "'window' it is part of what lets a browser trust a folder (see personConfirm). "
            "Ignored for other kinds."
        ),
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description=(
            "1.62.0 (browserProjectTrust): 'window' when the person confirmed in the "
            "browser's own native window. A browser connection may call project.trust.set "
            "only when all hold: actor 'person' and personConfirm 'window'; clientVersion "
            "0.4.0 or later; a local connection (not a remote login, a loopback peer); "
            "workdir is the workdir of a live session the browser owns, hosts or opened; "
            "scopes only of hooks, mcp, commands, agents, prompts, settings (never mode or "
            "externalCli). Else jarvis_person_only with details.reason browser_unconfirmed, "
            "browser_legacy, browser_remote, browser_not_owner or browser_not_allowed. "
            "digests as for any caller (trust_changed when stale). While the folder's mode "
            "scope is trusted, a browser grant keeps .snowpea/settings.json's pin as it was; "
            "a grant of settings that would need it pinned anew is refused with "
            "browser_not_allowed, details.what '.snowpea/settings.json'."
        ),
    )


class ProjectTrustRevokeParams(Payload):
    workdir: str = Field(description="A folder of the project.")
    scopes: list[ProjectTrustScope] | None = Field(
        default=None,
        description=(
            "Scopes to take back; omitted: all of them (from a browser, 1.62.0: hooks, mcp, "
            "commands, agents, prompts and settings — mode and externalCli stay a local "
            "person's)."
        ),
    )
    actor: Literal["person"] | None = Field(
        default=None,
        description="1.62.0 (browserProjectTrust): as ProjectTrustSetParams.actor.",
    )
    personConfirm: Literal["window"] | None = Field(
        default=None,
        description=(
            "1.62.0 (browserProjectTrust): as ProjectTrustSetParams.personConfirm — a browser "
            "revokes under the same rule as it trusts."
        ),
    )


class ProjectTrustFile(Payload):
    """One control file of the project (names and a short summary only, never contents)."""

    path: str = Field(description="Relative to the project root.")
    scope: ProjectTrustScope = Field(description="What it makes snowpea do.")
    state: ProjectTrustFileState = Field(
        description=(
            "active: trusted and unchanged; untrusted: its scope is not trusted; new: added "
            "since the project was trusted; changed: its content differs from what was trusted."
        )
    )
    summary: str = Field(
        default="",
        description=(
            "One line: hook commands, MCP servers, allowed-tools, … A long command is its "
            "head, ' … ', its tail and its length (1.55.0: the end is never cut off)."
        ),
    )
    digest: str = Field(
        default="",
        description="'sha256:<hex>' of the file now ('missing' / 'unreadable' otherwise).",
    )
    commands: list[ProjectTrustCommand] = Field(
        default_factory=list,
        description="The commands it runs (hooks, MCP servers), for a card that shows each.",
    )


class ProjectTrustRun(Payload):
    """A project file a hook, an MCP server or a skill runs: pinned with it (1.55.0)."""

    path: str = Field(description="Relative to the project root.")
    digest: str = Field(description="'sha256:<hex>' of the file now.")
    state: ProjectTrustFileState = Field(
        description=(
            "As for a control file. A changed or new one switches off every control file "
            "in by until the project is trusted again."
        )
    )
    by: list[str] = Field(
        default_factory=list, description="The control files (paths) that run it."
    )


class ProjectTrustNotPinned(Payload):
    """A command whose code trust cannot pin (1.55.0): the card should warn about it."""

    source: str = Field(description="The control file (path) the command is in.")
    command: ProjectTrustCommand = Field(description="The command, as shown.")
    reason: ProjectTrustNotPinnedReason = Field(
        description=(
            "computed: the command holds an expansion or substitution, so what runs is not "
            "known; project-script: npm/yarn/pnpm run, make, …: the project's own script "
            "table decides and may change; package-runner: npx/uvx/pipx run: a package "
            "resolved or fetched when it runs; module: python -m <module>; missing: it "
            "names a project file that is not there now; too-large / too-many: more than "
            "can be pinned; unreadable: the control file itself could not be read; "
            "outside-project (security review 7): it runs a file outside the project, which "
            "is not the project's to pin; symlink (security review 7): it runs a link "
            "inside the project that leads out of it."
        )
    )


class ProjectTrustStatus(Payload):
    """What a project brings, what of it is active and what changed since trust."""

    workdir: str = Field(description="The project root (real path).")
    trusted: bool = Field(description="Some scope is trusted.")
    scopes: list[ProjectTrustScope] = Field(default_factory=list)
    by: str | None = Field(
        default=None,
        description=(
            "Who trusted it (ide, tui, cli, migration; 1.62.0: 'browser:<clientId>' for a "
            "browser's confirmed grant)."
        ),
    )
    at: str | None = Field(default=None, description="When (UTC ISO).")
    files: list[ProjectTrustFile] = Field(default_factory=list)
    inactive: list[str] = Field(
        default_factory=list, description="Control files not active now (any reason)."
    )
    changed: list[str] = Field(
        default_factory=list,
        description="Files of a trusted scope that are new or changed since the trust.",
    )
    defaultModeAuto: bool = Field(
        default=False, description="The project file asks for defaultMode auto."
    )
    modeActive: bool = Field(
        default=False, description="…and the mode scope lets it apply (else accept)."
    )
    runs: list[ProjectTrustRun] = Field(
        default_factory=list,
        description=(
            "1.55.0 (security review 6): the project files the hooks, MCP servers and "
            "skills run — path-like words of a hook command or of a server's command and "
            "args that are files inside the project, and every other file in a skill's "
            "folder. Trusting pins them too; one that changes needs the project trusted "
            "again, like any pinned file."
        ),
    )
    notPinned: list[ProjectTrustNotPinned] = Field(
        default_factory=list,
        description=(
            "1.55.0: commands whose code cannot be pinned, each with a reason. Trust still "
            "covers the command line itself; the card should say that what it runs may "
            "change without asking again."
        ),
    )
    digests: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "1.55.0: what trusting the project now would pin, {path: 'sha256:<hex>'}. Send "
            "it back unchanged as project.trust.set {digests}."
        ),
    )
    limits: list[str] = Field(
        default_factory=list,
        description=(
            "1.55.0 (security review 7): what a pin does not cover, as sentences for the "
            "card — only the files listed are pinned, not what one of them loads or runs "
            "in turn."
        ),
    )


class ProjectTrustChangedNotification(Payload):
    """``project.trust.changed``: a project's trust changed (to every person's surface)."""

    workdir: str = Field(
        default="",
        description="The project root ('' for the migration, for store_lost and for tamper).",
    )
    scopes: list[ProjectTrustScope] = Field(default_factory=list)
    by: str | None = None
    reason: Literal["set", "revoke", "migration", "store_lost", "tamper"] = Field(
        default="set",
        description=(
            "store_lost (1.55.0, security review 6): the trusted-projects record is missing "
            "or unreadable, so no project is trusted until the person trusts it again; "
            "security.get.problems carries the sentence. tamper (1.55.0, security review "
            "7): something outside snowpea changed the record (or its marker) while the "
            "daemon was running; the change was NOT applied and the file was put back — "
            "nothing about any project's trust changed, the person is only told; "
            "security.get.problems carries the sentence."
        ),
    )
    projects: list[str] = Field(
        default_factory=list, description="migration: every project trusted as it was."
    )


# --------------------------------------------------------------------------
# security lists (1.54.0, capability securityLists)
# --------------------------------------------------------------------------

SecurityLevel = Literal["write", "all"]


class SecurityPresetsListParams(Payload):
    locale: str | None = Field(
        default=None,
        description=(
            "'en', 'ko', 'ja', 'es' or 'zh-CN' (ja/es/zh-CN since 1.64.0); a regional tag "
            "falls back to its language ('es-ES' -> 'es', 'zh-TW' -> 'zh'), anything else "
            "to English; default 'en'."
        ),
    )


class SecurityPreset(Payload):
    """One preset of the catalog."""

    id: str = Field(description="Stable id, e.g. 'essential', 'git-destructive', 'credentials'.")
    title: str = Field(description="Short name in the requested locale.")
    description: str = Field(description="What it covers, in the requested locale.")
    examples: list[str] = Field(
        default_factory=list,
        description="Commands it blocks (commands) or paths it covers (paths), as typed.",
    )
    default: bool = Field(description="On in a fresh install (only 'essential').")
    level: SecurityLevel | None = Field(
        default=None, description="Paths only: 'write' (no changes) or 'all' (no access)."
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "Paths only: what it protects on this machine now, as shown (~/.ssh, /etc, the "
            "registered projects, the mounted disks); the session's own project is left out "
            "when a check runs."
        ),
    )


class SecurityPresetsListResult(Payload):
    commands: list[SecurityPreset] = Field(default_factory=list)
    paths: list[SecurityPreset] = Field(default_factory=list)
    locale: str = Field(default="en", description="The locale the texts are in.")


class SecurityProtectedPathEntry(Payload):
    path: str = Field(description="Absolute path, or one starting with ~/.")
    level: SecurityLevel = Field(default="write")


class SecurityCommandList(Payload):
    presets: list[str] = Field(default_factory=list)
    custom: list[str] = Field(
        default_factory=list,
        description="Command patterns (see docs/manual/en/security.md, 'Custom patterns').",
    )


class SecurityPathList(Payload):
    presets: list[str] = Field(default_factory=list)
    custom: list[SecurityProtectedPathEntry] = Field(default_factory=list)


class SecurityLists(Payload):
    blockedCommands: SecurityCommandList = Field(default_factory=SecurityCommandList)
    protectedPaths: SecurityPathList = Field(default_factory=SecurityPathList)


class SecurityResolvedPath(Payload):
    """One protected path a preset or entry stands for right now."""

    path: str
    level: SecurityLevel
    scope: Literal["tree", "self"] = Field(
        description=(
            "'tree': the path and everything below it; 'self': the folder itself only "
            "(never deleted, moved or changed recursively, nor through a folder above it)."
        )
    )
    by: str = Field(description="'paths:<presetId>' or 'custom'.")
    exempt: list[str] = Field(
        default_factory=list, description="Folders below it that stay usable (the project)."
    )


class SecurityEffectiveCommand(Payload):
    pattern: str = Field(
        description=(
            "A pattern in force; for a preset rule the pattern language cannot spell (a "
            "download piped into a shell, rm -r of a folder holding the home) one of the "
            "preset's examples, for display."
        )
    )
    by: str = Field(description="'commands:<presetId>' or 'custom'.")


class SecurityEffective(SecurityLists):
    """The lists in force: the global ones plus the project's additions."""

    commands: list[SecurityEffectiveCommand] = Field(
        default_factory=list, description="Every command pattern in force, presets expanded."
    )
    paths: list[SecurityResolvedPath] = Field(
        default_factory=list,
        description="Every protected path, presets expanded for the given workdir.",
    )


class SecurityGetParams(Payload):
    workdir: str | None = Field(
        default=None, description="A project: adds its additions and resolves its presets."
    )
    locale: str | None = Field(default=None, description="For problems; default 'en'.")
    allProjects: bool = Field(
        default=False,
        description=(
            "1.64.0 (securityAllProjects): also list what every registered project adds "
            "(result projects, at most 200; truncated true past that). Read-only, the same "
            "gate as security.get."
        ),
    )


class SecurityProblem(Payload):
    """One unusable security-list entry, structured (1.55.0)."""

    list: Literal["commands", "paths"] | None = Field(
        description="The list it belongs to; null for the shape of the section itself."
    )
    entry: str | None = Field(
        default=None, description="The entry as written (a pattern, path or preset id), if any."
    )
    reason: str = Field(description="Why it is unusable, one short line.")
    code: str | None = Field(
        default=None,
        description=(
            "1.58.0: a stable id for a problem that is not an unusable entry; "
            "'security-disabled' while security.enabled is false (list and entry null). "
            "1.66.0: off is the default; the item stays for older clients and its reason is "
            "a neutral note (the checks are off, how to turn them on), not an error."
        ),
    )


class SecurityProjectAdditions(Payload):
    """What one registered project's .snowpea/settings.json adds (1.64.0)."""

    workdir: str = Field(description="The project's root.")
    name: str | None = Field(default=None, description="Its sidebar title, when it has one.")
    blockedCommands: SecurityCommandList = Field(default_factory=SecurityCommandList)
    protectedPaths: SecurityPathList = Field(default_factory=SecurityPathList)
    problems: list[str] | None = Field(
        default=None, description="Entries of that file skipped as unusable; absent when none."
    )


class SecurityGetResult(SecurityLists):
    """The global lists as stored (absent keys shown as their defaults)."""

    project: SecurityLists | None = Field(
        default=None, description="What the workdir's .snowpea/settings.json adds; null without."
    )
    unknownCode: Literal["ask", "auto", "allow"] = Field(
        default="auto",
        description=(
            "security.unknownCode (1.55.0, security review 6; 'auto' 1.57.0): what happens "
            "to a command or script snowpea cannot tell the effect of before it runs — a "
            "write whose target is computed while it runs, a program named by a variable, "
            "a script it cannot read, code it cannot parse (ApprovalRequest.reason 'cannot "
            "be checked against your block list' or 'cannot tell what this code changes'). "
            "'ask': put to the person in every mode. 'auto' (default since 1.57.0): let "
            "through unasked in a session in auto mode, asked in plan and accept. 'allow' "
            "(widened in 0.2.94): only known-dangerous patterns ask, in every mode — "
            "a line snowpea cannot parse or an eval runs unasked too. Still asked or refused "
            "under 'allow': blocked commands, protected paths, snowpea's own controls (its "
            "CLI, its files, a line naming snowpea next to something unreadable), a script "
            "the same line writes and then runs, anything piped into a shell or an "
            "interpreter (a script read "
            "from <(…) or /dev/stdin too), code handed to crontab/at/batch/launchctl, git "
            "told to run a program and writes where git or PATH finds programs, a change to "
            "HOME, PWD, PATH or a similar variable, an unseen part (a function, eval, "
            "source) that may change the shell's state next to a path that depends on it, "
            "control characters, and a reader failure. A line it could not read is judged "
            "on its raw text at its worst: a blocked command anywhere in it is refused; a "
            "path in it that (deleted with all below, from the project and from the home) "
            "reaches a protected path or snowpea's own files asks, and so do cd, pushd, "
            "popd, builtin, command, $IFS, a brace expansion, a text it cannot split and "
            "snowpea spelled in pieces. Under 'auto' and 'allow' alike, code that may be a "
            "download (a shell, eval or interpreter fed in a line that also fetches with "
            "curl, wget or another HTTP client) asks, and so does a program named by a "
            "variable in a line that sets IFS. What is let through "
            "is otherwise judged by what snowpea did see; "
            "a script or command whose target is computed at run time can then write to a "
            "protected folder without a prompt. Settings written by 0.2.88-0.2.89 hold 'ask' (the "
            "old default was written out): the first start of 0.2.90 changes a stored "
            "'ask' to 'auto' once (marker $SNOWPEA_HOME/security/unknown-code-migrated.json; "
            "problems carries one line saying so until the value changes or the daemon "
            "restarts), never again after. Written with settings.set {scope: "
            "'global', patch: {security: {unknownCode}}} by a local person (and, 1.58.0, a "
            "browser's Settings page with actor 'person'); a project "
            "scope refuses it (invalid_params, details.reason 'global_only'). Reported as "
            "stored: while enabled is false the check applies 'allow' whatever this says."
        ),
    )
    effective: SecurityEffective = Field(
        default_factory=SecurityEffective,
        description="What is in force; empty lists while enabled is false (1.58.0).",
    )
    enabled: bool = Field(
        default=True,
        description=(
            "1.58.0 (securityMasterSwitch): security.enabled, the master switch "
            "(SecuritySwitchState.enabled). While false, problems starts with a line and "
            "problemItems with an item of code 'security-disabled'. Written with "
            "settings.set {scope: 'global', patch: {security: {enabled}}}; a project scope "
            "refuses it (invalid_params, details.reason 'global_only'). 1.66.0: false by "
            "default (byDefault); the line is then a neutral note."
        ),
    )
    byDefault: bool = Field(
        default=False, description="1.66.0: SecuritySwitchState.byDefault."
    )
    disabledBy: str | None = Field(
        default=None, description="1.58.0: SecuritySwitchState.disabledBy."
    )
    disabledAt: str | None = Field(
        default=None, description="1.58.0: SecuritySwitchState.disabledAt."
    )
    writable: bool = Field(
        description=(
            "True for a local person's connection, and (1.58.0) for a browser connection "
            "of version 0.3.0 or later: its Settings page may change security.enabled, "
            "security.unknownCode, security.blockedCommands and security.protectedPaths with "
            "settings.set {scope: 'global', patch: {security: {...}}, actor: 'person'} (no "
            "other security key, never without the marker). A remote web login or a "
            "connector reads only."
        )
    )
    problems: list[str] = Field(
        default_factory=list,
        description=(
            "Entries skipped as unusable (a hand-edited file): unknown preset, bad pattern."
        ),
    )
    problemItems: list[SecurityProblem] = Field(
        default_factory=list,
        description=(
            "1.55.0: the same problems, structured, in the same order. settings.set and "
            "security.check refuse a bad security value with invalid_params whose "
            "details carry both problems (text) and problemItems (this shape)."
        ),
    )
    projects: list[SecurityProjectAdditions] | None = Field(
        default=None,
        description=(
            "1.64.0 (securityAllProjects): with allProjects true, what each registered "
            "project (the sidebar's workspaces, then live sessions' projects) adds, in the "
            "sidebar's order, at most 200; null without allProjects."
        ),
    )
    truncated: bool = Field(
        default=False,
        description="1.64.0: true when allProjects listed only the first 200 projects.",
    )


class SecurityCheckParams(Payload):
    command: str | None = Field(default=None, description="A shell line to judge.")
    path: str | None = Field(default=None, description="A path to judge.")
    access: Literal["write", "read"] = Field(
        default="write", description="With path: judge a change (default) or a read."
    )
    workdir: str | None = Field(
        default=None, description="Where it would run (relative paths, project additions)."
    )
    draft: SecurityLists | None = Field(
        default=None,
        description=(
            "Unsaved lists to judge against instead of the saved global ones (the project's "
            "additions still apply). Invalid entries are invalid_params."
        ),
    )
    locale: str | None = Field(default=None, description="Language of 'why'; default 'en'.")


class SecurityCheckResult(Payload):
    blocked: bool
    by: str | None = Field(
        default=None,
        description=(
            "'commands:<presetId>', 'paths:<presetId>', 'custom', or 'security' (an edit of "
            "the security lists themselves)."
        ),
    )
    why: str | None = Field(default=None, description="Localized reason.")
    opaque: bool = Field(
        default=False,
        description=(
            "Not blocked, but part of it cannot be seen while a list is set: it would always "
            "be put to the person (reason)."
        ),
    )
    reason: str | None = Field(default=None, description="'cannot be checked against your "
                                                         "block list' when opaque.")
    match: str | None = Field(default=None, description="The command or protected path hit.")
    level: SecurityLevel | None = Field(
        default=None, description="For a protected path hit: the entry's level."
    )


class SecurityBlockedNotification(Payload):
    """A call was refused by the security lists (1.54.0)."""

    sessionId: str = Field(
        description="The session ('ext:<cli>:<id>' for an external session's request)."
    )
    tool: str = Field(description="The tool the model called.")
    summary: str = Field(description="The command line or the call, shortened.")
    by: str = Field(description="As SecurityCheckResult.by.")
    why: str = Field(default="", description="The reason in English, as the model read it.")
    surface: Literal["agent", "approval", "external"] = Field(
        default="agent",
        description=(
            "Where it was stopped: snowpea's own tool execution, the approval queue, or an "
            "external CLI's bridged approval."
        ),
    )


# --------------------------------------------------------------------------
# 개선하기 — improvement feedback (1.59.0, capability improvementFeedback)
# --------------------------------------------------------------------------

FeedbackSurface = Literal["desktop", "tui", "web", "ide", "cli", "messenger", "other"]
FeedbackImageMime = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]


class FeedbackFlag(Payload):
    messageId: str = Field(
        min_length=1,
        max_length=200,
        description="The id of a message in report.session.messages (m_<seq>); an unknown id is "
        "invalid_params (details.field 'flagged').",
    )
    note: str | None = Field(
        default=None,
        max_length=4000,
        description="Why this message is a problem (masked like the rest).",
    )


class FeedbackPreviewParams(Payload):
    sessionId: str = Field(min_length=1, description="The conversation to send.")
    comment: str | None = Field(
        default=None, max_length=20000, description="What went wrong or what to improve (optional)."
    )
    flagged: list[FeedbackFlag] | None = Field(
        default=None, max_length=1000, description="Messages the person points at (at most 1000)."
    )


class FeedbackAttachment(Payload):
    name: str = Field(min_length=1, max_length=200, description="A file name (no path).")
    mime: FeedbackImageMime = Field(
        description="Images only; the bytes must start with the format's signature."
    )
    bytesBase64: str = Field(min_length=1, description="Standard base64, no data: prefix.")


class FeedbackSendParams(FeedbackPreviewParams):
    surface: FeedbackSurface = Field(
        description="The app the person sends from (report client.surface)."
    )
    surfaceVersion: str | None = Field(
        default=None,
        max_length=64,
        description="That app's own version (report client.surfaceVersion).",
    )
    attachments: list[FeedbackAttachment] | None = Field(
        default=None,
        max_length=10,
        description=(
            "Screenshots: at most 10, at most 5 MB decoded in total (invalid_params otherwise)."
        ),
    )


class FeedbackStats(Payload):
    messages: int = Field(description="Messages in the report.")
    toolMessages: int = Field(description="Of them, tool calls (a client hides them by default).")
    rawBytes: int = Field(description="The report's JSON size before gzip.")
    redactions: int = Field(
        description="Strings something was masked in, plus tool results withheld whole (sensitive)."
    )
    droppedResults: int = Field(
        description="Tool results, args or long texts cut to fit the server's size limits."
    )


class FeedbackPreviewResult(Payload):
    report: dict[str, Any] = Field(
        description=(
            "The exact JSON object feedback.send would POST (schema 1 of snowpea-browser's "
            "docs/feedback-api.md, client.app 'snowpea-core'), after masking and fitting, without "
            "attachments. Render report.session.messages: {id, seq?, role: user|assistant|tool|"
            "system|event, ts?, text?, reasoning?, tool?: {name, callId?, args?, result?, status?, "
            "durationMs?}, meta?}; tool rows are best hidden by default. client.surface is the "
            "caller's own kind here; send takes it from its params."
        )
    )
    stats: FeedbackStats
    endpoint: str = Field(description="The URL the report would go to.")


class FeedbackSendResult(Payload):
    id: str = Field(description="The report's id on the feedback server (24 hex).")
    receivedAt: str = Field(description="When the server received it (ISO).")


# --------------------------------------------------------------------------
# registries
# --------------------------------------------------------------------------


class RpcMethod(BaseModel):
    """One registry entry, used for dispatch validation and schema dumps."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    params: type[BaseModel]
    result: type[BaseModel]
    direction: Direction = "c2s"
    summary: str = ""


def _m(
    name: str,
    params: type[BaseModel],
    result: type[BaseModel],
    summary: str,
    direction: Direction = "c2s",
) -> RpcMethod:
    return RpcMethod(name=name, params=params, result=result, direction=direction, summary=summary)


METHODS: dict[str, RpcMethod] = {
    m.name: m
    for m in (
        _m(
            "system.hello",
            HelloParams,
            HelloResult,
            "Authenticate a connection and agree on the protocol version.",
        ),
        _m(
            "system.info",
            Empty,
            InfoResult,
            "Report the daemon's version, pid, port, start time and home.",
        ),
        _m(
            "system.health",
            Empty,
            HealthResult,
            "Liveness probe; answers as long as the daemon serves requests.",
        ),
        _m("system.shutdown", Empty, Ok, "Ask the daemon to shut down gracefully."),
        _m(
            "lsp.status",
            Empty,
            LspStatusResult,
            "Report every language server the daemon has started and its state.",
        ),
        _m(
            "lsp.catalog",
            Empty,
            LspCatalogResult,
            "List every registered language server, regardless of whether it has started.",
        ),
        _m(
            "lsp.install",
            LspInstallParams,
            LspInstallResult,
            "Install language servers the user asked for, even with lsp.autoInstall off.",
        ),
        _m(
            "lsp.toolchain.remove",
            LspToolchainParams,
            LspToolchainRemoveResult,
            "Delete a toolchain core installed for language servers (1.68.0).",
        ),
        _m(
            "lsp.toolchain.cancel",
            LspToolchainParams,
            LspToolchainCancelResult,
            "Stop a running toolchain install (1.68.0).",
        ),
        _m(
            "lsp.restart",
            LspRestartParams,
            LspRestartResult,
            "Stop a language server (or all) so it starts afresh; missing ones are re-probed.",
        ),
        _m(
            "mcp.list",
            McpListParams,
            McpListResult,
            "List every configured MCP server with its scope, state and tools.",
        ),
        _m(
            "mcp.add",
            McpAddParams,
            McpAddResult,
            "Write an MCP server into the project or global .mcp.json and start it.",
        ),
        _m("mcp.remove", McpRemoveParams, Ok, "Delete an MCP server entry and stop the server."),
        _m("mcp.update", McpUpdateParams, Ok, "Merge a patch into an existing MCP server entry."),
        _m(
            "mcp.test",
            McpTestParams,
            McpTestResult,
            "Probe a saved MCP server or an unsaved draft and report its tools.",
        ),
        _m(
            "mcp.reload",
            McpReloadParams,
            McpReloadResult,
            "Restart one MCP server, or every configured one.",
        ),
        _m(
            "mcp.authorize",
            McpAuthorizeParams,
            McpAuthorizeResult,
            "Start an OAuth sign-in for a remote MCP server; returns the consent URL (1.34.0).",
        ),
        _m(
            "mcp.authorize.complete",
            McpAuthorizeCompleteParams,
            McpAuthorizeCompleteResult,
            ("Finish an mcp.authorize flow with the code the host's own redirect caught. Not "
             "from a remote web login (1.55.0): jarvis_person_only, details.reason local_only."),
        ),
        _m(
            "mcp.deauthorize",
            McpDeauthorizeParams,
            McpDeauthorizeResult,
            "Forget a remote MCP server's stored OAuth tokens (1.34.0).",
        ),
        _m(
            "mcp.catalog",
            Empty,
            McpCatalogResult,
            "The curated MCP servers a client can offer as presets.",
        ),
        _m(
            "system.checkUpdate",
            CheckUpdateParams,
            CheckUpdateResult,
            "Report whether a newer snowpea release exists; cached for 24h.",
        ),
        _m(
            "system.update",
            Empty,
            UpdateResult,
            "Upgrade snowpea in a detached subprocess and report progress.",
        ),
        _m(
            "system.restart",
            Empty,
            Ok,
            "Shut the daemon down so the next launch runs the newly installed version.",
        ),
        _m(
            "system.reloadSettings",
            ActorParams,
            SettingsReloadResult,
            "Re-read settings.json and rebind the daemon's in-memory state.",
        ),
        _m(
            "session.create",
            SessionCreateParams,
            SessionCreateResult,
            "Open a session rooted at a working directory.",
        ),
        _m(
            "session.resume",
            SessionResumeParams,
            SessionResumeResult,
            "Replay the events a disconnected client missed.",
        ),
        _m("session.list", SessionListParams, SessionListResult, "List live or saved sessions."),
        _m(
            "session.deleteSaved",
            SessionDeleteParams,
            SessionDeleteResult,
            "Delete saved sessions.",
        ),
        _m("session.close", SessionIdParams, Ok, "Close a session and release its resources."),
        _m(
            "session.delete",
            SessionIdParams,
            SessionDeleteOutcome,
            "Close a session if it is live, then delete its saved entry "
            "(session.close + session.deleteSaved).",
        ),
        _m(
            "session.prompt",
            SessionPromptParams,
            TurnResult,
            (
                "Send user text to a session and start a turn. Events: turn.started "
                "{turnId, prompt, queued}, then message.user {text, attachments, refs, "
                "steered: false}, then the model's output. message.user.text is what the "
                "user typed; a page attachment is listed as {kind: page, name, url, title} "
                "and only the model sees the rendered page. A steered prompt emits message.user "
                "{steered: true} + turn.dequeued {reason: steered}; a queued one emits "
                "turn.queued first."
            ),
        ),
        _m(
            "file.complete",
            FileCompleteParams,
            FileCompleteResult,
            "Complete file and directory paths under a session workdir.",
        ),
        _m("session.interrupt", SessionIdParams, Ok, "Stop the running turn as soon as possible."),
        _m(
            "session.compact",
            SessionCompactParams,
            SessionCompactResult,
            "Summarise the conversation so far and replace the history with it.",
        ),
        _m(
            "session.setMode",
            SessionSetModeParams,
            SessionSetModeResult,
            "Switch a session between plan, accept and auto.",
        ),
        _m(
            "session.setModel",
            SessionSetModelParams,
            SessionSetModelResult,
            "Pin a session to a model profile, or clear the pin.",
        ),
        _m(
            "session.setEffort",
            SessionSetEffortParams,
            SessionSetEffortResult,
            "Pin how hard a session's model may think, or clear the pin.",
        ),
        _m(
            "audio.capabilities",
            Empty,
            AudioCapabilitiesResult,
            "Report what voice input and output can do on this machine.",
        ),
        _m(
            "audio.transcribe",
            AudioTranscribeParams,
            AudioTranscribeResult,
            "Transcribe recorded audio to text.",
        ),
        _m(
            "audio.speak",
            AudioSpeakParams,
            AudioSpeakResult,
            "Synthesise speech, optionally playing it on the daemon's machine.",
        ),
        _m(
            "audio.record.start",
            AudioRecordStartParams,
            AudioRecordResult,
            "Start recording the microphone.",
        ),
        _m(
            "audio.record.stop",
            AudioRecordStopParams,
            AudioRecordResult,
            "Stop the recording and return the wav it wrote.",
        ),
        _m(
            "audio.install",
            AudioInstallParams,
            AudioInstallResult,
            "Install a local voice engine (or one of its voices) and re-run detection.",
        ),
        _m(
            "audio.voices",
            AudioVoicesParams,
            AudioVoicesResult,
            "List the voices one speech engine offers here.",
        ),
        _m(
            "command.list",
            OptionalSessionParams,
            CommandListResult,
            "List the slash commands available to a session.",
        ),
        _m(
            "command.run",
            CommandRunParams,
            TurnResult,
            "Run a slash command; the only execution path for them.",
        ),
        _m(
            "tool.list",
            OptionalSessionParams,
            ToolListResult,
            "List the tools registered for a session.",
        ),
        _m(
            "approval.list",
            OptionalSessionParams,
            ApprovalListResult,
            "List tool calls still waiting for a decision.",
        ),
        _m(
            "approval.respond",
            ApprovalRespondParams,
            ApprovalRespondResult,
            "Answer a pending approval and unblock the turn.",
        ),
        _m(
            "question.list",
            OptionalSessionParams,
            QuestionListResult,
            "List questions the agent is still waiting on.",
        ),
        _m(
            "question.respond",
            QuestionRespondParams,
            Ok,
            "Answer a pending question and unblock the turn.",
        ),
        _m(
            "permission.allowlist.add",
            AllowlistAddParams,
            AllowlistAddResult,
            "Promote a pattern from ask to allow.",
        ),
        _m(
            "permission.allowlist.list",
            AllowlistListParams,
            AllowlistListResult,
            "List stored allowlist patterns.",
        ),
        _m(
            "permission.allowlist.remove",
            AllowlistRemoveParams,
            Ok,
            "Delete an allowlist pattern by id.",
        ),
        _m(
            "provider.list",
            Empty,
            ProviderListResult,
            "List chat providers and whether they are configured.",
        ),
        _m(
            "extension.list",
            Empty,
            ExtensionListResult,
            "Extensions found in $SNOWPEA_HOME/extensions, rescanned on each call (1.49.0, "
            "capability 'extensions').",
        ),
        _m("extension.enable", ExtensionNameParams, ExtensionResult, "Enable and load (1.49.0)."),
        _m(
            "extension.disable",
            ExtensionNameParams,
            ExtensionResult,
            "Unload and disable; everything it registered goes (1.49.0).",
        ),
        _m(
            "extension.reload",
            ExtensionNameParams,
            ExtensionResult,
            "Unload and load again, picking up edits (1.49.0).",
        ),
        _m(
            "extension.configure",
            ExtensionConfigureParams,
            ExtensionResult,
            "Save a plugin's config (validated by its configSchema) and reload it (1.49.0).",
        ),
        _m(
            "session.lineage",
            SessionLineageParams,
            SessionLineageResult,
            "A session's ancestors (breadcrumbs) and delegated descendants as a tree (1.48.0, "
            "capability 'lineage'); 1.50.0 follows forks up the chain and lists direct forks.",
        ),
        _m(
            "workspace.list",
            Empty,
            WorkspaceListResult,
            "Project folders in sidebar order, each with its conversation count (1.47.0, "
            "capability 'workspaces').",
        ),
        _m("workspace.add", WorkspaceAddParams, WorkspaceResult, "Add a project folder (1.47.0)."),
        _m("workspace.rename", WorkspaceRenameParams, WorkspaceResult, "Rename one (1.47.0)."),
        _m(
            "workspace.remove",
            WorkspaceIdParams,
            Ok,
            "Remove a folder from the sidebar; its conversations stay (1.47.0).",
        ),
        _m(
            "workspace.reorder",
            WorkspaceReorderParams,
            WorkspaceListResult,
            "Set the sidebar order (1.47.0).",
        ),
        _m("session.pin", SessionFlagParams, Ok, "Pin or unpin a conversation (1.47.0)."),
        _m(
            "session.archive",
            SessionFlagParams,
            Ok,
            "Archive or restore a conversation; refused while it runs unless stop (1.47.0).",
        ),
        _m(
            "session.fork",
            SessionForkParams,
            SessionForkResult,
            "Branch a session into a new one at the end of a finished turn; the source is not "
            "touched (1.46.0, capability 'fork'). Refusals carry error.data.code "
            "fork_after_compaction, fork_turn_not_found, fork_no_finished_turn, "
            "fork_child_session or fork_running (1.50.0).",
        ),
        _m(
            "session.trajectory",
            SessionTrajectoryParams,
            SessionTrajectoryResult,
            "The session's event log as turn-aware rows: turns, model calls with timing and "
            "usage, tool calls with results, delegations, compactions (1.45.0, capability "
            "'trajectory').",
        ),
        _m("goal.get", GoalGetParams, GoalResult, "Read the session's goal (1.43.0)."),
        _m(
            "goal.set",
            GoalSetParams,
            GoalResult,
            "Give the session a goal and arm it; rounds run until goal_update completes or "
            "blocks it, or the round cap (1.43.0, capability 'goals'). A delegated child or "
            "a scheduled run is refused with goal_not_allowed (1.50.0).",
        ),
        _m("goal.pause", GoalControlParams, GoalResult, "Pause and disarm the goal (1.43.0)."),
        _m(
            "goal.resume",
            GoalControlParams,
            GoalResult,
            "Re-arm a paused or blocked goal, optionally with more rounds, and start a round "
            "when idle (1.43.0); goal_not_allowed on a delegated child or a scheduled run "
            "(1.50.0).",
        ),
        _m("goal.clear", GoalControlParams, GoalResult, "Remove the goal (1.43.0)."),
        _m(
            "provider.visionReset",
            ProviderVisionResetParams,
            ProviderVisionResetResult,
            "Forget the learned vision answer for a model so the next picture is tried again "
            "(1.41.0, capability 'visionNotice'). A configured "
            "providers.<v>.vision wins as before.",
        ),
        _m(
            "provider.models",
            ProviderModelsParams,
            ProviderModelsResult,
            "Ask a vendor's endpoint which models it serves.",
        ),
        _m(
            "provider.configure",
            ProviderConfigureParams,
            Ok,
            ("Store settings and credentials for a provider. From a remote web login (1.55.0) "
             "no base_url and no credential (api_key, token, an OAuth record; clearing one "
             "stays open): jarvis_person_only, details {reason: local_only, what: "
             "'base_url'|'credential'}."),
        ),
        _m(
            "provider.remove",
            ProviderRemoveParams,
            Ok,
            "Forget a configured provider, typically a named local server.",
        ),
        _m(
            "provider.model.remove",
            ProviderModelParams,
            ProviderModelRemoveResult,
            ("1.60.0 (capability hiddenModels): take a model off a vendor's pickers. A "
             "hand-added id (providers.<vendor>.models or .oauth_models) is taken off that "
             "list ('unlisted'); any other id is added to providers.<vendor>.hiddenModels "
             "('hidden'). Written through settings.set's path (settings.changed fires); "
             "nothing that uses the model is rewritten. A local person, a remote web login, "
             "or a browser 0.3.0+ with actor 'person'; never a connector, a legacy or unmarked "
             "browser, Jarvis or an in-process caller (jarvis_person_only)."),
        ),
        _m(
            "provider.model.unhide",
            ProviderModelParams,
            Ok,
            ("1.60.0 (capability hiddenModels): take a model off "
             "providers.<vendor>.hiddenModels (ok when it was not hidden). Same callers as "
             "provider.model.remove."),
        ),
        _m(
            "provider.loginWeb",
            ProviderLoginWebParams,
            ProviderLoginWebResult,
            ("Start a browser-based login flow for a provider. Not from a remote web login "
             "(1.55.0): jarvis_person_only, details.reason local_only."),
        ),
        _m(
            "checkpoint.list",
            CheckpointListParams,
            CheckpointListResult,
            "List restore checkpoints for a session, newest first.",
        ),
        _m(
            "checkpoint.diff",
            CheckpointDiffParams,
            CheckpointDiffResult,
            "Show what restoring a checkpoint would change from the current workdir.",
        ),
        _m(
            "checkpoint.restore",
            CheckpointRestoreParams,
            CheckpointRestoreResult,
            "Restore files to their before-turn state, optionally through later turns.",
        ),
        _m(
            "checkpoint.delete",
            CheckpointDeleteParams,
            Ok,
            "Delete one checkpoint, or all checkpoints for a session.",
        ),
        _m("agent.list", Empty, AgentListResult, "List the named agents that are defined."),
        _m(
            "agent.create",
            AgentCreateParams,
            AgentCreateResult,
            "Define a named agent from a description. A local person's only (1.55.0): it "
            "writes the project's agent definition, a control file.",
        ),
        _m("agent.spawn", AgentSpawnParams, AgentSpawnResult, "Run a named agent on a task."),
        _m(
            "agent.bindChannel",
            AgentBindChannelParams,
            Ok,
            "Route a gateway channel to a named agent. A local person's only (1.55.0), "
            "like gateway.bind.",
        ),
        _m("agent.delete", AgentDeleteParams, Ok, "Delete a named agent."),
        _m("team.start", TeamStartParams, TeamStartResult, "Split a task across parallel workers."),
        _m("team.status", TeamStatusParams, TeamStatusResult, "Inspect a team's task board."),
        _m(
            "team.guide.get",
            TeamGuideGetParams,
            TeamGuideGetResult,
            "Read a team guide for a project or session.",
        ),
        _m(
            "team.guide.set",
            TeamGuideSetParams,
            TeamGuideSetResult,
            "Write a team guide to the project or global home.",
        ),
        _m(
            "team.guide.delete",
            TeamGuideDeleteParams,
            Ok,
            "Delete a team guide from the project or global home. As team.guide.set "
            "(1.55.0): a local person, or a browser with actor 'person'; not a remote web login.",
        ),
        _m(
            "team.guide.list",
            TeamGuideListParams,
            TeamGuideListResult,
            "List every team guide visible in a project.",
        ),
        _m(
            "job.schedule",
            JobScheduleParams,
            JobScheduleResult,
            "Schedule a prompt to run unattended.",
        ),
        _m(
            "job.preview",
            JobPreviewParams,
            JobPreviewResult,
            "Read a schedule spec without saving anything (1.10.0): its kind and when a "
            "job with it would first fire. An unreadable spec is invalid_params with "
            "job.schedule's message.",
        ),
        _m(
            "web.search",
            WebSearchParams,
            WebSearchResult,
            (
                "Web search for a browser host (1.11.0): the web_search tool's provider "
                "chain, settings, caps and timeouts, for the host's own fallback when its "
                "search page is blocked. Browser host clients (with a clientId) only; "
                "'unavailable' with details.reason when no provider can answer. Results "
                "are untrusted web content."
            ),
        ),
        _m(
            "llm.sample",
            LlmSampleParams,
            LlmSampleResult,
            (
                "One tool-less model call for a session's browser host (1.14.0): no tools, "
                "no history, nothing written to the transcript except a usage event. The "
                "session's host only ('unauthorized' otherwise); 'unavailable' when no model "
                "can answer; with schema, 'invalid_output' when the answer does not validate."
            ),
        ),
        _m("job.list", Empty, JobListResult, "List scheduled jobs and their next run times."),
        _m("job.cancel", JobIdParams, Ok, "Cancel a scheduled job."),
        _m("job.runNow", JobIdParams, Ok, "Fire a scheduled job immediately."),
        _m("job.update", JobUpdateParams, JobInfo, "Change an existing job (addendum 12)."),
        _m(
            "job.runs",
            JobRunsParams,
            JobRunsResult,
            "A job's run history, newest first (1.33.0).",
        ),
        _m(
            "job.nextRunForClient",
            JobNextRunForClientParams,
            JobNextRunForClientResult,
            "When a host should be awake for its routines (addendum 11).",
        ),
        _m(
            "job.delete",
            JobDeleteParams,
            JobDeleteResult,
            "Delete a job, its schedule and run history; optionally its run sessions.",
        ),
        _m(
            "gateway.bind",
            GatewayBindParams,
            GatewayBindResult,
            "Attach the daemon to a chat platform channel.",
        ),
        _m("gateway.list", Empty, GatewayListResult, "List live gateway bindings."),
        _m("gateway.unbind", GatewayUnbindParams, Ok, "Detach a gateway binding."),
        _m(
            "gateway.sync",
            Empty,
            GatewaySyncResult,
            "Reconcile the messenger bindings with settings.gateway.",
        ),
        _m(
            "memory.search",
            MemorySearchParams,
            MemorySearchResult,
            "Recall stored memories matching a query.",
        ),
        _m("memory.write", MemoryWriteParams, MemoryWriteResult, "Store a memory with tags."),
        _m(
            "memory.list",
            MemoryListParams,
            MemoryListResult,
            "List stored memories by scope, newest first.",
        ),
        _m("memory.delete", MemoryDeleteParams, Ok, "Forget one stored memory."),
        _m(
            "memory.update",
            MemoryUpdateParams,
            MemoryUpdateResult,
            "Edit one stored memory in place; it becomes confirmed (1.28.0).",
        ),
        _m(
            "memory.confirm",
            MemoryConfirmParams,
            MemoryConfirmResult,
            "Accept or drop a preference a memory.candidate event proposed (1.28.0).",
        ),
        _m("skill.search", SkillSearchParams, SkillSearchResult, "Search available skills."),
        _m(
            "skill.install", SkillInstallParams, Ok, "Install a skill from a path, URL or registry."
        ),
        _m("skill.list", SkillListParams, SkillListResult, "List installed skills."),
        _m("skill.reload", ActorParams, Ok, "Reload skills from disk without restarting."),
        _m(
            "skill.remove",
            SkillRemoveParams,
            Ok,
            "Delete a plugin, or a global or project skill, agent or command (1.33.0).",
        ),
        _m(
            "skill.create",
            SkillCreateParams,
            SkillCreateResult,
            "Write a new SKILL.md, generated from a brief or supplied verbatim.",
        ),
        _m(
            "skill.learn",
            SkillLearnParams,
            SkillLearnResult,
            "Turn a session's history into a SKILL.md, as /skill learn does, in the "
            "project or the user's skills (1.28.0). Answers when it is written and loaded.",
        ),
        _m("skill.read", SkillReadParams, SkillReadResult, "Read a skill's SKILL.md."),
        _m(
            "skill.write",
            SkillWriteParams,
            SkillWriteResult,
            "Save a skill's SKILL.md verbatim; says where, and whether it is in effect.",
        ),
        _m(
            "settings.get",
            SettingsGetParams,
            SettingsResult,
            "Read global or project settings, with secrets masked.",
        ),
        _m(
            "settings.set",
            SettingsSetParams,
            SettingsResult,
            "Deep-merge a patch into global or project settings and persist it.",
        ),
        _m(
            "settings.acceptDiskChange",
            SettingsAcceptDiskChangeParams,
            SettingsAcceptDiskChangeResult,
            "1.55.0 (security review 7): a LOCAL person accepts or discards the security, "
            "jarvis and web.remote settings that were edited in settings.json on disk while "
            "the daemon ran (settings.changed {waitingKeys}).",
        ),
        _m(
            "setup.catalog",
            Empty,
            SetupCatalogResult,
            "The setup wizard's vendor, search, browser, tools and gateway catalogs.",
        ),
        _m(
            "approval.request",
            ApprovalRequest,
            ApprovalAnswer,
            "Ask the client to approve a tool call.",
            "s2c",
        ),
        _m(
            "question.request",
            QuestionRequest,
            QuestionAnswer,
            "Ask the client to put a question to the human.",
            "s2c",
        ),
        _m(
            "tool.register",
            ToolRegisterParams,
            ToolRegisterResult,
            "Register tools this connection runs itself (host tools).",
        ),
        _m(
            "tool.unregister",
            ToolUnregisterParams,
            ToolUnregisterResult,
            "Remove some of this connection's host tools.",
        ),
        _m(
            "tool.progress",
            ToolProgressParams,
            Ok,
            "Notification: progress of a running tool.invoke, re-emitted as tool.progress.",
        ),
        _m(
            "tool.invoke",
            ToolInvokeRequest,
            ToolInvokeResult,
            "Ask the client to run one of its host tools.",
            "s2c",
        ),
        _m(
            "host.turnContext",
            HostTurnContextRequest,
            HostTurnContextResult,
            (
                "At turn start, ask the session's host-tools connection for a note for the "
                "turn's first model call (1.31.0). Only on a browser connection that sent "
                "tool.register {capabilities: ['turnContext']}; 300 ms deadline, fail-open."
            ),
            "s2c",
        ),
        _m(
            "approval.ask",
            ApprovalAskParams,
            ApprovalAskResult,
            (
                "Escalate one host action through the approval pipeline. The approver's "
                "approval.request carries tool, args with detail nested under "
                "args.detail and the site under args.site, note = reason, site, and "
                "scopeHint 'site' when a site is known. A local dev server navigation "
                "(tool 'repl.navigate', args.reasonCode 'localDevServer', args.url, "
                "args.project) gets scopeHint 'project': a 'project' answer stores a "
                "rule for the session's project and the URL's origin that only such "
                "asks match."
            ),
        ),
        _m(
            "session.attach",
            SessionAttachParams,
            SessionAttachResult,
            "Make this connection the origin of a session (approvals, host tools).",
        ),
        _m(
            "session.setAgent",
            SessionSetAgentParams,
            SessionSetAgentResult,
            (
                "Run a session as an agent definition, or as none (addendum 17). Only "
                "the session's origin connection, a browser session's hostToolsFrom "
                "host, or a non-browser surface (for a session it originated or that "
                "is not a browser session) may call it; others get 'unauthorized'."
            ),
        ),
        _m(
            "session.setBrowserProvider",
            SessionSetBrowserProviderParams,
            SessionSetBrowserProviderResult,
            "Per-session opt-in to core's own browser; never global (addendum 9).",
        ),
        _m(
            "usage.summary",
            UsageSummaryParams,
            UsageSummaryResult,
            "Token totals from stored usage events, grouped by provider, model, session or day.",
        ),
        _m(
            "memory.ingest",
            MemoryIngestParams,
            MemoryIngestResult,
            "Remember visited pages in the browser namespace, once per url and text.",
        ),
        _m(
            "session.notice",
            SessionNoticeParams,
            Ok,
            "Queue a [system] line for the model's next call.",
        ),
        _m(
            "session.rename",
            SessionRenameParams,
            Ok,
            "Set a session's title.",
        ),
        _m(
            "approval.evaluate",
            ApprovalEvaluateParams,
            ApprovalEvaluateResult,
            "Would a saved rule or the mode allow this action? A host-level, side-effect-free "
            "check (1.40.0): the allowlist entries of the caller's own profile (its clientId), "
            "then the mode matrix. Nothing is asked or stored.",
        ),
        _m(
            "fs.stat",
            FsPathParams,
            FsStatResult,
            "File metadata inside the session's roots (1.40.0): type, size, mtimeMs, nlink, "
            "isLink. Errors outside_root, not_found.",
        ),
        _m(
            "fs.list",
            FsListParams,
            FsListResult,
            "List a directory, recursively up to depth, capped by maxEntries (1.40.0). "
            "Symbolic links are listed, never followed.",
        ),
        _m(
            "fs.glob",
            FsGlobParams,
            FsListResult,
            "Files and directories matching a relative glob ('*', '?', '[..]', and a '**' segment "
            "for any depth), capped by maxEntries (1.40.0). Symbolic links are matched, never "
            "followed; '**' does not enter .git or node_modules.",
        ),
        _m(
            "fs.read",
            FsReadParams,
            FsReadResult,
            "Read a byte range of a file, at most 8 MiB per call (1.40.0). Errors outside_root, "
            "protected, not_found, is_dir, too_large.",
        ),
        _m(
            "fs.write",
            FsWriteParams,
            FsWriteResult,
            "Write a whole file through a temp file and an atomic rename, at most 8 MiB "
            "(1.40.0). 'create' never replaces; parents are created inside the roots. "
            "Errors outside_root, protected, exists, is_dir, too_large. 1.55.0: the person's "
            "security lists apply as to the file tools: a blocked path is security_blocked "
            "(for everyone); a path the tools would ask a person about is protected with "
            "details.reason local_only for anyone but a local person.",
        ),
        _m(
            "fs.hash",
            FsPathParams,
            FsHashResult,
            "SHA-256 of a file, read in a stream (1.40.0).",
        ),
        _m(
            "session.artifacts",
            SessionIdParams,
            SessionArtifactsResult,
            "Files the session saved for the user under its workspace's artifacts/.",
        ),
        _m(
            "session.attachments",
            SessionIdParams,
            SessionAttachmentsResult,
            "Files the user attached in this session, with the path of the original bytes "
            "the agent may use (1.23.0). A delegated child reports its root's.",
        ),
        _m(
            "turn.outcome",
            TurnOutcomeParams,
            TurnOutcomeResult,
            "Record whether a turn did what was asked (1.27.0): a browser client, for its "
            "own sessions. Stored as a turn.outcome session event.",
        ),
        _m(
            "session.events",
            SessionEventsParams,
            SessionEventsResult,
            "A session's saved events, read only (1.27.0): unlike session.resume it does "
            "not subscribe, attach, restore or take the session's approvals.",
        ),
        _m(
            "session.toolContent",
            SessionToolContentParams,
            SessionToolContentResult,
            "A host tool result's content blocks with image bytes; results with images "
            "are kept on disk.",
        ),
        _m(
            "session.steer",
            SessionSteerParams,
            SessionSteerResult,
            "Inject a user message into a running turn at its next tool round; never a "
            "slash command (steer_slash_command, 1.50.0).",
        ),
        _m(
            "session.continue",
            SessionContinueParams,
            SessionContinueResult,
            (
                "Continue a chat session on the system's behalf, e.g. after a human "
                "hand-back (1.8.0). A running turn gets the note at its next model call "
                "(like session.notice); otherwise a turn starts whose input is the "
                "[system] note, with turn.started initiator 'system' and no message.user. "
                "Same callers as session.setAgent; subagent and scheduled sessions are "
                "refused with 'invalid_params'. One system start per session per 10 s, "
                "and an idempotencyKey is honoured once; a call inside the 10 s window "
                "keeps its note as a notice (queued: true)."
            ),
        ),
        _m(
            "session.setActive",
            SessionIdParams,
            Ok,
            (
                "Mark the session the user is looking at as the active one: a messenger "
                "with gateway.<platform>.shareActive posts its questions and approvals to "
                "the owner's chat. session.prompt and session.steer from a client do the "
                "same. Subagent and scheduled sessions are refused with 'invalid_params'."
            ),
        ),
        _m(
            "site.get",
            SiteGetParams,
            SiteGetResult,
            (
                "Site memory (1.9.0): the entries stored for an origin in the caller's "
                "browser profile; with path, only those whose urlPattern matches it, most "
                "specific first; with pageType, only that one. Stale entries are included, "
                "flagged. Browser host clients only; entries are untrusted page-derived "
                "data and core never puts them in a prompt."
            ),
        ),
        _m(
            "site.put",
            SitePutParams,
            SitePutResult,
            (
                "Site memory (1.9.0): upsert an entry by (origin, pageType). Core checks "
                "the schema, refuses XPath, values on login fields and strings that look "
                "like personal data or credentials, and enforces 24 KB per entry (16 KB "
                "before 1.15.0) and 30 "
                "entries per origin ('invalid_params' naming the field); past 1,000 "
                "origins the least recently verified origin is dropped. A put clears "
                "stale and counts as a success. Browser host clients only."
            ),
        ),
        _m(
            "site.mark",
            SiteMarkParams,
            SiteMarkResult,
            (
                "Site memory (1.9.0): 'ok' counts a success, sets lastVerified and clears "
                "stale; 'stale' counts a failure and flags the entry until the next put. "
                "Browser host clients only."
            ),
        ),
        _m(
            "site.markPart",
            SiteMarkPartParams,
            SiteMarkPartResult,
            (
                "Site memory (1.15.0): count one use of a flow or recipe of an entry without "
                "a put. The entry's successCount, failureCount, stale and lastVerified are "
                "untouched, so this never counts as verification. 'not_found' for an unknown "
                "entry, flow or recipe. Browser host clients only."
            ),
        ),
        _m(
            "site.playbook.get",
            SitePlaybookGetParams,
            SitePlaybookGetResult,
            (
                "Site playbooks (1.38.0): the typed task procedures stored for an origin, "
                "by name. A browser client reads its own profile; any other owner client "
                "names one with hostToolsFrom. Untrusted page-derived data; core never "
                "puts it in a prompt. Capability sitePlaybooks."
            ),
        ),
        _m(
            "site.playbook.put",
            SitePlaybookPutParams,
            SitePlaybookPutResult,
            (
                "Site playbooks (1.38.0): upsert a playbook by (origin, name). Core refuses "
                "strings that look like personal data or credentials, a step that names a "
                "login field, a slot or a step the schema cannot express, and 16 KB per "
                "playbook or 10 per origin ('invalid_params' naming the field). Core owns "
                "id, version and prev: a change of slots or segments keeps the stored body "
                "one deep as prev and counts the version up. alwaysAsk, acked and an empty "
                "history keep their stored values. Browser host clients only."
            ),
        ),
        _m(
            "site.playbook.mark",
            SitePlaybookMarkParams,
            SitePlaybookResultOne,
            (
                "Site playbooks (1.38.0): append one run (day, result, segments done of "
                "total, rounds) to the history, keeping the last 10; an 'ok' run sets "
                "verifiedAt. Counts for site.stats. Browser host clients only."
            ),
        ),
        _m(
            "site.playbook.edit",
            SitePlaybookEditParams,
            SitePlaybookResultOne,
            (
                "Site playbooks (1.38.0): one edit the user makes in settings: rename, "
                "alwaysAsk, retire, unretire, restore (swap with the previous version), "
                "acknowledge, removeSegment, removePitfall. A browser client edits its own "
                "profile; any other owner client names one with hostToolsFrom."
            ),
        ),
        _m(
            "site.playbook.delete",
            SitePlaybookDeleteParams,
            SitePlaybookDeleteResult,
            (
                "Site playbooks (1.38.0): delete one playbook, or every playbook of an "
                "origin. site.delete of a whole origin removes them too."
            ),
        ),
        _m(
            "site.stats",
            SiteStatsParams,
            SiteStatsResult,
            (
                "Site memory (1.27.0): per-origin counts of the profile's site.put, "
                "site.mark and site.markPart calls over the last days. Browser clients "
                "only, for their own profile; nothing page-derived is in it."
            ),
        ),
        _m(
            "site.list",
            SiteListParams,
            SiteListResult,
            (
                "Site memory (1.9.0): one browser profile's remembered sites, by origin, "
                "for a settings UI. A browser client sees its own profile; any other "
                "owner client names one with hostToolsFrom."
            ),
        ),
        _m(
            "site.delete",
            SiteDeleteParams,
            SiteDeleteResult,
            (
                "Site memory (1.9.0): delete one entry, or every entry of an origin, in "
                "one browser profile. Same callers as site.list."
            ),
        ),
        _m(
            "setup.status",
            SetupStatusParams,
            SetupStatusResult,
            "What setup still needs; for the browser profile only a tested provider is required.",
        ),
        _m(
            "setup.applyDefaults",
            SetupApplyDefaultsParams,
            SetupApplyDefaultsResult,
            "Apply the profile's defaults for everything optional; idempotent.",
        ),
        _m(
            "provider.test",
            ProviderTestParams,
            ProviderTestResult,
            "Send one short completion to check a provider and model.",
        ),
        # -- M19 Jarvis mode (beta, 1.53.0); not_implemented until each WP lands --
        _m(
            "jarvis.status",
            Empty,
            JarvisStatusResult,
            (
                "Jarvis mode (1.53.0, WP3): enabled, the Jarvis session, model, folder, budget, "
                "pauses."
            ),
        ),
        _m(
            "jarvis.enable",
            JarvisEnableParams,
            JarvisStatusResult,
            (
                "Jarvis mode (1.53.0, WP3): set jarvis.enabled, create or resume the Jarvis "
                "session and start supervising every exposed project. Idempotent."
            ),
        ),
        _m(
            "jarvis.disable",
            Empty,
            JarvisStatusResult,
            (
                "Jarvis mode (1.53.0, WP3): clear jarvis.enabled and park the Jarvis session; "
                "everything routes to the person again, messenger bindings get their targets back."
            ),
        ),
        _m(
            "jarvis.overview",
            JarvisOverviewParams,
            JarvisOverviewResult,
            (
                "Jarvis mode (1.53.0, WP3): one row per supervised session, snowpea and "
                "external. Since 1.56.0 (jarvisSessionSwitch) only sessions whose Jarvis "
                "switch is on; hiddenCount says how many are not shared."
            ),
        ),
        _m(
            "jarvis.session.set",
            JarvisSessionSetParams,
            JarvisSessionSetResult,
            (
                "Jarvis mode (1.56.0, capability jarvisSessionSwitch): turn one session's "
                "Jarvis switch on or off. A person only: a local connection or a remote web "
                "login (jarvis_person_only for a browser, a connector or an in-process "
                "caller; Jarvis has no tool for it). jarvis_disabled while Jarvis mode is off; "
                "invalid_params (details.reason project_hidden / jarvis_session / "
                "browser_hidden) when the switch is locked. Off hands what waits for Jarvis "
                "there to the person at once (route null, reason session_off); on routes "
                "what waits as if just asked (session_on). Grants are not touched. Announced "
                "as jarvis.session.changed."
            ),
        ),
        _m(
            "jarvis.ledger",
            JarvisLedgerParams,
            JarvisLedgerResult,
            (
                "Jarvis mode (1.53.0, WP2): decisions made for supervised sessions, newest "
                "first, by Jarvis, the person or the engine alone, with classification, rule "
                "and grant."
            ),
        ),
        _m(
            "jarvis.delegation.list",
            JarvisDelegationListParams,
            JarvisDelegationListResult,
            "Jarvis mode (1.53.0, WP2): delegation grants in force.",
        ),
        _m(
            "jarvis.delegation.set",
            JarvisDelegationSetParams,
            JarvisDelegationSetResult,
            (
                "Jarvis mode (1.53.0, WP2): create or replace a grant. The calling connection "
                "is the person, so it applies at once; Jarvis's own proposals never use this "
                "method but raise a person-only approval. Not from a remote web login (1.55.0: "
                "jarvis_person_only, details.reason local_only; it may still revoke, take over "
                "and hand back); from a messenger only when "
                "jarvis.messenger.allowGrants."
            ),
        ),
        _m(
            "jarvis.delegation.revoke",
            JarvisDelegationRevokeParams,
            JarvisDelegationRevokeResult,
            "Jarvis mode (1.53.0, WP2): revoke a grant at once.",
        ),
        _m(
            "jarvis.takeover",
            JarvisTakeoverParams,
            JarvisPauseResult,
            (
                "Jarvis mode (1.53.0, WP2): the person takes one session back; Jarvis stops "
                "deciding for it until jarvis.handback or the given minutes."
            ),
        ),
        _m(
            "jarvis.handback",
            JarvisSessionTarget,
            JarvisPauseResult,
            (
                "Jarvis mode (1.53.0, WP2): give one session back to Jarvis now, ending a "
                "takeover or a typing pause. Human connections only (else jarvis_person_only)."
            ),
        ),
        _m(
            "jarvis.escalations.list",
            JarvisEscalationsListParams,
            JarvisEscalationsListResult,
            (
                "Jarvis mode (1.53.0): the open escalations that are not a waiting approval "
                "or question (finished, error, stalled, budget, jarvis), newest first, paged. "
                "They persist until dismissed, across client reloads and daemon restarts."
            ),
        ),
        _m(
            "jarvis.escalations.dismiss",
            JarvisEscalationsDismissParams,
            JarvisEscalationsDismissResult,
            (
                "Jarvis mode (1.53.0): dismiss escalations by id (or all); every client is "
                "told with jarvis.escalation.dismissed."
            ),
        ),
        _m(
            "jarvis.project.list",
            Empty,
            JarvisProjectListResult,
            (
                "Jarvis mode (1.53.0, WP3): projects with the name, aliases and exposure Jarvis "
                "and clients use in conversation."
            ),
        ),
        _m(
            "jarvis.project.rename",
            JarvisProjectRenameParams,
            JarvisProjectResult,
            "Jarvis mode (1.53.0, WP3): set or clear a project's display name.",
        ),
        _m(
            "jarvis.project.alias",
            JarvisProjectAliasParams,
            JarvisProjectResult,
            "Jarvis mode (1.53.0, WP3): add or remove a project's aliases.",
        ),
        _m(
            "jarvis.briefing.get",
            Empty,
            JarvisBriefing,
            "Jarvis mode (1.53.0, WP8): the periodic briefing's settings and next run.",
        ),
        _m(
            "jarvis.briefing.set",
            JarvisBriefingSetParams,
            JarvisBriefing,
            ("Jarvis mode (1.53.0, WP8): turn the periodic briefing on or off, or change it. "
             "A remote web login may only turn it off ({enabled: false}; anything else is "
             "jarvis_person_only, details {reason: local_only, what: 'briefing'}, 1.55.0)."),
        ),
        _m(
            "project.trust.status",
            ProjectTrustParams,
            ProjectTrustStatus,
            (
                "Project trust (1.55.0): what the project's own files would make snowpea do "
                "(hooks, MCP servers, commands with allowed-tools, agents with a permission, "
                "prompt overrides, widening settings, defaultMode auto), which are active, and "
                "what changed since it was trusted. People (a remote login too)."
            ),
        ),
        _m(
            "project.trust.set",
            ProjectTrustSetParams,
            ProjectTrustStatus,
            (
                "Project trust (1.55.0): trust the project for scopes, pinning every control "
                "file as it is now. A local person only."
            ),
        ),
        _m(
            "project.trust.revoke",
            ProjectTrustRevokeParams,
            ProjectTrustStatus,
            "Project trust (1.55.0): take scopes back (all when omitted). A local person only.",
        ),
        _m(
            "feedback.preview",
            FeedbackPreviewParams,
            FeedbackPreviewResult,
            (
                "개선하기 (1.59.0, capability improvementFeedback): the report feedback.send would "
                "upload for this conversation, masked and fitted, with counts and the endpoint. "
                "Nothing leaves the machine. People only (a remote web login too); never a "
                "connector, a browser host, Jarvis or an agent."
            ),
        ),
        _m(
            "feedback.send",
            FeedbackSendParams,
            FeedbackSendResult,
            (
                "개선하기 (1.59.0, capability improvementFeedback): upload the conversation to the "
                "Snowpea team, gzip, once (no automatic retry). Call it only after the person "
                "confirmed the preview. Errors: feedback_rejected (4xx), feedback_busy (429/503, "
                "details.retryAfterSec), feedback_unreachable (network, 5xx); details {status, "
                "retryAfterSec?, serverError?, serverMessage?}. People only, like "
                "feedback.preview."
            ),
        ),
        _m(
            "web.remote.status",
            Empty,
            WebRemoteStatus,
            (
                "Remote web access (1.54.0): the external listener, its mode, the addresses and "
                "the access link. A local person only (never a remote login, browser or "
                "connector)."
            ),
        ),
        _m(
            "web.remote.listen",
            WebRemoteListenParams,
            WebRemoteStatus,
            (
                "Remote web access (1.54.0): open (or move) the external listener now, without a "
                "daemon restart; save also writes web.remote.*. Every connection through it is "
                "a remote web login. A local person only."
            ),
        ),
        _m(
            "web.remote.stop",
            WebRemoteStopParams,
            WebRemoteStatus,
            "Remote web access (1.54.0): close the external listener; its sessions end.",
        ),
        _m(
            "web.remote.setPassword",
            WebRemoteSetPasswordParams,
            WebRemoteStatus,
            (
                "Remote web access (1.54.0): store a password (scrypt hash) — password mode: "
                "the login page is required; every open session ends. A local person only."
            ),
        ),
        _m(
            "web.remote.clearPassword",
            Empty,
            WebRemoteStatus,
            (
                "Remote web access (1.54.0): remove the password (back to the access link); "
                "every open session ends."
            ),
        ),
        _m(
            "web.remote.rotateToken",
            Empty,
            WebRemoteTokenResult,
            (
                "Remote web access (1.54.0): a new web access token; the old link stops working "
                "and every open session ends."
            ),
        ),
        _m(
            "security.presets.list",
            SecurityPresetsListParams,
            SecurityPresetsListResult,
            (
                "Security lists (1.54.0): the preset catalog of blocked commands and protected "
                "paths, localized (en, ko). People only (not a browser or connector)."
            ),
        ),
        _m(
            "security.get",
            SecurityGetParams,
            SecurityGetResult,
            (
                "Security lists (1.54.0): the stored global lists, a project's additions, the "
                "effective lists with presets expanded, and whether this connection may change "
                "them (settings.set {scope: 'global', patch: {security: …}}, a local person "
                "only; arrays replace)."
            ),
        ),
        _m(
            "security.check",
            SecurityCheckParams,
            SecurityCheckResult,
            (
                "Security lists (1.54.0): judge a command or a path against the lists (or an "
                "unsaved draft) without running anything — the 'try it' box."
            ),
        ),
        _m(
            "external.clis",
            Empty,
            ExternalClisResult,
            (
                "External sessions (1.53.0, WP5): Claude Code, Codex, agy and Cursor agent on "
                "the daemon host: installed, version, path, what works, problems."
            ),
        ),
        _m(
            "external.clis.confirmPath",
            ExternalClisConfirmPathParams,
            ExternalClisResult,
            (
                "External sessions (security review H2): the person confirms the resolved path "
                "of a jarvis.external.clis.<cli>.path override (ExternalCliInfo.pendingPath) "
                "so snowpea may run it; person's local connection only (jarvis_person_only)."
            ),
        ),
        _m(
            "external.list",
            ExternalListParams,
            ExternalListResult,
            (
                "External sessions (1.53.0, WP5/6): one working directory's sessions (or every "
                "exposed project's) for the requested CLIs, newest first, paged."
            ),
        ),
        _m(
            "external.open",
            ExternalOpenParams,
            ExternalOpenResult,
            (
                "External sessions (1.53.0, WP5/6): subscribe this connection to a session's "
                "external.event stream and return the newest page of its timeline; a host "
                "'none' session is continued under snowpea (host 'snowpea')."
            ),
        ),
        _m(
            "external.read",
            ExternalReadParams,
            ExternalReadResult,
            "External sessions (1.53.0, WP5/6): an older page of a session's timeline.",
        ),
        _m(
            "external.send",
            ExternalSendParams,
            ExternalSendResult,
            (
                "External sessions (1.53.0, WP6): send a prompt; the reply streams as "
                "external.event to the sender, which is subscribed to the ref (idempotent; "
                "no external.open needed). A host 'none' session is continued first in its "
                "current mode or the CLI's defaultMode ('unrestricted' only for a human "
                "connection, else jarvis_person_only). During a running turn: added to that "
                "turn when capabilities.steer (same turnId back), else session_busy. A tmux "
                "pane that is not ready answers session_busy with error.data.details.reason "
                "terminal_prompt, terminal_busy or terminal_typing. Refused with "
                "external_view_only while canSend is false (host 'terminal'). A send from a "
                "human connection pauses Jarvis in that session (jarvis.quietMinutes)."
            ),
        ),
        _m(
            "external.setMode",
            ExternalSetModeParams,
            ExternalCreateResult,
            (
                "External sessions (1.53.0, WP6): switch a session to default, readonly or "
                "unrestricted; unrestricted only from a human connection."
            ),
        ),
        _m(
            "external.setModel",
            ExternalSetModelParams,
            ExternalSetModelResult,
            (
                "External sessions (1.67.0, externalModelSwitch): the model a session uses "
                "from its next turn (null: the CLI's default); a running turn finishes on the "
                "old one (appliesNextTurn). Kept across daemon restarts; external.state "
                "carries the new model. A session in a tmux pane: external_unavailable "
                "(details.reason 'tmux_transport'; change it there with /model); one open in "
                "another terminal: external_view_only."
            ),
        ),
        _m(
            "external.interrupt",
            ExternalRefParams,
            Ok,
            (
                "External sessions (1.53.0, WP6): stop the running turn. host 'tmux': Escape "
                "to the pane while Claude is busy (whoever started the turn; never C-c); host "
                "'terminal': external_view_only; nothing running: ok, no-op."
            ),
        ),
        _m(
            "external.create",
            ExternalCreateParams,
            ExternalCreateResult,
            (
                "External sessions (1.53.0, WP6): start a new session of a CLI in a registered "
                "project root and open it; the calling connection is subscribed to its "
                "external.event stream as with external.open."
            ),
        ),
        _m(
            "external.close",
            ExternalRefParams,
            Ok,
            (
                "External sessions (1.53.0, WP5): stop the process snowpea runs for it and "
                "unsubscribe; the CLI's own transcript stays. A screen that only goes away "
                "calls external.unwatch instead."
            ),
        ),
        _m(
            "external.get",
            ExternalRefParams,
            ExternalGetResult,
            (
                "External sessions (1.53.0): one session's row without opening or reading it "
                "(deep links, pop-outs, restored tabs). not_found when the CLI has no such "
                "session; outside_root when it is not in a registered project."
            ),
        ),
        _m(
            "external.terminalSnapshot",
            ExternalRefParams,
            ExternalTerminalSnapshotResult,
            (
                "External sessions (1.56.0, capability externalTmux): the visible text of the "
                "person's tmux pane a session runs in, for a prompt snowpea does not recognise. "
                "People only (a remote web login too); external_unavailable when the session "
                "does not run in a tmux pane snowpea can read."
            ),
        ),
        _m(
            "external.unwatch",
            ExternalRefParams,
            Ok,
            (
                "External sessions (1.53.0): stop sending this connection external.event for "
                "the session; the process keeps running (an idle one is stopped after "
                "jarvis.external.idleSec). Unknown or never-opened refs: ok."
            ),
        ),
        _m(
            "mcp.preset.list",
            McpPresetListParams,
            McpPresetListResult,
            "Assistant presets (1.53.0, WP8): mail, calendar and to-do MCP setups with recipes.",
        ),
        _m(
            "mcp.preset.check",
            McpPresetCheckParams,
            McpPresetCheckResult,
            (
                "Assistant presets (1.53.0): whether each server of an applied preset is "
                "connected and still signed in (masked account; no tokens)."
            ),
        ),
        _m(
            "mcp.preset.remove",
            McpPresetRemoveParams,
            McpPresetRemoveResult,
            (
                "Assistant presets (1.53.0, WP8): undo an applied preset: its servers, "
                "permission tags, stored client secret and recipe jobs go back to what they "
                "were before."
            ),
        ),
        _m(
            "mcp.preset.apply",
            McpPresetApplyParams,
            McpPresetApplyResult,
            (
                "Assistant presets (1.53.0, WP8): add the preset's MCP server with the given "
                "inputs and schedule the chosen recipes."
            ),
        ),
        # -- M37 terminal sessions, hook integrations, web tunnel (1.73.0) ------
        _m("terminal.profiles", Empty, TerminalProfilesResult,
           "Terminal profiles, permission options and which CLIs are installed (1.73.0)."),
        _m("terminal.list", TerminalListParams, TerminalListResult,
           "Terminals the daemon holds (1.73.0, terminalSessions)."),
        _m("terminal.create", TerminalCreateParams, TerminalCreateResult,
           "Start a terminal in a registered project; dangerous options need confirmDangerous "
           "from a human connection (1.73.0)."),
        _m("terminal.attach", TerminalAttachParams, TerminalAttachResult,
           "Attach this connection as a viewer; terminal.data then goes to it alone (1.73.0)."),
        _m("terminal.detach", TerminalIdParams, Ok,
           "Stop viewing; the terminal keeps running (1.73.0)."),
        _m("terminal.write", TerminalWriteParams, Ok,
           "Send bytes to the PTY (a viewer's typing) (1.73.0)."),
        _m("terminal.resize", TerminalResizeParams, Ok,
           "Resize from the most recent interactive viewer (1.73.0)."),
        _m("terminal.ack", TerminalAckParams, Ok,
           "Replenish a viewer's flow-control credit (1.73.0)."),
        _m("terminal.read", TerminalReadParams, TerminalReadResult,
           "Plain-text screen and history, status and title (1.73.0)."),
        _m("terminal.send", TerminalSendParams, TerminalSendResult,
           "Send text through the input gate (waits for the person to be quiet) (1.73.0)."),
        _m("terminal.keys", TerminalKeysParams, Ok,
           "Send named keys 30 ms apart (1.73.0)."),
        _m("terminal.interrupt", TerminalIdParams, Ok,
           "Interrupt the program (Esc if it reports status, else Ctrl-C) (1.73.0)."),
        _m("terminal.wait", TerminalWaitParams, TerminalWaitResult,
           "Wait for the program to leave working, exit or print a match (1.73.0)."),
        _m("terminal.close", TerminalCloseParams, Ok,
           "Kill the terminal (HUP, TERM, KILL) and forget it (1.73.0)."),
        _m("terminal.seen", TerminalIdParams, Ok,
           "A person looked at the terminal: clears its done/blocked attention (1.73.0)."),
        _m("terminal.unlock", TerminalUnlockParams, TerminalUnlockResult,
           "Over the tunnel: enter the web password again to use terminals (1.73.0)."),
        _m("terminal.report", TerminalReportParams, Ok,
           "Hook report from a CLI inside a terminal; per-terminal token only (1.73.0)."),
        _m("integration.list", Empty, IntegrationListResult,
           "Hook integration status per CLI (1.73.0, cliIntegrations)."),
        _m("integration.install", IntegrationActionParams, IntegrationActionResult,
           "Install the hook entries into a CLI's config; human connection only (1.73.0)."),
        _m("integration.uninstall", IntegrationActionParams, IntegrationActionResult,
           "Remove exactly what install added; human connection only (1.73.0)."),
        _m("web.tunnel.status", Empty, WebTunnelStatus,
           "Tunnel provider, state and public URL (1.73.0, webTunnel)."),
        _m("web.tunnel.start", TunnelStartParams, WebTunnelStatus,
           "Start the tunnel; needs web.remote.enabled and a web password (1.73.0)."),
        _m("web.tunnel.stop", TunnelStopParams, WebTunnelStatus, "Stop the tunnel (1.73.0)."),
        _m("web.tunnel.setToken", TunnelSetTokenParams, WebTunnelStatus,
           "Store the ngrok authtoken or Cloudflare tunnel token; local human only (1.73.0)."),
        _m("web.tunnel.install", TunnelInstallParams, WebTunnelInstallResult,
           "Download the pinned, checksummed tunnel binary into SNOWPEA_HOME/bin (1.73.0)."),
        _m("web.tunnel.unlock", TunnelUnlockParams, TunnelUnlockResult,
           "Re-enter the web password to unlock terminals on a tunnel connection (1.73.0)."),
    )
}

EVENTS: dict[str, type[BaseModel]] = {
    "session.event": SessionEventNotification,
    "approval.pending": ApprovalPendingNotification,
    "approval.resolved": ApprovalResolvedNotification,
    "question.pending": QuestionPendingNotification,
    "question.resolved": QuestionResolvedNotification,
    # 1.53.0: a waiting request's route changed (Jarvis mode on/off, pause, budget, ...).
    "approval.route.changed": ApprovalRouteChangedNotification,
    "question.route.changed": QuestionRouteChangedNotification,
    "job.event": JobEventNotification,
    "gateway.event": GatewayEventNotification,
    "commands.changed": CommandsChangedNotification,
    "system.updateProgress": UpdateProgressNotification,
    "provider.loginProgress": ProviderLoginProgressNotification,
    "settings.changed": SettingsChangedNotification,
    "mcp.changed": McpChangedNotification,
    "mcp.auth.done": McpAuthDoneNotification,
    "teams.changed": TeamsChangedNotification,
    "audio.install.progress": AudioInstallProgressNotification,
    "lsp.install.progress": LspInstallProgressNotification,
    "lsp.changed": LspChangedNotification,
    "tool.cancel": ToolCancelNotification,
    "sessions.changed": SessionsChangedNotification,
    # M19 (1.53.0): every one daemon-wide, except external.event, which goes only
    # to the connections subscribed to its ref (external.open/create/send).
    "jarvis.state": JarvisStateNotification,
    "jarvis.decision": JarvisDecisionNotification,
    "jarvis.escalation": JarvisEscalationNotification,
    "jarvis.delegation.changed": JarvisDelegationChangedNotification,
    "jarvis.escalation.dismissed": JarvisEscalationsDismissedNotification,
    "jarvis.access": JarvisAccessNotification,
    # 1.56.0: a session's Jarvis switch changed (jarvisSessionSwitch).
    "jarvis.session.changed": JarvisSessionChangedNotification,
    "external.event": ExternalEventNotification,
    "external.state": ExternalStateNotification,
    "external.clis.changed": ExternalClisChangedNotification,
    # 1.54.0: a call the security lists refused; to every person's surface.
    "security.blocked": SecurityBlockedNotification,
    # 1.55.0: a project's trust changed (set, revoke, the one-time migration).
    "project.trust.changed": ProjectTrustChangedNotification,
    # 1.73.0 (M37): terminal.data goes to attached viewers only; the rest daemon-wide.
    "terminal.data": TerminalDataEvent,
    "terminal.status": TerminalStatusEvent,
    "terminal.created": TerminalCreatedEvent,
    "terminal.exit": TerminalExitEvent,
    "terminal.title": TerminalTitleEvent,
    "terminal.closed": TerminalClosedEvent,
    "integration.changed": IntegrationChangedEvent,
    "web.tunnel.changed": TunnelChangedEvent,
}

CAPABILITIES: list[str] = [
    "audio",
    "lsp",
    "mcp",
    "sessions",
    "approvals",
    "commands",
    "tools",
    "settings",
    "setup",
    "update",
    "checkpoints",
    "hostTools",
    "sessionContinue",
    "siteMemory",
    "webSearch",
    "onceOnlyScope",
    "tabReadAlways",
    "llmSample",
    "snapshotSupersede",
    "readOnlyDelegation",
    "siteRecipes",
    "siteFrameActions",
    "providerModelsError",
    "batchedFileTools",
    "hostToolParallel",
    "pageActionAlways",
    "sessionSetAgent",
    "standingOrders",
    "sessionAttachments",
    "setAgentGuardTurn",
    "toolInvokeGuard",
    "llmTimings",
    "sharedDaemon",
    "sessionEvents",
    "memorySafe",
    "sessionOverrides",
    "turnOutcome",
    "siteStats",
    "browserMemoryList",
    "siteActionParts",
    "skillLearn",
    "memoryCandidates",
    "sitePacks",
    "lspInstall",
    "lspRestart",
    "lspChanged",
    "sessionEffort",
    "writeOutsideAsks",
    "hostTurnContext",
    "planAutoApprove",
    "modelCategories",
    "jobRuns",
    "mcpOAuth",
    "skillToggle",
    "mcpVarExpansion",
    "shellRemember",
    "hostTurnContextCompaction",
    "providerStatusEvents",
    "ephemeralImages",
    "hostToolsSessionEvents",
    "sitePlaybooks",
    "fs",
    "machineIdentity",
    "holdSec",
    "llmSampleHost",
    "approvalEvaluate",
    "visionNotice",
    "contextWindowSource",
    "goals",
    "steps",
    "trajectory",
    "fork",
    "workspaces",
    "lineage",
    "extensions",
    "messageOrigin",
    # jarvis.ledger, jarvis.delegation.*, jarvis.takeover/handback,
    # jarvis.decision/escalation/delegation.changed, ApprovalRequest.route —
    # M19 WP2 (delegation engine).
    "jarvisDelegation",
    # jarvis.status/enable/disable/overview, jarvis.project.*, jarvis.state,
    # jarvis.escalations.*, jarvis.escalation.dismissed, jarvis.access,
    # SessionSummary.role/jarvis — M19 WP3/WP4 (Jarvis service and tools).
    "jarvis",
    # M19 WP5: external.* and ApprovalRequest.external.
    "externalSessions",
    # mcp.preset.list/apply/remove/check — M19 WP8 (assistant presets).
    "mcpPresets",
    # jarvis.briefing.get/set — M19 WP8 (briefings and assistant tasks).
    "jarvisBriefing",
    # The browser marker ``actor: "person"`` (security review 3): a browser that
    # sends clientVersion 0.3.0 or later must set it on the config methods.
    "browserActor",
    # security.presets.list/get/check, security.blocked, settings security.* and
    # ApprovalRequest.neverDelegable/reason — 1.54.0.
    "securityLists",
    # web.remote.status/listen/stop/setPassword/clearPassword/rotateToken — 1.54.0.
    "webRemoteControl",
    # 1.55.0: tool.invoke model {canSee, contextWindow, localStyle}.
    "toolInvokeModel",
    # 1.55.0: HostToolSpec helpers/descriptionShort/sections, tool_help, meta.helpSection.
    "hostToolManuals",
    # 1.55.0: tool.progress state/note, forwarded on the tool.progress event.
    "toolProgressState",
    # 1.55.0: llm.sample images and modelCategory 'vision'.
    "llmSampleImages",
    # 1.55.0: context.autoCompactAt, compaction(.started).reason 'overflow'.
    "compactionReason",
    # 1.55.0: instant commands beside a running turn (command.result); turn
    # commands queue (command.run whenBusy).
    "commandsBesideTurn",
    # project.trust.status/set/revoke, project.trust.changed — 1.55.0 (security review 5).
    "projectTrust",
    # 1.56.0: jarvis.delegation.set / approval.respond reauth, approval.respond
    # grantExpiresAt, JarvisGrant.remoteLogin (settings jarvis.delegation.remoteGrant).
    "jarvisRemoteGrant",
    # 1.56.0: ExternalCliInfo.nativeModes/defaultNativeMode, ExternalSessionRow.nativeMode,
    # external.create/open/setMode nativeMode (each CLI's own modes).
    "externalNativeModes",
    # 1.56.0: the tmux bridge — ExternalSessionRow.terminal/controllable,
    # ApprovalRequest.externalTerminal, external.terminalSnapshot.
    "externalTmux",
    # 1.56.0: the per-session Jarvis switch — SessionSummary/ExternalSessionRow
    # jarvisOn/jarvisLocked, jarvis.session.set, jarvis.session.changed,
    # JarvisOverviewResult.hiddenCount, settings jarvis.sessions.default.
    "jarvisSessionSwitch",
    # 1.57.0 (m21, fewer needless asks): security.unknownCode 'auto' (the new
    # default: unasked in a session in auto mode, asked in plan/accept).
    "unknownCodeAuto",
    # 1.58.0: settings security.enabled (the master switch), SecurityGetResult
    # enabled/disabledBy/disabledAt, SecurityProblem.code 'security-disabled',
    # SettingsChangedNotification.security; a browser's Settings page (actor
    # 'person') writes security.enabled/unknownCode/blockedCommands/protectedPaths.
    "securityMasterSwitch",
    # 1.59.0 (개선하기): feedback.preview / feedback.send, error codes
    # feedback_rejected / feedback_busy / feedback_unreachable.
    "improvementFeedback",
    # 1.60.0: settings providers.<vendor>.hiddenModels, ProviderInfo.hiddenModels /
    # hiddenInUse, provider.model.remove / provider.model.unhide.
    "hiddenModels",
    # 1.61.0 (owner decision 2026-11-18): a blocked call asks the person once
    # (ApprovalRequest.blocked, reasonCode blocked_*) instead of being refused, but
    # for a read of the daemon's token or saved logins; a local browser 0.4.0+
    # answers person-only security asks of its own session with actor 'person' +
    # personConfirm 'window' (ApprovalAnswer / approval.respond); RequestRefusal.reason
    # browser_*.
    "browserPersonApprovals",
    # 1.62.0: a local browser 0.4.0+ trusts (and revokes) a folder of its own
    # session with actor 'person' + personConfirm 'window' (project.trust.set /
    # .revoke), default scopes only; the project_untrusted notice's details
    # (scopes, browserScopes, canTrustHere, browserText, legacyBrowserText).
    "browserProjectTrust",
    # 1.63.0 (behaviour since 0.2.96): an edit allowed through an approval card
    # is applied as an exact match only (no fuzzy fallback, unescape or
    # reindent), its preview diff is exact, and an approved write_file is
    # written byte-verbatim; a client may show patch/edit diffs for approval.
    "exactApprovedEdits",
    # 1.64.0: security.get allProjects -> SecurityGetResult.projects/truncated
    # (each registered project's additions, read-only); security.presets.list
    # and the check's texts also in ja, es and zh-CN.
    "securityAllProjects",
    # (behaviour since m28, no wire change): an approved patch edits the file's
    # bytes — read as strict UTF-8 without newline translation and written back
    # verbatim, so every other CRLF / lone CR survives and a "\n" stays "\n" on
    # Windows; a non-UTF-8 file is refused; the card diff shows a lone CR as ␍.
    "byteExactEdits",
    # 1.65.0: system.hello locale; validation problems and person-only refusals
    # (RPC error messages, details.problems, security.get problems) come back
    # in that language (en, ko, ja, es, zh-CN).
    "clientLocale",
    # 1.66.0 (owner decision 2026-11-19): security.enabled is off by default
    # (one-time migration of a stored true nobody chose), SecuritySwitchState /
    # SecurityGetResult byDefault, and while off reads of tokens and logins
    # follow the mode rules instead of being blocked.
    "securityDefaultOff",
    # 1.67.0: external.setModel (an external session's model from its next turn),
    # ExternalStateNotification.model, ExternalCliInfo.defaultModel.
    "externalModelSwitch",
    # 1.68.0: routine approvals. A scheduled run's approval reaches every surface
    # (ApprovalRequest.job, job.event approval_needed, the messenger's private
    # chat) and waits scheduler.approvalTimeoutSec; approval scope 'task'; job
    # grants (job.schedule grants, job.update mode/addGrants/removeGrants,
    # JobInfo grants/lastError/lastSessionId/waitingApproval/approvalTimeoutSec).
    "jobApprovals",
    # 1.68.0: machine-readable lsp reasons — LspCatalogEntry / LspInstallItem
    # reason (needs_toolchain, npm_failed), toolchain, hint, url, npmCode, detail.
    "lspInstallReasons",
    # 1.68.0: lsp.install {withToolchain, personConfirm} installs the .NET SDK
    # (needsToolchain offer, progress phase/percent), lsp.toolchain.remove/cancel.
    "lspToolchainInstall",
    # 1.69.0: HostToolSpec.kind ('act' | 'read'): an acting host tool (a repl that
    # clicks, a form fill) is work, not reading, for the answer-now note.
    "hostToolKind",
    # 1.69.0: an occurrence missed past scheduler.catchUpSec (or its host's
    # window) is a job.runs row 'missed' with its reason, job.event missed
    # {scheduledAt, reason, error, title, nextRunAt}, a notice to the job's
    # channel or the bound person's private chat, and JobInfo.lastError.
    "jobMissedRuns",
    # 1.70.0: a model that answers only in its reasoning is asked again (with
    # thinking off where the provider can switch it); if it still writes nothing
    # visible, the turn ends with message.done reasoningFallback: true carrying
    # that reasoning under a marker line, not an empty_response error.
    "reasoningOnlyFallback",
    # 1.73.0 (M37): the snowpea TUI takes --session <id> (session.resume, no new
    # session) and emits OSC 7501 status inside a terminal session.
    "tuiSessionAttach",
    # 1.73.0 (M37 T1): PTY terminals held by the daemon (terminal.* methods and
    # events) and their status (OSC 7501, screen rules, hook reports).
    "terminalSessions",
    "terminalStatus",
    # 1.73.0 (M37): integration.list|install|uninstall and integration.changed —
    # snowpea's hook in Claude Code / Codex / Cursor / agy hook configs.
    "cliIntegrations",
    # 1.73.0 (M37): web.tunnel.status|start|stop|setToken|install|unlock and the
    # web.tunnel.changed event.
    "webTunnel",
    # 1.73.0 (M37 T3): external.open|create {transport: "pty"} run the CLI as a
    # terminal session (ExternalTerminal.kind "pty"); terminal profiles and options.
    "externalPty",
]

#: M19 capabilities (1.53.0) declared with the contract but not advertised in
#: ``system.hello`` until the work package that implements them lands; that
#: package moves its name into :data:`CAPABILITIES`.  Their methods answer
#: ``not_implemented`` until then.
PENDING_CAPABILITIES: dict[str, str] = {
    # M37 (1.73.0): each moves into CAPABILITIES when its work package lands.
}

#: ``models.categories`` slots core routes by (1.33.0): ``fast`` and
#: ``standard`` since 1.7.0, ``deep`` and ``vision`` since 1.33.0.
MODEL_CATEGORIES: list[str] = ["fast", "standard", "deep", "vision"]

#: Where the daemon listens; mirrored into the schema dump for the SDK.
TRANSPORT: dict[str, Any] = {
    "ws": "/ws",
    "http": {"health": "/health", "version": "/version", "schema": "/protocol.json"},
}

#: Methods implemented at M1; everything else answers ``not_implemented``.
IMPLEMENTED_METHODS: frozenset[str] = frozenset(
    {
        "system.hello",
        "system.info",
        "system.health",
        "system.shutdown",
        "system.checkUpdate",
        "system.update",
        "system.restart",
        "system.reloadSettings",
        "session.create",
        "session.resume",
        "session.list",
        "session.deleteSaved",
        "session.close",
        "session.delete",
        "session.prompt",
        "file.complete",
        "session.interrupt",
        "session.compact",
        "session.setMode",
        "session.setModel",
        "session.setEffort",
        "audio.capabilities",
        "audio.transcribe",
        "audio.speak",
        "audio.record.start",
        "audio.record.stop",
        "audio.install",
        "audio.voices",
        "command.list",
        "command.run",
        "tool.list",
        "approval.list",
        "approval.respond",
        "question.list",
        "question.respond",
        "provider.list",
        "provider.models",
        "provider.visionReset",
        "session.trajectory",
        "session.fork",
        "session.lineage",
        "extension.list",
        "extension.enable",
        "extension.disable",
        "extension.reload",
        "extension.configure",
        "workspace.list",
        "workspace.add",
        "workspace.rename",
        "workspace.remove",
        "workspace.reorder",
        "session.pin",
        "session.archive",
        "goal.get",
        "goal.set",
        "goal.pause",
        "goal.resume",
        "goal.clear",
        # M19 WP2 (1.53.0, capability jarvisDelegation).
        "jarvis.ledger",
        "jarvis.delegation.list",
        "jarvis.delegation.set",
        "jarvis.delegation.revoke",
        "jarvis.takeover",
        "jarvis.handback",
        "provider.configure",
        "provider.remove",
        "provider.model.remove",
        "provider.model.unhide",
        "provider.loginWeb",
        "checkpoint.list",
        "checkpoint.diff",
        "checkpoint.restore",
        "checkpoint.delete",
        "memory.search",
        "memory.write",
        "memory.list",
        "memory.delete",
        "memory.update",
        "memory.confirm",
        "gateway.bind",
        "gateway.list",
        "gateway.unbind",
        "gateway.sync",
        "job.schedule",
        "job.preview",
        "web.search",
        "llm.sample",
        "job.list",
        "job.cancel",
        "job.runNow",
        "job.delete",
        "job.nextRunForClient",
        "job.update",
        "job.runs",
        "agent.create",
        "agent.list",
        "agent.bindChannel",
        "agent.delete",
        "team.start",
        "team.status",
        "skill.list",
        "skill.search",
        "skill.install",
        "skill.reload",
        "skill.remove",
        "skill.create",
        "skill.learn",
        "skill.read",
        "skill.write",
        "settings.get",
        "settings.set",
        "settings.acceptDiskChange",
        "setup.catalog",
        "lsp.status",
        "lsp.catalog",
        "lsp.install",
        "lsp.restart",
        "lsp.toolchain.remove",
        "lsp.toolchain.cancel",
        "mcp.list",
        "mcp.add",
        "mcp.remove",
        "mcp.update",
        "mcp.test",
        "mcp.reload",
        "mcp.catalog",
        "mcp.authorize",
        "mcp.authorize.complete",
        "mcp.deauthorize",
        "tool.register",
        "tool.unregister",
        "tool.progress",
        "approval.ask",
        "session.attach",
        "session.setBrowserProvider",
        "session.setAgent",
        "session.steer",
        "session.continue",
        "session.setActive",
        "session.events",
        "turn.outcome",
        "session.toolContent",
        "approval.evaluate",
        "fs.stat",
        "fs.list",
        "fs.glob",
        "fs.read",
        "fs.write",
        "fs.hash",
        "session.artifacts",
        "session.attachments",
        "session.rename",
        "session.notice",
        "memory.ingest",
        "usage.summary",
        "setup.status",
        "setup.applyDefaults",
        "provider.test",
        "site.get",
        "site.put",
        "site.mark",
        "site.markPart",
        "site.playbook.get",
        "site.playbook.put",
        "site.playbook.mark",
        "site.playbook.edit",
        "site.playbook.delete",
        "site.stats",
        "site.list",
        "site.delete",
        # M19 WP3 (capability jarvis)
        "jarvis.status",
        "jarvis.enable",
        "jarvis.disable",
        "jarvis.overview",
        "jarvis.project.list",
        "jarvis.project.rename",
        "jarvis.project.alias",
        "jarvis.escalations.list",
        "jarvis.escalations.dismiss",
        # 1.56.0 (capability jarvisSessionSwitch)
        "jarvis.session.set",
        # M19 WP5 (1.53.0): the external.* family, answered by the common layer
        # (external_unavailable until an adapter for the CLI is registered).
        "external.clis",
        "external.clis.confirmPath",
        "external.list",
        "external.open",
        "external.read",
        "external.send",
        "external.setMode",
        "external.interrupt",
        "external.create",
        "external.close",
        "external.get",
        "external.unwatch",
        "external.terminalSnapshot",
        # 1.67.0 (capability externalModelSwitch)
        "external.setModel",
        # M19 WP8 (capabilities mcpPresets, jarvisBriefing)
        "mcp.preset.list",
        "mcp.preset.apply",
        "mcp.preset.remove",
        "mcp.preset.check",
        "jarvis.briefing.get",
        "jarvis.briefing.set",
        # 1.54.0 (capability webRemoteControl)
        "web.remote.status",
        "web.remote.listen",
        "web.remote.stop",
        "web.remote.setPassword",
        "web.remote.clearPassword",
        "web.remote.rotateToken",
        # 1.54.0 (capability securityLists)
        "security.presets.list",
        "security.get",
        "security.check",
        # 1.55.0 (capability projectTrust)
        "project.trust.status",
        "project.trust.set",
        "project.trust.revoke",
        # 1.59.0 (capability improvementFeedback)
        "feedback.preview",
        "feedback.send",
    }
)


def protocol_major(version: str) -> str:
    """Major component of a semver string (``"0.1.0"`` -> ``"0"``)."""
    return version.split(".", 1)[0]


def dump_schema() -> dict[str, Any]:
    """Machine-readable description of the whole protocol.

    This is exactly what ``GET /protocol.json`` returns and what
    ``scripts/gen_protocol.py`` consumes.  Schemas are plain
    ``model_json_schema()`` output, so local ``#/$defs/...`` references are
    resolved by the consumer.  ``version`` and ``protocolVersion`` are aliases
    of each other and always carry the same value.
    """
    methods: dict[str, Any] = {}
    for name, method in METHODS.items():
        entry: dict[str, Any] = {
            "direction": method.direction,
            "params": method.params.model_json_schema(),
            "result": method.result.model_json_schema(),
        }
        if method.summary:
            entry["summary"] = method.summary
        methods[name] = entry
    return {
        "version": PROTOCOL_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "serverVersion": SERVER_VERSION,
        "methods": methods,
        "events": {name: model.model_json_schema() for name, model in EVENTS.items()},
        "sessionEventKinds": {
            kind: model.model_json_schema() for kind, model in SESSION_EVENT_MODELS.items()
        },
        "errorCodes": list(ERROR_CODES),
        # details.reason values and the jarvis_person_only details.kind values
        # a client may switch on (1.55.0).
        "errorDetails": {
            "reasons": {
                reason: {"code": code, "meaning": meaning}
                for reason, (code, meaning) in DETAIL_REASONS.items()
            },
            "personOnlyKinds": list(PERSON_ONLY_KINDS),
        },
        "capabilities": list(CAPABILITIES),
        "transport": TRANSPORT,
    }
