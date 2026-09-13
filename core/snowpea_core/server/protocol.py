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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from snowpea_core import __version__ as _core_version
from snowpea_core.server.errors import ERROR_CODES

PROTOCOL_VERSION = "1.5.0"
SERVER_VERSION = _core_version

Mode = Literal["plan", "accept", "auto"]
#: ``config`` marks a call that changes snowpea's own settings, credentials or
#: state.  It is never a silent ``allow``: plan denies it, accept and auto both
#: ask, and the allowlist may not promote it (CORE-search-fix).
PermissionTag = Literal["read", "write", "exec", "network", "send", "config"]
ToolState = Literal["active", "inactive"]
#: ``builtin``, ``global``, ``project``, ``skill`` or ``plugin:<plugin name>``;
#: a free string because a plugin names itself (M6 contract §1).
CommandSource = str
Decision = Literal["allow", "deny"]
ApprovalScope = Literal["once", "session", "project", "always"]
AllowlistScope = Literal["session", "project", "always"]
BackendKind = Literal["local", "docker", "ssh"]
#: What a language server is doing right now (M13 contract §4).
LspServerState = Literal["starting", "ready", "broken", "stopped"]
#: State of a vendor's stored credential (CORE-codex-login).
AuthStatus = Literal["unconfigured", "active", "expired"]
TurnReason = Literal["complete", "interrupted", "error", "denied", "timeout"]
#: Why a queued prompt left the queue: it started running, or it was dropped.
QueuedTurnReason = Literal["started", "dropped"]
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


class Payload(BaseModel):
    """Base for every wire model: JSON objects, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class Empty(Payload):
    """A method that takes no parameters (``params`` is still an object)."""


class Ok(Payload):
    ok: bool = Field(default=True, description="True when the call succeeded.")


# --------------------------------------------------------------------------
# system.*
# --------------------------------------------------------------------------


class HelloParams(Payload):
    token: str = Field(description="Shared secret read from $SNOWPEA_HOME/token or daemon.json.")
    clientVersion: str = Field(description="Version string of the connecting client.")
    protocolVersion: str = Field(description="Protocol semver the client speaks; major must match.")


class HelloResult(Payload):
    protocolVersion: str = Field(description="Protocol semver the daemon speaks.")
    serverVersion: str = Field(description="Version of the running daemon.")
    capabilities: list[str] = Field(
        default_factory=list, description="Feature flags this daemon supports."
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


# --------------------------------------------------------------------------
# session.*
# --------------------------------------------------------------------------


class Attachment(Payload):
    """A file, image or inline text sent along with a prompt.

    Exactly one of ``path``, ``data`` and ``text`` carries the content.  The
    daemon sniffs the real media type from the bytes, so ``mimeType`` is a
    hint it may overrule; anything over 20MB is refused with
    ``invalid_params``.
    """

    kind: Literal["file", "image", "text"] = Field(
        default="file", description="Attachment flavour."
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
        default=None, description="Starting mode; defaults to the project setting."
    )
    provider: str | None = Field(
        default=None, description="Chat provider vendor; defaults to configured."
    )
    model: str | None = Field(
        default=None, description="Model id; defaults to the provider's default."
    )
    agent: str | None = Field(default=None, description="Named agent whose persona to load.")
    maxConcurrent: int | None = Field(
        default=None, description="Override for concurrent subagents."
    )
    originSurface: str | None = Field(
        None, description="Surface that owns approvals for this session (TUI, gateway, ...)."
    )


class SessionCreateResult(Payload):
    sessionId: str = Field(description="Id of the new session.")


class SessionEvent(Payload):
    """One entry of a session's ordered event log."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number; resume replays after it.")
    kind: str = Field(description="Event kind; see sessionEventKinds in the schema dump.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")


class SessionResumeParams(Payload):
    sessionId: str = Field(description="Session to resume.")
    afterSeq: int | None = Field(default=None, description="Replay only events with a greater seq.")


class SessionResumeResult(Payload):
    sessionId: str = Field(description="Session that was resumed.")
    events: list[SessionEvent] = Field(
        default_factory=list, description="Missed events in seq order."
    )


class SessionSummary(Payload):
    """One row of ``session.list``."""

    sessionId: str = Field(description="Session id.")
    workdir: str = Field(description="Absolute working directory.")
    mode: Mode = Field(description="Current permission mode.")
    provider: str | None = Field(default=None, description="Chat provider vendor in use.")
    model: str | None = Field(default=None, description="Model id in use.")
    originSurface: str | None = Field(default=None, description="Surface that owns approvals.")
    createdAt: str = Field(description="UTC ISO-8601 creation timestamp.")
    seq: int = Field(default=0, description="Sequence number of the latest event.")
    contextUsed: int = Field(
        default=0, description="Tokens the session's current prompt occupies (CORE-context)."
    )
    contextWindow: int | None = Field(
        default=None, description="Context window of the session's model; null when unknown."
    )
    lastPrompt: str | None = Field(default=None, description="Latest saved user input.")


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


class SessionListParams(Payload):
    includeClosed: bool = Field(default=False, description="Include persisted closed sessions.")
    workdir: str | None = Field(default=None, description="Only sessions rooted here.")


class SessionDeleteParams(Payload):
    sessionId: str | None = Field(default=None, description="Delete one saved session.")
    workdir: str | None = Field(default=None, description="Delete saved sessions rooted here.")
    all: bool = Field(default=False, description="Delete saved sessions from every directory.")


class SessionDeleteResult(Payload):
    deleted: int = 0


class SessionIdParams(Payload):
    sessionId: str = Field(description="Target session.")


class SessionPromptParams(Payload):
    sessionId: str = Field(description="Session to prompt.")
    text: str = Field(description="User text; a leading '/' is parsed as a slash command.")
    attachments: list[Attachment] | None = Field(
        default=None, description="Files or images to include."
    )


class TurnResult(Payload):
    turnId: str = Field(description="Id of the started turn; turn.done carries it back.")


class SessionSetModeParams(Payload):
    sessionId: str = Field(description="Session to change.")
    mode: Mode = Field(description="New permission mode.")


class SessionSetModeResult(Payload):
    mode: Mode = Field(description="Mode now in effect.")


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

    path: str | None = Field(default=None, description="Audio file on the daemon's machine.")
    data: str | None = Field(default=None, description="Base64 (or data-URI) audio.")
    mime: str | None = Field(default=None, description="Media type of the audio, e.g. audio/wav.")
    language: str | None = Field(default=None, description="BCP-47 hint for the backend.")
    sessionId: str | None = Field(default=None, description="Session the audio belongs to.")


class AudioTranscribeResult(Payload):
    text: str = Field(description="What the backend heard.")
    provider: str = Field(description="Backend that produced the transcript.")


class AudioSpeakParams(Payload):
    """Synthesise speech; the client plays it unless ``play`` is set."""

    text: str = Field(description="What to say.")
    sessionId: str | None = Field(default=None, description="Session the audio belongs to.")
    voice: str | None = Field(default=None, description="Voice id; defaults to the setting.")
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


class CommandListResult(Payload):
    commands: list[CommandInfo] = Field(
        default_factory=list, description="Available slash commands."
    )


class CommandRunParams(Payload):
    sessionId: str = Field(description="Session to run the command in.")
    name: str = Field(description="Command name without the leading slash.")
    args: str = Field(default="", description="Raw argument string, as typed after the name.")


class ToolInfo(Payload):
    """One registered tool."""

    name: str = Field(description="Tool name as the model calls it.")
    category: str = Field(description="Grouping used by the UI.")
    permissionTag: PermissionTag = Field(description="Permission class checked against the mode.")
    state: ToolState = Field(
        default="active", description="Inactive tools are hidden from the model."
    )
    source: str = Field(default="builtin", description="builtin, skill, plugin or MCP server name.")
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


class ToolListResult(Payload):
    tools: list[ToolInfo] = Field(default_factory=list, description="Registered tools.")


# --------------------------------------------------------------------------
# approval.* / permission.*
# --------------------------------------------------------------------------


class ApprovalRequest(Payload):
    """A tool call waiting for a human decision."""

    requestId: str = Field(description="Id to answer with approval.respond.")
    sessionId: str = Field(description="Session whose turn is blocked.")
    tool: str = Field(description="Tool the model wants to run.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments it wants to use.")
    risk: str = Field(default="low", description="Risk hint for the UI.")
    timeoutSec: int = Field(default=300, description="Seconds before the request auto-denies.")
    scopeHint: ApprovalScope = Field(default="once", description="Scope the UI should preselect.")
    note: str = Field(
        default="",
        description=(
            "Extra warning shown with the prompt, e.g. \"modifies snowpea configuration\"."
        ),
    )


class ApprovalListResult(Payload):
    requests: list[ApprovalRequest] = Field(
        default_factory=list, description="Approvals still pending."
    )


class ApprovalRespondParams(Payload):
    requestId: str = Field(description="Request being answered.")
    decision: Decision = Field(description="allow runs the tool, deny ends the turn.")
    scope: ApprovalScope = Field(default="once", description="How long the decision applies.")


class ApprovalAnswer(Payload):
    """Result of the server-initiated ``approval.request``."""

    decision: Decision = Field(description="The human's decision.")
    scope: ApprovalScope = Field(default="once", description="How long the decision applies.")


class AllowlistAddParams(Payload):
    pattern: str = Field(description="Glob or command prefix promoted from ask to allow.")
    scope: AllowlistScope = Field(default="project", description="Where the pattern is stored.")


class AllowlistAddResult(Payload):
    patternId: str = Field(description="Id used to remove the pattern later.")


class AllowlistListParams(Payload):
    scope: AllowlistScope | None = Field(default=None, description="Filter by scope; omit for all.")


class AllowlistPattern(Payload):
    """One stored allowlist entry."""

    patternId: str = Field(description="Stable id of the pattern.")
    pattern: str = Field(description="The glob or command prefix.")
    scope: AllowlistScope = Field(description="Where it is stored.")


class AllowlistListResult(Payload):
    patterns: list[AllowlistPattern] = Field(
        default_factory=list, description="Stored allowlist entries."
    )


class AllowlistRemoveParams(Payload):
    patternId: str = Field(description="Pattern to delete.")


# --------------------------------------------------------------------------
# provider.* / backend.*
# --------------------------------------------------------------------------


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


class ProviderListResult(Payload):
    providers: list[ProviderInfo] = Field(default_factory=list, description="Known chat providers.")


class ProviderModelsParams(Payload):
    vendor: str | None = Field(
        default=None, description="Vendor to query; defaults to the configured one."
    )


class ProviderModelsResult(Payload):
    vendor: str = Field(description="Vendor the listing came from.")
    models: list[str] = Field(
        default_factory=list, description="Model ids the vendor's endpoint reports."
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


class ProviderConfigureParams(Payload):
    vendor: str = Field(description="Vendor to configure.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Vendor-specific settings, including credentials."
    )


class ProviderLoginWebParams(Payload):
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


class BackendSetParams(Payload):
    sessionId: str = Field(description="Session whose execution backend changes.")
    kind: BackendKind = Field(description="Where tools execute.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Backend settings, e.g. container or SSH target."
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
    state: Literal["running", "done", "failed"] = Field(
        default="running", description="Overall state."
    )
    tasks: list[TeamTask] = Field(default_factory=list, description="Task board contents.")
    workers: int = Field(default=0, description="How many workers the run was started with.")
    task: str = Field(default="", description="The task the team was given.")
    worktrees: list[str] = Field(
        default_factory=list, description="Worker worktrees that exist right now."
    )


# --------------------------------------------------------------------------
# job.*
# --------------------------------------------------------------------------


class JobScheduleParams(Payload):
    spec: str = Field(description="Cron expression or natural-language schedule.")
    task: str = Field(description="Prompt run on each firing.")
    mode: Mode = Field(default="accept", description="Permission mode for the unattended run.")
    channel: str | None = Field(
        default=None, description="Gateway channel that receives the output."
    )
    agent: str | None = Field(default=None, description="Named agent that runs the task.")
    workdir: str | None = Field(
        default=None, description="Working directory for the run; defaults to the daemon's home."
    )


class JobScheduleResult(Payload):
    jobId: str = Field(description="Id of the scheduled job.")
    nextRunAt: str | None = Field(
        default=None, description="UTC ISO-8601 time of the first firing."
    )


class JobInfo(Payload):
    """One scheduled job."""

    jobId: str = Field(description="Job id.")
    spec: str = Field(description="Schedule as given.")
    task: str = Field(description="Prompt run on each firing.")
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


class JobListResult(Payload):
    jobs: list[JobInfo] = Field(default_factory=list, description="Known jobs.")


class JobIdParams(Payload):
    jobId: str = Field(description="Target job.")


# --------------------------------------------------------------------------
# gateway.*
# --------------------------------------------------------------------------


class GatewayBindParams(Payload):
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


class MemorySearchResult(Payload):
    hits: list[MemoryHit] = Field(default_factory=list, description="Matches, best first.")


class MemoryWriteParams(Payload):
    text: str = Field(description="Text to remember.")
    tags: list[str] = Field(default_factory=list, description="Tags for later filtering.")
    namespace: str | None = Field(
        default=None, description='Memory namespace to write into; defaults to "default".'
    )


class MemoryWriteResult(Payload):
    id: str = Field(description="Id of the stored memory.")


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
            "installed entry, or the marketplace that offered it."
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


class SkillSearchResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Matching skills.")
    unavailable: list[str] = Field(
        default_factory=list,
        description=(
            "Sources that could not be reached, as '<source>: <reason>'. "
            "Empty skills with a non-empty list means offline, not no match."
        ),
    )


class SkillInstallParams(Payload):
    source: str = Field(description="Path, URL or registry name to install from.")


class SkillListResult(Payload):
    skills: list[SkillInfo] = Field(default_factory=list, description="Installed skills.")


class SkillRemoveParams(Payload):
    name: str = Field(description="Installed plugin or skill to delete.")


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
    scope: SettingsScope = Field(
        default="global",
        description=(
            '"global" writes $SNOWPEA_HOME/settings.json; '
            '"project" writes <workdir>/.snowpea/settings.json.'
        ),
    )
    patch: dict[str, Any] = Field(description="Fields to deep-merge into the existing settings.")
    workdir: str | None = Field(
        default=None, description='Project root; required when scope is "project".'
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


# --------------------------------------------------------------------------
# session.event kinds
# --------------------------------------------------------------------------


class MessageDelta(Payload):
    """Streaming assistant text."""

    kind: Literal["message.delta"] = "message.delta"
    text: str = Field(description="Text fragment to append to the current message.")


class MessageDone(Payload):
    """A completed message."""

    kind: Literal["message.done"] = "message.done"
    text: str = Field(description="Full message text.")
    role: Literal["assistant", "user", "system"] = Field(
        "assistant", description="Who produced the message."
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


class DiffEvent(Payload):
    """A file was edited."""

    kind: Literal["diff"] = "diff"
    path: str = Field(description="File that changed.")
    patch: str = Field(description="Unified diff of the change.")


#: Lifecycle of one delegated subagent run (M7 contract §3).
SubagentStatus = Literal["queued", "running", "done", "error"]


class SubagentSpawn(Payload):
    """A subagent started."""

    kind: Literal["subagent.spawn"] = "subagent.spawn"
    agentId: str = Field(description="Id correlating this subagent's events.")
    name: str = Field(default="", description="Named agent that was spawned.")
    task: str = Field(default="", description="Task it was given.")
    status: SubagentStatus = Field(
        default="queued", description="State at spawn: queued until a concurrency slot frees up."
    )
    sessionId: str | None = Field(
        default=None, description="The subagent's own session, once it has one."
    )


class SubagentUpdate(Payload):
    """Progress from a running subagent."""

    kind: Literal["subagent.update"] = "subagent.update"
    agentId: str = Field(description="Subagent reporting progress.")
    status: SubagentStatus = Field(default="running", description="Lifecycle state.")
    text: str = Field(default="", description="Human-readable progress text.")
    lastText: str = Field(default="", description="Most recent text the subagent produced.")
    name: str = Field(default="", description="Named agent that is running, when there is one.")
    sessionId: str | None = Field(default=None, description="The subagent's own session.")


class SubagentUsage(Payload):
    """Tokens one subagent consumed."""

    inputTokens: int = Field(default=0, description="Prompt tokens the subagent used.")
    outputTokens: int = Field(default=0, description="Completion tokens the subagent used.")


class SubagentDone(Payload):
    """A subagent finished."""

    kind: Literal["subagent.done"] = "subagent.done"
    agentId: str = Field(description="Subagent that finished.")
    ok: bool = Field(default=True, description="False when it failed.")
    result: str = Field(default="", description="Final report.")
    status: SubagentStatus = Field(default="done", description="Terminal state: done or error.")
    summary: str = Field(default="", description="The subagent's final answer.")
    usage: SubagentUsage = Field(
        default_factory=SubagentUsage, description="Tokens the subagent consumed."
    )
    name: str = Field(default="", description="Named agent that ran, when there was one.")
    sessionId: str | None = Field(default=None, description="The subagent's own session.")


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


class ModeChanged(Payload):
    """The session's permission mode changed."""

    kind: Literal["mode.changed"] = "mode.changed"
    mode: Mode = Field(description="Mode now in effect.")


class BackendChanged(Payload):
    """The session's execution backend was replaced."""

    kind: Literal["backend.changed"] = "backend.changed"
    backend: BackendKind = Field(description="Where tools now execute.")


class UsageEvent(Payload):
    """Token usage for the turn."""

    kind: Literal["usage"] = "usage"
    inputTokens: int = Field(default=0, description="Prompt tokens consumed.")
    outputTokens: int = Field(default=0, description="Completion tokens produced.")


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


class CompactionEvent(Payload):
    """The conversation was summarised and replaced (CORE-context)."""

    kind: Literal["compaction"] = "compaction"
    before: int = Field(default=0, description="Estimated tokens the history held before.")
    after: int = Field(default=0, description="Estimated tokens the history holds now.")
    summaryChars: int = Field(default=0, description="Length of the summary in characters.")
    auto: bool = Field(
        default=False, description="True when the auto-compaction threshold triggered it."
    )
    kept: int = Field(default=0, description="Messages kept verbatim after the summary.")


class ErrorEvent(Payload):
    """Something went wrong inside a turn."""

    kind: Literal["error"] = "error"
    code: str = Field(description="One of the protocol error codes.")
    message: str = Field(description="Human-readable detail.")


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


class ModelChanged(Payload):
    """The session's provider/model changed (``/model``, ``session.setModel``).

    The HUD is fed once by ``session/ready`` and had no way to learn about a
    later pin, so it showed a stale model for the rest of the session
    (CORE-model-assignment B-P2-3).
    """

    kind: Literal["model.changed"] = "model.changed"
    provider: str | None = Field(default=None, description="Vendor now in effect.")
    model: str | None = Field(default=None, description="Model id now in effect.")


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


class TurnDequeued(Payload):
    """A queued prompt left the queue - it started, or it was dropped.

    ``reason="dropped"`` is emitted for every prompt flushed by
    ``session.interrupt``, so a surface can clear its queue indicator.
    """

    kind: Literal["turn.dequeued"] = "turn.dequeued"
    turnId: str = Field(description="Turn id that left the queue.")
    reason: QueuedTurnReason = Field(
        default="started", description="started = it is now running, dropped = it was discarded."
    )
    queued: int = Field(default=0, description="Prompts still waiting after this one left.")


class TurnDone(Payload):
    """A turn ended, for any reason."""

    kind: Literal["turn.done"] = "turn.done"
    turnId: str = Field(description="Turn that ended.")
    reason: TurnReason = Field(default="complete", description="Why the turn ended.")


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


SessionEventPayload = Annotated[
    MessageDelta
    | MessageDone
    | ToolCallEvent
    | ToolResultEvent
    | DiffEvent
    | SubagentSpawn
    | SubagentUpdate
    | SubagentDone
    | TeamTaskUpdate
    | ModeChanged
    | BackendChanged
    | ModelChanged
    | UsageEvent
    | ContextEvent
    | CompactionEvent
    | ErrorEvent
    | AudioSpoken
    | TurnQueued
    | TurnDequeued
    | TurnDone
    | LspDiagnostics,
    Field(discriminator="kind"),
]

#: ``session.event`` payload model per ``kind`` (contract §1).
SESSION_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "message.delta": MessageDelta,
    "message.done": MessageDone,
    "tool.call": ToolCallEvent,
    "tool.result": ToolResultEvent,
    "diff": DiffEvent,
    "subagent.spawn": SubagentSpawn,
    "subagent.update": SubagentUpdate,
    "subagent.done": SubagentDone,
    "team.task.update": TeamTaskUpdate,
    "mode.changed": ModeChanged,
    "backend.changed": BackendChanged,
    "model.changed": ModelChanged,
    "usage": UsageEvent,
    "context": ContextEvent,
    "compaction": CompactionEvent,
    "error": ErrorEvent,
    "audio.spoken": AudioSpoken,
    "turn.queued": TurnQueued,
    "turn.dequeued": TurnDequeued,
    "turn.done": TurnDone,
    "lsp.diagnostics": LspDiagnostics,
}

SESSION_EVENT_KINDS: tuple[str, ...] = tuple(SESSION_EVENT_MODELS)


class SessionEventNotification(Payload):
    """Server notification carrying one session event."""

    sessionId: str = Field(description="Session the event belongs to.")
    seq: int = Field(description="Monotonic per-session sequence number.")
    kind: str = Field(description="Event kind; see sessionEventKinds for the payload schema.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")
    ts: str = Field(description="UTC ISO-8601 timestamp.")


class SessionEventKindEnvelope(Payload):
    """Typed view of a ``session.event`` payload, discriminated by ``kind``."""

    event: SessionEventPayload


class ApprovalResolvedNotification(Payload):
    """An approval was answered elsewhere; stop showing it."""

    requestId: str = Field(description="Request that was resolved.")
    decision: Decision = Field(description="The decision that was recorded.")
    by: str = Field(description="Surface or user that answered.")


class ApprovalPendingNotification(Payload):
    """An unattended approval is waiting; any authenticated surface may answer."""

    request: ApprovalRequest = Field(description="The request now in the shared queue.")


class CommandsChangedNotification(Payload):
    """The slash-command table changed; re-read it (``skill.reload``, install)."""

    commands: list[CommandInfo] = Field(
        default_factory=list, description="The command table as it stands now."
    )
    reason: str = Field(default="reload", description="Why the table changed.")


class JobEventNotification(Payload):
    """Progress from a scheduled job."""

    jobId: str = Field(description="Job the event belongs to.")
    kind: Literal["started", "finished", "failed", "denied"] = Field(
        description="Where the run got to."
    )
    payload: dict[str, Any] = Field(default_factory=dict, description="Kind-specific body.")


class UpdateProgressNotification(Payload):
    """Progress of the upgrade ``system.update`` started."""

    phase: UpdatePhase = Field(description="Where the upgrade got to.")
    message: str = Field(default="", description="One line for humans.")


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


class LspStatusResult(Payload):
    """``lsp.status`` — every server, for the TUI's `lsp` segment and `/lsp`."""

    servers: list[LspServerStatus] = Field(
        default_factory=list, description="One row per (server, root) pair."
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
        description="True when lsp.autoInstall could obtain it (npm/pip/go); "
        "False means it must already be on PATH."
    )
    installHint: str | None = Field(
        default=None, description="A command that installs it manually, e.g. 'pip install ty'."
    )
    disabled: bool = Field(
        description="True when settings (lsp.disabled, a default-off id, or an "
        "explicit lsp.servers override) keep this server from starting."
    )


class LspCatalogResult(Payload):
    """``lsp.catalog`` — every registered server, for settings UIs (M13 §4)."""

    servers: list[LspCatalogEntry] = Field(
        default_factory=list, description="One row per registered server id."
    )


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
            Empty,
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
            "session.prompt",
            SessionPromptParams,
            TurnResult,
            "Send user text to a session and start a turn.",
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
            Ok,
            "Answer a pending approval and unblock the turn.",
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
            "provider.models",
            ProviderModelsParams,
            ProviderModelsResult,
            "Ask a vendor's endpoint which models it serves.",
        ),
        _m(
            "provider.configure",
            ProviderConfigureParams,
            Ok,
            "Store settings and credentials for a provider.",
        ),
        _m(
            "provider.loginWeb",
            ProviderLoginWebParams,
            ProviderLoginWebResult,
            "Start a browser-based login flow for a provider.",
        ),
        _m(
            "backend.set",
            BackendSetParams,
            Ok,
            "Choose where a session's tools execute: local, docker or ssh.",
        ),
        _m("agent.list", Empty, AgentListResult, "List the named agents that are defined."),
        _m(
            "agent.create",
            AgentCreateParams,
            AgentCreateResult,
            "Define a named agent from a description.",
        ),
        _m("agent.spawn", AgentSpawnParams, AgentSpawnResult, "Run a named agent on a task."),
        _m(
            "agent.bindChannel",
            AgentBindChannelParams,
            Ok,
            "Route a gateway channel to a named agent.",
        ),
        _m("agent.delete", AgentDeleteParams, Ok, "Delete a named agent."),
        _m("team.start", TeamStartParams, TeamStartResult, "Split a task across parallel workers."),
        _m("team.status", TeamStatusParams, TeamStatusResult, "Inspect a team's task board."),
        _m(
            "job.schedule",
            JobScheduleParams,
            JobScheduleResult,
            "Schedule a prompt to run unattended.",
        ),
        _m("job.list", Empty, JobListResult, "List scheduled jobs and their next run times."),
        _m("job.cancel", JobIdParams, Ok, "Cancel a scheduled job."),
        _m("job.runNow", JobIdParams, Ok, "Fire a scheduled job immediately."),
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
        _m("skill.search", SkillSearchParams, SkillSearchResult, "Search available skills."),
        _m(
            "skill.install", SkillInstallParams, Ok, "Install a skill from a path, URL or registry."
        ),
        _m("skill.list", Empty, SkillListResult, "List installed skills."),
        _m("skill.reload", Empty, Ok, "Reload skills from disk without restarting."),
        _m("skill.remove", SkillRemoveParams, Ok, "Delete an installed skill or plugin."),
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
    )
}

EVENTS: dict[str, type[BaseModel]] = {
    "session.event": SessionEventNotification,
    "approval.pending": ApprovalPendingNotification,
    "approval.resolved": ApprovalResolvedNotification,
    "job.event": JobEventNotification,
    "gateway.event": GatewayEventNotification,
    "commands.changed": CommandsChangedNotification,
    "system.updateProgress": UpdateProgressNotification,
    "provider.loginProgress": ProviderLoginProgressNotification,
    "settings.changed": SettingsChangedNotification,
}

CAPABILITIES: list[str] = [
    "audio",
    "lsp",
    "sessions",
    "approvals",
    "commands",
    "tools",
    "settings",
    "setup",
    "update",
]

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
        "session.prompt",
        "session.interrupt",
        "session.compact",
        "session.setMode",
        "session.setModel",
        "audio.capabilities",
        "audio.transcribe",
        "audio.speak",
        "audio.record.start",
        "audio.record.stop",
        "command.list",
        "command.run",
        "tool.list",
        "approval.list",
        "approval.respond",
        "provider.list",
        "provider.models",
        "provider.configure",
        "provider.loginWeb",
        "backend.set",
        "memory.search",
        "memory.write",
        "gateway.bind",
        "gateway.list",
        "gateway.unbind",
        "gateway.sync",
        "job.schedule",
        "job.list",
        "job.cancel",
        "job.runNow",
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
        "settings.get",
        "settings.set",
        "setup.catalog",
        "lsp.status",
        "lsp.catalog",
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
        "capabilities": list(CAPABILITIES),
        "transport": TRANSPORT,
    }
