"""Chat-platform adapter protocol (M5 contract §3).

An adapter knows one platform and nothing about snowpea: it turns whatever the
platform sends into an :class:`InboundMessage` and whatever snowpea says into a
platform message.  Everything above it — sessions, approvals, permissions — is
:class:`~snowpea_core.gateway.router.GatewayRouter`'s business.

Two kinds of inbound traffic share one callback.  A normal chat message carries
``text``; a button press carries ``callback_data`` (``"apr:<requestId>:allow"``)
and is answered by the router without ever reaching the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: Prefix of the ``callback_data`` an approval button carries (contract §4).
APPROVAL_CALLBACK_PREFIX = "apr"
#: Prefix of the ``callback_data`` an ``ask_user`` option button carries.
QUESTION_CALLBACK_PREFIX = "qst"
#: The index reserved for the free-text row, so "Other" is a button too.
QUESTION_OTHER = "other"
#: Prefix of the ``callback_data`` a ``/sessions`` row carries.
SESSION_CALLBACK_PREFIX = "ses"
#: Prefix of the ``callback_data`` a ``/projects`` row carries.
PROJECT_CALLBACK_PREFIX = "prj"


class GatewayError(RuntimeError):
    """An adapter could not talk to its platform."""


@dataclass
class Button:
    """One inline button; ``data`` comes back as :attr:`InboundMessage.callback_data`."""

    text: str
    data: str


@dataclass
class InboundMessage:
    """Something a human sent us on a chat platform."""

    platform: str
    channel_id: str
    user_id: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    message_id: str | None = None
    reply_to: str | None = None
    #: Set when this is a button press rather than a typed message.
    callback_data: str | None = None
    #: Platform handle for answering the press (Telegram's callback query id).
    callback_id: str | None = None


#: What an adapter calls for every inbound message.
OnMessage = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class PlatformAdapter(Protocol):
    """The whole surface the router needs from a platform.

    Two more methods are *optional*, and the router feature-detects them with
    ``getattr`` rather than requiring them here — not every platform has them
    (Slack bots cannot show a typing indicator at all):

    ``async def typing(self, channel_id: str) -> None``
        Show the "…is typing" hint for a few seconds.  See :class:`SupportsTyping`.
    ``async def edit(self, channel_id: str, message_id: str, text: str) -> None``
        Replace the text of a message this bot sent.  See :class:`SupportsEdit`.
    """

    platform: str

    async def start(self, on_message: OnMessage) -> None:
        """Begin receiving; must return once the receive loop is running."""

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        """Post ``text`` (optionally with buttons) and return the message id."""

    async def stop(self) -> None:
        """Stop receiving and release the transport."""


@runtime_checkable
class SupportsTyping(Protocol):
    """An adapter that can show a transient "typing…" hint."""

    async def typing(self, channel_id: str) -> None:
        """Show the indicator once; platforms expire it after a few seconds."""


@runtime_checkable
class SupportsEdit(Protocol):
    """An adapter that can rewrite a message it already sent."""

    async def edit(self, channel_id: str, message_id: str, text: str) -> None:
        """Replace the text of ``message_id`` in ``channel_id``."""


def session_callback(session_id: str) -> str:
    """``ses:<sessionId>`` — what a ``/sessions`` row button carries.

    The id rather than the row number: the list a person is looking at can be
    minutes old, and a number would then resume whatever has since taken that
    position.
    """
    return f"{SESSION_CALLBACK_PREFIX}:{session_id}"


def parse_session_callback(data: str | None) -> str | None:
    """The session id in a ``/sessions`` button callback, or ``None``."""
    if not data:
        return None
    prefix, _, session_id = data.partition(":")
    if prefix != SESSION_CALLBACK_PREFIX or not session_id or ":" in session_id:
        return None
    return session_id


def project_callback(index: int) -> str:
    """``prj:<n>`` — what a ``/projects`` row button carries.

    A position, not a path: a filesystem path does not fit in Telegram's 64
    bytes of callback data, and the list is rebuilt the same way on the press.
    """
    return f"{PROJECT_CALLBACK_PREFIX}:{index}"


def parse_project_callback(data: str | None) -> int | None:
    """The 1-based row number in a ``/projects`` button callback, or ``None``."""
    if not data:
        return None
    prefix, _, index = data.partition(":")
    if prefix != PROJECT_CALLBACK_PREFIX or not index.isdigit():
        return None
    return int(index)


def question_callback(request_id: str, choice: str) -> str:
    """``qst:<requestId>:<index>`` — what an option button carries.

    ``<index>`` is the option's position from 1, or ``other`` for the free-text
    row.  A number, not the label: platform callback payloads are short, and a
    label in Korean would not survive the limit.
    """
    return f"{QUESTION_CALLBACK_PREFIX}:{request_id}:{choice}"


def parse_question_callback(data: str | None) -> tuple[str, str] | None:
    """Split an option callback back into ``(requestId, index-or-"other")``."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != QUESTION_CALLBACK_PREFIX:
        return None
    request_id, choice = parts[1], parts[2]
    if not request_id or not choice:
        return None
    if choice != QUESTION_OTHER and not choice.isdigit():
        return None
    return request_id, choice


def question_buttons(
    request_id: str, options: list[dict[str, Any]], allow_other: bool
) -> list[Button]:
    """One numbered button per option, plus "Other" when free text is allowed.

    The button says the number and the label; the numbers also appear in the
    message body, so a platform that drops the keyboard still leaves the user
    able to reply "2".
    """
    buttons = [
        Button(
            text=f"{index}. {str(option.get('label', ''))[:40]}",
            data=question_callback(request_id, str(index)),
        )
        for index, option in enumerate(options, start=1)
    ]
    if allow_other:
        buttons.append(
            Button(
                text="\u270f\ufe0f 기타 / Other", data=question_callback(request_id, QUESTION_OTHER)
            )
        )
    return buttons


def question_text(item: dict[str, Any], index: int = 1, total: int = 1) -> str:
    """One question as chat text: header, question, numbered options, how to reply.

    A messenger has no tabs, so it walks the batch one message at a time and
    the counter says where in it the user is.
    """
    lines: list[str] = []
    header = str(item.get("header") or "").strip()
    counter = f" ({index}/{total})" if total > 1 else ""
    lines.append(f"\u2753 {header}{counter}" if header else f"\u2753 질문{counter}")
    lines.append(str(item.get("question") or "").strip())
    options = list(item.get("options") or [])
    for position, option in enumerate(options, start=1):
        label = str(option.get("label", ""))
        description = str(option.get("description") or "").strip()
        lines.append(f"{position}. {label}" + (f" \u2014 {description}" if description else ""))
    if options:
        hint = "번호로 답해 주세요 (예: 1,3)" if item.get("multi") else "번호로 답해 주세요"
        lines.append(f"({hint} / reply with the number, or type your own answer)")
    else:
        lines.append("(답을 그대로 적어 주세요 / reply with your answer)")
    return "\n".join(line for line in lines if line)


def approval_callback(request_id: str, decision: str) -> str:
    """``apr:<requestId>:allow|deny`` — what an approval button carries."""
    return f"{APPROVAL_CALLBACK_PREFIX}:{request_id}:{decision}"


def parse_approval_callback(data: str | None) -> tuple[str, str] | None:
    """Split an approval callback back into ``(requestId, decision)``.

    Anything that is not a well-formed ``allow``/``deny`` callback returns
    ``None``, so a stray button press is ignored rather than guessed at.
    """
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != APPROVAL_CALLBACK_PREFIX:
        return None
    request_id, decision = parts[1], parts[2]
    if not request_id or decision not in ("allow", "deny"):
        return None
    return request_id, decision


def approval_buttons(request_id: str) -> list[Button]:
    """The allow/deny pair attached to an unattended approval."""
    return [
        Button(text="✅ allow", data=approval_callback(request_id, "allow")),
        Button(text="⛔ deny", data=approval_callback(request_id, "deny")),
    ]


def approval_text(tool: str, args: dict[str, Any], session_id: str) -> str:
    """Human-readable body of an approval message."""
    command = args.get("command") or args.get("path") or ""
    detail = f"\n{command}" if command else ""
    return f"snowpea wants to run `{tool}` (session {session_id}).{detail}"


__all__ = [
    "APPROVAL_CALLBACK_PREFIX",
    "PROJECT_CALLBACK_PREFIX",
    "QUESTION_CALLBACK_PREFIX",
    "QUESTION_OTHER",
    "SESSION_CALLBACK_PREFIX",
    "Button",
    "GatewayError",
    "InboundMessage",
    "OnMessage",
    "PlatformAdapter",
    "SupportsEdit",
    "SupportsTyping",
    "approval_buttons",
    "approval_callback",
    "approval_text",
    "parse_approval_callback",
    "parse_project_callback",
    "parse_question_callback",
    "parse_session_callback",
    "project_callback",
    "question_buttons",
    "question_callback",
    "question_text",
    "session_callback",
]
