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
from snowpea_core.external.proc import (
    ExternalError,
    LedgerHook,
    PersonConfirmation,
    ProcessSupervisor,
    UnrestrictedGrant,
)
from snowpea_core.external.registry import CLI_ORDER, CLI_TITLES, AdapterRegistry
from snowpea_core.external.timeline import TimelineBuilder, TimelineItem
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
    """``(tool, the exact call)``: the whole command line, or every argument."""
    if command:
        return (tool, command.strip())
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
        self.title = title


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
        self.env = proc.scrubbed_env()

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
        if title and title != self._record.info.title:
            self._record.info.title = title
            await self._manager._announce(self._record)


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
        for cli in self.registry.ids():
            adapter = self.registry.get(cli)
            if adapter is not None:
                self._bind(adapter)

    def _bind(self, adapter: ExternalAdapter) -> ExternalAdapter:
        """Hand the daemon's supervisor to ``adapter`` (helpers, shared servers)."""
        if adapter.supervisor is None:
            adapter.supervisor = self.supervisor
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
        """``jarvis.external.clis.<id>.defaultMode`` or the CLI's own default."""
        configured = getattr(self._cli_settings(adapter.id), "defaultMode", None)
        mode = configured or adapter.builtin_default_mode
        if mode not in adapter.capabilities.modes:
            mode = adapter.builtin_default_mode
        return mode

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
        """Clean up after a daemon that died, then start the idle sweep."""
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
        try:
            found = await adapter.discover(override)
        except ExternalError as exc:
            found = Discovery(installed=False, problems=[exc.message])
        except Exception as exc:  # noqa: BLE001 - one broken adapter must not break the list
            log.warning("discovery of %s failed", cli, exc_info=True)
            found = Discovery(installed=False, problems=[f"discovery failed: {type(exc).__name__}"])
        self._discovery[cli] = (now, override, found)
        return found

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
                "models": [],
                "modelsLoaded": False,
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
        out = []
        for root in await self.roots():
            if self._exposed is None or await self._exposed(root):
                out.append(root)
        return out

    # -- rows -------------------------------------------------------------
    def _approvals(self, record: _Record, found: Discovery | None) -> str:
        if record.host == "tmux":
            return "person"
        if record.host != "snowpea":
            return "none"
        return record.adapter.approvals(record.mode or "default", found)

    def _controlled(self, record: _Record, found: Discovery | None) -> bool:
        if record.host not in ("snowpea", "tmux"):
            return True
        return record.adapter.controlled(record.mode or "default", found)

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
            "title": info.title,
            "cwd": info.cwd,
            "project": record.project,
            "updatedAt": info.updated_at,
            "status": record.status,
            "host": record.host,
            "canSend": self._can_send(record),
            "approvals": self._approvals(record, found),
            "tmux": record.channel.tmux if record.channel is not None else None,
            "mode": record.mode if record.host in ("snowpea", "tmux") else None,
            "controlled": self._controlled(record, found),
            "preview": (info.preview or "").splitlines()[0][:300] if info.preview else "",
            "model": info.model,
        }

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
            "controlled": row["controlled"],
            "title": row["title"],
            "error": record.error if record.status == "error" else None,
        }

    async def _announce(
        self, record: _Record, previous: str | None = None, previous_host: str | None = None
    ) -> None:
        """``external.state`` to every authenticated connection."""
        if self.hub is None:
            return
        try:
            await self.hub.notify("external.state", self._state(record, previous, previous_host))
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
                    key = (info.updated_at or "", adapter.id, info.id)
                    if cursor is not None and key >= cursor:
                        continue
                    rows.append((key, info, adapter))
        rows.sort(key=lambda row: row[0], reverse=True)
        page = rows[:limit]
        all_roots = await self.roots()
        out = []
        for _key, info, adapter in page:
            record = self._records.get((adapter.id, info.id))
            if record is not None:
                out.append(self._row(record))
                continue
            out.append(await self._listed_row(adapter, info, all_roots))
        next_before = _cursor(page[-1][0]) if len(rows) > limit and page else None
        return {"sessions": out, "nextBefore": next_before}

    async def _listed_row(
        self, adapter: ExternalAdapter, info: SessionInfo, roots: list[str]
    ) -> dict[str, Any]:
        try:
            live = await adapter.liveness(info, own_pids=self.supervisor.pids())
        except Exception:  # noqa: BLE001 - unknown liveness reads as "not running"
            log.debug("liveness of %s %s failed", adapter.id, info.id, exc_info=True)
            live = None
        host = live.host if live is not None else "none"
        return {
            "ref": _ref(adapter.id, info.id),
            "title": info.title,
            "cwd": info.cwd,
            "project": proc.project_of(info.cwd, roots),
            "updatedAt": info.updated_at,
            "status": "running" if live is not None and live.busy else info.status,
            "host": host,
            "canSend": host == "none",
            "approvals": "none",
            "tmux": live.tmux if live is not None else None,
            "mode": None,
            "controlled": True,
            "preview": (info.preview or "").splitlines()[0][:300] if info.preview else "",
            "model": info.model,
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
        confirmation: PersonConfirmation | None = None,
    ) -> dict[str, Any]:
        """``external.open``: subscribe and return the newest page; continue a free session.

        ``transport="tmux"`` on a Claude session the person runs in a tmux pane
        adopts that pane (after the person allows it, person-only) instead of
        showing it view only.
        """
        record, found = await self._load(cli, session_id)
        if transport not in (None, "structured", "tmux"):
            raise ExternalError(errors.INVALID_PARAMS, f"unknown transport {transport!r}")
        try:
            return await self._open(record, found, limit=limit, mode=mode, conn=conn,
                                    transport=transport, confirmation=confirmation)
        except ExternalError:
            await self._maybe_forget(record)
            raise

    async def _open(
        self, record: _Record, found: Discovery, *, limit: int, mode: str | None, conn: Any,
        transport: str | None = None, confirmation: PersonConfirmation | None = None,
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
                    chosen = proc.validate_mode(mode, record.adapter.capabilities.modes)
                    chosen = chosen or record.mode or self.default_mode(record.adapter)
                    grant = self._grant_if_unrestricted(record, chosen, conn, "open",
                                                        confirmation=confirmation)
                    try:
                        await self._start_channel(
                            record, chosen, grant, found, resume=True,
                            transport="tmux" if adopt or transport == "tmux" else "structured",
                        )
                    except ExternalError as exc:
                        if exc.code in (errors.JARVIS_PERSON_ONLY, errors.INVALID_PARAMS):
                            raise
                        if adopt:
                            await self._refresh_host(record)
                        # View it anyway; the next send tries again.
                        record.error = exc.message
                        log.info("could not continue %s %s: %s", cli, session_id, exc.message)
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

    # -- channels ---------------------------------------------------------
    def _grant_if_unrestricted(
        self, record_or_cli: _Record | str, mode: str, conn: Any, action: str,
        project: str | None = None, confirmation: PersonConfirmation | None = None,
    ) -> UnrestrictedGrant | None:
        """The person's grant for ``unrestricted`` (a ledger entry), or ``jarvis_person_only``.

        ``confirmation`` comes from :func:`proc.person_confirmation_from_gateway`
        (a messenger's bound person); otherwise ``conn`` must be a person.
        """
        if mode != "unrestricted":
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
        )

    async def _start_channel(
        self, record: _Record, mode: str, grant: UnrestrictedGrant | None, found: Discovery,
        *, resume: bool, model: str | None = None, transport: str = "structured",
    ) -> None:
        assert found.executable is not None
        spec = LaunchSpec(
            cwd=record.info.cwd,
            project=record.project,
            executable=found.executable,
            mode=mode,
            session_id=record.info.id or None,
            resume=resume,
            model=model,
            grant=grant,
            transport=transport,  # type: ignore[arg-type]
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
        record.mode = mode
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
    ) -> dict[str, str]:
        """``external.send``: start a turn; the reply streams as ``external.event``.

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
            return await self._send(record, found, text, conn=conn, origin=origin)
        except ExternalError:
            await self._maybe_forget(record)
            raise

    async def _send(
        self, record: _Record, found: Discovery, text: str, *, conn: Any,
        origin: dict[str, Any] | None,
    ) -> dict[str, str]:
        cli = record.cli
        async with record.lock:
            if record.channel is None or not record.channel.alive:
                record.channel = None
                await self._refresh_host(record)
                if record.host == "terminal":
                    raise ExternalError(
                        errors.EXTERNAL_VIEW_ONLY,
                        f"this {record.adapter.title} session is open in another terminal; "
                        "it can be continued here once that one exits",
                    )
                previous_host = record.host
                mode = record.mode or self.default_mode(record.adapter)
                grant = None
                if mode == "unrestricted":
                    # A takeover in unrestricted mode is a new choice: the person makes it.
                    grant = self._grant_if_unrestricted(record, mode, conn, "open")
                try:
                    await self._start_channel(record, mode, grant, found, resume=True)
                except ExternalError:
                    if record.host != previous_host:
                        await self._announce(record, None, previous_host)
                    raise
                await self._announce(record, None, previous_host)
            channel = record.channel
            assert channel is not None
            if record.turn_id is not None:
                if record.adapter.capabilities.steer and channel.can_steer:
                    return await self._steer(record, channel, text, conn=conn, origin=origin)
                raise ExternalError(
                    errors.SESSION_BUSY, "a turn is running in this session; interrupt it first"
                )
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
            ))
        except Exception:  # noqa: BLE001 - unknown is not cacheable
            return False
        return verdict.classification in ("safe", "risky")

    async def _ask_approval(self, record: _Record, ask: ApprovalAsk) -> ApprovalAnswer:
        """Bridge one CLI prompt into the shared approval queue (``ApprovalRequest.external``).

        "For this session" is remembered per exact call (the tool and its full
        command line or arguments), never per program: allowing ``git status``
        does not allow ``git push``, and allowing one ``Write`` does not allow
        every later one.  A guarded or ``never`` request is never answered from
        it (:meth:`_cacheable`).
        """
        command = command_of(ask.args or {})
        cache_key = _call_key(ask.tool, ask.args or {}, command)
        if (
            not ask.person_only
            and cache_key in record.approval_cache
            and (not command or matchable(command))
            and self._cacheable(record, ask)
        ):
            return ApprovalAnswer(allow=True, scope="session", by="cache")
        if self.approvals is None:
            return ApprovalAnswer(allow=False, reason="no approval queue", by="none")
        proxy = _ProxySession(record.cli, record.id, record.info.cwd, record.project,
                              record.info.title)
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
        )
        if ask.person_only and decision.allowed and decision.person is None:
            # Allowed by anything but a person (a cache, a rule): not this one.
            return ApprovalAnswer(allow=False, reason="only a person may allow this", by="none")
        scope = decision.scope if decision.allowed else "once"
        if (
            decision.allowed
            and scope != "once"
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
        confirmation: PersonConfirmation | None = None,
    ) -> dict[str, Any]:
        """``external.create``: a new session in a registered project, already open.

        The creating connection is subscribed (``external.event``) as on open.
        """
        adapter, found = await self._usable(cli)
        roots = await self.roots()
        cwd = proc.validate_cwd(project, roots)
        proc.validate_model(model)
        chosen = proc.validate_mode(mode, adapter.capabilities.modes) or self.default_mode(adapter)
        if transport == "tmux" and not (adapter.capabilities.tmux and proc.tmux_supported()):
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"{adapter.title} cannot run in a shared tmux terminal on this machine",
            )
        root = proc.project_of(cwd, [str(Path(r).resolve()) for r in roots])
        grant = self._grant_if_unrestricted(cli, chosen, conn, "create", project=root,
                                            confirmation=confirmation)
        record = _Record(
            adapter=adapter,
            info=SessionInfo(id="", cwd=cwd, updated_at=utc_now()),
            project=root,
            builder=TimelineBuilder(cli, "new", salt=f"live-{uuid.uuid4().hex[:8]}"),
        )
        await self._start_channel(
            record, chosen, grant, found, resume=False, model=model, transport=transport
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

    async def set_mode(self, cli: str, session_id: str, mode: str, *, conn: Any = None,
                       confirmation: PersonConfirmation | None = None) -> dict[str, Any]:
        """``external.setMode``: from the next turn; ``unrestricted`` only from a person."""
        record, _found = await self._load(cli, session_id)
        chosen = proc.validate_mode(mode, record.adapter.capabilities.modes)
        assert chosen is not None
        async with record.lock:
            if record.host == "terminal":
                raise ExternalError(
                    errors.EXTERNAL_VIEW_ONLY, "this session runs in another terminal"
                )
            grant = self._grant_if_unrestricted(record, chosen, conn, "setMode",
                                                confirmation=confirmation)
            if record.channel is not None and record.channel.alive:
                await record.channel.set_mode(chosen, grant)
            record.mode = chosen
            await self._announce(record)
            return {"session": self._row(record)}

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
