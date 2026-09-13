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
    """The whole surface the router needs from a platform."""

    platform: str

    async def start(self, on_message: OnMessage) -> None:
        """Begin receiving; must return once the receive loop is running."""

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        """Post ``text`` (optionally with buttons) and return the message id."""

    async def stop(self) -> None:
        """Stop receiving and release the transport."""


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


def question_text(request: dict[str, Any]) -> str:
    """The question as chat text: header, question, numbered options, how to reply."""
    lines: list[str] = []
    header = str(request.get("header") or "").strip()
    total = int(request.get("total") or 1)
    index = int(request.get("index") or 1)
    counter = f" ({index}/{total})" if total > 1 else ""
    lines.append(f"\u2753 {header}{counter}" if header else f"\u2753 질문{counter}")
    lines.append(str(request.get("question") or "").strip())
    options = list(request.get("options") or [])
    for position, option in enumerate(options, start=1):
        label = str(option.get("label", ""))
        description = str(option.get("description") or "").strip()
        lines.append(f"{position}. {label}" + (f" \u2014 {description}" if description else ""))
    if options:
        hint = "번호로 답해 주세요 (예: 1,3)" if request.get("multi") else "번호로 답해 주세요"
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
    "QUESTION_CALLBACK_PREFIX",
    "QUESTION_OTHER",
    "Button",
    "GatewayError",
    "InboundMessage",
    "OnMessage",
    "PlatformAdapter",
    "approval_buttons",
    "approval_callback",
    "approval_text",
    "parse_approval_callback",
    "parse_question_callback",
    "question_buttons",
    "question_callback",
    "question_text",
]
