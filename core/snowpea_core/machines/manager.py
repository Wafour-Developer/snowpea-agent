"""Connection pool and RPC proxy for remote machines (M40)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core import __version__
from snowpea_core.cli import remote
from snowpea_core.cli.daemon_client import DaemonClient, DaemonInfo
from snowpea_core.cli.remote import RemoteSession
from snowpea_core.machines.proxy import (
    RELAY_PERSON_EVENTS,
    RELAY_SESSION_EVENTS,
    RELAY_TERMINAL_EVENTS,
)
from snowpea_core.machines.registry import MachineRegistry
from snowpea_core.machines.ssh import HostKeys, ssh_runner
from snowpea_core.machines.ssh_askpass import (
    SshAskpassBridge,
    classify_prompt_kind,
    sanitize_ssh_prompt,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
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
ASK_TIMEOUT_SEC = 240.0
NOTIFY_QUEUE_MAX = 256
_PROXY_RPC_DETAIL_KEYS = frozenset(
    {
        "reason",
        "project",
        "method",
        "id",
        "sessionId",
        "terminalId",
        "max",
        "maxMode",
        "kind",
        "field",
        "machine",
    }
)


@dataclass
class PendingAsk:
    kind: str
    fields: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    answer: list[bool] = field(default_factory=list)
    secret: list[str] = field(default_factory=list, repr=False)


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
            if not box.event.wait(ASK_TIMEOUT_SEC):
                log.warning("machine %s ask %s timed out", self._machine_id, kind)
                return False
            return box.answer[-1] if box.answer else False
        finally:
            if self._live.ask is box:
                self._live.ask = None
            self._pending.pop(ident, None)
            if self._live.status == "asking":
                self._live.status = "connecting"
            self._announce()

    def ask_secret(self, kind: str, **fields: Any) -> str | None:
        ident = uuid.uuid4().hex
        box = PendingAsk(kind=kind, fields=fields)
        self._pending[ident] = box
        self._live.ask = box
        self._live.status = "asking"
        self._announce()
        try:
            if not box.event.wait(ASK_TIMEOUT_SEC):
                log.warning("machine %s ask %s timed out", self._machine_id, kind)
                return None
            if not box.answer or not box.answer[-1]:
                return None
            value = box.secret[-1] if box.secret else None
            box.secret.clear()
            return value
        finally:
            if self._live.ask is box:
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
        self._session_viewers: dict[tuple[str, str], set[RpcConnection]] = defaultdict(set)
        self._conn_clients: dict[tuple[str, str], DaemonClient] = {}
        self._jarvis_clients: dict[str, DaemonClient] = {}
        self._jarvis_relays: dict[str, asyncio.Task[None]] = {}
        self._jarvis_cache: Any = None
        self._jarvis_remote_attached = False
        self._conn_relays: dict[tuple[str, str], asyncio.Task[None]] = {}
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
        from snowpea_core.server.person_gate import connection_kind, is_person

        all_rows = [r.model_dump() for r in self.rows()]
        payload = {"machines": all_rows}
        redacted = {
            "machines": [
                {
                    **row,
                    "ask": None,
                    "keyPath": None,
                    "error": None,
                    "pastedKey": False,
                }
                for row in all_rows
            ]
        }
        hub = self.core.hub
        clients = list(getattr(hub, "_clients", []))
        for conn in clients:
            if getattr(conn, "closed", False):
                continue
            kind = connection_kind(conn)
            if kind == "person" and is_person(conn, local=True):
                await conn.notify("machine.changed", payload)
            elif kind == "remote_login" and self.core.settings.web.remote.machines:
                await conn.notify("machine.changed", redacted)

    def current_ask(self, machine_id: str) -> tuple[str, PendingAsk] | None:
        pending = self._pending_asks.get(machine_id) or {}
        for ident, box in pending.items():
            if not box.event.is_set():
                return ident, box
        return None

    def answer(
        self, machine_id: str, ask_id: str, accepted: bool, *, secret: str | None = None
    ) -> bool:
        box = self._pending_asks.get(machine_id, {}).get(ask_id)
        if box is None or box.event.is_set():
            return False
        if accepted and box.kind in ("password", "passphrase"):
            if not secret:
                return False
            box.secret.append(secret)
        box.answer.append(accepted)
        box.event.set()
        self._announce_sync()
        return True

    def _password_prompt(
        self, machine_id: str, live: LiveConnection, rec: Any, prompt: str
    ) -> str | None:
        clean = sanitize_ssh_prompt(prompt)
        kind = classify_prompt_kind(prompt)
        target = rec.target()
        asks = _Asks(machine_id, self._pending_asks[machine_id], live, self._announce_sync)
        return asks.ask_secret(
            kind,
            question=clean,
            host=target.label,
            user=target.user or rec.user,
        )

    def _open_session(
        self, machine_id: str, live: LiveConnection, rec, *, interactive: bool
    ) -> None:
        target = rec.target()
        known = str(self.registry.known_hosts_file())
        live.status = "connecting"
        asks = _Asks(machine_id, self._pending_asks[machine_id], live, self._announce_sync)
        ssh_bin = self._ssh or remote.find_ssh_tool("ssh") or "ssh"
        bridge: SshAskpassBridge | None = None
        try:
            extra_env = None
            on_spawn = None
            if interactive and sys.platform != "win32":
                bridge_holder: list[SshAskpassBridge | None] = [None]

                def _ask(prompt: str, mid: str = machine_id) -> str | None:
                    secret = self._password_prompt(mid, live, rec, prompt)
                    br = bridge_holder[0]
                    if secret is None and br is not None:
                        br.note_declined()
                    return secret

                bridge = SshAskpassBridge(
                    lambda prompt, mid=machine_id: _ask(prompt, mid),
                    home=self.core.paths.home,
                    on_stop=lambda mid=machine_id: self._cancel_asks(mid),
                )
                bridge_holder[0] = bridge
                bridge.start()
                extra_env = bridge.env
                on_spawn = bridge.set_ssh_pid
            session = RemoteSession(
                target,
                local_protocol=PROTOCOL_VERSION,
                confirm=lambda q: asks.ask("install", question=q, version=__version__),
                batch=not interactive,
                ssh=ssh_bin,
                run=ssh_runner(None, extra_env, on_spawn=on_spawn),
            )
            host = HostKeys(session._ssh, asks, known_hosts=known)  # noqa: SLF001
            options = host.ensure(target)
            identity = self.registry.ssh_identity(rec)
            extra_opts = (
                ["NumberOfPasswordPrompts=1"] if interactive and sys.platform != "win32" else []
            )
            session._options = [  # noqa: SLF001
                *options,
                f"UserKnownHostsFile={known}",
                *identity,
                *extra_opts,
            ]
            session._ssh_extra = list(self.registry.ssh_extra_args(rec))  # noqa: SLF001
            advert = session.prepare()
            live.port = session.open_tunnel(watch=False)
            live.token = advert.token
            live.session = session
        except remote.RemoteError as exc:
            if not interactive and exc.code == "auth":
                raise remote.RemoteError(
                    "needs a password: connect manually", code="auth", reason="password"
                ) from exc
            raise
        finally:
            if bridge is not None:
                bridge.stop()

    def _connect_worker(self, machine_id: str, *, interactive: bool = False) -> None:
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
                self._open_session(machine_id, live, rec, interactive=interactive)
            live.status = "connected"
            self._start_supervisor(machine_id, live)
            self._schedule_jarvis_relay(machine_id)
        except remote.RemoteError as exc:
            live.status = "error"
            live.error = str(exc)
            if exc.code == "auth" and interactive:
                log.warning("machine %s authentication failed", machine_id)
            else:
                log.exception("machine %s connect failed", machine_id)
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
                if self._loop is not None:
                    for key in [k for k in list(self._conn_clients) if k[0] == machine_id]:

                        async def _drop(conn_key: tuple[str, str] = key) -> None:
                            await self._close_conn_client(conn_key)

                        asyncio.run_coroutine_threadsafe(_drop(), self._loop)
                live.client = None
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
                        self._open_session(machine_id, live, rec, interactive=False)
                        live.status = "connected"
                        self._announce_sync()
                        self._schedule_jarvis_relay(machine_id)
                        break
                    except remote.RemoteError as exc:
                        live.error = str(exc)
                        live.status = "error"
                        self._announce_sync()
                        if exc.code == "auth":
                            return
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

    def connect(self, machine_id: str, *, interactive: bool = False) -> None:
        live = self._live.setdefault(machine_id, LiveConnection())
        if live.status in ("connected", "connecting", "asking"):
            return
        threading.Thread(
            target=self._connect_worker,
            args=(machine_id,),
            kwargs={"interactive": interactive},
            daemon=True,
        ).start()

    def _cancel_asks(self, machine_id: str) -> None:
        pending = self._pending_asks.get(machine_id) or {}
        for box in list(pending.values()):
            if not box.event.is_set():
                box.answer.append(False)
                box.event.set()

    def _conn_key(self, machine_id: str, conn: RpcConnection) -> tuple[str, str]:
        return machine_id, str(getattr(conn, "surface_id", id(conn)))

    async def _close_conn_client(self, key: tuple[str, str]) -> None:
        relay = self._conn_relays.pop(key, None)
        if relay is not None:
            relay.cancel()
        client = self._conn_clients.pop(key, None)
        if client is not None:
            await client.close()

    def close_connection(self, conn: RpcConnection) -> None:
        """Drop per-connection proxy clients and terminal viewers (L9)."""
        surface = str(getattr(conn, "surface_id", ""))
        for key in list(self._conn_clients):
            if key[1] == surface:
                self._conn_clients.pop(key, None)
                self._conn_relays.pop(key, None)
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(self._close_conn_client(key), self._loop)
        for term_key, viewers in list(self._viewers.items()):
            if conn in viewers:
                viewers.discard(conn)
                if not viewers:
                    self._viewers.pop(term_key, None)
        for sess_key, viewers in list(self._session_viewers.items()):
            if conn in viewers:
                viewers.discard(conn)
                if not viewers:
                    self._session_viewers.pop(sess_key, None)

    def disconnect(self, machine_id: str) -> None:
        live = self._live.get(machine_id)
        if live is None:
            return
        self._cancel_asks(machine_id)
        if live.stop_supervisor is not None:
            live.stop_supervisor.set()
        if live.relay_task is not None:
            live.relay_task.cancel()
        if live.session is not None:
            live.session.close()
        for key in [k for k in self._conn_clients if k[0] == machine_id]:
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._close_conn_client(key), self._loop)
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.close_jarvis_client(machine_id), self._loop)
        for key in [k for k in list(self._session_viewers) if k[0] == machine_id]:
            self._session_viewers.pop(key, None)
        if live.client is not None and self._loop:
            asyncio.run_coroutine_threadsafe(live.client.close(), self._loop)
        live.client = None
        live.session = None
        live.port = 0
        live.token = ""
        live.status = "off"
        live.error = None
        live.ask = None

    @staticmethod
    def _relay_request_id(machine_id: str, request_id: str) -> str:
        return f"m40:{machine_id}:{request_id}"

    @staticmethod
    def _approval_timeout(params: dict[str, Any]) -> float | None:
        raw = params.get("timeoutSec")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _remote_login_blocked_allow(request: dict[str, Any]) -> bool:
        """True when a remote web login must not relay ``allow`` to the remote."""
        if request.get("personOnly") is True:
            return True
        if request.get("blocked"):
            return True
        if str(request.get("tool", "")) == "delegation_set":
            args = request.get("args")
            if isinstance(args, dict) and args.get("allow"):
                return True
        return False

    def _clamp_approval_reply(
        self,
        conn: RpcConnection,
        request: dict[str, Any],
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        from snowpea_core.permissions.approval_queue import clamp_scope
        from snowpea_core.server.person_gate import connection_kind

        decision = raw.get("decision")
        if decision not in ("allow", "deny"):
            return {
                "decision": "deny",
                "scope": "once",
                "reason": "invalid approval reply from the local person",
            }
        if (
            decision == "allow"
            and connection_kind(conn) == "remote_login"
            and self._remote_login_blocked_allow(request)
        ):
            return {
                "decision": "deny",
                "scope": "once",
                "reason": "only a local person may allow this on the remote",
            }
        scope = str(raw.get("scope", "once"))
        scope = clamp_scope(conn, scope)
        out: dict[str, Any] = {"decision": decision, "scope": scope}
        reason = raw.get("reason")
        if isinstance(reason, str) and reason.strip():
            out["reason"] = reason.strip()
        return out

    def _sanitize_question_reply(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("declined") is True:
            out: dict[str, Any] = {"answers": [], "declined": True}
            reason = raw.get("reason")
            if isinstance(reason, str) and reason.strip():
                out["reason"] = reason.strip()
            return out
        answers = raw.get("answers")
        if not isinstance(answers, list):
            return {"answers": [], "declined": True, "reason": "invalid question reply"}
        return {"answers": answers}

    async def _relay_to_person(
        self,
        conn: RpcConnection,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        return await conn.call(method, params, timeout=timeout)

    def _approval_handler(self, conn: RpcConnection, machine_id: str):
        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            rid = str(params.get("requestId", ""))
            tagged = {
                **params,
                "machine": machine_id,
                "requestId": self._relay_request_id(machine_id, rid),
            }
            timeout = self._approval_timeout(params)
            try:
                result = await self._relay_to_person(
                    conn, "approval.request", tagged, timeout=timeout
                )
                return self._clamp_approval_reply(conn, params, result)
            except Exception as exc:
                log.warning("machine %s approval relay failed: %s", machine_id, exc)
                return {
                    "decision": "deny",
                    "scope": "once",
                    "reason": f"could not reach the local person: {exc}",
                }

        return handler

    def _question_handler(self, conn: RpcConnection, machine_id: str):
        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            rid = str(params.get("requestId", ""))
            tagged = {
                **params,
                "machine": machine_id,
                "requestId": self._relay_request_id(machine_id, rid),
            }
            timeout = self._approval_timeout(params)
            try:
                result = await self._relay_to_person(
                    conn, "question.request", tagged, timeout=timeout
                )
                return self._sanitize_question_reply(result)
            except Exception as exc:
                log.warning("machine %s question relay failed: %s", machine_id, exc)
                return {"answers": [], "declined": True, "reason": str(exc)}

        return handler

    async def _ensure_client(self, machine_id: str, conn: RpcConnection) -> DaemonClient:
        live = self._live.get(machine_id)
        if live is None or live.status != "connected":
            raise RuntimeError("machine is not connected")
        key = self._conn_key(machine_id, conn)
        client = self._conn_clients.get(key)
        if client is None:
            info = DaemonInfo(port=live.port, pid=0, token=live.token)
            drop_handler = self._notification_drop_handler(machine_id, conn)
            client = DaemonClient(
                info,
                client_version=f"snowpea-proxy {__version__}",
                notification_queue_max=NOTIFY_QUEUE_MAX,
                approval_handler=self._approval_handler(conn, machine_id),
                question_handler=self._question_handler(conn, machine_id),
                notification_drop_handler=drop_handler,
            )
            await client.connect()
            self._conn_clients[key] = client
            relay = self._relay_notifications(machine_id, client, conn)
            self._conn_relays[key] = asyncio.create_task(relay)
        return client

    def _notification_drop_handler(
        self, machine_id: str, owner: RpcConnection
    ) -> Callable[[dict[str, Any]], None]:
        def handler(frame: dict[str, Any]) -> None:
            if self._loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._repaint_after_dropped_terminal(owner, machine_id, frame),
                self._loop,
            )

        return handler

    async def _repaint_after_dropped_terminal(
        self, owner: RpcConnection, machine_id: str, frame: dict[str, Any]
    ) -> None:
        if str(frame.get("method", "")) != "terminal.data":
            return
        if getattr(owner, "closed", False):
            return
        params = frame.get("params") or {}
        term_id = str(params.get("terminalId", ""))
        if not term_id:
            return
        viewers = self._viewers.get((machine_id, term_id), set())
        if owner not in viewers:
            return
        await owner.notify(
            "terminal.data",
            {
                "terminalId": term_id,
                "machine": machine_id,
                "repaint": True,
                "seq": 0,
            },
        )

    async def _relay_notifications(
        self, machine_id: str, client: DaemonClient, owner: RpcConnection
    ) -> None:
        from snowpea_core.server.person_gate import connection_kind

        async for frame in client.notifications():
            method = str(frame.get("method", ""))
            if (
                connection_kind(owner) == "remote_login"
                and not self.core.settings.web.remote.machines
            ):
                continue
            if method in RELAY_PERSON_EVENTS:
                params = dict(frame.get("params") or {})
                rid = str(params.get("requestId", ""))
                if rid:
                    params["requestId"] = self._relay_request_id(machine_id, rid)
                params["machine"] = machine_id
                if getattr(owner, "closed", False):
                    continue
                await owner.notify(method, params)
                continue
            if method in RELAY_SESSION_EVENTS:
                params = dict(frame.get("params") or {})
                params["machine"] = machine_id
                session_id = str(params.get("sessionId", ""))
                if not session_id:
                    continue
                viewers = self._session_viewers.get((machine_id, session_id), set())
                if owner not in viewers or getattr(owner, "closed", False):
                    continue
                await owner.notify(method, params)
                continue
            if method not in RELAY_TERMINAL_EVENTS:
                continue
            params = dict(frame.get("params") or {})
            params["machine"] = machine_id
            term_id = str(params.get("terminalId", ""))
            if not term_id:
                continue
            viewers = self._viewers.get((machine_id, term_id), set())
            if owner not in viewers or getattr(owner, "closed", False):
                continue
            await owner.notify(method, params)

    @staticmethod
    def _map_rpc_error(exc: Exception) -> RpcError:
        from snowpea_core.cli.daemon_client import RpcCallError

        if isinstance(exc, RpcCallError):
            details = exc.details if isinstance(exc.details, dict) else {}
            clean = {k: v for k, v in details.items() if k in _PROXY_RPC_DETAIL_KEYS}
            return RpcError(str(exc.code), str(exc.message), clean)
        raise exc

    async def proxy_call(
        self, machine_id: str, method: str, params: dict[str, Any], conn: RpcConnection
    ) -> dict[str, Any]:
        client = await self._ensure_client(machine_id, conn)
        clean = {k: v for k, v in params.items() if k != "machine"}
        try:
            return await client.call(method, clean)
        except Exception as exc:
            raise self._map_rpc_error(exc) from exc

    def register_viewer(self, machine_id: str, terminal_id: str, conn: RpcConnection) -> None:
        self._viewers[(machine_id, terminal_id)].add(conn)

    def unregister_viewer(self, machine_id: str, terminal_id: str, conn: RpcConnection) -> None:
        self._viewers.get((machine_id, terminal_id), set()).discard(conn)

    def register_session_viewer(
        self, machine_id: str, session_id: str, conn: RpcConnection
    ) -> None:
        self._session_viewers[(machine_id, session_id)].add(conn)

    def unregister_session_viewer(
        self, machine_id: str, session_id: str, conn: RpcConnection
    ) -> None:
        self._session_viewers.get((machine_id, session_id), set()).discard(conn)

    def _schedule_jarvis_relay(self, machine_id: str) -> None:
        jarvis = getattr(self.core, "jarvis", None)
        ensure = getattr(self, "ensure_jarvis_relay", None)
        if jarvis is None or not jarvis.running or ensure is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(ensure(machine_id), self._loop)

    async def jarvis_proxy_call(
        self, machine_id: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Forward one RPC to a remote machine for Jarvis (1.77.0, jarvisRemote)."""
        from snowpea_core.machines.gates import gate_jarvis_machine_call, run_jarvis_proxy_gates
        from snowpea_core.server.protocol import METHODS

        spec = METHODS.get(method)
        if spec is None:
            raise RpcError(errors.METHOD_NOT_FOUND_CODE, f"unknown method: {method}")
        validated = spec.params.model_validate({k: v for k, v in params.items() if k != "machine"})
        run_jarvis_proxy_gates(method, validated, self.core, machine_id)
        gate_jarvis_machine_call(method, validated, self.core, machine_id)
        client = await self.jarvis_client(machine_id)
        clean = {k: v for k, v in params.items() if k != "machine"}
        try:
            result = await client.call(method, clean)
        except Exception as exc:
            raise self._map_rpc_error(exc) from exc
        cache = self._jarvis_cache
        if cache is not None:
            if method == "terminal.list":
                for item in result.get("terminals") or []:
                    if isinstance(item, dict):
                        cache.upsert(machine_id, item)
            elif method == "session.list":
                for item in result.get("sessions") or []:
                    if isinstance(item, dict):
                        cache.upsert_session(machine_id, item)
            elif method == "external.list":
                for item in result.get("sessions") or []:
                    if isinstance(item, dict):
                        cache.upsert_external(machine_id, item)
        return result

    async def jarvis_client(self, machine_id: str) -> DaemonClient:
        """In-process proxy client for Jarvis (1.77.0, jarvisRemote); not an RPC connection.

        The hello carries no separate ``clientKind`` yet (L3): local gates and Jarvis's
        own rules enforce safety; newer remotes may recognise a dedicated jarvis-proxy kind.
        """
        live = self._live.get(machine_id)
        if live is None or live.status != "connected":
            raise RuntimeError("machine is not connected")
        client = self._jarvis_clients.get(machine_id)
        if client is None:
            info = DaemonInfo(port=live.port, pid=0, token=live.token)
            client = DaemonClient(
                info,
                client_version=f"snowpea-jarvis-proxy {__version__}",
                notification_queue_max=NOTIFY_QUEUE_MAX,
            )
            await client.connect()
            self._jarvis_clients[machine_id] = client
        return client

    async def close_jarvis_client(self, machine_id: str) -> None:
        client = self._jarvis_clients.pop(machine_id, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

