"""The adapter interface every external CLI implements (M19 WP5).

An adapter knows one CLI: how to find it, where it keeps its sessions, how to
turn a transcript into timeline items, how to tell whether a session is open
somewhere else, and how to run a session over the CLI's own structured
protocol.  Everything else — subscriptions, ``external.state``, approvals,
turn bookkeeping, idle close, takeover, process limits — is the manager's.

Where a session's process lives (``host``):

* ``none`` — nothing runs it; snowpea may continue it (``external.open``).
* ``terminal`` — the person runs it in some other terminal: view only, a live
  tail; once that process exits it can be taken over.
* ``tmux`` — a shared terminal snowpea started (Claude only, Linux/macOS).
* ``snowpea`` — the daemon runs it over the structured transport, owned by
  an :class:`ExternalChannel`.

Writing an adapter: subclass :class:`ExternalAdapter`, fill in the class
attributes and the abstract methods, return an :class:`ExternalChannel`
subclass from :meth:`ExternalAdapter.channel`.  Start processes only through
``host.supervisor.spawn(build_argv(...), cwd=spec.cwd, env=host.env, ...)``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from snowpea_core.external import modes as native
from snowpea_core.external import proc
from snowpea_core.external.modes import NativeMode
from snowpea_core.external.proc import (
    ExternalError,
    PinnedExecutable,
    ProcessSupervisor,
    UnrestrictedGrant,
)
from snowpea_core.external.timeline import TimelineBuilder, TimelineItem
from snowpea_core.server import errors

Host = Literal["none", "terminal", "tmux", "snowpea"]
Approvals = Literal["bridged", "person", "degraded", "none"]
Mode = Literal["default", "readonly", "unrestricted"]
Status = Literal["idle", "running", "error"]

ALL_MODES: tuple[str, ...] = ("default", "readonly", "unrestricted")


@dataclass(frozen=True)
class CliCapabilities:
    """What a CLI can do with snowpea (``ExternalCliCapabilities``)."""

    streaming: bool = True
    approvals: Literal["bridged", "degraded"] = "bridged"
    live_view: bool = False
    tmux: bool = False
    modes: tuple[str, ...] = ALL_MODES
    #: ``external.send`` during a running turn adds to it instead of answering
    #: ``session_busy`` (Codex ``turn/steer``).
    steer: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "approvals": self.approvals,
            "liveView": self.live_view,
            "tmux": self.tmux,
            "modes": list(self.modes),
            "steer": self.steer,
        }


@dataclass
class Discovery:
    """The result of looking for a CLI on this machine."""

    installed: bool
    executable: PinnedExecutable | None = None
    capabilities: CliCapabilities | None = None
    approval_policy: Literal["asks", "auto"] | None = None
    problems: list[str] = field(default_factory=list)
    #: The modes the installed CLI offers (1.56.0); ``None`` when the adapter
    #: did not look (its table rows without ``requires`` then stand).
    native_modes: tuple[NativeMode, ...] | None = None

    @property
    def usable(self) -> bool:
        """Installed and with nothing that stops sessions from running."""
        return self.installed and self.executable is not None and not self.problems


@dataclass
class SessionInfo:
    """One stored session as the CLI recorded it."""

    id: str
    cwd: str
    title: str | None = None
    #: UTC ISO time of its last entry; the list is sorted on it.
    updated_at: str | None = None
    status: Status = "idle"
    model: str | None = None
    preview: str = ""
    #: Anything the adapter wants to keep with the row (file paths, ...).
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass
class Liveness:
    """Whether some process other than snowpea's holds the session."""

    host: Literal["none", "terminal", "tmux"] = "none"
    busy: bool = False
    tmux: dict[str, str] | None = None
    #: The processes holding it (1.56.0): where the tmux bridge looks for a pane.
    pids: tuple[int, ...] = ()


@dataclass
class ApprovalAsk:
    """A permission prompt from the CLI, as snowpea's approval card shows it."""

    tool: str
    args: dict[str, Any]
    risk: str = "medium"
    note: str = ""
    diff: str | None = None
    paths: list[str] | None = None
    #: Only a person may allow it (``ApprovalRequest.personOnly``): never Jarvis,
    #: never the engine, never an unattended rule.  E.g. adopting a tmux pane the
    #: person started themselves.
    person_only: bool = False
    #: A prompt read off a tmux pane (1.56.0): ``{pane, text}`` (plain, clipped,
    #: untrusted); ``ApprovalRequest.externalTerminal``.  Never answered from the
    #: session's "allow for this session" memory.
    terminal: dict[str, Any] | None = None


@dataclass
class ApprovalAnswer:
    """The person's (or Jarvis's) answer, handed back to the adapter."""

    allow: bool
    #: ``once``, ``session``, ``project``, ``always``: map wider scopes to the
    #: CLI's own "for this session" only when it offers one; never write the
    #: owner's CLI config ("always" rules) on its behalf.
    scope: str = "once"
    reason: str = ""
    by: str = ""


@dataclass
class LaunchSpec:
    """Everything a channel needs to start or continue one session."""

    cwd: str
    project: str | None
    executable: PinnedExecutable
    mode: str
    #: ``None`` for a new session whose id the CLI picks (``id_known`` later).
    session_id: str | None = None
    #: ``True`` to continue ``session_id``, ``False`` to create it.
    resume: bool = False
    model: str | None = None
    #: Present only when ``mode == "unrestricted"``; see :func:`proc.bypass_args`.
    grant: UnrestrictedGrant | None = None
    transport: Literal["structured", "tmux"] = "structured"
    #: The CLI's own mode id (1.56.0), from the adapter's table; ``None`` means
    #: the row ``legacy_native[mode]`` names.
    native_mode: str | None = None


class ChannelHost(Protocol):
    """What the manager gives a channel; the channel's only way out."""

    supervisor: ProcessSupervisor
    #: Scrubbed environment for the child (:func:`proc.scrubbed_env`).
    env: dict[str, str]
    @property
    def builder(self) -> TimelineBuilder:
        """The session's builder: use it for every item so keys stay unique."""

    async def emit(self, items: Sequence[TimelineItem]) -> None:
        """Publish items (``builder.drain()``) as ``external.event``."""

    async def ask_approval(self, ask: ApprovalAsk) -> ApprovalAnswer:
        """Bridge one permission prompt into snowpea's approval queue; blocks."""

    async def turn_finished(self, reason: str = "complete", error: str | None = None) -> None:
        """The running turn ended (``complete``, ``interrupted``, ``error``, ``denied``)."""

    async def channel_died(self, error: str) -> None:
        """The process went away on its own; the manager cleans up."""

    async def id_known(self, session_id: str) -> None:
        """A new session's id, once the CLI reports it."""

    async def title_known(self, title: str) -> None:
        """The CLI named the session."""

    async def mode_known(self, native_mode: str) -> None:
        """The CLI changed its own mode (Claude Code leaving plan mode once the
        person approved the plan); the row follows."""


class ExternalChannel(ABC):
    """A session snowpea runs (host ``snowpea`` or ``tmux``)."""

    host: Literal["snowpea", "tmux"] = "snowpea"

    def __init__(self, spec: LaunchSpec, host: ChannelHost) -> None:
        self.spec = spec
        self.host_api = host
        self.session_id: str | None = spec.session_id
        self.mode: str = spec.mode
        #: The CLI's own mode id; the manager sets it before :meth:`set_mode`.
        self.native_mode: str | None = spec.native_mode
        self.grant: UnrestrictedGrant | None = spec.grant
        self.tmux: dict[str, str] | None = None

    @property
    @abstractmethod
    def alive(self) -> bool:
        """The process is running."""

    @abstractmethod
    async def start(self) -> None:
        """Spawn and hand-shake; raise :class:`ExternalError` when it cannot."""

    @abstractmethod
    async def send(self, text: str, turn_id: str) -> None:
        """Start a turn with ``text`` and return once the CLI took it.

        Stream output through ``host.emit`` and end the turn with
        ``host.turn_finished``.  Do not emit the prompt itself: the manager
        already published ``turn.started`` and ``message.user``.
        """

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the running turn (the turn still ends through ``turn_finished``)."""

    @property
    def can_steer(self) -> bool:
        """:meth:`steer` works now (a turn runs and the CLI takes more input into it)."""
        return False

    async def steer(self, text: str) -> None:
        """Add ``text`` to the running turn (``CliCapabilities.steer``).

        Do not emit the prompt: the manager publishes ``message.user`` for it.
        Raise ``session_busy`` when the CLI no longer takes it (the turn ended or
        changed); the manager then answers the send with that error.
        """
        raise unavailable("this CLI cannot add to a running turn")

    async def set_mode(self, mode: str, grant: UnrestrictedGrant | None) -> None:
        """Switch mode from the next turn.

        The default only records it; the channel applies it when its next turn
        starts (restart with the new flags, or the CLI's own mode switch such
        as ACP ``session/set_mode``).  ``grant`` is set exactly when ``mode``
        is ``unrestricted``.
        """
        self.mode = mode
        self.grant = grant

    @abstractmethod
    async def close(self) -> None:
        """Stop the process (tree) and release what the channel holds."""

    def pids(self) -> set[int]:
        """Pids of the processes this channel runs (to tell them from the person's)."""
        return set()

    async def precheck(self, text: str) -> None:
        """Refuse a send before the manager publishes anything (default: nothing)."""
        return None


class ExternalAdapter(ABC):
    """One external CLI.  Subclasses set the attributes and the abstract methods."""

    #: ``claude``, ``codex``, ``agy`` or ``cursor``.
    id: str = ""
    #: Display name, e.g. "Claude Code".
    title: str = ""
    #: Executable name looked up on ``PATH`` (``cursor-agent`` for cursor).
    binary: str = ""
    capabilities: CliCapabilities = CliCapabilities()
    #: Exact tokens this CLI treats as a bypass without saying so.
    forbidden_args: tuple[str, ...] = ()
    #: ``(flag, value)`` pairs that amount to a bypass (``("-a", "never")``).
    forbidden_pairs: tuple[tuple[str, str], ...] = ()
    #: The mode used when nobody chose one and the setting is unset.
    builtin_default_mode: str = "default"
    #: Every mode this adapter knows how to run (1.56.0), as the CLI names them;
    #: empty: the three snowpea buckets (:data:`modes.GENERIC`).
    native_table: tuple[NativeMode, ...] = ()
    #: ``legacy mode -> native id``: what an old client's ``mode`` means.
    legacy_native: dict[str, str] = {}
    #: The daemon's supervisor, handed over by the manager; helper processes
    #: (:meth:`helper_supervisor`) and channels run under it.
    supervisor: ProcessSupervisor | None = None
    #: The environment a process of this CLI starts with (the scrubbed one plus
    #: ``jarvis.external.clis.<id>.passEnv``), bound by the manager; a process
    #: started before any channel (a shared server at discovery) uses it too.
    env_provider: Any = None

    def base_env(self) -> dict[str, str]:
        """:attr:`env_provider`'s environment, or the scrubbed one without it."""
        from snowpea_core.external import proc as _proc

        if self.env_provider is not None:
            try:
                return dict(self.env_provider())
            except Exception:  # noqa: BLE001 - a broken provider passes nothing extra
                return _proc.scrubbed_env()
        return _proc.scrubbed_env()

    # -- discovery ----------------------------------------------------------
    async def discover(self, path_override: str | None = None) -> Discovery:
        """Find the binary and ask ``--version`` (spends no tokens).

        Override to add problems ("not logged in", "unsupported version")
        or the agy approval policy; call ``super().discover`` first.
        """
        try:
            path = proc.resolve_executable(self.binary, path_override)
        except ExternalError as exc:
            return Discovery(installed=False, problems=[exc.message])
        if path is None:
            return Discovery(installed=False)
        version = await proc.probe_version(path)
        problems = [] if version else [f"{path} --version did not answer"]
        return Discovery(
            installed=True,
            executable=PinnedExecutable(path=path, version=version),
            capabilities=self.capabilities,
            problems=problems,
        )

    async def list_models(self, discovery: Discovery | None = None) -> list[str]:
        """Model ids ``external.create {model}`` accepts, without spending tokens.

        Called in the background (``external.clis`` never waits for it) and
        cached by the manager; empty when the CLI cannot say.
        """
        return []

    def helper_supervisor(self) -> ProcessSupervisor:
        """Where short-lived helper processes run (:meth:`ProcessSupervisor.run_helper`).

        The daemon's supervisor once the manager bound this adapter; a private
        one under ``$SNOWPEA_HOME`` otherwise (an adapter used on its own).
        """
        if self.supervisor is None:
            from snowpea_core.config.paths import resolve_home

            self.supervisor = ProcessSupervisor(resolve_home())
        return self.supervisor

    async def shutdown(self) -> None:
        """Daemon stop: release what the adapter keeps beyond its channels.

        Called once, after every channel was closed and before the supervisor
        stops what is left.  The default keeps nothing; Codex stops its shared
        app-server gracefully here.
        """
        return None

    # -- native modes (1.56.0) ---------------------------------------------
    def table(self) -> tuple[NativeMode, ...]:
        """The whole mode table (the generic buckets for an adapter without one)."""
        if self.native_table:
            return self.native_table
        return tuple(row for row in native.GENERIC if row.id in self.capabilities.modes)

    def native_modes(self, discovery: Discovery | None) -> tuple[NativeMode, ...]:
        """The modes the installed CLI offers (discovery decides; see :func:`modes.offered`)."""
        if discovery is not None and discovery.native_modes is not None:
            return discovery.native_modes
        return native.offered(self.table(), None)

    def native_entry(self, mode_id: str | None, discovery: Discovery | None = None
                     ) -> NativeMode | None:
        """The offered row ``mode_id``, or ``None``."""
        return native.find(self.native_modes(discovery), mode_id)

    def native_for_legacy(self, mode: str | None, discovery: Discovery | None = None
                          ) -> NativeMode | None:
        """The row an old client's ``mode`` stands for (``None`` when not offered)."""
        if not mode:
            return None
        mode_id = self.legacy_native.get(mode, mode)
        return self.native_entry(mode_id, discovery)

    def resolve_mode(self, mode: str | None, native_mode: str | None,
                     discovery: Discovery | None = None) -> tuple[str, NativeMode] | None:
        """``(legacy, row)`` for a request; ``nativeMode`` wins over ``mode``.

        ``None`` when the request names neither.  An unknown or not offered
        ``nativeMode`` (or a legacy ``mode`` with no row) is ``invalid_params``.
        """
        if native_mode is not None:
            if not isinstance(native_mode, str) or not native_mode:
                raise ExternalError(errors.INVALID_PARAMS, "nativeMode must be a mode id")
            row = self.native_entry(native_mode, discovery)
            if row is None:
                offered = ", ".join(r.id for r in self.native_modes(discovery))
                raise ExternalError(
                    errors.INVALID_PARAMS,
                    f"{self.title or self.id} does not offer mode {native_mode[:64]!r}"
                    + (f"; it offers {offered}" if offered else ""),
                )
            return row.legacy, row
        legacy = proc.validate_mode(mode, self.capabilities.modes)
        if legacy is None:
            return None
        row = self.native_for_legacy(legacy, discovery)
        if row is None:
            raise ExternalError(
                errors.INVALID_PARAMS, f"{self.title or self.id} does not offer mode {legacy!r}"
            )
        return legacy, row

    def controlled(self, mode: str, discovery: Discovery | None) -> bool:
        """False when snowpea cannot gate what the session runs."""
        return mode != "unrestricted"

    def approvals(self, mode: str, discovery: Discovery | None) -> Approvals:
        """How a session snowpea runs in ``mode`` gets its prompts answered."""
        if mode == "unrestricted":
            return "none"
        if mode == "readonly":
            return "none" if self.capabilities.approvals == "degraded" else "bridged"
        return self.capabilities.approvals

    # -- stored sessions ----------------------------------------------------
    @abstractmethod
    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        """Sessions whose cwd is one of ``roots`` or under it, newest first.

        ``before`` is an ``updated_at`` bound: return rows at or before it
        (the manager breaks ties).  Return at most ``limit`` rows.
        """

    @abstractmethod
    async def get_session(self, session_id: str) -> SessionInfo | None:
        """One stored session, or ``None``."""

    @abstractmethod
    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        """The whole stored transcript as timeline items, oldest first.

        Use a fresh :class:`TimelineBuilder` and a :class:`RecordFilter` that
        drops every record kind that is not conversation.
        """

    @abstractmethod
    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        """Is it open in a process that is not one of ``own_pids``?

        Never take a lock to find out (S2, S3): read ``/proc/locks``, pid
        files or ``/proc/<pid>/fd``.
        """

    async def follow(
        self, info: SessionInfo, *, interval: float = 1.0
    ) -> AsyncIterator[list[TimelineItem]]:
        """Live tail of a session open elsewhere; the default re-reads and lets
        the manager drop what it already has.  Override with a file tail."""
        while True:
            await asyncio.sleep(interval)
            fresh = await self.get_session(info.id)
            yield await self.read(fresh or info)

    # -- running ------------------------------------------------------------
    @abstractmethod
    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        """A channel that will run ``spec`` (the manager calls ``start``)."""


def unavailable(message: str) -> ExternalError:
    """The error for something this CLI or host cannot do."""
    return ExternalError(errors.EXTERNAL_UNAVAILABLE, message)


__all__ = [
    "ALL_MODES",
    "ApprovalAnswer",
    "ApprovalAsk",
    "Approvals",
    "ChannelHost",
    "CliCapabilities",
    "Discovery",
    "ExternalAdapter",
    "ExternalChannel",
    "Host",
    "LaunchSpec",
    "Liveness",
    "Mode",
    "NativeMode",
    "SessionInfo",
    "Status",
    "unavailable",
]
