"""Context accounting and conversation compaction (CORE-context).

Two jobs live here.

**Accounting.**  :func:`measure` estimates what the next prompt costs — system
prompt, history and tool results — and :func:`emit_context` publishes that as
a ``context`` event.  After a provider call the turn hands the vendor-reported
``input_tokens`` to :func:`record_provider_usage`, which is authoritative for
that turn: an estimate is a fallback, never a correction of the real number.

**Compaction.**  :func:`compact_session` asks the session's own provider for a
summary of the conversation, then replaces the history with that summary plus
the last few messages verbatim.  The swap is one call to
:meth:`~snowpea_core.session.history.History.replace`, so no turn ever sees a
half-replaced history.  :func:`maybe_auto_compact` runs the same thing between
turns once the prompt passes ``context.autoCompactPercent`` of the window —
between turns only, because dropping the message a pending tool result refers
to would leave the model answering a call it can no longer see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import build_messages
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage, ProviderError
from snowpea_core.session import events
from snowpea_core.session.history import estimate_messages

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.context")

#: Messages kept verbatim after the summary when settings say nothing.
DEFAULT_KEEP_LAST = 4

#: Ceiling on the summary itself, in tokens.
SUMMARY_MAX_TOKENS = 2048

#: Marker that opens the summary message, so a resumed session can recognise it.
SUMMARY_HEADING = "Session summary"

SUMMARY_SYSTEM_PROMPT = load("workflows/compaction")


@dataclass
class ContextState:
    """What a surface needs to draw ``used / window``."""

    used: int = 0
    window: int | None = None
    estimated: bool = True

    @property
    def percent(self) -> float | None:
        if not self.window:
            return None
        return round(self.used * 100.0 / self.window, 1)


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def _keep_last(core: Core) -> int:
    value = getattr(core.settings.context, "keepLastMessages", DEFAULT_KEEP_LAST)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_LAST


def prompt_messages(core: Core, session: Session) -> list[ChatMessage]:
    """The messages a turn would send right now, system prompt included."""
    try:
        specs = core.tools.specs(session)
    except Exception:  # noqa: BLE001 - accounting must not break on a bad tool
        log.debug("could not list tool specs for context accounting", exc_info=True)
        specs = []
    return build_messages(session, specs, core=core)


def resolved_identity(core: Core, session: Session) -> tuple[str | None, str | None]:
    """``(vendor, model)`` the session would actually use, not what it pinned.

    A session that never chose either still runs against something, and that
    is what the HUD has to name next to the window.
    """
    try:
        vendor = session.provider or core.providers.default_vendor()
    except Exception:  # noqa: BLE001 - naming is cosmetic, never fatal
        return session.provider, session.model
    try:
        model = core.providers.model_for(vendor, session.model) or session.model
    except Exception:  # noqa: BLE001
        model = session.model
    return vendor, model


def window_for(core: Core, session: Session) -> int | None:
    """Context window of the session's model, from cache and the static table."""
    vendor = session.provider or core.providers.default_vendor()
    try:
        return core.providers.context_window(vendor, session.model)
    except Exception:  # noqa: BLE001 - an unknown vendor is simply unknown
        log.debug("no context window for %s/%s", vendor, session.model, exc_info=True)
        return None


async def resolve_window(core: Core, session: Session) -> int | None:
    """Like :func:`window_for`, but lets a ``local`` server be asked once."""
    vendor = session.provider or core.providers.default_vendor()
    try:
        return await core.providers.resolve_context_window(vendor, session.model)
    except Exception:  # noqa: BLE001 - discovery failures degrade to unknown
        log.debug("context window discovery failed for %s", vendor, exc_info=True)
        return None


def measure(core: Core, session: Session) -> ContextState:
    """Estimate the current prompt size against the model's window."""
    used = estimate_messages(prompt_messages(core, session))
    return ContextState(used=used, window=window_for(core, session), estimated=True)


def record_provider_usage(session: Session, input_tokens: int) -> None:
    """Adopt the vendor's own prompt-token count for this turn.

    The provider knows exactly what it billed; once it has said so, the local
    estimate is not used again until the history changes.
    """
    if input_tokens <= 0:
        return
    session.context_used = int(input_tokens)
    session.context_estimated = False


def state_for(core: Core, session: Session) -> ContextState:
    """Current accounting for ``session``, provider-reported where possible."""
    window = window_for(core, session)
    if session.context_used and not session.context_estimated:
        return ContextState(used=session.context_used, window=window, estimated=False)
    used = estimate_messages(prompt_messages(core, session))
    return ContextState(used=used, window=window, estimated=True)


async def emit_context(
    core: Core,
    session: Session,
    state: ContextState | None = None,
    *,
    discover: bool = True,
) -> ContextState:
    """Publish one ``context`` event and remember what it said.

    ``discover=False`` keeps the call to a single await (the emit itself) by
    using only the cached and static windows. The turn-ending path passes it:
    that path also runs while a turn is being cancelled, and an extra await
    there could take the cancellation before ``turn.done`` is sent.
    :func:`maybe_auto_compact` has already warmed the cache at the top of the
    turn, so nothing is lost.
    """
    resolved = state or state_for(core, session)
    if resolved.window is None and discover:
        resolved.window = await resolve_window(core, session)
    session.context_used = resolved.used
    session.context_estimated = resolved.estimated
    session.context_window = resolved.window
    vendor, model = resolved_identity(core, session)
    await core.hub.emit_event(
        session.id,
        events.context(
            resolved.used,
            resolved.window,
            estimated=resolved.estimated,
            model=model,
            provider=vendor,
        ),
    )
    return resolved


# ---------------------------------------------------------------------------
# compaction
# ---------------------------------------------------------------------------


def split_index(messages: list[ChatMessage], keep_last: int) -> int:
    """Index at which the verbatim tail may start without orphaning a call.

    A tool result whose assistant message has been summarised away confuses
    every vendor, so the boundary is walked forward past any leading ``tool``
    message.  Returns ``len(messages)`` when nothing can be kept.
    """
    if keep_last <= 0:
        return len(messages)
    index = max(0, len(messages) - keep_last)
    while index < len(messages) and messages[index].role == "tool":
        index += 1
    return index


def render_conversation(messages: list[ChatMessage]) -> str:
    """The conversation as plain text for the summariser to read."""
    from snowpea_core.session.history import message_text

    lines: list[str] = []
    for message in messages:
        body = message_text(message).strip()
        if not body:
            continue
        label = message.role.upper()
        if message.role == "tool" and message.name:
            label = f"TOOL({message.name})"
        lines.append(f"{label}: {body}")
    return "\n\n".join(lines)


async def summarise(
    core: Core, session: Session, messages: list[ChatMessage], instructions: str | None
) -> str:
    """Ask the session's provider for the summary text.

    A provider failure is not fatal: the caller falls back to a mechanical
    summary rather than leaving the user with an over-full window.
    """
    transcript = render_conversation(messages)
    if not transcript:
        return ""
    ask = "Summarise the conversation below.\n\n" + transcript
    if instructions:
        ask = (
            f"Summarise the conversation below. Extra instructions: {instructions}"
            f"\n\n{transcript}"
        )
    provider = core.providers.get(session.provider, session.model)
    chunks: list[str] = []
    stream = provider.stream(
        [
            ChatMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=ask),
        ],
        [],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    async for event in stream:
        if event.kind == "text_delta" and event.text:
            chunks.append(event.text)
    return "".join(chunks).strip()


def fallback_summary(messages: list[ChatMessage]) -> str:
    """What to keep when the provider could not summarise: the bare facts."""
    roles: dict[str, int] = {}
    tools: list[str] = []
    for message in messages:
        roles[message.role] = roles.get(message.role, 0) + 1
        for call in message.tool_calls or []:
            if call.name not in tools:
                tools.append(call.name)
    counted = ", ".join(f"{count} {role}" for role, count in sorted(roles.items()))
    lines = [f"{len(messages)} earlier messages were dropped ({counted})."]
    if tools:
        lines.append("Tools used: " + ", ".join(tools) + ".")
    lines.append("The summary itself could not be generated; ask the user if context is missing.")
    return "\n".join(lines)


def summary_message(text: str) -> ChatMessage:
    """Wrap summary text in the system message that replaces the history."""
    return ChatMessage(role="system", content=f"{SUMMARY_HEADING}\n\n{text}")


@dataclass
class CompactionResult:
    """What one compaction did, in estimated tokens."""

    before: int = 0
    after: int = 0
    summary_chars: int = 0
    kept: int = 0
    compacted: bool = False


async def compact_session(
    core: Core,
    session: Session,
    instructions: str | None = None,
    *,
    auto: bool = False,
) -> CompactionResult:
    """Summarise the conversation and replace the history with it.

    Emits ``compaction``, a ``message.done`` carrying the summary as a system
    message, and a fresh ``context``.  A history too short to be worth
    summarising is left alone and reported as ``compacted=False``.
    """
    keep_last = _keep_last(core)
    messages = session.history.snapshot()
    before = estimate_messages(messages)
    boundary = split_index(messages, keep_last)
    head, tail = messages[:boundary], messages[boundary:]
    if not head:
        return CompactionResult(before=before, after=before, kept=len(tail))

    try:
        text = await summarise(core, session, head, instructions)
    except ProviderError as exc:
        log.warning("compaction summary failed (%s); using the mechanical fallback", exc)
        text = ""
    except Exception:  # noqa: BLE001 - a broken provider must not lose history
        log.exception("compaction summary raised; using the mechanical fallback")
        text = ""
    if not text:
        text = fallback_summary(head)

    replacement = [summary_message(text), *tail]
    session.history.replace(replacement)
    after = estimate_messages(replacement)
    # The prompt is a different size now, so the provider's old count no longer
    # describes it; go back to estimating until the next call reports again.
    session.context_estimated = True

    result = CompactionResult(
        before=before,
        after=after,
        summary_chars=len(text),
        kept=len(tail),
        compacted=True,
    )
    await core.hub.emit_event(
        session.id,
        events.compaction(before, after, len(text), auto=auto, kept=len(tail)),
    )
    await core.hub.emit_event(
        session.id, events.message_done(f"{SUMMARY_HEADING}\n\n{text}", role="system")
    )
    await emit_context(core, session)
    log.info(
        "session %s compacted: ~%d -> ~%d tokens (%s)",
        session.id,
        before,
        after,
        "auto" if auto else "manual",
    )
    return result


def should_auto_compact(core: Core, session: Session, state: ContextState) -> bool:
    """True when ``state`` has passed the configured threshold."""
    settings: Any = core.settings.context
    if not getattr(settings, "autoCompact", True):
        return False
    if state.window is None or state.used <= 0:
        return False
    try:
        threshold = float(getattr(settings, "autoCompactPercent", 85))
    except (TypeError, ValueError):
        threshold = 85.0
    if threshold <= 0 or threshold >= 100:
        return False
    if len(session.history) <= _keep_last(core):
        return False
    percent = state.percent
    return percent is not None and percent >= threshold


async def maybe_auto_compact(core: Core, session: Session) -> CompactionResult | None:
    """Compact before a turn starts when the window is nearly full.

    Called from the top of the agent loop, before the user's new message is
    appended, so the summary never has to describe a message the model has not
    answered yet.
    """
    state = state_for(core, session)
    if state.window is None:
        state.window = await resolve_window(core, session)
    if not should_auto_compact(core, session, state):
        return None
    return await compact_session(core, session, auto=True)


def format_tokens(count: int) -> str:
    """``12345`` -> ``12.3k``; what the compaction divider shows."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


__all__ = [
    "DEFAULT_KEEP_LAST",
    "SUMMARY_HEADING",
    "SUMMARY_SYSTEM_PROMPT",
    "CompactionResult",
    "ContextState",
    "compact_session",
    "emit_context",
    "fallback_summary",
    "format_tokens",
    "maybe_auto_compact",
    "measure",
    "prompt_messages",
    "record_provider_usage",
    "render_conversation",
    "resolved_identity",
    "resolve_window",
    "should_auto_compact",
    "split_index",
    "state_for",
    "summarise",
    "summary_message",
    "window_for",
]
