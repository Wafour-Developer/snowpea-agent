"""Jarvis mode in the messengers: routing, notices, grants and commands (M19 WP7).

While Jarvis mode is on (and ``jarvis.messenger.route`` is, which it is by
default) every messenger chat talks to the Jarvis session and to nothing else:
what the person writes becomes a ``message.user`` there (origin ``human`` with
the platform as surface for the bound person, ``gateway`` for any other member
of the chat), and what the other sessions would have said to the chat goes
through Jarvis instead.  Nothing stored is rewritten.  A chat's connection
remembers the session it was in (:attr:`GatewayConnection.own_session_id`) and
the bindings and ``gateway-chats.json`` are never touched, so turning Jarvis
off puts every chat back exactly where it was.

The bridge hangs off :class:`~snowpea_core.gateway.router.GatewayRouter`
(``router.jarvis``) and attaches to the :class:`JarvisService` through its
hooks: ``hooks.state`` re-points the chats, ``hooks.notify`` carries the
important notices.  What reaches the person's chat:

* every approval and question routed to the **person**, from any session, with
  the session and project name, answerable there (the answer counts as the
  bound person's); requests routed to Jarvis or decided by the engine are not
  sent;
* a few proactive notices (finished, error, needs the person, Jarvis decided),
  honouring ``jarvis.notify.messenger`` and ``jarvis.proactive.*``, coalesced
  into one message per window and never more often than ``NOTICE_MIN_GAP_SEC``;
* Jarvis's ``delegation_set`` proposal, shown with scope, level, guarded
  classes and expiry.  One press by the **bound person** confirms it (while
  ``jarvis.messenger.allowGrants``); anyone else in a group may only decline.

Unrestricted mode for an external CLI also needs a person's confirmation.
:meth:`MessengerBridge.confirm_unrestricted` is the seam for that on a
messenger: it asks the bound person with a one-press message and returns a
:class:`MessengerConfirmation` (or ``None``).  ``external/proc.py`` turns that
proof into its own ``PersonConfirmation`` where it accepts the messenger.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.config.paths import utc_now
from snowpea_core.gateway.base import (
    Button,
    InboundMessage,
    approval_buttons,
    approval_callback,
    approval_text,
)
from snowpea_core.gateway.chat import clip, short_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.gateway.router import Binding, GatewayConnection, GatewayRouter
    from snowpea_core.jarvis.service import JarvisService

log = logging.getLogger("snowpea.jarvis.messenger")

#: Notices gather for this long before one message goes out.
NOTICE_WINDOW_SEC = 5.0
#: Two notice messages are never closer than this.
NOTICE_MIN_GAP_SEC = 30.0
#: Lines in one notice message, characters in a line, and in the whole message.
NOTICE_LINES = 8
NOTICE_LINE_CHARS = 160
NOTICE_CHARS = 1500
#: Notices kept while waiting for the window; older ones are only counted.
NOTICE_KEPT = 50
#: The longest briefing message, whatever the platform takes (one message, never a flood).
BRIEFING_CHARS = 3500
#: Rows in ``/jarvis`` and the longest one-press confirmation wait.
OVERVIEW_ROWS = 8
CONFIRM_TIMEOUT_SEC = 600.0
#: Longest first message an unrestricted confirmation shows.  The whole prompt
#: is always shown; a longer one is refused rather than cut, so the person
#: never allows text they could not read.
CONFIRM_PROMPT_MAX = 3000
#: Jarvis turns remembered as the bound person's (for the unrestricted ask).
TURNS_KEPT = 64
#: Importance levels that reach a messenger (as in the service).
IMPORTANT = frozenset({"high", "urgent"})

#: ``jarvis.proactive.<switch>`` each escalation reason belongs to.
_PROACTIVE = {
    "finished": "finished",
    "error": "error",
    "stalled": "error",
    "guarded": "needsYou",
    "risky": "needsYou",
    "never": "needsYou",
    "question": "needsYou",
    "jarvis": "needsYou",
    "budget": "needsYou",
}
_ICONS = {
    "finished": "✅",
    "error": "❌",
    "stalled": "⏳",
    "budget": "⚠️",
    "decision": "\U0001f916",
}
_NEEDS_YOU_ICON = "❗"
_STATUS_ICONS = {
    "running": "▶️",
    "awaiting_approval": _NEEDS_YOU_ICON,
    "awaiting_question": _NEEDS_YOU_ICON,
    "error": "❌",
    "idle": "·",
}

ROUTING_ON_TEXT = (
    "자비스 모드가 켜졌습니다 — 이제 이 채팅은 자비스와만 대화합니다 / Jarvis mode is on: "
    "this chat now talks to Jarvis only."
)
ROUTING_OFF_TEXT = (
    "자비스 모드가 꺼졌습니다 — 이 채팅은 원래 대상으로 돌아갑니다 / Jarvis mode is off: "
    "this chat is back where it was."
)
COMMANDS_HELP = (
    "/jarvis — overview · /jarvis on|off · /jarvis takeover <name> · /jarvis handback <name>"
)
PERSON_ONLY_TEXT = (
    "이 채팅에 연결된 사람만 할 수 있습니다 / only the person bound to this chat can do that"
)


@dataclass(frozen=True)
class MessengerConfirmation:
    """The bound person of a messenger chat pressed "confirm" (see the module docstring)."""

    platform: str
    user_id: str
    channel_id: str
    ts: str


class MessengerBridge:
    """Routing, notices and commands between one gateway router and the Jarvis service."""

    def __init__(
        self,
        router: GatewayRouter,
        *,
        window_sec: float = NOTICE_WINDOW_SEC,
        min_gap_sec: float = NOTICE_MIN_GAP_SEC,
    ) -> None:
        self.router = router
        self.service: JarvisService | None = None
        self.window_sec = window_sec
        self.min_gap_sec = min_gap_sec
        #: Notice id -> line, in arrival order, waiting for the window.
        self._notices: OrderedDict[str, str] = OrderedDict()
        self._dropped = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._last_sent: float | None = None
        #: Routing as of the last :meth:`sync`, and whether a chat command is switching it.
        self._was_routing = False
        self._muted = False
        #: Token -> (binding id, channel id, future) of a pending one-press confirmation.
        self._confirms: dict[
            str, tuple[str, str, asyncio.Future[MessengerConfirmation | None]]
        ] = {}
        #: Jarvis turn id -> (binding id, channel id, user id) of the bound
        #: person's message that started it; one unrestricted ask each.
        self._turns: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self.confirm_timeout_sec = CONFIRM_TIMEOUT_SEC
        #: ``(binding id, channel id, request id)`` of approvals a chat already
        #: shows with buttons, from before it was routed: not posted again.
        self._posted: set[tuple[str, str, str]] = set()

    # -- wiring ----------------------------------------------------------
    def attach(self, service: JarvisService) -> None:
        """Follow ``service`` (idempotent; called when either side appears)."""
        if self.service is service:
            return
        self.service = service
        service.hooks.notify.append(self.notify)
        service.hooks.state.append(self.on_state)
        service.hooks.unrestricted.append(self.unrestricted_for_turn)

    def _service(self) -> JarvisService:
        if self.service is None:
            from snowpea_core.jarvis.service import service_of

            assert self.router.core is not None
            self.attach(service_of(self.router.core))
        assert self.service is not None
        return self.service

    async def close(self) -> None:
        """Daemon shutdown: drop queued notices and unanswered confirmations."""
        task, self._flush_task = self._flush_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for _binding, _channel, future in self._confirms.values():
            if not future.done():
                future.set_result(None)
        self._confirms.clear()
        self._notices.clear()

    # -- routing -----------------------------------------------------------
    def jarvis_session(self) -> Any:
        """The Jarvis session every chat talks to now, or ``None`` when routing is off."""
        service = self.service
        if service is None or not service.running:
            return None
        settings = service.core.settings.jarvis
        if not (settings.enabled and settings.messenger.route):
            return None
        return service.session()

    @property
    def routing(self) -> bool:
        return self.jarvis_session() is not None

    async def on_state(self, _reason: str, _running: bool) -> None:
        await self.sync()

    async def sync(self) -> None:
        """Point every chat at Jarvis, or back at where it was."""
        session = self.jarvis_session()
        changed = (session is not None) != self._was_routing
        self._was_routing = session is not None
        conns = list(self.router._conns.values())
        for conn in conns:
            if session is not None:
                await self._route(conn, session.id)
            else:
                await self._unroute(conn)
        if changed and not self._muted:
            note = ROUTING_ON_TEXT if session is not None else ROUTING_OFF_TEXT
            for conn in conns:
                await self.router.send(conn.binding, conn.channel_id, note)

    async def _detach(self, conn: GatewayConnection) -> None:
        """Leave the session ``conn`` is subscribed to; its half-finished asks stay in the app."""
        core = self.router.core
        if core is not None:
            core.hub.unsubscribe(conn)
        await conn.activity.cancel()
        conn.child_sessions.clear()
        conn.global_subscription = False
        conn.asked.clear()
        conn.question = None
        conn.question_at = 0
        conn.question_answers = []
        conn.reply_wanted = False

    async def _route(self, conn: GatewayConnection, jarvis_id: str) -> None:
        if conn.routed and conn.session_id == jarvis_id:
            return
        if not conn.routed:
            conn.own_session_id = conn.session_id
            conn.routed = True
        # Its buttons still answer them; a request routed later is not posted twice.
        for request_id in conn.asked:
            self._posted.add((conn.binding.id, conn.channel_id, request_id))
        await self._detach(conn)
        conn.session_id = jarvis_id
        assert self.router.core is not None
        self.router.core.hub.subscribe(conn, jarvis_id)

    async def _unroute(self, conn: GatewayConnection) -> None:
        if not conn.routed:
            return
        await self._detach(conn)
        core = self.router.core
        assert core is not None
        own, conn.own_session_id, conn.routed = conn.own_session_id, None, False
        if own and core.sessions.get(own) is not None:
            conn.session_id = own
            core.hub.subscribe(conn, own)
            return
        # Nothing live to go back to: the chat's remembered session is reopened
        # (or its binding's target used) by the next message, as after a restart.
        conn.session_id = None
        for key in [k for k, other in self.router._conns.items() if other is conn]:
            del self.router._conns[key]

    async def resolve(
        self, binding: Binding, channel_id: str, chat_type: str
    ) -> tuple[Any, GatewayConnection]:
        """The Jarvis session and this chat's connection to it (routing is on)."""
        from snowpea_core.gateway.router import GatewayConnection

        session = self.jarvis_session()
        assert session is not None and self.router.core is not None
        key = (binding.id, channel_id)
        conn = self.router._conns.get(key)
        if conn is None:
            conn = GatewayConnection(self.router, binding, channel_id)
            conn.own_session_id = self.router.chats.get(binding.id, channel_id)
            conn.routed = True
            conn.session_id = session.id
            self.router._conns[key] = conn
            self.router.core.hub.subscribe(conn, session.id)
        else:
            await self._route(conn, session.id)
        if chat_type:
            conn.chat_type = chat_type
        return session, conn

    def reply_wanted(self, conn: GatewayConnection, payload: dict[str, Any]) -> bool:
        """Is the turn starting on a routed chat one the chat asked for?

        Jarvis also runs turns of its own (wakes for finished work, the
        briefing); their output is the app's business, the chat hears about
        them through the notices.
        """
        core = self.router.core
        session = None
        if core is not None and conn.session_id:
            session = core.sessions.get(conn.session_id)
        turn_id = str(payload.get("turnId") or "")
        origin = (getattr(session, "turn_origins", None) or {}).get(turn_id)
        return origin in ("human", "member")

    # -- requests shared with the chat --------------------------------------
    def already_posted(self, binding_id: str, channel_id: str, request_id: str) -> bool:
        """The chat already shows this request (asked there before Jarvis mode came on)."""
        return (binding_id, channel_id, request_id) in self._posted

    def resolved(self, request_id: str) -> None:
        self._posted = {key for key in self._posted if key[2] != request_id}

    def hidden(self, request: dict[str, Any]) -> bool:
        """The request comes from a project hidden from Jarvis (not forwarded by it)."""
        service = self.service
        core = self.router.core
        if service is None or core is None:
            return False
        session = core.sessions.get(str(request.get("sessionId") or ""))
        workdir = getattr(session, "workdir", None)
        if workdir is None:
            return False
        try:
            return bool(service.path_hidden(workdir))
        except Exception:  # noqa: BLE001 - unknown is hidden
            return True

    async def _private_targets(self) -> list[tuple[Binding, str]]:
        """``(binding, private chat)`` of every bound person Jarvis may write to (H3)."""
        out = []
        for binding in self.router.list():
            if binding.state != "active" or not binding.user_id:
                continue
            channel = self.router.private_chat(binding)
            if channel is None:
                await self.router.no_private_chat(binding)
                continue
            out.append((binding, channel))
        return out

    def label(self, request: dict[str, Any]) -> str:
        """``[title · project · s-1a2b3c]``: which session asks, as the person names it."""
        session_id = str(request.get("sessionId") or "")
        external = request.get("external")
        core = self.router.core
        session = core.sessions.get(session_id) if core is not None else None
        title = ""
        project = ""
        if session is not None:
            title = (session.title or "").strip()
            project = Path(str(session.workdir)).name
        elif isinstance(external, dict):
            title = f"{external.get('cli', 'external')}"
        parts = [part for part in (title, project) if part]
        if len(parts) == 2 and parts[0] == parts[1]:
            parts = parts[:1]
        parts.append(short_id(session_id) if session_id else "?")
        return "[" + " · ".join(parts) + "]"

    def approval_message(self, request: dict[str, Any]) -> tuple[str, list[Button]]:
        """The chat text and buttons for one approval (a grant proposal gets its own)."""
        request_id = str(request.get("requestId", ""))
        if request.get("personOnly") and request.get("tool") == "delegation_set":
            return self._grant_message(request, request_id)
        text = approval_text(
            str(request.get("tool", "?")),
            dict(request.get("args") or {}),
            str(request.get("sessionId", "")),
        )
        if request.get("personOnly") and request.get("blocked"):
            # A call a security check blocked is the local person's to allow
            # (security review H3): a chat may only decline it.
            return text + (
                "\n\U0001f512 보안 검사에 걸린 호출입니다 — 이 컴퓨터의 앱에서 허용하세요 / "
                "a security check blocked this call: allow it on the machine."
            ), [Button(text="⛔ decline", data=approval_callback(request_id, "deny"))]
        return text, approval_buttons(request_id)

    def _grant_message(self, request: dict[str, Any], request_id: str) -> tuple[str, list[Button]]:
        service = self._service()
        args = dict(request.get("args") or {})
        scope = str(args.get("scope") or "?")
        target = args.get("target")
        if scope == "global":
            where = "every project / 모든 프로젝트"
        elif scope == "project" and target:
            where = f"{service.projects.name_of(str(target))} ({target})"
        else:
            core = service.core
            found = core.sessions.get(str(target)) if target else None
            name = ((found.title or "").strip() or Path(str(found.workdir)).name) if found else ""
            where = f"{name} ({target})" if name else str(target or "?")
        allow = [str(item) for item in (args.get("allow") or [])]
        lines = [
            "\U0001f510 Jarvis가 권한 위임을 요청합니다 / Jarvis asks for a delegation",
            f"scope: {scope} · {where}",
            f"level: {args.get('level', '?')}",
            "guarded classes: " + (", ".join(allow) if allow else "none / 없음"),
            f"expires: {args.get('expiresAt') or 'never / 만료 없음'}",
        ]
        note = str(args.get("note") or "").strip()
        if note:
            lines.append(f"note: {clip(note, 300)}")
        if service.core.settings.jarvis.messenger.allowGrants:
            lines.append(
                "연결된 사람만 한 번 눌러 확인할 수 있습니다 / only the bound person can confirm "
                "with one press; anyone may decline."
            )
            return "\n".join(lines), [
                Button(text="✅ confirm", data=approval_callback(request_id, "allow")),
                Button(text="⛔ decline", data=approval_callback(request_id, "deny")),
            ]
        lines.append(
            "이 메신저에서는 확인할 수 없습니다 — 앱에서 확인하세요 / this messenger cannot "
            "confirm grants (jarvis.messenger.allowGrants is off): confirm in the app."
        )
        return "\n".join(lines), [
            Button(text="⛔ decline", data=approval_callback(request_id, "deny"))
        ]

    # -- proactive notices ------------------------------------------------------
    async def notify(self, kind: str, params: dict[str, Any]) -> None:
        """``hooks.notify``: queue an important escalation or decision for the chat.

        ``briefing`` (``{"text", "report"}``) is not a notice: the briefing
        itself goes out at once, one message per bound person, trimmed to the
        platform's limit with a pointer to the saved report.
        """
        service = self._service()
        settings = service.core.settings.jarvis
        if not settings.notify.messenger:
            return
        if kind == "briefing":
            await self._briefing(str(params.get("text") or ""), str(params.get("report") or ""))
            return
        if kind == "escalation":
            if params.get("requestId"):
                return  # the approval or question itself is in the chat
            if params.get("importance") not in IMPORTANT:
                return
            reason = str(params.get("reason") or "")
            switch = _PROACTIVE.get(reason)
            if switch is None or not getattr(settings.proactive, switch):
                return
            icon = _ICONS.get(reason, _NEEDS_YOU_ICON)
            summary = str(params.get("summary") or "").strip()
            title = str(params.get("title") or "").strip()
            if title and title not in summary:
                summary = f"{title}: {summary}"
            self._queue(str(params.get("id") or uuid.uuid4().hex), f"{icon} {summary}")
        elif kind == "decision":
            entry = dict(params.get("entry") or {})
            if not settings.proactive.decided or entry.get("importance") not in IMPORTANT:
                return
            summary = str(entry.get("summary") or "").strip()
            self._queue(str(entry.get("id") or uuid.uuid4().hex), f"{_ICONS['decision']} {summary}")

    async def _briefing(self, text: str, report: str) -> None:
        text = text.strip()
        if not text:
            return
        head = "\U0001f5de️ Jarvis · briefing / 브리핑"
        where = f"{report} (Jarvis folder / 자비스 폴더)" if report else "the Jarvis folder"
        for binding, channel in await self._private_targets():
            limit = min(self.router.message_limit(binding), BRIEFING_CHARS)
            pointer = f"\n\n(저장 / saved: {where})"
            body = f"{head}\n{text}"
            if len(body) + len(pointer) > limit:
                pointer = f"\n…\n(전체 / full briefing: {where})"
                body = body[: max(0, limit - len(pointer))].rstrip()
            await self.router.send(binding, channel, body + pointer)

    def _queue(self, notice_id: str, line: str) -> None:
        line = clip(line, NOTICE_LINE_CHARS)
        if not line.strip():
            return
        self._notices[notice_id] = line
        while len(self._notices) > NOTICE_KEPT:
            self._notices.popitem(last=False)
            self._dropped += 1
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.ensure_future(self._flush_later())

    async def _flush_later(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self.window_sec
        if self._last_sent is not None:
            delay = max(delay, self._last_sent + self.min_gap_sec - loop.time())
        await asyncio.sleep(delay)
        await self.flush()

    async def flush(self) -> None:
        """Send everything queued as one message to each bound person's chat."""
        if not self._notices:
            return
        lines = list(self._notices.values())
        extra = self._dropped + max(0, len(lines) - NOTICE_LINES)
        self._notices.clear()
        self._dropped = 0
        shown = lines[-NOTICE_LINES:]
        body = "\n".join(f"• {line}" for line in shown)
        if extra:
            body += f"\n… +{extra} more / 외 {extra}건 — /jarvis"
        header = "Jarvis" if len(lines) == 1 else f"Jarvis ({len(lines) + self._dropped})"
        text = f"{header}\n{body}"
        if len(text) > NOTICE_CHARS:
            pointer = "…\n(/jarvis — 더 보기 / more)"
            text = text[: NOTICE_CHARS - len(pointer)] + pointer
        self._last_sent = asyncio.get_running_loop().time()
        for binding, channel in await self._private_targets():
            await self.router.send(binding, channel, text)

    # -- one-press confirmations (unrestricted mode seam) ------------------------
    def note_turn(self, turn_id: str, binding: Binding, message: InboundMessage) -> None:
        """A Jarvis turn the bound person started with ``message`` in this chat."""
        self._turns[turn_id] = (binding.id, message.channel_id, str(message.user_id))
        while len(self._turns) > TURNS_KEPT:
            self._turns.popitem(last=False)

    async def unrestricted_for_turn(self, turn_id: str, ask: dict[str, Any]) -> Any:
        """``hooks.unrestricted``: the bound person allows unrestricted mode with one press.

        Only for a Jarvis turn started by the bound person's own message in a
        chat, once per message, and only while ``jarvis.messenger.allowGrants``
        is on.  Returns the external layer's ``PersonConfirmation``, a refusal
        reason, or ``None`` when the turn did not come from a chat.
        """
        from snowpea_core.external.proc import ExternalError, person_confirmation_from_gateway

        found = self._turns.pop(turn_id, None)
        if found is None:
            return None
        binding_id, _channel, user_id = found
        binding = self.router.get(binding_id)
        if binding is None or not binding.user_id or str(binding.user_id) != user_id:
            return "only the person bound to that chat may choose unrestricted mode"
        settings = self._service().core.settings
        try:
            # Refused here already (allowGrants off) before anything is posted.
            person_confirmation_from_gateway(binding.platform, bound_person=True, settings=settings)
        except ExternalError as exc:
            return exc.message
        prompt_text = str(ask.get("prompt") or "").strip()
        if len(prompt_text) > CONFIRM_PROMPT_MAX:
            return (
                f"the first message is {len(prompt_text)} characters; a confirmation shows it "
                f"whole, so at most {CONFIRM_PROMPT_MAX}: start the session with a shorter one"
            )
        proof = await self.confirm_unrestricted(
            binding_id,
            cli=str(ask.get("cli") or "?"),
            session="new / 새 세션",
            project=str(ask.get("project") or "?"),
            prompt=str(ask.get("prompt") or ""),
            timeout_sec=self.confirm_timeout_sec,
        )
        if proof is None or proof.user_id != str(binding.user_id):
            return "the person did not allow unrestricted mode (declined, or no answer in time)"
        try:
            return person_confirmation_from_gateway(
                binding.platform, bound_person=True, settings=settings
            )
        except ExternalError as exc:
            return exc.message

    async def confirm_unrestricted(
        self,
        binding_id: str,
        *,
        cli: str,
        session: str,
        project: str,
        prompt: str = "",
        timeout_sec: float = CONFIRM_TIMEOUT_SEC,
    ) -> MessengerConfirmation | None:
        """Ask the bound person of ``binding_id`` to allow unrestricted mode; one press.

        The seam for ``external/proc.py``: it returns the person's proof, or
        ``None`` when they declined, did not answer in time, or the binding has
        no bound person.  Only that person's press counts.
        """
        binding = self.router.get(binding_id)
        # Only in the person's private chat, never a group (security review H3).
        channel = self.router.private_chat(binding) if binding is not None else None
        if binding is None or not binding.user_id or channel is None:
            if binding is not None:
                await self.router.no_private_chat(binding)
            return None
        if len(prompt.strip()) > CONFIRM_PROMPT_MAX:
            return None  # never ask the person to allow text they cannot see whole
        token = f"uc-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MessengerConfirmation | None] = loop.create_future()
        self._confirms[token] = (binding.id, channel, future)
        text = f"⚠️ unrestricted · {cli}\nsession: {session}\nproject: {project}\n"
        if prompt.strip():
            text += f"first message / 첫 메시지 (whole / 전체):\n{prompt.strip()}\n"
        text += (
            "snowpea does not control what this session runs; Jarvis's guarded-class rules "
            "do not apply inside it. / snowpea는 이 세션이 실행하는 것을 통제하지 않습니다."
        )
        try:
            await self.router.send(binding, channel, text, buttons=approval_buttons(token))
            return await asyncio.wait_for(future, timeout_sec)
        except TimeoutError:
            return None
        finally:
            self._confirms.pop(token, None)

    async def answer_confirmation(
        self, binding: Binding, message: InboundMessage, token: str, decision: str
    ) -> bool:
        """A button press on a confirmation; True when ``token`` was one (even if refused)."""
        pending = self._confirms.get(token)
        if pending is None:
            return False
        binding_id, channel, future = pending
        if binding.id != binding_id or message.channel_id != channel:
            return False
        bound = bool(binding.user_id) and str(message.user_id) == str(binding.user_id)
        if not bound or future.done():
            await self.router.acknowledge(binding, message, "not your confirmation")
            return True
        await self.router.acknowledge(binding, message, decision)
        future.set_result(
            MessengerConfirmation(
                platform=binding.platform,
                user_id=str(message.user_id),
                channel_id=message.channel_id,
                ts=utc_now(),
            )
            if decision == "allow"
            else None
        )
        return True

    # -- commands (/jarvis ...) -------------------------------------------------
    def is_person(self, binding: Binding, message: InboundMessage) -> bool:
        """The message is the bound person's (a binding with nobody bound has no person)."""
        return bool(binding.user_id) and str(message.user_id) == str(binding.user_id)

    async def command(self, binding: Binding, message: InboundMessage, args: str) -> str:
        """Run ``/jarvis <args>`` and return the reply."""
        word, _, rest = args.strip().partition(" ")
        word = word.lower()
        rest = rest.strip()
        if word in ("help", "?"):
            return COMMANDS_HELP
        # Everything else, the status too (it names sessions, projects and what
        # waits), is the bound person's only, not any member of a group chat.
        if not self.is_person(binding, message):
            return PERSON_ONLY_TEXT
        if word in ("", "status", "overview", "상태"):
            return self.status_text()
        service = self._service()
        if word in ("on", "enable", "켜", "켜기"):
            return await self._switch(service, True)
        if word in ("off", "disable", "끄", "끄기"):
            return await self._switch(service, False)
        if word in ("takeover", "take", "인수"):
            return await self._pause(service, binding, rest, handback=False)
        if word in ("handback", "back", "return", "반환"):
            return await self._pause(service, binding, rest, handback=True)
        return COMMANDS_HELP

    async def _switch(self, service: JarvisService, on: bool) -> str:
        from snowpea_core.jarvis.service import JarvisError

        self._muted = True
        try:
            if on:
                await service.enable()
            else:
                await service.disable()
            await self.sync()
        except JarvisError as exc:
            return f"Jarvis: {exc}"
        finally:
            self._muted = False
        return ROUTING_ON_TEXT if on else ROUTING_OFF_TEXT

    def status_text(self) -> str:
        service = self._service()
        settings = service.core.settings.jarvis
        if not settings.enabled:
            return "자비스 모드가 꺼져 있습니다 / Jarvis mode is off. /jarvis on turns it on."
        if not service.running:
            problem = "; ".join(service.problems) or "starting"
            return f"자비스가 실행 중이 아닙니다 / Jarvis is on but not running: {problem}"
        rows = service.overview_rows(limit=OVERVIEW_ROWS * 2)
        waiting = sum(1 for row in rows if row.waitingOnPerson)
        running = sum(1 for row in rows if row.status == "running")
        head = f"Jarvis · {len(rows)} sessions · running {running} · waiting for you {waiting}"
        if self.routing:
            head += "\n(이 채팅은 자비스와만 대화합니다 / this chat talks to Jarvis only)"
        lines = [head]
        for row in rows[:OVERVIEW_ROWS]:
            lines.append(self._row_line(service, row))
        if len(rows) > OVERVIEW_ROWS:
            lines.append(f"… +{len(rows) - OVERVIEW_ROWS}")
        lines.append(COMMANDS_HELP)
        return "\n".join(lines)

    def _row_line(self, service: JarvisService, row: Any) -> str:
        project = service.projects.name_of(row.project) if row.project else ""
        name = (row.title or "").strip() or project or "untitled"
        ident = row.sessionId or (f"{row.external.cli}:{row.external.id}" if row.external else "")
        bits = [row.status.replace("_", " ")]
        if row.waitingOnPerson:
            bits.append("waits for you")
        if row.takenOver:
            bits.append("taken over")
        elif row.pausedUntil:
            bits.append("paused")
        where = f" ({project})" if project and project != name else ""
        icon = _STATUS_ICONS.get(row.status, "·")
        return f"{icon} {clip(name, 40)}{where} — {', '.join(bits)} [{short_id(ident)}]"

    def _find(self, service: JarvisService, wanted: str) -> tuple[Any | None, str]:
        """The one overview row ``wanted`` names, or why there is not exactly one."""
        if not wanted:
            return None, "usage: /jarvis takeover <session name>"
        needle = wanted.casefold()
        rows = service.overview_rows()

        def names(row: Any) -> list[str]:
            ident = row.sessionId or (row.external.id if row.external else "")
            project = service.projects.name_of(row.project) if row.project else ""
            return [(row.title or "").casefold(), project.casefold(), ident.casefold()]

        exact = [row for row in rows if needle in names(row)]
        hits = exact or [
            row
            for row in rows
            if any(needle in name for name in names(row)[:2])
            or (row.sessionId or "").casefold().startswith(needle)
        ]
        if len(hits) == 1:
            return hits[0], ""
        if not hits:
            return None, f"no session called {wanted!r} — /jarvis lists them"
        listing = "; ".join(self._row_line(service, row) for row in hits[:5])
        return None, f"{wanted!r} matches {len(hits)} sessions — be exact: {listing}"

    async def _pause(
        self, service: JarvisService, binding: Binding, wanted: str, *, handback: bool
    ) -> str:
        from snowpea_core.jarvis.delegation import Caller, DelegationError, PersonOnly
        from snowpea_core.jarvis.service import JarvisError

        try:
            row, why = self._find(service, wanted)
        except JarvisError as exc:
            return f"Jarvis: {exc}"
        if row is None:
            return why
        caller = Caller.messenger(binding.platform, bound_person=True)
        name = (row.title or "").strip() or (row.project or row.sessionId or "session")
        engine = service.engine
        try:
            if handback:
                engine.handback(row.sessionId, external=row.external, caller=caller)
            else:
                engine.takeover(
                    row.sessionId, external=row.external, project=row.project, caller=caller
                )
        except (PersonOnly, DelegationError) as exc:
            return f"Jarvis: {exc}"
        with contextlib.suppress(Exception):
            await service.settle()
        if handback:
            return f"{clip(name, 60)}: 자비스에게 돌려줬습니다 / handed back to Jarvis"
        return (
            f"{clip(name, 60)}: 직접 맡습니다 — 자비스는 여기서 결정하지 않습니다 / you have "
            "it; Jarvis decides nothing there until /jarvis handback"
        )


__all__ = [
    "COMMANDS_HELP",
    "NOTICE_MIN_GAP_SEC",
    "NOTICE_WINDOW_SEC",
    "MessengerBridge",
    "MessengerConfirmation",
]
