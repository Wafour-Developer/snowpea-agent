"""Gateway router: bindings, lazy sessions and chat-side approvals (M5 §3/§4).

A *binding* attaches one credentialed platform account to one target — a named
agent, an existing session, or a fresh session per conversation.  Inbound text
becomes ``session.prompt`` on the session for that ``(binding, channel_id)``
pair; the assistant's ``message.done`` goes back out through the adapter.

Approvals raised by those sessions are unattended, so they are broadcast to
every authenticated client *and* pushed to the bound conversation with
allow/deny buttons.  The router listens on the daemon's own event hub for that
(one fake "connection" per gateway session), which is why nothing in the
approval queue needs to know a gateway exists.

Bindings live in ``state.db`` and are restored when the daemon starts (AC-17).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.config.credentials import CredentialError, CredentialStore
from snowpea_core.config.paths import utc_now
from snowpea_core.gateway.base import (
    Button,
    GatewayError,
    InboundMessage,
    PlatformAdapter,
    approval_buttons,
    approval_text,
    parse_approval_callback,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.gateway")

#: Channel string that means "just write it to the daemon log" (contract §2).
LOG_CHANNEL = "log"

#: Alias so annotations below still mean the builtin ``list`` even though
#: :class:`GatewayRouter` defines a method called ``list``.
Buttons = list[Button]

#: Set to ``1`` to build :class:`~snowpea_core.gateway.fake.FakeAdapter` for
#: every platform, the way ``SNOWPEA_PROVIDER=fake:`` swaps the model out.  It
#: is what lets a test restart the daemon and watch bindings come back without
#: a credential or a socket anywhere.
FAKE_GATEWAY_ENV = "SNOWPEA_GATEWAY_FAKE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_bindings (
    id              TEXT PRIMARY KEY,
    platform        TEXT NOT NULL,
    credentials_ref TEXT NOT NULL,
    target_json     TEXT NOT NULL,
    channel_id      TEXT,
    user_id         TEXT,
    created_at      TEXT NOT NULL
);
"""



@dataclass
class Binding:
    """One live attachment between a platform account and a snowpea target."""

    id: str
    platform: str
    credentials_ref: str
    target: dict[str, Any] = field(default_factory=dict)
    #: When set, only this conversation is served by the binding.
    channel_id: str | None = None
    #: When set, only this platform user may answer approvals (risk 4).
    user_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    state: str = "active"

    def describe_target(self) -> str:
        """Short human-readable target, e.g. ``agent:ops`` or ``new_session``."""
        for key in ("agent", "session"):
            if self.target.get(key):
                return f"{key}:{self.target[key]}"
        if "new_session" in self.target:
            return "new_session"
        return "unknown"


class BindingStore:
    """The ``gateway_bindings`` table in ``$SNOWPEA_HOME/state.db``.

    Its own connection rather than :class:`~snowpea_core.session.store.Store`'s,
    so restoring bindings at startup does not depend on the session store being
    wired yet, and so this story adds no method to a file other stories edit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def insert(self, binding: Binding) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO gateway_bindings"
                " (id, platform, credentials_ref, target_json, channel_id, user_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    binding.id,
                    binding.platform,
                    binding.credentials_ref,
                    json.dumps(binding.target),
                    binding.channel_id,
                    binding.user_id,
                    binding.created_at,
                ),
            )
            self._conn.commit()

    def delete(self, binding_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM gateway_bindings WHERE id = ?", (binding_id,))
            self._conn.commit()

    def all(self) -> list[Binding]:
        with self._lock:
            rows = list(self._conn.execute("SELECT * FROM gateway_bindings ORDER BY created_at"))
        out: list[Binding] = []
        for row in rows:
            try:
                target = json.loads(row["target_json"])
            except (TypeError, ValueError):
                target = {}
            out.append(
                Binding(
                    id=row["id"],
                    platform=row["platform"],
                    credentials_ref=row["credentials_ref"],
                    target=target if isinstance(target, dict) else {},
                    channel_id=row["channel_id"],
                    user_id=row["user_id"],
                    created_at=row["created_at"],
                )
            )
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class GatewayConnection:
    """Fake RPC connection that turns one session's events into chat messages.

    ``EventHub`` only ever calls ``notify`` and reads ``closed``, so a binding
    can subscribe to a session exactly like a TUI does.  Approval traffic
    (``approval.pending`` / ``approval.resolved``) arrives the same way.
    """

    def __init__(self, router: GatewayRouter, binding: Binding, channel_id: str) -> None:
        self.router = router
        self.binding = binding
        self.channel_id = channel_id
        self.session_id: str | None = None
        self.closed = False
        self.surface_id = f"gateway:{binding.platform}:{channel_id}"
        #: Approval requests this conversation was asked about.
        self.asked: set[str] = set()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.closed:
            return
        if method == "session.event":
            await self._session_event(params)
        elif method == "approval.pending":
            await self._approval_pending(params)
        elif method == "approval.resolved":
            await self._approval_resolved(params)

    async def _session_event(self, params: dict[str, Any]) -> None:
        if params.get("sessionId") != self.session_id or params.get("kind") != "message.done":
            return
        text = str((params.get("payload") or {}).get("text") or "").strip()
        if text:
            await self.router.send(self.binding, self.channel_id, text)

    async def _approval_pending(self, params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        if request.get("sessionId") != self.session_id:
            return
        request_id = str(request.get("requestId", ""))
        if not request_id:
            return
        self.asked.add(request_id)
        await self.router.send(
            self.binding,
            self.channel_id,
            approval_text(
                str(request.get("tool", "?")),
                dict(request.get("args") or {}),
                str(request.get("sessionId", "")),
            ),
            buttons=approval_buttons(request_id),
        )

    async def _approval_resolved(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId", ""))
        if request_id not in self.asked:
            return
        self.asked.discard(request_id)
        await self.router.send(
            self.binding,
            self.channel_id,
            f"approval {request_id}: {params.get('decision')} (by {params.get('by')})",
        )


class GatewayRouter:
    """Owns bindings, their adapters and the sessions they talk through."""

    def __init__(self, core: Core | None = None, factory: Any = None) -> None:
        self.core = core
        #: ``(platform, tokens) -> PlatformAdapter``; injected in tests.
        self._factory = factory
        self._bindings: dict[str, Binding] = {}
        self._adapters: dict[str, PlatformAdapter] = {}
        self._conns: dict[tuple[str, str], GatewayConnection] = {}
        self._store: BindingStore | None = None
        self._credentials: CredentialStore | None = None

    # -- wiring --------------------------------------------------------
    def bind_core(self, core: Core) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.core = core
        self._store = BindingStore(core.paths.state_db)
        self._credentials = CredentialStore(core.paths)

    def _build_adapter(self, platform: str, credentials_ref: str) -> PlatformAdapter:
        """Resolve the credential and construct the adapter for ``platform``."""
        if self._factory is not None:
            return self._factory(platform, credentials_ref)  # type: ignore[no-any-return]
        if os.environ.get(FAKE_GATEWAY_ENV) == "1":
            from snowpea_core.gateway.fake import FakeAdapter

            return FakeAdapter(platform, credentials_ref)
        if self._credentials is None:
            raise GatewayError("gateway router is not wired to a home directory")
        tokens = self._credentials.resolve_tokens(credentials_ref)
        return build_adapter(platform, tokens)

    # -- bind / list / unbind ------------------------------------------
    async def bind(
        self,
        platform: str,
        credentials_ref: str,
        target: dict[str, Any],
        *,
        channel_id: str | None = None,
        user_id: str | None = None,
        binding_id: str | None = None,
        persist: bool = True,
    ) -> Binding:
        """Attach ``platform`` to ``target`` and start listening."""
        if not isinstance(target, dict) or not target:
            raise GatewayError(
                "target must be one of {'agent': name}, {'session': id}, {'new_session': {...}}"
            )
        binding = Binding(
            id=binding_id or f"gw-{uuid.uuid4().hex[:12]}",
            platform=platform,
            credentials_ref=credentials_ref,
            target=dict(target),
            channel_id=channel_id,
            user_id=user_id,
        )
        adapter = self._build_adapter(platform, credentials_ref)
        await adapter.start(self._handler_for(binding))
        self._adapters[binding.id] = adapter
        self._bindings[binding.id] = binding
        if persist and self._store is not None:
            self._store.insert(binding)
        self._count()
        log.info(
            "gateway binding %s: %s -> %s (credentials %s)",
            binding.id,
            platform,
            binding.describe_target(),
            credentials_ref,
        )
        return binding

    def list(self) -> list[Binding]:
        return list(self._bindings.values())

    def get(self, binding_id: str) -> Binding | None:
        return self._bindings.get(binding_id)

    def adapter(self, binding_id: str) -> PlatformAdapter | None:
        return self._adapters.get(binding_id)

    async def unbind(self, binding_id: str) -> bool:
        """Stop the adapter, drop the binding and forget its sessions."""
        binding = self._bindings.pop(binding_id, None)
        adapter = self._adapters.pop(binding_id, None)
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.stop()
        for key in [key for key in self._conns if key[0] == binding_id]:
            conn = self._conns.pop(key)
            conn.closed = True
            if self.core is not None:
                self.core.hub.unsubscribe(conn)
        if self._store is not None:
            self._store.delete(binding_id)
        self._count()
        return binding is not None

    async def restore(self) -> int:
        """Re-create every persisted binding at daemon start (AC-17)."""
        if self._store is None:
            return 0
        restored = 0
        for binding in self._store.all():
            try:
                await self.bind(
                    binding.platform,
                    binding.credentials_ref,
                    binding.target,
                    channel_id=binding.channel_id,
                    user_id=binding.user_id,
                    binding_id=binding.id,
                    persist=False,
                )
            except (GatewayError, CredentialError) as exc:
                log.warning("could not restore gateway binding %s: %s", binding.id, exc)
                binding.state = "inactive"
                self._bindings[binding.id] = binding
                continue
            restored += 1
        self._count()
        return restored

    async def stop(self) -> None:
        """Stop every adapter; the rows stay so the next start restores them."""
        for binding_id in list(self._adapters):
            adapter = self._adapters.pop(binding_id)
            with contextlib.suppress(Exception):
                await adapter.stop()
        for conn in self._conns.values():
            conn.closed = True
        self._conns.clear()
        if self._store is not None:
            with contextlib.suppress(Exception):
                self._store.close()
            self._store = None

    def _count(self) -> None:
        if self.core is not None:
            self.core.lifecycle.set_counter(
                "gateway_bindings",
                len([b for b in self._bindings.values() if b.state == "active"]),
            )

    # -- outbound ------------------------------------------------------
    async def send(
        self,
        binding: Binding,
        channel_id: str,
        text: str,
        *,
        buttons: Buttons | None = None,
    ) -> str:
        adapter = self._adapters.get(binding.id)
        if adapter is None:
            log.warning("gateway binding %s has no adapter; dropping message", binding.id)
            return ""
        try:
            return await adapter.send(channel_id, text, buttons=buttons)
        except Exception as exc:  # noqa: BLE001 - a dead platform must not end a turn
            log.warning("gateway send on %s failed: %s", binding.id, exc)
            return ""

    async def deliver(self, channel: str, text: str) -> bool:
        """Send ``text`` to ``"<platform>:<channel_id>"`` or the log (contract §2)."""
        if not channel or channel == LOG_CHANNEL:
            log.info("gateway[log] %s", text)
            return True
        platform, _, channel_id = channel.partition(":")
        if not channel_id:
            log.warning("gateway channel %r has no channel id", channel)
            return False
        for binding in self._bindings.values():
            if binding.platform != platform:
                continue
            if binding.channel_id and binding.channel_id != channel_id:
                continue
            await self.send(binding, channel_id, text)
            return True
        log.warning("no gateway binding serves %s", channel)
        return False

    # -- inbound -------------------------------------------------------
    def _handler_for(self, binding: Binding) -> Any:
        async def handler(message: InboundMessage) -> None:
            await self.handle(binding, message)

        return handler

    async def handle(self, binding: Binding, message: InboundMessage) -> None:
        """Route one inbound message: a button press, or a prompt."""
        if binding.channel_id and message.channel_id != binding.channel_id:
            log.info("ignoring %s message from unbound channel", binding.platform)
            return
        callback = parse_approval_callback(message.callback_data)
        if callback is not None:
            await self._handle_approval(binding, message, *callback)
            return
        if not message.text.strip():
            return
        await self._handle_prompt(binding, message)

    async def _handle_approval(
        self, binding: Binding, message: InboundMessage, request_id: str, decision: str
    ) -> None:
        """Answer a pending approval from a button press (risk 4: check the user)."""
        adapter = self._adapters.get(binding.id)
        # Fail closed (plan risk 4): a binding without an approver user id can never
        # approve, otherwise any member of the chat could authorise a remote shell command.
        if not binding.user_id or message.user_id != binding.user_id:
            reason = (
                "binding has no approver user id; re-bind with --user <id> to allow approvals"
                if not binding.user_id
                else "not your approval"
            )
            log.warning(
                "ignoring approval %s from %s:%s on binding %s — %s",
                request_id,
                binding.platform,
                message.user_id,
                binding.id,
                reason,
            )
            if adapter is not None and message.callback_id:
                with contextlib.suppress(Exception):
                    await adapter.acknowledge(  # type: ignore[attr-defined]
                        message.callback_id, reason
                    )
            return
        if adapter is not None and message.callback_id:
            with contextlib.suppress(AttributeError, Exception):
                await adapter.acknowledge(message.callback_id, decision)  # type: ignore[attr-defined]
        if self.core is None:
            return
        by = f"gateway:{binding.platform}:{message.user_id}"
        try:
            await self.core.approvals.respond(request_id, decision, "once", by=by)
        except Exception as exc:  # noqa: BLE001 - already answered or gone
            log.info("approval %s from %s was not applied: %s", request_id, by, exc)

    async def _handle_prompt(self, binding: Binding, message: InboundMessage) -> None:
        if self.core is None:
            return
        from snowpea_core.agent import loop as agent_loop

        session, conn = await self._session_for(binding, message.channel_id)
        text = message.text
        parsed = self.core.commands.parse(text)
        if parsed is not None:
            name, args = parsed
            self.core.commands.start(self.core, session, name, args, None)
            return
        agent_loop.start_turn(self.core, session, text, unattended=True)

    async def _session_for(
        self, binding: Binding, channel_id: str
    ) -> tuple[Any, GatewayConnection]:
        """The session for this conversation, created on first message."""
        assert self.core is not None
        key = (binding.id, channel_id)
        conn = self._conns.get(key)
        if conn is not None and conn.session_id is not None:
            session = self.core.sessions.get(conn.session_id)
            if session is not None:
                return session, conn
        conn = GatewayConnection(self, binding, channel_id)
        session = await self._create_session(binding, channel_id)
        conn.session_id = session.id
        self._conns[key] = conn
        self.core.hub.subscribe(conn, session.id)
        return session, conn

    async def _create_session(self, binding: Binding, channel_id: str) -> Any:
        assert self.core is not None
        target = binding.target
        origin_surface = f"gateway:{binding.platform}:{channel_id}"
        existing_id = target.get("session")
        if existing_id:
            session = self.core.sessions.get(str(existing_id))
            if session is None:
                raise GatewayError(f"binding {binding.id} targets missing session {existing_id}")
            return session
        spec = target.get("new_session") or {}
        if not isinstance(spec, dict):
            spec = {}
        agent = target.get("agent")
        workdir = spec.get("workdir") or str(self.core.paths.home)
        mode = spec.get("mode")
        session = await self.core.sessions.create(
            workdir=workdir,
            mode=mode,
            agent=str(agent) if agent else None,
            origin_surface=origin_surface,
            origin_conn=None,
        )
        if agent:
            session.memory_namespace = f"agent:{agent}"
        return session


def build_adapter(platform: str, tokens: dict[str, Any]) -> PlatformAdapter:
    """Construct the adapter for ``platform`` from resolved credentials."""
    from snowpea_core.gateway.discord import DiscordAdapter
    from snowpea_core.gateway.slack import SlackAdapter
    from snowpea_core.gateway.telegram import TelegramAdapter

    token = str(tokens.get("token", ""))
    if platform == "telegram":
        return TelegramAdapter(token)
    if platform == "discord":
        return DiscordAdapter(token)
    if platform == "slack":
        app_token = tokens.get("app_token")
        return SlackAdapter(token, app_token=str(app_token) if app_token else None)
    raise GatewayError(f"unknown gateway platform: {platform}")


__all__ = [
    "FAKE_GATEWAY_ENV",
    "Buttons",
    "LOG_CHANNEL",
    "SCHEMA",
    "Binding",
    "BindingStore",
    "GatewayConnection",
    "GatewayRouter",
    "build_adapter",
]
