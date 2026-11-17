"""External sessions: open table, subscriptions, approvals, takeover (M19 WP5).

One :class:`ExternalManager` per daemon.  It owns every external session
snowpea has opened (``external.open`` / ``external.create``), the processes
it runs for them, the ``external.event`` subscriptions and the daemon-wide
``external.state`` notices.  The RPC handlers (``server/external_handlers``)
and, later, Jarvis's tools call the same methods; an in-process caller passes
``conn=None`` and so can never choose ``unrestricted``.

Lifecycle of one session (``host``):

* ``none`` -> ``snowpea`` when it is opened or sent to: a channel starts.
* ``terminal`` while another process holds it: view only, a live tail, and a
  watcher that notices when that process exits -> ``none`` (it can then be
  taken over by the next send).
* ``snowpea`` -> ``none`` on close, after ``idleSec`` without a turn, or when
  the CLI dies (mid-turn: an ``error`` event, ``turn.done`` with reason
  ``error``, status ``error``).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import inspect
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import utc_now
from snowpea_core.external import proc
from snowpea_core.external.base import (
    ApprovalAnswer,
    ApprovalAsk,
    Discovery,
    ExternalAdapter,
    ExternalChannel,
    LaunchSpec,
    SessionInfo,
)
from snowpea_core.external.modes import NativeMode
from snowpea_core.external.proc import (
    ExternalError,
    LedgerHook,
    PersonConfirmation,
    ProcessSupervisor,
    UnrestrictedGrant,
)
from snowpea_core.external.registry import CLI_ORDER, CLI_TITLES, AdapterRegistry
from snowpea_core.external.timeline import (
    TimelineBuilder,
    TimelineItem,
    clean_text,
)
from snowpea_core.external.trust import ProjectTrust
from snowpea_core.permissions.allowlist import command_of
from snowpea_core.permissions.shell_remember import matchable
from snowpea_core.server import errors
from snowpea_core.server.protocol import ExternalRef

log = logging.getLogger("snowpea.external")

#: Seconds a discovery result (``--version``) is reused.
DISCOVERY_TTL_SEC = 60.0
#: Timeline entries kept in memory per session.
MAX_EVENTS = 20_000
#: Seconds a CLI's model list (``external.clis`` ``models``) is reused.
MODELS_TTL_SEC = 600.0
#: Seconds one model listing may take before it is given up (the listing
#: itself never waits for it).
MODELS_TIMEOUT_SEC = 60.0
#: Prompts snowpea sent that a later transcript copy is matched against.
LIVE_PROMPTS_KEPT = 64
#: Seconds an adapter's shutdown hook may take at daemon stop.
ADAPTER_SHUTDOWN_SEC = 10.0
#: Event kinds observers (Jarvis) are not told about: streaming noise.
_UNOBSERVED = frozenset({"message.delta", "message.reasoning", "tool.progress"})

RootsFn = Callable[[], Awaitable[list[str]]]
ExposedFn = Callable[[str], Awaitable[bool]]
WorkdirFn = Callable[[str], Awaitable[str | None]]
#: ``observer(row, event)``: one newly published, non-streaming timeline entry
#: (``ExternalTimelineEvent`` dict) of an external session and its row now.
#: History read when a session is loaded is not observed.
Observer = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _ref(cli: str, session_id: str) -> dict[str, str]:
    return {"cli": cli, "id": session_id}


def _call_key(tool: str, args: dict[str, Any], command: str | None) -> tuple[str, str]:
    """``(tool, the exact call)``: the whole command line (and the folder it runs in,
    security review 4 M2: ``cat .env`` elsewhere is another call), or every argument."""
    if command:
        cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else ""
        return (tool, f"{cwd}\x00{command.strip()}" if cwd else command.strip())
    import json

    try:
        return (tool, json.dumps(args, sort_keys=True, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return (tool, repr(sorted(args.items(), key=lambda item: str(item[0]))))


def _origin_for(conn: Any, origin: dict[str, Any] | None) -> dict[str, Any]:
    if origin is not None:
        return origin
    if proc.is_human_connection(conn):
        return {"kind": "human", "surface": getattr(conn, "client_kind", None)}
    return {"kind": "system", "surface": None}


class _ProxySession:
    """What :meth:`ApprovalQueue.request` needs from a session, for ``ext:<cli>:<id>``.

    ``external`` and ``project`` let the approval router (the delegation
    engine) judge it as that external session in its registered project.
    """

    kind = "external"
    origin_conn = None
    host_tools_from = None
    owned_by = None
    parent_session_id = None

    def __init__(self, cli: str, session_id: str, cwd: str, project: str | None = None,
                 title: str | None = None) -> None:
        self.id = f"ext:{cli}:{session_id}"
        self.workdir = Path(cwd)
        self.external = ExternalRef(cli=cli, id=session_id)  # type: ignore[arg-type]
        self.project = project
        self.title = clean_text(title) if title else title


@dataclass
class _LivePrompt:
    """A prompt snowpea typed or sent itself; the CLI's own copy of it is not shown twice."""

    text: str
    matched: bool = False


@dataclass
class _Record:
    """One external session snowpea has opened."""

    adapter: ExternalAdapter
    info: SessionInfo
    project: str | None
    builder: TimelineBuilder
    host: str = "none"
    status: str = "idle"
    mode: str | None = None
    #: The CLI's own mode id (1.56.0); ``mode`` is its legacy bucket.
    native_mode: str | None = None
    error: str | None = None
    channel: ExternalChannel | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    seq: int = 0
    subscribers: list[Any] = field(default_factory=list)
    turn_id: str | None = None
    turn_cancel: asyncio.Event | None = None
    last_activity: float = field(default_factory=time.monotonic)
    follow_task: asyncio.Task[None] | None = None
    approval_cache: set[tuple[str, str]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Keys of the ``message.user`` items the manager published itself.
    live_keys: set[str] = field(default_factory=set)
    #: Prompts snowpea sent, oldest first (see :meth:`ExternalManager._duplicate`).
    live_prompts: collections.deque[_LivePrompt] = field(
        default_factory=lambda: collections.deque(maxlen=LIVE_PROMPTS_KEPT)
    )
    steers: int = 0
    #: The validated real path of the session's folder (security review L8):
    #: what a CLI is started in, never the transcript's raw string.
    real_cwd: str | None = None

    @property
    def cli(self) -> str:
        return self.adapter.id

    @property
    def id(self) -> str:
        return self.info.id

    @property
    def ref(self) -> dict[str, str]:
        return _ref(self.cli, self.info.id)


class _Host:
    """:class:`~snowpea_core.external.base.ChannelHost` for one record."""

    def __init__(self, manager: ExternalManager, record: _Record) -> None:
        self._manager = manager
        self._record = record
        self.supervisor = manager.supervisor
        pass_env = getattr(manager._cli_settings(record.cli), "passEnv", None) or ()
        self.env = proc.scrubbed_env(pass_env=pass_env)

    @property
    def builder(self) -> TimelineBuilder:
        return self._record.builder

    async def emit(self, items: Sequence[TimelineItem]) -> None:
        record = self._record
        record.last_activity = time.monotonic()
        for item in items:
            # Live output belongs to the turn snowpea started, whatever the CLI calls it.
            if item.turn_id is None and record.turn_id is not None:
                item.turn_id = record.turn_id
        await self._manager._publish(record, items)

    async def ask_approval(self, ask: ApprovalAsk) -> ApprovalAnswer:
        return await self._manager._ask_approval(self._record, ask)

    async def turn_finished(self, reason: str = "complete", error: str | None = None) -> None:
        await self._manager._turn_finished(self._record, reason, error)

    async def channel_died(self, error: str) -> None:
        await self._manager._channel_died(self._record, error)

    async def id_known(self, session_id: str) -> None:
        proc.validate_session_id(session_id)
        if self._record.info.id in ("", None):
            self._record.info.id = session_id

    async def title_known(self, title: str) -> None:
        title = clean_text(title)[:300] if title else title
        if title and title != self._record.info.title:
            self._record.info.title = title
            await self._manager._announce(self._record)

    async def mode_known(self, native_mode: str) -> None:
        await self._manager._mode_known(self._record, native_mode)

    async def changed(self) -> None:
        """Something the row shows changed (a prompt waits in the tmux pane)."""
        await self._manager._announce(self._record)

    async def bridge_lost(self, reason: str) -> None:
        """The tmux bridge ended (the CLI exited, the guard refused): view only again."""
        await self._manager._bridge_lost(self._record, reason)


class ExternalManager:
    """Every external session snowpea has open, and the processes behind them."""

    def __init__(
        self,
        *,
        home: Path,
        settings: Callable[[], Any],
        hub: Any = None,
        approvals: Any = None,
        registry: AdapterRegistry | None = None,
        roots: RootsFn | None = None,
        exposed: ExposedFn | None = None,
        workdir_of: WorkdirFn | None = None,
        poll_interval: float = 1.0,
        sweep_interval: float = 30.0,
        ledger: LedgerHook | None = None,
    ) -> None:
        self.home = Path(home)
        self._settings = settings
        self.hub = hub
        self.approvals = approvals
        self.registry = registry if registry is not None else AdapterRegistry.builtin()
        self._roots = roots
        self._exposed = exposed
        self._workdir_of = workdir_of
        self.poll_interval = poll_interval
        self.sweep_interval = sweep_interval
        self.supervisor = ProcessSupervisor(self.home, self._max_processes)
        #: Projects the person trusted for external CLIs (security review M4).
        self.trust = ProjectTrust(self.home)
        self._records: dict[tuple[str, str], _Record] = {}
        self._discovery: dict[str, tuple[float, str | None, Discovery]] = {}
        self._watched_conns: set[int] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False
        #: Writes the ledger entry of an unrestricted grant (the delegation
        #: engine's ledger in the daemon); no write, no grant.
        self.ledger = ledger
        #: Told about every new, non-streaming timeline entry (Jarvis's intake).
        self.observers: list[Observer] = []
        #: cli -> (monotonic time, executable path, model ids).
        self._models: dict[str, tuple[float, str | None, list[str]]] = {}
        self._model_tasks: dict[str, asyncio.Task[None]] = {}
        from snowpea_core.external import tmux as _tmux

        #: tmux servers the bridge looks in (None: the process's own, then the
        #: person's tmux folder) and how it reads processes; tests replace both.
        self.tmux_sockets: list[str] | None = None
        self.tmux_probe: Any = _tmux.PROBE
        #: Seconds a bridged send waits to see the CLI busy before its turn ends.
        self.tmux_start_timeout = 20.0
        for cli in self.registry.ids():
            adapter = self.registry.get(cli)
            if adapter is not None:
                self._bind(adapter)

    def _bind(self, adapter: ExternalAdapter) -> ExternalAdapter:
        """Hand the daemon's supervisor (helpers, shared servers) and the CLI's
        environment (with its ``passEnv``) to ``adapter``."""
        if adapter.supervisor is None:
            adapter.supervisor = self.supervisor
        if getattr(adapter, "env_provider", None) is None:
            cli = adapter.id

            def provider() -> dict[str, str]:
                pass_env = getattr(self._cli_settings(cli), "passEnv", None) or ()
                return proc.scrubbed_env(pass_env=pass_env)

            adapter.env_provider = provider
        return adapter

    # -- settings ---------------------------------------------------------
    def _ext_settings(self) -> Any:
        settings = self._settings()
        return getattr(getattr(settings, "jarvis", None), "external", None)

    def _max_processes(self) -> int:
        ext = self._ext_settings()
        return int(getattr(ext, "maxProcesses", 4) or 4)

    def _cli_settings(self, cli: str) -> Any:
        ext = self._ext_settings()
        clis = getattr(ext, "clis", None) or {}
        return clis.get(cli)

    def _idle_sec(self) -> float:
        ext = self._ext_settings()
        return float(getattr(ext, "idleSec", 900) or 900)

    def default_mode(self, adapter: ExternalAdapter) -> str:
        """``jarvis.external.clis.<id>.defaultMode`` or the CLI's own default.

        Never ``unrestricted`` (security review H2): that mode comes only from
        an explicit ``mode`` in the person's own request.
        """
        configured = getattr(self._cli_settings(adapter.id), "defaultMode", None)
        mode = configured or adapter.builtin_default_mode
        if mode == "unrestricted" or mode not in adapter.capabilities.modes:
            mode = adapter.builtin_default_mode
        if mode == "unrestricted" or mode not in adapter.capabilities.modes:
            mode = next(m for m in adapter.capabilities.modes if m != "unrestricted")
        return mode

    def _implicit_mode(self, record: _Record) -> str:
        """The mode a session continues in when the request names none.

        The session's last mode unless that was ``unrestricted`` or another
        person-only mode: a continued session (after an idle stop, a restart, a
        takeover) never comes back in one without a new, explicit choice.
        """
        return self._implicit(record)[0]

    def _implicit(self, record: _Record) -> tuple[str, NativeMode]:
        """``(legacy, row)`` a session continues in when the request names none."""
        found = self._cached_discovery(record.adapter.id)
        native = getattr(record, "native_mode", None)
        if record.mode and record.mode != "unrestricted":
            row = (record.adapter.native_entry(native, found)
                   if native else
                   record.adapter.native_for_legacy(record.mode, found))
            if row is not None and not row.person_only and row.legacy != "unrestricted":
                return row.legacy, row
        return self.default_native(record.adapter, found)

    def default_native(self, adapter: ExternalAdapter, found: Discovery | None = None
                       ) -> tuple[str, NativeMode]:
        """``(legacy, row)`` of :meth:`default_mode`: never a person-only row."""
        legacy = self.default_mode(adapter)
        row = adapter.native_for_legacy(legacy, found)
        if row is None or row.person_only:
            row = next((r for r in adapter.native_modes(found) if not r.person_only), None)
        if row is None:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE,
                                f"{adapter.title} offers no mode snowpea can start it in")
        return row.legacy, row

    def _default_native_id(self, adapter: ExternalAdapter, found: Discovery | None) -> str:
        try:
            return self.default_native(adapter, found)[1].id
        except ExternalError:
            return adapter.legacy_native.get(self.default_mode(adapter), self.default_mode(adapter))

    def resolve_mode(self, adapter: ExternalAdapter, mode: str | None, native_mode: str | None,
                     found: Discovery | None = None) -> tuple[str, NativeMode] | None:
        """A request's ``(legacy, row)``: ``nativeMode`` wins over ``mode``; ``None``
        when it names neither (:meth:`ExternalAdapter.resolve_mode`)."""
        return adapter.resolve_mode(mode, native_mode, found or self._cached_discovery(adapter.id))

    def _require_trust(self, root: str | None, cwd: str, conn: Any, trust: bool) -> None:
        """A CLI may start in ``root`` only when the person trusted it (security review M4).

        A person's request trusts it with ``trust=True`` (the client's one-time
        confirmation); without it the answer is ``external_unavailable`` with
        ``details.reason: "untrusted_project"``.  Anyone else (Jarvis, a browser
        host, the connector, a remote web login) gets ``details.reason:
        "untrusted_project_person_only"``: the person must open an external
        session there once.
        """
        from snowpea_core.server.person_gate import connection_kind

        key = root or cwd
        if self.trust.trusted(key):
            return
        if connection_kind(conn) == "person":
            if trust:
                self.trust.trust(key, by=str(getattr(conn, "client_kind", None) or "local"))
                return
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                "this is the first external CLI session in this project: the CLI will load the "
                "project's own settings and hooks and may run code from it. Confirm once "
                "(trust: true) to trust this project for external CLIs",
                {"reason": "untrusted_project", "project": key},
            )
        raise ExternalError(
            errors.EXTERNAL_UNAVAILABLE,
            "the person must open an external session in this project once before anyone "
            "else can",
            {"reason": "untrusted_project_person_only", "project": key},
        )

    def watched_by(self, cli: str, session_id: str, conn: Any) -> bool:
        """True when ``conn`` is subscribed to the session (it opened, created or sent to it)."""
        record = self._records.get((cli, session_id))
        return record is not None and conn is not None and any(
            other is conn for other in record.subscribers
        )

    def watched_refs(self, conn: Any) -> set[tuple[str, str]]:
        return {
            key for key, record in self._records.items()
            if any(other is conn for other in record.subscribers)
        }

    def controlled_default_mode(self, adapter: ExternalAdapter) -> str:
        """The default mode for a caller that is not a person: never ``unrestricted``.

        The configured ``defaultMode`` when it is not ``unrestricted``, else the
        CLI's own default.  Jarvis creates and continues sessions in this one.
        """
        mode = self.default_mode(adapter)
        if mode == "unrestricted":
            mode = adapter.builtin_default_mode
        if mode == "unrestricted" or mode not in adapter.capabilities.modes:
            mode = next(m for m in adapter.capabilities.modes if m != "unrestricted")
        return mode

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Clean up after a daemon that died, then start the idle sweep.

        An orphan is killed only when it runs the executable configured and
        pinned for its CLI now, not the one its pid record names (N15).  It is
        resolved as discovery resolves it, without running anything.
        """
        for cli in self.registry.ids():
            adapter = self.registry.get(cli)
            if adapter is None or self.pending_path(cli) is not None:
                continue  # an unconfirmed override pins nothing
            try:
                path = proc.resolve_executable(
                    adapter.binary, getattr(self._cli_settings(cli), "path", None)
                )
            except Exception:  # noqa: BLE001 - an unresolvable CLI pins nothing
                continue
            self.supervisor.pin(cli, path)
        killed = await asyncio.to_thread(self.supervisor.reap_orphans)
        if killed:
            log.info("stopped %d external CLI process(es) a previous run left", killed)
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def shutdown(self) -> None:
        """Stop every channel, the adapters' shared processes, then every child (daemon exit)."""
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        for task in self._model_tasks.values():
            task.cancel()
        await asyncio.gather(*self._model_tasks.values(), return_exceptions=True)
        self._model_tasks.clear()
        for record in list(self._records.values()):
            await self._stop_follow(record)
            if record.channel is not None:
                with contextlib.suppress(Exception):
                    await record.channel.close()
                record.channel = None
            if record.turn_cancel is not None:
                record.turn_cancel.set()
        for cli in self.registry.ids():
            adapter = self.registry.get(cli)
            if adapter is None:
                continue
            try:
                await asyncio.wait_for(adapter.shutdown(), ADAPTER_SHUTDOWN_SEC)
            except Exception:  # noqa: BLE001 - the supervisor stops what is left
                log.warning("%s adapter did not shut down cleanly", cli, exc_info=True)
        await self.supervisor.shutdown()

    # -- discovery --------------------------------------------------------
    async def discover(self, cli: str, *, refresh: bool = False) -> Discovery | None:
        adapter = self.registry.get(cli)
        if adapter is None:
            return None
        self._bind(adapter)
        override = getattr(self._cli_settings(cli), "path", None)
        cached = self._discovery.get(cli)
        now = time.monotonic()
        if (
            not refresh
            and cached is not None
            and cached[1] == override
            and now - cached[0] < DISCOVERY_TTL_SEC
        ):
            return cached[2]
        pending = self.pending_path(cli)
        if pending is not None:
            # Never run an override the person has not confirmed (not even
            # ``--version``): security review H2.
            found = Discovery(
                installed=False,
                problems=[
                    f"the configured path {pending} has not been confirmed yet "
                    "(external.clis.confirmPath)"
                ],
            )
            self._discovery[cli] = (now, override, found)
            return found
        try:
            found = await adapter.discover(override)
        except ExternalError as exc:
            found = Discovery(installed=False, problems=[exc.message])
        except Exception as exc:  # noqa: BLE001 - one broken adapter must not break the list
            log.warning("discovery of %s failed", cli, exc_info=True)
            found = Discovery(installed=False, problems=[f"discovery failed: {type(exc).__name__}"])
        self._discovery[cli] = (now, override, found)
        return found

    def pending_path(self, cli: str) -> str | None:
        """The resolved ``path`` override still waiting for the person's confirmation.

        ``None`` when there is no override or its real path is the confirmed
        one.  An override that does not resolve is shown as written.
        """
        cli_settings = self._cli_settings(cli)
        override = getattr(cli_settings, "path", None)
        if not override:
            return None
        real = resolved_override(override)
        if real is not None and real == getattr(cli_settings, "confirmedPath", None):
            return None
        return real or str(override)

    async def clis(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """``external.clis``: all four, in a fixed order."""
        ext = self._ext_settings()
        enabled_all = bool(getattr(ext, "enabled", True))
        out: list[dict[str, Any]] = []
        for cli in CLI_ORDER:
            adapter = self.registry.get(cli)
            cli_settings = self._cli_settings(cli)
            enabled = enabled_all and bool(getattr(cli_settings, "enabled", True))
            row: dict[str, Any] = {
                "id": cli,
                "title": adapter.title if adapter is not None else CLI_TITLES[cli],
                "installed": False,
                "enabled": enabled,
                "version": None,
                "path": None,
                "capabilities": None,
                "approvalPolicy": None,
                "defaultMode": (
                    self.default_mode(adapter)
                    if adapter is not None
                    else ("readonly" if cli == "cursor" else "default")
                ),
                "modes": list(adapter.capabilities.modes) if adapter is not None else [],
                "nativeModes": (
                    [row.to_wire() for row in adapter.native_modes(None)]
                    if adapter is not None else []
                ),
                "defaultNativeMode": (
                    self._default_native_id(adapter, None) if adapter is not None
                    else ("ask" if cli == "cursor" else "default")
                ),
                "models": [],
                "modelsLoaded": False,
                "pendingPath": self.pending_path(cli),
                "problems": [],
            }
            if adapter is None:
                row["problems"] = ["not supported by this snowpea core yet"]
                out.append(row)
                continue
            found = await self.discover(cli, refresh=refresh)
            assert found is not None
            row["installed"] = found.installed
            if found.executable is not None:
                row["path"] = found.executable.path
                row["version"] = found.executable.version
            if found.capabilities is not None:
                row["capabilities"] = found.capabilities.to_wire()
                row["modes"] = list(found.capabilities.modes)
            row["approvalPolicy"] = found.approval_policy
            row["nativeModes"] = [entry.to_wire() for entry in adapter.native_modes(found)]
            row["defaultNativeMode"] = self._default_native_id(adapter, found)
            problems = list(found.problems)
            if not found.installed:
                problems.insert(0, f"{adapter.binary} was not found")
            if not enabled_all:
                problems.append("external sessions are turned off (jarvis.external.enabled)")
            row["problems"] = problems
            row["models"] = self._models_now(adapter, found, refresh=refresh)
            cached = self._models.get(adapter.id)
            path = found.executable.path if found.executable is not None else None
            row["modelsLoaded"] = bool(found.usable and cached is not None and cached[1] == path)
            out.append(row)
        return out

    # -- models -----------------------------------------------------------
    def _models_now(self, adapter: ExternalAdapter, found: Discovery, *, refresh: bool
                    ) -> list[str]:
        """The cached model list; a stale or missing one is fetched in the background.

        ``external.clis`` never waits for a CLI to list its models; when the list
        arrives (or changes) ``external.clis.changed`` goes out.
        """
        if not found.usable:
            return []
        path = found.executable.path if found.executable is not None else None
        cached = self._models.get(adapter.id)
        fresh = (
            cached is not None
            and cached[1] == path
            and time.monotonic() - cached[0] < MODELS_TTL_SEC
        )
        if (refresh or not fresh) and not self._closed:
            running = self._model_tasks.get(adapter.id)
            if running is None or running.done():
                self._model_tasks[adapter.id] = asyncio.ensure_future(
                    self._fetch_models(adapter, found, path)
                )
        return list(cached[2]) if cached is not None and cached[1] == path else []

    async def _fetch_models(self, adapter: ExternalAdapter, found: Discovery,
                            path: str | None) -> None:
        try:
            result = adapter.list_models(found)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, MODELS_TIMEOUT_SEC)
            models = [
                str(model) for model in (result or [])
                if isinstance(model, str) and proc.MODEL_RE.fullmatch(model)
                and not proc.is_dangerous(model)
            ]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a CLI that cannot list models lists none
            log.debug("listing %s models failed", adapter.id, exc_info=True)
            models = []
        previous = self._models.get(adapter.id)
        self._models[adapter.id] = (time.monotonic(), path, list(dict.fromkeys(models)))
        if previous is None or previous[2] != self._models[adapter.id][2]:
            await self._announce_clis()

    async def models(self, cli: str) -> list[str]:
        """The model ids of ``cli``, waiting for the listing when none is cached."""
        adapter, found = await self._usable(cli)
        cached = self._models.get(cli)
        path = found.executable.path if found.executable is not None else None
        if cached is None or cached[1] != path:
            self._models_now(adapter, found, refresh=False)
            task = self._model_tasks.get(cli)
            if task is not None:
                await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            cached = self._models.get(cli)
        return list(cached[2]) if cached is not None else []

    async def _announce_clis(self) -> None:
        """``external.clis.changed`` (daemon-wide): a CLI row changed (its models arrived)."""
        if self.hub is None or self._closed:
            return
        try:
            rows = await self.clis()
            await self.hub.notify("external.clis.changed", {"clis": rows})
        except Exception:  # noqa: BLE001 - a dead socket must not break the listing
            log.debug("could not broadcast external.clis.changed", exc_info=True)

    async def _usable(self, cli: str) -> tuple[ExternalAdapter, Discovery]:
        """The adapter for ``cli`` when it can run sessions, else ``external_unavailable``."""
        ext = self._ext_settings()
        if not bool(getattr(ext, "enabled", True)):
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                "external sessions are turned off (jarvis.external.enabled)",
            )
        adapter = self.registry.get(cli)
        if adapter is None:
            title = CLI_TITLES.get(cli, cli)
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"{title} is not supported by this snowpea core yet"
            )
        self._bind(adapter)
        if not bool(getattr(self._cli_settings(cli), "enabled", True)):
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"{adapter.title} is turned off (jarvis.external.clis.{cli}.enabled)",
            )
        found = await self.discover(cli)
        assert found is not None
        if not found.installed or found.executable is None:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"{adapter.title} is not installed on this machine"
            )
        if found.problems:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"{adapter.title}: {'; '.join(found.problems)}",
                {"problems": list(found.problems)},
            )
        return adapter, found

    # -- projects ---------------------------------------------------------
    async def roots(self) -> list[str]:
        return list(await self._roots()) if self._roots is not None else []

    async def _exposed_roots(self) -> list[str]:
        """Exposed roots, never one below a hidden root, never a wide root (M2/M3).

        A folder too wide to stand for its contents (``/``, home) only ever
        matches itself, so listing "every project" never sweeps every CLI
        session on the machine in through it.
        """
        roots = await self.roots()
        hidden: list[Path] = []
        shown: list[str] = []
        for root in roots:
            if self._exposed is None or await self._exposed(root):
                shown.append(root)
            else:
                hidden.append(Path(root).expanduser().resolve())
        out = []
        for root in shown:
            real = Path(root).expanduser().resolve()
            if proc._wide_root(real):
                continue
            if any(real == base or (not proc._wide_root(base) and base in real.parents)
                   for base in hidden):
                continue
            out.append(root)
        return out

    # -- rows -------------------------------------------------------------
    def _row_of(self, record: _Record, found: Discovery | None) -> NativeMode | None:
        """The native row a session runs in (from the full table: a row the CLI
        switched to itself is still described)."""
        if record.native_mode:
            from snowpea_core.external.modes import find

            return find(record.adapter.table(), record.native_mode)
        return None

    def _approvals(self, record: _Record, found: Discovery | None) -> str:
        if record.host == "tmux":
            return "person"
        if record.host != "snowpea":
            return "none"
        value = record.adapter.approvals(record.mode or "default", found)
        row = self._row_of(record, found)
        if row is not None and row.approvals == "none" and value == "bridged":
            # The mode's prompts are answered by the CLI itself (dontAsk), not here.
            return "none"
        return value

    def _controlled(self, record: _Record, found: Discovery | None) -> bool:
        if record.host not in ("snowpea", "tmux"):
            return True
        if not record.adapter.controlled(record.mode or "default", found):
            return False
        row = self._row_of(record, found)
        return not (row is not None and row.uncontrolled)

    def _can_send(self, record: _Record) -> bool:
        if record.host == "terminal":
            return False
        if record.host == "tmux":
            return record.channel is not None and record.channel.alive
        return True

    def _cached_discovery(self, cli: str) -> Discovery | None:
        cached = self._discovery.get(cli)
        return cached[2] if cached else None

    def _row(self, record: _Record) -> dict[str, Any]:
        found = self._cached_discovery(record.cli)
        info = record.info
        return {
            "ref": record.ref,
            "title": _line(info.title),
            "cwd": info.cwd,
            "project": record.project,
            "updatedAt": info.updated_at,
            "status": record.status,
            "host": record.host,
            "canSend": self._can_send(record),
            "approvals": self._approvals(record, found),
            "tmux": record.channel.tmux if record.channel is not None else None,
            "terminal": self._terminal_of(record),
            "controllable": "tmux" if self._terminal_of(record) is not None else None,
            "mode": record.mode if record.host in ("snowpea", "tmux") else None,
            "nativeMode": record.native_mode if record.host in ("snowpea", "tmux") else None,
            "controlled": self._controlled(record, found),
            "preview": _preview(info.preview),
            "model": info.model,
            "securityListsApply": self._lists_apply(
                record.project, record.host, self._approvals(record, found),
                self._controlled(record, found),
            ),
        }

    def _lists_apply(self, project: str | None, host: str, approvals: str,
                     controlled: bool) -> bool | None:
        """``ExternalSessionRow.securityListsApply``: null with empty lists; false when
        snowpea cannot see what the CLI runs (unrestricted or uncontrolled, a tmux pane
        whose prompts are answered there, another terminal)."""
        from snowpea_core.security.enforce import lists_for

        try:
            lists = lists_for(getattr(self.approvals, "settings", None), project)
        except Exception:  # noqa: BLE001 - unknown lists: say nothing
            return None
        if lists.empty:
            return None
        if host == "terminal":
            return False
        if host in ("snowpea", "tmux"):
            return bool(controlled) and approvals == "bridged"
        return True

    def _state(self, record: _Record, previous: str | None, previous_host: str | None
               ) -> dict[str, Any]:
        row = self._row(record)
        return {
            "ref": row["ref"],
            "status": row["status"],
            "previous": previous,
            "host": row["host"],
            "previousHost": previous_host,
            "canSend": row["canSend"],
            "approvals": row["approvals"],
            "tmux": row["tmux"],
            "mode": row["mode"],
            "nativeMode": row["nativeMode"],
            "terminal": row["terminal"],
            "controllable": row["controllable"],
            "controlled": row["controlled"],
            "title": row["title"],
            "error": clean_text(record.error) if record.status == "error" and record.error
            else None,
        }

    async def _announce(
        self, record: _Record, previous: str | None = None, previous_host: str | None = None
    ) -> None:
        """``external.state`` to every person's connection and the session's own subscribers.

        A browser host or the connector hears only about sessions it watches
        (security review L6).
        """
        if self.hub is None:
            return
        from snowpea_core.server.person_gate import is_person

        state = self._state(record, previous, previous_host)
        for conn in list(record.subscribers):
            # The hub sends it to people only; a browser host or the connector
            # gets it here, for a session it watches.
            if getattr(conn, "closed", False) or is_person(conn):
                continue
            if not hasattr(conn, "client_kind"):
                continue  # an in-process listener: the hub tells it
            with contextlib.suppress(Exception):
                await conn.notify("external.state", state)
        try:
            await self.hub.notify("external.state", state)
        except Exception:  # noqa: BLE001 - a dead socket must not break the session
            log.debug("could not broadcast external.state", exc_info=True)

    async def _set(self, record: _Record, *, status: str | None = None, host: str | None = None,
                   error: str | None = None) -> None:
        previous, previous_host = record.status, record.host
        if status is not None:
            record.status = status
        if host is not None:
            record.host = host
        if error is not None or status == "idle":
            record.error = error
        await self._announce(
            record,
            previous if previous != record.status else None,
            previous_host if previous_host != record.host else None,
        )

    # -- events -----------------------------------------------------------
    def _duplicate(self, record: _Record, item: TimelineItem) -> bool:
        """A transcript copy of a prompt snowpea itself sent (published already).

        The CLI records every prompt under its own id (a Codex item id, agy's
        replay index, Claude's record uuid), so a later re-read of the history
        brings back what snowpea published under its live key.  Such a copy is
        matched to the oldest prompt snowpea sent that no copy matched yet, by its
        text; it is dropped, and never read as the person typing.
        """
        if item.kind != "message.user" or item.key in record.live_keys:
            return False
        text = _norm(item.payload.get("text"))
        if not text:
            return False
        for prompt in record.live_prompts:
            if not prompt.matched and prompt.text == text:
                prompt.matched = True
                return True
        return False

    def _live_user(self, record: _Record, text: str, *, key: str, turn_id: str,
                   origin: dict[str, Any]) -> None:
        """Build the ``message.user`` of a prompt snowpea sends, and remember it."""
        record.live_keys.add(key)
        record.live_prompts.append(_LivePrompt(_norm(text)))
        record.builder.user(text, key=key, turn_id=turn_id, origin=origin)

    async def _publish(self, record: _Record, items: Iterable[TimelineItem], *,
                       observe: bool = True) -> None:
        """Number, keep and send ``external.event`` to the session's subscribers.

        ``observe`` is false for the history read when a session is loaded:
        observers (Jarvis) hear only what happens from then on.
        """
        observed: list[dict[str, Any]] = []
        for item in items:
            if item.key in record.keys:
                continue
            if self._duplicate(record, item):
                record.keys.add(item.key)
                continue
            record.keys.add(item.key)
            record.seq += 1
            event = item.to_event(record.seq)
            record.events.append(event)
            if len(record.events) > MAX_EVENTS:
                del record.events[: len(record.events) - MAX_EVENTS]
            params = {"ref": record.ref, **event}
            for conn in list(record.subscribers):
                if getattr(conn, "closed", False):
                    record.subscribers.remove(conn)
                    continue
                try:
                    await conn.notify("external.event", params)
                except Exception:  # noqa: BLE001 - one dead socket must not stop the rest
                    log.debug("dropping external.event for a closed connection", exc_info=True)
            if observe and item.kind not in _UNOBSERVED:
                observed.append(event)
        if observed and self.observers and record.info.id:
            row = self._row(record)
            for event in observed:
                for observer in list(self.observers):
                    try:
                        await observer(row, event)
                    except Exception:  # noqa: BLE001 - an observer never breaks the session
                        log.warning("external observer failed", exc_info=True)

    def _page(self, record: _Record, before_seq: int | None, limit: int
              ) -> tuple[list[dict[str, Any]], bool]:
        events = record.events
        if before_seq is not None:
            events = [event for event in events if event["seq"] < before_seq]
        page = events[-limit:] if limit > 0 else []
        has_more = len(events) > len(page)
        return [dict(event) for event in page], has_more

    def _subscribe(self, record: _Record, conn: Any) -> None:
        if conn is None:
            return
        if not any(other is conn for other in record.subscribers):
            record.subscribers.append(conn)
        marker = id(conn)
        if marker not in self._watched_conns and hasattr(conn, "on_close"):
            self._watched_conns.add(marker)
            conn.on_close.append(self._connection_closed)

    async def _connection_closed(self, conn: Any) -> None:
        self._watched_conns.discard(id(conn))
        for record in list(self._records.values()):
            if any(other is conn for other in record.subscribers):
                record.subscribers = [other for other in record.subscribers if other is not conn]
                await self._maybe_forget(record)

    async def _maybe_forget(self, record: _Record) -> None:
        """Drop a record nobody watches and nothing runs; the next open re-reads it."""
        if record.subscribers or record.turn_id is not None:
            return
        if record.channel is not None and record.channel.alive:
            return
        await self._stop_follow(record)
        if self._records.get((record.cli, record.id)) is record:
            self._records.pop((record.cli, record.id), None)

    # -- list -------------------------------------------------------------
    async def list_sessions(
        self,
        *,
        clis: Sequence[str] | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        all_projects: bool = False,
        limit: int = 100,
        before: str | None = None,
    ) -> dict[str, Any]:
        """``external.list``: one folder's sessions (or every exposed project's), newest first."""
        wanted = [cli for cli in CLI_ORDER if clis is None or cli in clis]
        usable: list[ExternalAdapter] = []
        problems: list[str] = []
        for cli in wanted:
            try:
                adapter, _found = await self._usable(cli)
            except ExternalError as exc:
                problems.append(exc.message)
                continue
            usable.append(adapter)
        if not usable:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                "no external CLI is available" + (f": {problems[0]}" if problems else ""),
                {"problems": problems},
            )
        if all_projects:
            scope = await self._exposed_roots()
        else:
            folder = cwd
            if folder is None and session_id is not None:
                folder = await self._workdir_of(session_id) if self._workdir_of else None
                if folder is None:
                    raise ExternalError(errors.NOT_FOUND, f"no session {session_id}")
            if folder is None:
                raise ExternalError(
                    errors.INVALID_PARAMS, "one of cwd, sessionId or allProjects is required"
                )
            scope = [proc.validate_cwd(folder, await self.roots())]
        cursor = _parse_cursor(before)
        rows: list[tuple[tuple[str, str, str], SessionInfo, ExternalAdapter]] = []
        real_scope = [str(Path(root).expanduser().resolve()) for root in scope]
        if scope:
            for adapter in usable:
                bound = cursor[0] if cursor is not None else None
                try:
                    found = await adapter.list_sessions(scope, limit=limit + 1, before=bound)
                except ExternalError:
                    raise
                except Exception:  # noqa: BLE001 - one broken store must not hide the rest
                    log.warning("listing %s sessions failed", adapter.id, exc_info=True)
                    continue
                for info in found:
                    # The adapters match by path prefix; keep a row only when its
                    # real folder is under the scope by the wide-root rule (M3).
                    if proc.project_of(os.path.realpath(info.cwd or "/"), real_scope) is None:
                        continue
                    key = (info.updated_at or "", adapter.id, info.id)
                    if cursor is not None and key >= cursor:
                        continue
                    rows.append((key, info, adapter))
        rows.sort(key=lambda row: row[0], reverse=True)
        page = rows[:limit]
        all_roots = await self.roots()
        out = []
        pane_cache: dict[str, list[Any]] = {}
        for _key, info, adapter in page:
            record = self._records.get((adapter.id, info.id))
            if record is not None:
                out.append(self._row(record))
                continue
            out.append(await self._listed_row(adapter, info, all_roots,
                                              pane_cache=pane_cache))
        next_before = _cursor(page[-1][0]) if len(rows) > limit and page else None
        return {"sessions": out, "nextBefore": next_before}

    async def _listed_row(
        self, adapter: ExternalAdapter, info: SessionInfo, roots: list[str],
        pane_cache: dict[str, list[Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            live = await adapter.liveness(info, own_pids=self.supervisor.pids())
        except Exception:  # noqa: BLE001 - unknown liveness reads as "not running"
            log.debug("liveness of %s %s failed", adapter.id, info.id, exc_info=True)
            live = None
        host = live.host if live is not None else "none"
        pane = None
        if live is not None and live.host in ("terminal", "tmux") and live.pids:
            pane = await self._pane_of(live, cache=pane_cache)
        if pane is not None:
            host = "tmux"
        return {
            "ref": _ref(adapter.id, info.id),
            "title": _line(info.title),
            "cwd": info.cwd,
            "project": proc.project_of(info.cwd, roots),
            "updatedAt": info.updated_at,
            "status": "running" if live is not None and live.busy else info.status,
            "host": host,
            "canSend": host == "none" or pane is not None,
            "approvals": "person" if pane is not None else "none",
            "tmux": ({"session": pane.session, "pane": pane.pane} if pane is not None
                     else live.tmux if live is not None else None),
            "terminal": ({**pane.wire(), "modeState": "unknown", "prompt": None}
                         if pane is not None else None),
            "controllable": "tmux" if pane is not None else None,
            "mode": None,
            "nativeMode": None,
            "controlled": True,
            "preview": _preview(info.preview),
            "model": info.model,
            "securityListsApply": self._lists_apply(
                proc.project_of(info.cwd, roots), host, "none", True
            ),
        }

    # -- open / read --------------------------------------------------------
    async def _load(self, cli: str, session_id: str) -> tuple[_Record, Discovery]:
        """The record for a stored session, reading its transcript when needed."""
        adapter, found = await self._usable(cli)
        proc.validate_session_id(session_id)
        key = (cli, session_id)
        record = self._records.get(key)
        if record is not None:
            return record, found
        info = await adapter.get_session(session_id)
        if info is None:
            raise ExternalError(errors.NOT_FOUND, f"no {adapter.title} session {session_id}")
        roots = await self.roots()
        cwd = proc.validate_cwd(info.cwd, roots)
        record = self._records.get(key)
        if record is not None:
            return record, found
        record = _Record(
            adapter=adapter,
            info=info,
            project=proc.project_of(cwd, [str(Path(root).resolve()) for root in roots]),
            builder=TimelineBuilder(cli, session_id, salt=f"live-{uuid.uuid4().hex[:8]}"),
            real_cwd=cwd,
        )
        self._records[key] = record
        try:
            await self._publish(record, await adapter.read(info), observe=False)
        except ExternalError:
            self._records.pop(key, None)
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable transcript is an error, not a crash
            self._records.pop(key, None)
            log.warning("reading %s session %s failed", cli, session_id, exc_info=True)
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"could not read the {adapter.title} transcript"
            ) from exc
        return record, found

    async def _refresh_host(self, record: _Record) -> str | None:
        """Re-check where the process lives (records snowpea does not run).

        Returns the liveness host (``tmux`` for a pane the person runs, which the
        row shows as ``terminal`` until it is adopted).
        """
        if record.channel is not None and record.channel.alive:
            return None
        live = await record.adapter.liveness(record.info, own_pids=self.supervisor.pids())
        # A pane snowpea did not start is somebody's terminal: view only.
        record.host = "terminal" if live.host in ("terminal", "tmux") else "none"
        if record.host == "terminal":
            record.status = "running" if live.busy else "idle"
        return live.host

    async def open(
        self, cli: str, session_id: str, *, limit: int = 200, mode: str | None = None,
        conn: Any = None, transport: str | None = None,
        confirmation: PersonConfirmation | None = None, trust: bool = False,
        native_mode: str | None = None,
    ) -> dict[str, Any]:
        """``external.open``: subscribe and return the newest page; continue a free session.

        ``transport="tmux"`` on a Claude session the person runs in a tmux pane
        adopts that pane (after the person allows it, person-only) instead of
        showing it view only.  ``trust`` is the person's one-time confirmation
        for a project no external CLI ran in yet (:meth:`_require_trust`).
        """
        record, found = await self._load(cli, session_id)
        if transport not in (None, "structured", "tmux"):
            raise ExternalError(errors.INVALID_PARAMS, f"unknown transport {transport!r}")
        try:
            return await self._open(record, found, limit=limit, mode=mode, conn=conn,
                                    transport=transport, confirmation=confirmation, trust=trust,
                                    native_mode=native_mode)
        except ExternalError:
            await self._maybe_forget(record)
            raise

    async def _open(
        self, record: _Record, found: Discovery, *, limit: int, mode: str | None, conn: Any,
        transport: str | None = None, confirmation: PersonConfirmation | None = None,
        trust: bool = False, native_mode: str | None = None,
    ) -> dict[str, Any]:
        cli, session_id = record.cli, record.id
        async with record.lock:
            previous, previous_host = record.status, record.host
            if record.channel is None or not record.channel.alive:
                if record.channel is not None:
                    record.channel = None
                    record.host = "none"
                live_host = await self._refresh_host(record)
                adopt = transport == "tmux" and live_host == "tmux"
                if adopt and not (record.adapter.capabilities.tmux and proc.tmux_supported()):
                    raise ExternalError(
                        errors.EXTERNAL_UNAVAILABLE,
                        f"{record.adapter.title} cannot share a tmux terminal on this machine",
                    )
                if record.host == "none" or adopt:
                    resolved = self.resolve_mode(record.adapter, mode, native_mode, found)
                    chosen, row = resolved or self._implicit(record)
                    self._require_trust(record.project, record.real_cwd or record.info.cwd,
                                        conn, trust)
                    grant = self._grant_if_unrestricted(record, chosen, conn, "open",
                                                        confirmation=confirmation, row=row)
                    try:
                        await self._start_channel(
                            record, chosen, grant, found, resume=True,
                            transport="tmux" if adopt or transport == "tmux" else "structured",
                            native_mode=row.id,
                        )
                    except ExternalError as exc:
                        if exc.code in (errors.JARVIS_PERSON_ONLY, errors.INVALID_PARAMS):
                            raise
                        if adopt:
                            await self._refresh_host(record)
                        # View it anyway; the next send tries again.
                        record.error = exc.message
                        log.info("could not continue %s %s: %s", cli, session_id, exc.message)
            if record.host == "terminal" and transport != "tmux":
                with contextlib.suppress(ExternalError):
                    await self._attach_bridge(record, found)
            self._subscribe(record, conn)
            if record.host == "terminal":
                self._start_follow(record)
            if (previous, previous_host) != (record.status, record.host) or record.error:
                await self._announce(
                    record,
                    previous if previous != record.status else None,
                    previous_host if previous_host != record.host else None,
                )
            events, has_more = self._page(record, None, limit)
            return {
                "session": self._row(record),
                "events": events,
                "hasMore": has_more,
                "readOnly": not self._can_send(record),
            }

    async def read(self, cli: str, session_id: str, *, before_seq: int | None = None,
                   limit: int = 200) -> dict[str, Any]:
        """``external.read``: an older page."""
        record, _found = await self._load(cli, session_id)
        events, has_more = self._page(record, before_seq, limit)
        await self._maybe_forget(record)
        return {"events": events, "hasMore": has_more}

    async def get(self, cli: str, session_id: str) -> dict[str, Any]:
        """``external.get``: one session's row, without opening or reading it.

        For deep links, pop-outs and restored tabs.  A session outside every
        registered project is refused (``outside_root``) as on open.
        """
        adapter, _found = await self._usable(cli)
        proc.validate_session_id(session_id)
        record = self._records.get((cli, session_id))
        if record is not None:
            return {"session": self._row(record)}
        info = await adapter.get_session(session_id)
        if info is None:
            raise ExternalError(errors.NOT_FOUND, f"no {adapter.title} session {session_id}")
        roots = await self.roots()
        proc.validate_cwd(info.cwd, roots)
        resolved = [str(Path(root).resolve()) for root in roots]
        return {"session": await self._listed_row(adapter, info, resolved)}

    async def unwatch(self, cli: str, session_id: str, *, conn: Any = None) -> None:
        """``external.unwatch``: stop sending ``external.event`` to ``conn``; nothing stops.

        A screen that closes calls this (not ``external.close``) so the CLI keeps
        running; an idle process is stopped by the idle sweep (``idleSec``).
        """
        if self.registry.get(cli) is None:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"{CLI_TITLES.get(cli, cli)} is not available"
            )
        proc.validate_session_id(session_id)
        record = self._records.get((cli, session_id))
        if record is None or conn is None:
            return
        record.subscribers = [other for other in record.subscribers if other is not conn]
        await self._maybe_forget(record)

    # -- follow (view only) and takeover -------------------------------------
    def _start_follow(self, record: _Record) -> None:
        if record.follow_task is not None and not record.follow_task.done():
            return
        record.follow_task = asyncio.ensure_future(self._follow(record))

    async def _stop_follow(self, record: _Record) -> None:
        task = record.follow_task
        record.follow_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _follow(self, record: _Record) -> None:
        """Tail a session open elsewhere and notice when that process exits."""
        tail: asyncio.Task[None] | None = None
        if record.adapter.capabilities.live_view:
            tail = asyncio.ensure_future(self._tail(record))
        try:
            while not self._closed and record.host == "terminal":
                await asyncio.sleep(self.poll_interval)
                try:
                    live = await record.adapter.liveness(
                        record.info, own_pids=self.supervisor.pids()
                    )
                except Exception:  # noqa: BLE001 - try again on the next tick
                    log.debug("liveness check failed", exc_info=True)
                    continue
                if live.host == "none":
                    # The other terminal closed: catch up on what it wrote last,
                    # then the session is free to take over.
                    with contextlib.suppress(Exception):
                        fresh = await record.adapter.get_session(record.id)
                        if fresh is not None:
                            record.info = fresh
                        await self._publish(record, await record.adapter.read(record.info))
                    await self._set(record, host="none", status="idle")
                    return
                busy = "running" if live.busy else "idle"
                if busy != record.status:
                    await self._set(record, status=busy)
        except asyncio.CancelledError:
            raise
        finally:
            if tail is not None:
                tail.cancel()
                await asyncio.gather(tail, return_exceptions=True)
            if record.follow_task is asyncio.current_task():
                record.follow_task = None

    async def _tail(self, record: _Record) -> None:
        try:
            async for items in record.adapter.follow(record.info, interval=self.poll_interval):
                await self._publish(record, items)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken tail leaves the view as it was
            log.debug("live tail of %s %s stopped", record.cli, record.id, exc_info=True)

    # -- the tmux bridge (1.56.0) --------------------------------------------
    def _bridge_of(self, record: _Record) -> Any:
        from snowpea_core.external.tmux_channel import TmuxBridgeChannel

        channel = record.channel
        return channel if isinstance(channel, TmuxBridgeChannel) and channel.alive else None

    def _terminal_of(self, record: _Record) -> dict[str, Any] | None:
        bridge = self._bridge_of(record)
        return bridge.wire() if bridge is not None else None

    async def _pane_of(self, live: Any, *, cache: dict[str, list[Any]] | None = None) -> Any:
        """The tmux pane one of ``live.pids`` runs in, or ``None`` (no tmux, no match)."""
        from snowpea_core.external import tmux

        for pid in live.pids:
            try:
                pane = await tmux.find_pane(pid, cache=cache, sockets=self.tmux_sockets,
                                             probe=self.tmux_probe)
            except Exception:  # noqa: BLE001 - no pane is today's view-only behaviour
                log.debug("tmux pane lookup failed", exc_info=True)
                pane = None
            if pane is not None:
                return pane
        return None

    async def _attach_bridge(self, record: _Record, found: Discovery) -> bool:
        """Drive a session open in the person's tmux pane through the bridge.

        Only from the process that holds the session (its tty -> the pane); no
        pane, no bridge: the session stays view only.
        """
        from snowpea_core.external import tmux
        from snowpea_core.external.tmux_channel import TmuxBridgeChannel

        profile = tmux.PROFILES.get(record.cli)
        if profile is None or found.executable is None:
            return False
        live = await record.adapter.liveness(record.info, own_pids=self.supervisor.pids())
        if live.host not in ("terminal", "tmux") or not live.pids:
            return False
        for pid in live.pids:
            try:
                pane = await tmux.find_pane(pid, sockets=self.tmux_sockets,
                                             probe=self.tmux_probe)
            except Exception:  # noqa: BLE001
                log.debug("tmux pane lookup failed", exc_info=True)
                pane = None
            if pane is None:
                continue
            facts = self.tmux_probe.facts(pid)
            bridge = tmux.TerminalBridge(
                cli=record.cli, pane=pane, cli_pid=pid,
                start=facts.start if facts is not None else None, profile=profile,
                probe=self.tmux_probe, audit=tmux.AuditLog(self.home / "external" /
                                                           "tmux-sends.jsonl"),
            )
            spec = LaunchSpec(cwd=record.real_cwd or record.info.cwd, project=record.project,
                              executable=found.executable, mode=record.mode or "default",
                              session_id=record.info.id, resume=True,
                              transport="tmux")
            channel = TmuxBridgeChannel(spec, _Host(self, record), record.adapter, record.info,
                                        bridge, poll_interval=self.poll_interval,
                                        start_timeout=self.tmux_start_timeout)
            try:
                await channel.start()
            except ExternalError as exc:
                log.info("not bridging %s %s: %s", record.cli, record.id, exc.message)
                await channel.close()
                continue
            await self._stop_follow(record)
            record.channel = channel
            record.host = "tmux"
            confirmed = channel.mode_state == "confirmed"
            record.mode = channel.mode if confirmed else None
            record.native_mode = channel.native_mode if confirmed else None
            record.error = None
            return True
        return False

    async def _bridge_lost(self, record: _Record, reason: str) -> None:
        channel = record.channel
        if channel is None:
            return
        record.channel = None
        with contextlib.suppress(Exception):
            await channel.close()
        record.mode = record.native_mode = None
        previous = record.host
        record.host = "none"
        await self._refresh_host(record)
        if record.host == "terminal":
            self._start_follow(record)
        log.info("tmux bridge of %s %s ended: %s", record.cli, record.id, reason)
        await self._announce(record, None, previous if previous != record.host else None)

    def _bridge_send_allowed(self, record: _Record, conn: Any,
                             confirmation: PersonConfirmation | None) -> None:
        """Who may type into a person's tmux pane.

        The person at a local surface: always.  Anyone else (Jarvis, a remote web
        login, a connector) only while the pane shows a mode snowpea recognised
        that nobody but the person chooses and that asks before it acts.
        """
        if confirmation is not None or (proc.is_human_connection(conn)
                                        and not getattr(conn, "remote_login", False)):
            return
        bridge = self._bridge_of(record)
        row = self._row_of(record, self._cached_discovery(record.cli))
        if (bridge is None or bridge.mode_state != "confirmed" or row is None
                or row.person_only or row.uncontrolled or row.legacy == "unrestricted"):
            raise ExternalError(
                errors.JARVIS_PERSON_ONLY,
                "this session runs in the person's tmux pane in a mode snowpea could not "
                "confirm, or one only the person chooses; only the person may type into it",
            )

    # -- channels ---------------------------------------------------------
    def _grant_if_unrestricted(
        self, record_or_cli: _Record | str, mode: str, conn: Any, action: str,
        project: str | None = None, confirmation: PersonConfirmation | None = None,
        row: NativeMode | None = None,
    ) -> UnrestrictedGrant | None:
        """The person's grant for a person-only mode (a ledger entry), or ``jarvis_person_only``.

        ``unrestricted`` and every native row with ``personOnly`` (1.56.0: Claude
        Code auto, acceptEdits, ...) need it, with the same gate: a person's own
        connection (a remote web login may not), or ``confirmation`` from
        :func:`proc.person_confirmation_from_gateway` (a messenger's bound person).
        """
        if mode != "unrestricted" and not (row is not None and row.person_only):
            return None
        if confirmation is None:
            confirmation = proc.confirm_person(conn, self._settings())
        if isinstance(record_or_cli, _Record):
            cli, session_id, project = record_or_cli.cli, record_or_cli.id, record_or_cli.project
        else:
            cli, session_id = record_or_cli, None
        return proc.grant_unrestricted(
            confirmation, cli=cli, session_id=session_id, project=project, action=action,
            home=self.home, ledger=self.ledger,
            mode_label=row.label if row is not None and mode != "unrestricted" else None,
        )

    async def _start_channel(
        self, record: _Record, mode: str, grant: UnrestrictedGrant | None, found: Discovery,
        *, resume: bool, model: str | None = None, transport: str = "structured",
        native_mode: str | None = None,
    ) -> None:
        assert found.executable is not None
        # The folder is validated again now and the CLI starts in its real path,
        # never the transcript's raw string (security review L8).
        real = proc.validate_cwd(record.real_cwd or record.info.cwd, await self.roots())
        if record.real_cwd is not None and real != record.real_cwd:
            raise ExternalError(
                errors.OUTSIDE_ROOT, "the session's folder changed since it was opened"
            )
        record.real_cwd = real
        spec = LaunchSpec(
            cwd=real,
            project=record.project,
            executable=found.executable,
            mode=mode,
            session_id=record.info.id or None,
            resume=resume,
            model=model,
            grant=grant,
            transport=transport,  # type: ignore[arg-type]
            native_mode=native_mode,
        )
        channel = record.adapter.channel(spec, _Host(self, record))
        try:
            await channel.start()
        except ExternalError as exc:
            with contextlib.suppress(Exception):
                await channel.close()
            if exc.code == errors.EXTERNAL_VIEW_ONLY and resume:
                # The CLI says another process holds it ("active writer"): show it
                # view only right away, and follow it until that process exits.
                record.host = "terminal"
                self._start_follow(record)
            raise
        except Exception as exc:  # noqa: BLE001 - an adapter bug is an unavailable CLI
            with contextlib.suppress(Exception):
                await channel.close()
            log.warning("%s channel failed to start", record.cli, exc_info=True)
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"{record.adapter.title} did not start: {type(exc).__name__}",
            ) from exc
        record.channel = channel
        # An adopted pane started with a bypass flag runs unrestricted whatever
        # was asked (security review M5).
        record.mode = getattr(channel, "actual_mode", None) or mode
        record.native_mode = (getattr(channel, "actual_native", None)
                              or getattr(channel, "native_mode", None) or native_mode)
        record.host = channel.host
        record.error = None
        record.last_activity = time.monotonic()
        if channel.session_id and not record.info.id:
            record.info.id = channel.session_id

    async def _close_channel(self, record: _Record, *, reason: str = "interrupted") -> None:
        channel = record.channel
        record.channel = None
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.close()
        if record.turn_id is not None:
            await self._end_turn(record, reason)

    # -- turns ------------------------------------------------------------
    async def send(
        self, cli: str, session_id: str, text: str, *, conn: Any = None,
        origin: dict[str, Any] | None = None,
        confirmation: PersonConfirmation | None = None,
        trust: bool = False,
    ) -> dict[str, str]:
        """``external.send``: start a turn; the reply streams as ``external.event``.

        ``trust`` as on :meth:`open`: continuing a ``host: none`` session in a
        project no person trusted yet (1.54.0).

        A ``host: none`` session is continued first in its current mode, or the
        CLI's ``defaultMode`` (``unrestricted`` only for a person; anyone else
        gets ``jarvis_person_only``).  During a running turn a CLI that can steer
        (``capabilities.steer``) takes the text into that turn; the others answer
        ``session_busy``.
        """
        if not text or not text.strip():
            raise ExternalError(errors.INVALID_PARAMS, "empty prompt")
        record, found = await self._load(cli, session_id)
        try:
            return await self._send(record, found, text, conn=conn, origin=origin,
                                    confirmation=confirmation, trust=trust)
        except ExternalError:
            await self._maybe_forget(record)
            raise

    async def _send(
        self, record: _Record, found: Discovery, text: str, *, conn: Any,
        origin: dict[str, Any] | None, confirmation: PersonConfirmation | None = None,
        trust: bool = False,
    ) -> dict[str, str]:
        cli = record.cli
        async with record.lock:
            if record.channel is None or not record.channel.alive:
                record.channel = None
                await self._refresh_host(record)
                bridged = (record.host == "terminal"
                           and await self._attach_bridge(record, found))
                if bridged:
                    await self._announce(record, None, "terminal")
                elif record.host == "terminal":
                    raise ExternalError(
                        errors.EXTERNAL_VIEW_ONLY,
                        f"this {record.adapter.title} session is open in another terminal; "
                        "it can be continued here once that one exits",
                    )
                if not bridged:
                    previous_host = record.host
                    # Continued in its last controlled mode: unrestricted needs a new,
                    # explicit choice (external.open / external.setMode), never a send.
                    mode, row = self._implicit(record)
                    self._require_trust(record.project, record.real_cwd or record.info.cwd,
                                        conn, trust)
                    try:
                        await self._start_channel(record, mode, None, found, resume=True,
                                                  native_mode=row.id)
                    except ExternalError:
                        if record.host != previous_host:
                            await self._announce(record, None, previous_host)
                        raise
                    await self._announce(record, None, previous_host)
            channel = record.channel
            assert channel is not None
            row = self._row_of(record, found)
            if (
                (record.mode == "unrestricted" or (row is not None and row.person_only))
                and not proc.is_human_connection(conn)
                and confirmation is None
            ):
                # Nothing gates what an unrestricted CLI does with a prompt, and a
                # person-only mode (auto, accept edits) decides much on its own:
                # only the person types into it (security review M5, 1.56.0).
                raise ExternalError(
                    errors.JARVIS_PERSON_ONLY,
                    "this session runs unrestricted; only the person may send to it"
                    if record.mode == "unrestricted" or row is None else
                    f"this session runs in mode {row.label}, which only the person chose; "
                    "only the person may send to it",
                )
            if self._bridge_of(record) is not None:
                self._bridge_send_allowed(record, conn, confirmation)
                # A slash command only from the person at a local surface.
                channel.allow_slash = bool(  # type: ignore[attr-defined]
                    confirmation is None and proc.is_human_connection(conn)
                    and not getattr(conn, "remote_login", False))
                channel.actor = (  # type: ignore[attr-defined]
                    "person" if proc.is_human_connection(conn) and not getattr(
                        conn, "remote_login", False) else
                    "remote" if proc.is_human_connection(conn) else
                    str((origin or {}).get("kind") or "jarvis"))
            if record.turn_id is not None:
                if record.adapter.capabilities.steer and channel.can_steer:
                    self._subscribe(record, conn)
                    return await self._steer(record, channel, text, conn=conn, origin=origin)
                raise ExternalError(
                    errors.SESSION_BUSY, "a turn is running in this session; interrupt it first"
                )
            # A refusal the channel can tell now (a busy terminal) publishes nothing.
            try:
                await channel.precheck(text)
            except ExternalError as exc:
                if isinstance(exc.details, dict) and exc.details.get("reason") == "terminal_guard":
                    await self._bridge_lost(record, exc.message)
                raise
            # The reply streams to the sender as external.event, opened or not.
            self._subscribe(record, conn)
            turn_id = f"xt-{uuid.uuid4().hex[:12]}"
            record.turn_id = turn_id
            record.turn_cancel = asyncio.Event()
            record.builder.turn_started(turn_id, text)
            self._live_user(record, text, key=f"{turn_id}:user", turn_id=turn_id,
                            origin=_origin_for(conn, origin))
            await self._publish(record, record.builder.drain())
            await self._set(record, status="running")
            record.info.preview = text
            record.info.updated_at = utc_now()
            try:
                await channel.send(text, turn_id)
            except ExternalError as exc:
                await self._end_turn(record, "error", exc.message)
                raise
            except Exception as exc:  # noqa: BLE001 - adapter bug: end the turn cleanly
                log.warning("%s send failed", cli, exc_info=True)
                await self._end_turn(record, "error", f"{type(exc).__name__}")
                raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "the CLI did not take the prompt"
                                    ) from exc
            if self._bridge_of(record) is not None:
                return {"turnId": turn_id, "via": "tmux"}
            return {"turnId": turn_id}

    async def _steer(self, record: _Record, channel: ExternalChannel, text: str, *,
                     conn: Any, origin: dict[str, Any] | None) -> dict[str, str]:
        """Add ``text`` to the running turn (``external.send`` on a CLI that steers)."""
        turn_id = record.turn_id
        assert turn_id is not None
        try:
            await channel.steer(text)
        except ExternalError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter bug: the turn goes on as it was
            log.warning("%s steer failed", record.cli, exc_info=True)
            raise ExternalError(
                errors.SESSION_BUSY, "the CLI did not take the text into the running turn"
            ) from exc
        record.steers += 1
        self._live_user(record, text, key=f"{turn_id}:steer:{record.steers}", turn_id=turn_id,
                        origin=_origin_for(conn, origin))
        await self._publish(record, record.builder.drain())
        record.info.preview = text
        record.info.updated_at = utc_now()
        record.last_activity = time.monotonic()
        return {"turnId": turn_id}

    async def _end_turn(self, record: _Record, reason: str, error: str | None = None) -> None:
        turn_id = record.turn_id
        if turn_id is None:
            return
        record.turn_id = None
        if record.turn_cancel is not None:
            record.turn_cancel.set()
            record.turn_cancel = None
        if error:
            record.builder.error(errors.EXTERNAL_UNAVAILABLE if reason == "error"
                                 else errors.INTERNAL, error, turn_id=turn_id)
        record.builder.turn_done(turn_id, reason)
        await self._publish(record, record.builder.drain())
        record.last_activity = time.monotonic()

    async def _turn_finished(self, record: _Record, reason: str, error: str | None) -> None:
        if record.turn_id is None:
            return
        await self._end_turn(record, reason, error)
        if reason == "error":
            await self._set(record, status="error", error=error or "the turn failed")
        else:
            await self._set(record, status="idle")

    async def _channel_died(self, record: _Record, error: str) -> None:
        """The CLI went away by itself: end its turn, free the session, say why."""
        channel = record.channel
        record.channel = None
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.close()
        message = error or f"{record.adapter.title} stopped"
        if record.turn_id is not None:
            await self._end_turn(record, "error", message)
            await self._set(record, status="error", host="none", error=message)
        else:
            await self._set(record, status="idle", host="none")
        await self._maybe_forget(record)

    async def interrupt(self, cli: str, session_id: str) -> None:
        """``external.interrupt``: stop the running turn (no-op without one).

        ``host: terminal`` answers ``external_view_only``.  ``host: tmux`` sends
        Escape to the pane while Claude is busy, whoever started the turn (the
        person typing in the pane, or snowpea); never ``C-c``.
        """
        record, _found = await self._load(cli, session_id)
        if record.host == "terminal":
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY, "this session runs in another terminal; stop it there"
            )
        if record.host == "tmux" and record.channel is not None and record.channel.alive:
            await record.channel.interrupt()
            return
        if record.turn_id is None or record.channel is None:
            await self._maybe_forget(record)
            return
        if record.turn_cancel is not None:
            record.turn_cancel.set()
        try:
            await record.channel.interrupt()
        except ExternalError:
            raise
        except Exception as exc:  # noqa: BLE001 - a channel that cannot interrupt is stopped
            log.warning("%s interrupt failed; stopping it", cli, exc_info=True)
            await self._close_channel(record)
            await self._set(record, status="idle", host="none")
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, "interrupt failed; stopped it"
            ) from exc

    # -- approvals --------------------------------------------------------
    def _cacheable(self, record: _Record, ask: ApprovalAsk) -> bool:
        """The session's "allow for this session" may answer ``ask`` without anyone.

        Never a guarded or ``never`` request (pushes, deletes outside, secrets,
        sends, snowpea's own files), whatever was allowed before: those always
        go through the queue, where the delegation engine routes them.
        """
        from snowpea_core.jarvis.classify import ClassifyRequest, classify

        try:
            verdict = classify(ClassifyRequest(
                tool=ask.tool,
                args=dict(ask.args or {}),
                workdir=record.project or record.info.cwd,
                session_id=f"ext:{record.cli}:{record.id}",
                home=str(self.home),
                external=True,
            ))
        except Exception:  # noqa: BLE001 - unknown is not cacheable
            return False
        return verdict.classification in ("safe", "risky")

    async def _security(self, record: _Record, ask: ApprovalAsk) -> Any:
        """The security lists' verdict on a bridged request; a block is announced."""
        from snowpea_core.security import enforce

        queue = self.approvals
        core = getattr(queue, "core", None)
        args = dict(ask.args or {})
        cwd = record.real_cwd or record.info.cwd
        if cwd and not any(k in args for k in ("cwd", "workdir", "dir")):
            args["cwd"] = cwd  # the CLI's folder, for relative paths (judgement only)
        if core is not None:
            await enforce.refresh_projects(core)
        projects = enforce.live_projects(core) if core is not None else []
        verdict = enforce.judge(
            settings=getattr(queue, "settings", None), home=self.home,
            workdir=record.project or cwd, tool=ask.tool, args=args, tags=(),
            projects=projects,
        )
        if verdict.blocked:
            await enforce.announce(
                hub=getattr(queue, "hub", None), paths=getattr(queue, "paths", None),
                session_id=f"ext:{record.cli}:{record.id}", tool=ask.tool, args=ask.args,
                verdict=verdict, surface="external", core=core,
            )
        return verdict

    async def _ask_approval(self, record: _Record, ask: ApprovalAsk) -> ApprovalAnswer:
        """Bridge one CLI prompt into the shared approval queue (``ApprovalRequest.external``).

        "For this session" is remembered per exact call (the tool and its full
        command line or arguments), never per program: allowing ``git status``
        does not allow ``git push``, and allowing one ``Write`` does not allow
        every later one.  A guarded or ``never`` request is never answered from
        it (:meth:`_cacheable`).
        """
        # What the CLI asks is shown on every surface: no escape sequences (M7).
        # The arguments stay raw (security review 3, N3): they are what the CLI
        # runs, so they are what is classified and cached; the approval queue
        # writes any control character out for the card (argsEscaped) and the
        # engine judges such a request opaque.
        ask.args = dict(ask.args or {})
        ask.note = clean_text(ask.note) if ask.note else ask.note
        ask.diff = clean_text(ask.diff) if ask.diff else ask.diff
        # The security lists before the session's own cache (1.54.0): a blocked
        # call is denied with the reason, so the CLI's model reads why.
        security = await self._security(record, ask)
        if security.blocked:
            return ApprovalAnswer(allow=False, reason=security.refusal, by="security")
        command = command_of(ask.args or {})
        cache_key = _call_key(ask.tool, ask.args or {}, command)
        if (
            not ask.person_only
            and not ask.terminal
            and not security.opaque
            and cache_key in record.approval_cache
            and (not command or matchable(command))
            and self._cacheable(record, ask)
        ):
            return ApprovalAnswer(allow=True, scope="session", by="cache")
        if self.approvals is None:
            return ApprovalAnswer(allow=False, reason="no approval queue", by="none")
        proxy = _ProxySession(record.cli, record.id, record.real_cwd or record.info.cwd,
                              record.project, record.info.title)
        decision = await self.approvals.request(
            proxy,
            ask.tool,
            dict(ask.args or {}),
            risk=ask.risk,
            unattended=True,
            cancel_event=record.turn_cancel,
            note=ask.note,
            # The answer is the CLI's, not snowpea's: never written into the
            # snowpea allowlist.  "For this session" is kept here instead.
            cacheable=False,
            diff=ask.diff,
            paths=list(ask.paths) if ask.paths else None,
            person_only=ask.person_only,
            external=ExternalRef(cli=record.cli, id=record.id),  # type: ignore[arg-type]
            external_terminal=dict(ask.terminal) if ask.terminal else None,
        )
        if ask.person_only and decision.allowed and decision.person is None:
            # Allowed by anything but a person (a cache, a rule): not this one.
            return ApprovalAnswer(allow=False, reason="only a person may allow this", by="none")
        scope = decision.scope if decision.allowed else "once"
        if (
            decision.allowed
            and scope != "once"
            and not ask.terminal
            and (not command or matchable(command))
            and self._cacheable(record, ask)
        ):
            record.approval_cache.add(cache_key)
        record.last_activity = time.monotonic()
        return ApprovalAnswer(
            allow=decision.allowed, scope=scope, reason=decision.reason, by=decision.by
        )

    # -- create / mode / close ----------------------------------------------
    async def create(
        self, cli: str, project: str, *, model: str | None = None, mode: str | None = None,
        transport: str = "structured", conn: Any = None,
        confirmation: PersonConfirmation | None = None, trust: bool = False,
        native_mode: str | None = None,
    ) -> dict[str, Any]:
        """``external.create``: a new session in a registered project, already open.

        The creating connection is subscribed (``external.event``) as on open.
        ``trust`` as on :meth:`open`.
        """
        adapter, found = await self._usable(cli)
        roots = await self.roots()
        cwd = proc.validate_cwd(project, roots)
        proc.validate_model(model)
        resolved = self.resolve_mode(adapter, mode, native_mode, found)
        chosen, row = resolved or self.default_native(adapter, found)
        if transport == "tmux" and not (adapter.capabilities.tmux and proc.tmux_supported()):
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"{adapter.title} cannot run in a shared tmux terminal on this machine",
            )
        root = proc.project_of(cwd, [str(Path(r).resolve()) for r in roots])
        self._require_trust(root, cwd, conn, trust)
        grant = self._grant_if_unrestricted(cli, chosen, conn, "create", project=root,
                                            confirmation=confirmation, row=row)
        record = _Record(
            adapter=adapter,
            info=SessionInfo(id="", cwd=cwd, updated_at=utc_now()),
            project=root,
            builder=TimelineBuilder(cli, "new", salt=f"live-{uuid.uuid4().hex[:8]}"),
            real_cwd=cwd,
        )
        await self._start_channel(
            record, chosen, grant, found, resume=False, model=model, transport=transport,
            native_mode=row.id,
        )
        channel = record.channel
        assert channel is not None
        session_id = record.info.id or channel.session_id
        if not session_id:
            await self._close_channel(record)
            raise ExternalError(errors.INTERNAL, f"{adapter.title} did not report a session id")
        proc.validate_session_id(session_id)
        record.info.id = session_id
        record.builder.session_id = session_id
        record.info.model = model
        self._records[(cli, session_id)] = record
        self._subscribe(record, conn)
        await self._announce(record, None, "none")
        return {"session": self._row(record)}

    async def set_mode(self, cli: str, session_id: str, mode: str | None = None, *,
                       conn: Any = None, confirmation: PersonConfirmation | None = None,
                       native_mode: str | None = None) -> dict[str, Any]:
        """``external.setMode``: from the next turn; a person-only mode only from a person.

        ``native_mode`` (1.56.0) wins over ``mode``; one of them is required.
        """
        record, found = await self._load(cli, session_id)
        resolved = self.resolve_mode(record.adapter, mode, native_mode, found)
        if resolved is None:
            raise ExternalError(errors.INVALID_PARAMS, "mode or nativeMode is required")
        chosen, row = resolved
        async with record.lock:
            if record.host == "terminal":
                raise ExternalError(
                    errors.EXTERNAL_VIEW_ONLY, "this session runs in another terminal"
                )
            grant = self._grant_if_unrestricted(record, chosen, conn, "setMode",
                                                confirmation=confirmation, row=row)
            if self._bridge_of(record) is not None and confirmation is None and not (
                    proc.is_human_connection(conn) and not getattr(conn, "remote_login", False)):
                # Keys into the person's own terminal: only the person at a local surface.
                raise ExternalError(
                    errors.JARVIS_PERSON_ONLY,
                    "only the person switches the mode of a session in their tmux pane",
                )
            channel = record.channel
            if channel is not None and channel.alive:
                before = channel.native_mode
                channel.native_mode = row.id
                try:
                    await channel.set_mode(chosen, grant)
                except BaseException:
                    channel.native_mode = before
                    raise
            record.mode = chosen
            record.native_mode = row.id
            await self._announce(record)
            return {"session": self._row(record)}

    async def terminal_snapshot(self, cli: str, session_id: str) -> dict[str, Any]:
        """``external.terminalSnapshot``: the bridged pane's text, plain and clipped."""
        record, found = await self._load(cli, session_id)
        async with record.lock:
            bridge = self._bridge_of(record)
            if bridge is None and record.channel is None:
                await self._refresh_host(record)
                if record.host == "terminal" and await self._attach_bridge(record, found):
                    await self._announce(record, None, "terminal")
                bridge = self._bridge_of(record)
            if bridge is None:
                await self._maybe_forget(record)
                raise ExternalError(errors.EXTERNAL_UNAVAILABLE,
                                    "this session does not run in a tmux pane snowpea can read")
            text = await bridge.bridge.snapshot()
        return {"ref": record.ref, "pane": bridge.bridge.pane.pane, "text": text,
                "capturedAt": utc_now()}

    async def _mode_known(self, record: _Record, native_mode: str) -> None:
        """The CLI switched its own mode (plan approved): the row says so."""
        from snowpea_core.external.modes import find

        row = find(record.adapter.table(), native_mode)
        if row is None or row.id == record.native_mode:
            return
        record.native_mode = row.id
        if record.mode != "unrestricted":
            record.mode = row.legacy
        await self._announce(record)

    async def close(self, cli: str, session_id: str, *, conn: Any = None) -> None:
        """``external.close``: stop snowpea's process for it and unsubscribe ``conn``."""
        adapter = self.registry.get(cli)
        if adapter is None:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"{CLI_TITLES.get(cli, cli)} is not available"
            )
        proc.validate_session_id(session_id)
        record = self._records.get((cli, session_id))
        if record is None:
            return
        async with record.lock:
            was = record.host
            if record.channel is not None:
                # The closing view still sees its turn end (turn.done interrupted).
                await self._close_channel(record)
                record.host = "none"
                record.status = "idle"
                await self._announce(record, None, was)
            if conn is not None:
                record.subscribers = [other for other in record.subscribers if other is not conn]
        await self._maybe_forget(record)

    # -- idle sweep -------------------------------------------------------
    async def _sweep_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.sweep_interval)
            with contextlib.suppress(Exception):
                await self.sweep_idle()

    async def sweep_idle(self) -> int:
        """Stop channels with no turn for ``idleSec``; the views stay subscribed."""
        limit = self._idle_sec()
        now = time.monotonic()
        stopped = 0
        for record in list(self._records.values()):
            channel = record.channel
            if channel is None or record.turn_id is not None:
                continue
            if now - record.last_activity < limit:
                continue
            async with record.lock:
                if record.channel is None or record.turn_id is not None:
                    continue
                await self._close_channel(record)
                was = record.host
                record.host = "none"
                await self._announce(record, None, was)
                stopped += 1
            await self._maybe_forget(record)
        return stopped

    # -- introspection (tests, Jarvis) ----------------------------------------
    def opened(self) -> list[dict[str, Any]]:
        return [self._row(record) for record in self._records.values()]


def _line(text: str | None) -> str | None:
    """A title for a row: control and escape sequences removed (security review M7)."""
    if text is None:
        return None
    return clean_text(str(text))[:300]


def _preview(text: str | None) -> str:
    if not text:
        return ""
    lines = clean_text(str(text)).splitlines()
    return lines[0][:300] if lines else ""


def resolved_override(path: str) -> str | None:
    """The real path a ``path`` override resolves to (no execution), or ``None``."""
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            return None
        return str(candidate.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None


def _cursor(key: tuple[str, str, str]) -> str:
    return "|".join(key)


def _parse_cursor(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        raise ExternalError(errors.INVALID_PARAMS, "invalid cursor")
    return parts[0], parts[1], parts[2]


__all__ = ["ExternalManager"]
