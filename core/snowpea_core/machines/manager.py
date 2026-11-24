"""Connection pool and RPC proxy for remote machines (M40)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core import __version__
from snowpea_core.cli import remote
from snowpea_core.cli.daemon_client import DaemonClient, DaemonInfo
from snowpea_core.cli.remote import RemoteSession, parse_target
from snowpea_core.cli.remote_up import HostKeys, ssh_runner
from snowpea_core.machines.proxy import RELAY_TERMINAL_EVENTS
from snowpea_core.machines.registry import MachineRegistry
from snowpea_core.server.protocol import PROTOCOL_VERSION, MachineAsk, MachineRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection

log = logging.getLogger("snowpea.machines")

# Test hook: bypass ssh and connect straight to loopback (port, token).
DirectAdvert = Callable[[str], DaemonInfo | None]

HEALTH_EVERY_SEC = 5.0
HEALTH_FAILURES = 3
BACKOFF_CAP_SEC = 15.0


@dataclass
class PendingAsk:
    kind: str
    fields: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    answer: list[bool] = field(default_factory=list)


@dataclass
class LiveConnection:
    client: DaemonClient | None = None
    session: RemoteSession | None = None
    port: int = 0
    token: str = ""
    status: str = "off"
    error: str | None = None
    ask: PendingAsk | None = None
    relay_task: asyncio.Task[None] | None = None
    stop_supervisor: threading.Event | None = None
    supervisor: threading.Thread | None = None


class _Asks:
    def __init__(
        self,
        machine_id: str,
        pending: dict[str, PendingAsk],
        live: LiveConnection,
        announce: Callable[[], None],
    ) -> None:
        self._machine_id = machine_id
        self._pending = pending
        self._live = live
        self._announce = announce

    def ask(self, kind: str, **fields: Any) -> bool:
        ident = uuid.uuid4().hex
        box = PendingAsk(kind=kind, fields=fields)
        self._pending[ident] = box
        self._live.ask = box
        self._live.status = "asking"
        self._announce()
        try:
            box.event.wait()
            return box.answer[-1] if box.answer else False
        finally:
            self._live.ask = None
            self._pending.pop(ident, None)
            if self._live.status == "asking":
                self._live.status = "connecting"
            self._announce()


class MachineManager:
    def __init__(
        self,
        core: Core,
        *,
        direct: DirectAdvert | None = None,
        ssh: str | None = None,
    ) -> None:
        self.core = core
        self.registry = MachineRegistry(core.paths.home)
        self.registry.load()
        self._live: dict[str, LiveConnection] = {}
        self._pending_asks: dict[str, dict[str, PendingAsk]] = defaultdict(dict)
        self._direct = direct
        self._ssh = ssh
        self._viewers: dict[tuple[str, str], set[RpcConnection]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _announce_sync(self) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.announce(), self._loop)

    def rows(self) -> list[MachineRow]:
        out: list[MachineRow] = []
        for rec in self.registry.records.values():
            live = self._live.get(rec.id)
            status = live.status if live else "off"
            err = live.error if live else None
            ask = None
            if live and live.ask:
                ident = next(
                    (k for k, v in self._pending_asks.get(rec.id, {}).items() if v is live.ask),
                    "",
                )
                if ident:
                    ask = MachineAsk(
                        id=ident,
                        kind=live.ask.kind,  # type: ignore[arg-type]
                        **{k: v for k, v in live.ask.fields.items() if k != "id"},
                    )
            out.append(
                MachineRow.model_validate(
                    rec.to_row(status=status, error=err, ask=ask),
                )
            )
        return out

    async def announce(self) -> None:
        await self.core.hub.notify(
            "machine.changed", {"machines": [r.model_dump() for r in self.rows()]}
        )

    def current_ask(self, machine_id: str) -> tuple[str, PendingAsk] | None:
        pending = self._pending_asks.get(machine_id) or {}
        for ident, box in pending.items():
            if not box.event.is_set():
                return ident, box
        return None

    def answer(self, machine_id: str, ask_id: str, accepted: bool) -> bool:
        box = self._pending_asks.get(machine_id, {}).get(ask_id)
        if box is None or box.event.is_set():
            return False
        box.answer.append(accepted)
        box.event.set()
        self._announce_sync()
        return True

    def _open_session(self, machine_id: str, live: LiveConnection, rec) -> None:
        target = parse_target(
            f"{rec.user}@{rec.host}" + (f":{rec.port}" if rec.port else "")
        )
        known = str(self.registry.known_hosts_file())
        live.status = "connecting"
        asks = _Asks(machine_id, self._pending_asks[machine_id], live, self._announce_sync)
        ssh_bin = self._ssh or remote.find_ssh_tool("ssh") or "ssh"
        session = RemoteSession(
            target,
            local_protocol=PROTOCOL_VERSION,
            confirm=lambda q: asks.ask("install", question=q, version=__version__),
            batch=True,
            ssh=ssh_bin,
            run=ssh_runner(None),
        )
        host = HostKeys(session._ssh, asks, known_hosts=known)  # noqa: SLF001
        options = host.ensure(target)
        identity = self.registry.ssh_identity(rec)
        session._options = [  # noqa: SLF001
            *options,
            f"UserKnownHostsFile={known}",
            *identity,
        ]
        advert = session.prepare()
        live.port = session.open_tunnel(watch=False)
        live.token = advert.token
        live.session = session

    def _connect_worker(self, machine_id: str) -> None:
        rec = self.registry.get(machine_id)
        if rec is None:
            return
        live = self._live.setdefault(machine_id, LiveConnection())
        live.status = "connecting"
        live.error = None
        live.ask = None
        self._announce_sync()
        try:
            if self._direct is not None:
                info = self._direct(machine_id)
                if info is None:
                    raise RuntimeError("direct advert missing")
                live.port, live.token = info.port, info.token
            else:
                self._open_session(machine_id, live, rec)
            live.status = "connected"
            self._start_supervisor(machine_id, live)
        except Exception as exc:  # noqa: BLE001
            live.status = "error"
            live.error = str(exc)
            log.exception("machine %s connect failed", machine_id)
        finally:
            self._announce_sync()

    def _start_supervisor(self, machine_id: str, live: LiveConnection) -> None:
        if live.supervisor is not None and live.stop_supervisor:
            live.stop_supervisor.set()
        stop = threading.Event()
        live.stop_supervisor = stop

        def run() -> None:
            failures = 0
            delay = 1.0
            while not stop.wait(HEALTH_EVERY_SEC):
                if live.status != "connected" or self._direct is not None:
                    failures = 0
                    continue
                if live.port and remote.daemon_healthy(live.port):
                    failures = 0
                    delay = 1.0
                    continue
                failures += 1
                if failures < HEALTH_FAILURES:
                    continue
                failures = 0
                live.status = "connecting"
                self._announce_sync()
                if live.client is not None and self._loop:
                    client_to_close = live.client
                    live.client = None

                    async def _close(client: DaemonClient = client_to_close) -> None:
                        await client.close()

                    asyncio.run_coroutine_threadsafe(_close(), self._loop)
                if live.relay_task is not None:
                    live.relay_task.cancel()
                    live.relay_task = None
                if live.session is not None:
                    with contextlib.suppress(Exception):
                        live.session.close()
                    live.session = None
                rec = self.registry.get(machine_id)
                if rec is None:
                    return
                while not stop.is_set():
                    try:
                        self._open_session(machine_id, live, rec)
                        live.status = "connected"
                        self._announce_sync()
                        break
                    except Exception as exc:  # noqa: BLE001
                        live.error = str(exc)
                        live.status = "error"
                        self._announce_sync()
                        if stop.wait(delay):
                            return
                        delay = min(delay * 2, BACKOFF_CAP_SEC)
            return

        name = f"machine-{machine_id[:8]}"
        live.supervisor = threading.Thread(target=run, name=name, daemon=True)
        live.supervisor.start()

    def connect(self, machine_id: str) -> None:
        live = self._live.setdefault(machine_id, LiveConnection())
        if live.status in ("connected", "connecting", "asking"):
            return
        threading.Thread(target=self._connect_worker, args=(machine_id,), daemon=True).start()

    def disconnect(self, machine_id: str) -> None:
        live = self._live.get(machine_id)
        if live is None:
            return
        if live.stop_supervisor is not None:
            live.stop_supervisor.set()
        if live.relay_task is not None:
            live.relay_task.cancel()
        if live.session is not None:
            live.session.close()
        if live.client is not None and self._loop:
            asyncio.run_coroutine_threadsafe(live.client.close(), self._loop)
        live.client = None
        live.session = None
        live.port = 0
        live.token = ""
        live.status = "off"
        live.error = None
        live.ask = None

    async def _ensure_client(self, machine_id: str) -> DaemonClient:
        live = self._live.get(machine_id)
        if live is None or live.status != "connected":
            raise RuntimeError("machine is not connected")
        if live.client is None:
            info = DaemonInfo(port=live.port, pid=0, token=live.token)
            live.client = DaemonClient(info, client_version=f"snowpea-proxy {__version__}")
            await live.client.connect()
            relay = self._relay_notifications(machine_id, live.client)
            live.relay_task = asyncio.create_task(relay)
        return live.client

    async def _relay_notifications(self, machine_id: str, client: DaemonClient) -> None:
        async for frame in client.notifications():
            method = str(frame.get("method", ""))
            if method not in RELAY_TERMINAL_EVENTS:
                continue
            params = dict(frame.get("params") or {})
            params["machine"] = machine_id
            term_id = str(params.get("terminalId", ""))
            targets = list(self._viewers.get((machine_id, term_id), set()))
            for conn in targets:
                if not getattr(conn, "closed", False):
                    await conn.notify(method, params)

    async def proxy_call(
        self, machine_id: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        client = await self._ensure_client(machine_id)
        clean = {k: v for k, v in params.items() if k != "machine"}
        return await client.call(method, clean)

    def register_viewer(self, machine_id: str, terminal_id: str, conn: RpcConnection) -> None:
        self._viewers[(machine_id, terminal_id)].add(conn)

    def unregister_viewer(self, machine_id: str, terminal_id: str, conn: RpcConnection) -> None:
        self._viewers.get((machine_id, terminal_id), set()).discard(conn)

