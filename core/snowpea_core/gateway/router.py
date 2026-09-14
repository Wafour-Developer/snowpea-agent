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
    QUESTION_OTHER,
    Button,
    GatewayError,
    InboundMessage,
    PlatformAdapter,
    approval_buttons,
    approval_text,
    parse_approval_callback,
    parse_question_callback,
    question_buttons,
    question_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.gateway")

#: Channel string that means "just write it to the daemon log" (contract §2).
LOG_CHANNEL = "log"

#: Alias so annotations below still mean the builtin ``list`` even though
#: :class:`GatewayRouter` defines a method called ``list``.
Buttons = list[Button]
#: Same trick for the plain ``list[str]``s :meth:`GatewayRouter.sync_from_settings`
#: returns, and for the ``dict`` it packs them into.
Names = list[str]
SyncReport = dict[str, Names]

#: Set to ``1`` to build :class:`~snowpea_core.gateway.fake.FakeAdapter` for
#: every platform, the way ``SNOWPEA_PROVIDER=fake:`` swaps the model out.  It
#: is what lets a test restart the daemon and watch bindings come back without
#: a credential or a socket anywhere.
FAKE_GATEWAY_ENV = "SNOWPEA_GATEWAY_FAKE"

#: :attr:`Binding.source` of a binding the user made by hand (``gateway.bind``).
SOURCE_MANUAL = "manual"
#: :attr:`Binding.source` of a binding :meth:`GatewayRouter.sync_from_settings`
#: owns.  Only these are added and removed as ``settings.gateway`` changes; a
#: manual binding is never touched by the sync.
SOURCE_SETTINGS = "settings"

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
    #: ``"manual"`` (``gateway.bind``) or ``"settings"`` (the wizard's auto binding).
    source: str = SOURCE_MANUAL

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns released after the table shipped (caller holds the lock)."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(gateway_bindings)")}
        if "source" not in columns:
            self._conn.execute(
                "ALTER TABLE gateway_bindings ADD COLUMN source TEXT NOT NULL"
                f" DEFAULT '{SOURCE_MANUAL}'"
            )

    def insert(self, binding: Binding) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO gateway_bindings"
                " (id, platform, credentials_ref, target_json, channel_id, user_id,"
                " created_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding.id,
                    binding.platform,
                    binding.credentials_ref,
                    json.dumps(binding.target),
                    binding.channel_id,
                    binding.user_id,
                    binding.created_at,
                    binding.source,
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
                    source=(row["source"] if "source" in row.keys() else SOURCE_MANUAL)
                    or SOURCE_MANUAL,
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
        #: The ``ask_user`` batch this chat is working through, if any.  A
        #: messenger has no tabs, so it posts one question at a time and keeps
        #: the answers here until the last one is in.
        self.question: dict[str, Any] | None = None
        #: Index of the question now posted, and the answers collected so far.
        self.question_at: int = 0
        self.question_answers: list[dict[str, Any]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.closed:
            return
        if method == "session.event":
            await self._session_event(params)
        elif method == "approval.pending":
            await self._approval_pending(params)
        elif method == "approval.resolved":
            await self._approval_resolved(params)
        elif method == "question.pending":
            await self._question_pending(params)
        elif method == "question.resolved":
            await self._question_resolved(params)

    async def _session_event(self, params: dict[str, Any]) -> None:
        # Only the finished assistant message goes back to the chat.  Notably
        # not ``message.user``: the prompt exists so a resumed transcript can
        # show it, and echoing it would send the person their own words back.
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

    async def _question_pending(self, params: dict[str, Any]) -> None:
        """Start an ``ask_user`` batch: post its first question."""
        request = params.get("request") or {}
        if request.get("sessionId") != self.session_id:
            return
        request_id = str(request.get("requestId", ""))
        if not request_id or not (request.get("questions") or []):
            return
        self.question = dict(request)
        self.question_at = 0
        self.question_answers = []
        await self.post_question()

    async def post_question(self) -> None:
        """Post the question the chat is on, with one button per option."""
        request = self.question or {}
        items = list(request.get("questions") or [])
        if not 0 <= self.question_at < len(items):
            return
        item = items[self.question_at]
        request_id = str(request.get("requestId", ""))
        await self.router.send(
            self.binding,
            self.channel_id,
            question_text(item, self.question_at + 1, len(items)),
            buttons=question_buttons(
                request_id,
                list(item.get("options") or []),
                bool(item.get("allowOther", True)),
            ),
        )

    def current_question(self) -> dict[str, Any]:
        """The question this chat is on, or an empty dict when there is none."""
        items = list((self.question or {}).get("questions") or [])
        if not 0 <= self.question_at < len(items):
            return {}
        return items[self.question_at]

    async def _question_resolved(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId", ""))
        if not self.question or self.question.get("requestId") != request_id:
            return
        self.question = None
        self.question_at = 0
        self.question_answers = []


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
        source: str = SOURCE_MANUAL,
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
            source=source,
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
                    source=binding.source,
                )
            except (GatewayError, CredentialError) as exc:
                log.warning("could not restore gateway binding %s: %s", binding.id, exc)
                binding.state = "inactive"
                self._bindings[binding.id] = binding
                continue
            restored += 1
        self._count()
        return restored

    # -- settings-driven bindings --------------------------------------
    async def sync_from_settings(self, settings: Any) -> SyncReport:
        """Make the ``source="settings"`` bindings match ``settings.gateway``.

        A messenger the setup wizard enabled should simply *work* the next time
        the daemon runs, without a second ``gateway.bind`` call.  For every
        ``settings.gateway.<platform>`` that is enabled and carries a token this
        stores the token in ``credentials.json`` under the ref ``<platform>``
        and ensures exactly one **catch-all** binding for it: ``channel_id`` is
        ``None``, so any chat that messages the bot gets its own lazily-created
        session (see :meth:`_session_for`).

        Only bindings this method created are added or removed; a binding made
        by hand through ``gateway.bind`` is never touched.  Approvals stay
        fail-closed: a catch-all binding whose ``allowed_user_id`` is unset can
        approve nothing at all (plan risk 4), which is why the wizard asks for
        it right after the token.

        Returns ``{"added": [...], "removed": [...], "kept": [...]}`` by
        platform, which is what ``gateway.sync`` reports back.
        """
        desired = desired_gateways(settings)
        added: Names = []
        removed: Names = []
        kept: Names = []

        for platform, block in desired.items():
            token = str(block.get("token") or "")
            if token and self._credentials is not None:
                try:
                    self._credentials.set(platform, token)
                except OSError as exc:  # pragma: no cover - unwritable home
                    log.warning("could not store the %s token: %s", platform, exc)

        for binding in list(self._bindings.values()):
            if binding.source != SOURCE_SETTINGS:
                continue
            wanted = desired.get(binding.platform)
            if (
                wanted is not None
                and binding.state == "active"
                and binding.channel_id is None
                and binding.user_id == _allowed_user_id(wanted)
                and binding.target == self._auto_target(wanted)
            ):
                kept.append(binding.platform)
                desired.pop(binding.platform)
                continue
            await self.unbind(binding.id)
            removed.append(binding.platform)

        for platform, block in desired.items():
            try:
                await self.bind(
                    platform,
                    platform,
                    self._auto_target(block),
                    channel_id=None,
                    user_id=_allowed_user_id(block),
                    source=SOURCE_SETTINGS,
                )
            except (GatewayError, CredentialError) as exc:
                log.warning("could not start the %s messenger from settings: %s", platform, exc)
                continue
            added.append(platform)

        self._count()
        return {"added": added, "removed": removed, "kept": kept}

    def _auto_target(self, block: dict[str, Any]) -> dict[str, Any]:
        """``{"new_session": {...}}`` for a catch-all binding built from settings."""
        workdir = block.get("workdir") or str(Path.home())
        mode = block.get("mode") or "accept"
        return {"new_session": {"workdir": str(workdir), "mode": str(mode)}}

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
        question = parse_question_callback(message.callback_data)
        if question is not None:
            await self._handle_question_button(binding, message, *question)
            return
        if not message.text.strip():
            return
        # A chat with an open question reads the next typed line as its answer
        # — "2", "1,3", or whatever the user wants to say — rather than as a
        # new prompt.  Without this the button is the only way to answer, and
        # a platform that drops the keyboard leaves the turn stuck.
        if await self._answer_open_question(binding, message):
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

    def _open_question(self, binding: Binding, channel_id: str) -> GatewayConnection | None:
        """The conversation's open ``ask_user`` batch, if there is one."""
        conn = self._conns.get((binding.id, channel_id))
        return conn if conn is not None and conn.question else None

    async def _handle_question_button(
        self, binding: Binding, message: InboundMessage, request_id: str, choice: str
    ) -> None:
        """Answer an ``ask_user`` question from a button press.

        Unlike an approval this needs no approver check: a question grants no
        permission, so anyone in the conversation the agent is talking to may
        answer it.  "Other" is the exception — it has no label to send, so it
        asks the user to type instead.
        """
        adapter = self._adapters.get(binding.id)
        conn = self._open_question(binding, message.channel_id)
        if conn is None or str((conn.question or {}).get("requestId")) != request_id:
            return
        options = list(conn.current_question().get("options") or [])
        if choice == QUESTION_OTHER:
            if adapter is not None and message.callback_id:
                with contextlib.suppress(AttributeError, Exception):
                    await adapter.acknowledge(message.callback_id, "other")  # type: ignore[attr-defined]
            await self.send(
                binding, message.channel_id, "답을 적어 주세요 / type your answer as a reply."
            )
            return
        position = int(choice)
        if not 1 <= position <= len(options):
            return
        label = str(options[position - 1].get("label", ""))
        if adapter is not None and message.callback_id:
            with contextlib.suppress(AttributeError, Exception):
                await adapter.acknowledge(message.callback_id, label)  # type: ignore[attr-defined]
        await self._advance_question(binding, message, conn, [label], None)

    async def _answer_open_question(self, binding: Binding, message: InboundMessage) -> bool:
        """Read a typed reply as the answer to the open question; True if it was one."""
        conn = self._open_question(binding, message.channel_id)
        if conn is None:
            return False
        item = conn.current_question()
        options = list(item.get("options") or [])
        text = message.text.strip()
        picked = _picked_labels(text, options, bool(item.get("multi")))
        if picked:
            await self._advance_question(binding, message, conn, picked, None)
            return True
        if options and not item.get("allowOther", True):
            await self.send(
                binding,
                message.channel_id,
                f"번호로 답해 주세요 (1-{len(options)}) / reply with a number.",
            )
            return True
        await self._advance_question(binding, message, conn, [], text)
        return True

    async def _advance_question(
        self,
        binding: Binding,
        message: InboundMessage,
        conn: GatewayConnection,
        selected: list[str],
        text: str | None,
    ) -> None:
        """Record one answer, then post the next question or submit the batch."""
        request = conn.question or {}
        items = list(request.get("questions") or [])
        conn.question_answers.append({"selected": selected, "text": text})
        conn.question_at += 1
        if conn.question_at < len(items):
            await conn.post_question()
            return
        await self._respond_question(
            binding, message, str(request.get("requestId", "")), conn.question_answers
        )

    async def _respond_question(
        self,
        binding: Binding,
        message: InboundMessage,
        request_id: str,
        answers: list[dict[str, Any]],
    ) -> None:
        if self.core is None:
            return
        by = f"gateway:{binding.platform}:{message.user_id}"
        try:
            await self.core.questions.respond(request_id, answers, by=by)
        except Exception as exc:  # noqa: BLE001 - already answered or gone
            log.info("question %s from %s was not applied: %s", request_id, by, exc)

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


def desired_gateways(settings: Any) -> dict[str, dict[str, Any]]:
    """``settings.gateway`` entries that are enabled *and* have a token.

    An enabled platform with no token is a half-finished wizard run, not an
    error: it is logged and skipped so the daemon still starts.
    """
    out: dict[str, dict[str, Any]] = {}
    for platform, block in (getattr(settings, "gateway", None) or {}).items():
        if not isinstance(block, dict) or not block.get("enabled"):
            continue
        if not block.get("token"):
            log.warning("messenger %s is enabled but has no token; skipping it", platform)
            continue
        out[str(platform)] = dict(block)
    return out


def _allowed_user_id(block: dict[str, Any]) -> str | None:
    """The platform user allowed to answer approvals, or ``None`` (fail closed)."""
    value = block.get("allowed_user_id")
    return str(value) if value not in (None, "") else None


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


def _picked_labels(text: str, options: list[dict[str, Any]], multi: bool) -> list[str]:
    """Labels a typed reply names: "2", "1,3", or the label spelled out.

    Returns nothing when the reply is not a choice, which is what makes it free
    text instead.  A multi-select reply may name several; a single-select one
    takes the first, because "1,3" to a question that wanted one answer is a
    misunderstanding better resolved by taking the first than by guessing.
    """
    if not options:
        return []
    labels = [str(option.get("label", "")) for option in options]
    lowered = {label.lower(): label for label in labels if label}
    tokens = [token.strip() for token in text.replace(" ", ",").split(",") if token.strip()]
    picked: list[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(labels):
            picked.append(labels[int(token) - 1])
        elif token.lower() in lowered:
            picked.append(lowered[token.lower()])
        else:
            return []
    if not multi:
        picked = picked[:1]
    # Keep the order the options were offered in, and drop duplicates.
    return [label for label in labels if label in picked]


__all__ = [
    "FAKE_GATEWAY_ENV",
    "SOURCE_MANUAL",
    "SOURCE_SETTINGS",
    "Buttons",
    "LOG_CHANNEL",
    "Names",
    "SyncReport",
    "SCHEMA",
    "Binding",
    "BindingStore",
    "GatewayConnection",
    "GatewayRouter",
    "build_adapter",
    "desired_gateways",
]
