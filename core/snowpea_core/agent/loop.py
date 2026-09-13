"""The agent turn (contract §8).

One turn is: prompt -> provider stream -> tool calls -> provider stream -> …
until the model answers without calling a tool.  Everything the turn learns is
published as a ``session.event``, so a client that only watches the event
stream sees the whole thing.

A turn never raises into its caller: policy denials, approval denials,
interrupts and bugs all end in ``turn.done`` with the matching reason.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import AgentConfig, build_messages
from snowpea_core.attachments import pending
from snowpea_core.config.settings import THINKING_CHOICES
from snowpea_core.exec.local import LocalBackend
from snowpea_core.memory import context_for_turn, nudge_after_turn
from snowpea_core.permissions.policy import UNPROMOTABLE, PermissionPolicy
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import context_windows
from snowpea_core.providers.base import ChatMessage, ProviderError, ToolCall
from snowpea_core.server import errors
from snowpea_core.session import compaction, events
from snowpea_core.session.history import message_to_json
from snowpea_core.skills import hooks as plugin_hooks
from snowpea_core.tools.registry import (
    Tool,
    ToolContext,
    ToolResult,
    effective_permission,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent")


#: Refusals tolerated in one turn before it ends with reason ``"denied"``.
MAX_DENIALS_PER_TURN = 3

#: How many times one assistant turn may be resumed after the model stopped at
#: the output limit.  Two is enough for a long review and still bounded
#: (CORE-reasoning-budget).
MAX_CONTINUATIONS = 2

#: What the model is told when its answer was cut off mid-sentence.
CONTINUE_INSTRUCTION = "Continue exactly where you stopped, without repeating."

#: Shortest gap between two ``message.reasoning`` events, in seconds.
#:
#: Reasoning arrives a fragment at a time and carries a running character
#: count, so publishing every fragment costs every attached surface a repaint
#: to move a number nobody reads that closely.  Fragments inside one window are
#: concatenated and sent as one event (CORE-reasoning-budget).
REASONING_EMIT_INTERVAL = 0.15


@dataclass(frozen=True)
class QueuedTurn:
    """Everything one prompt needs after it has left the RPC handler."""

    turn_id: str
    text: str
    unattended: bool
    attachments: list[Any]


def new_turn_id() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


async def finish_turn(core: Core, session: Session, turn_id: str, reason: str) -> str:
    """Close one turn: the context reading first, then ``turn.done``.

    Order matters.  ``turn.done`` is what every surface treats as "the turn is
    over" — the headless CLI stops reading on it and closes the session — so an
    event emitted after it is one no consumer is guaranteed to see.  The
    ``context`` reading therefore goes out just before it (CORE-context).
    Accounting must never fail a turn, and it is skipped entirely once
    shutdown has begun, for the same reason the final write is skipped in
    :func:`run_turn` (CORE-session-race).
    """
    if not getattr(core, "stopping", False):
        store = getattr(core, "store", None)
        if store is not None:
            try:
                await store.replace_messages(
                    session.id,
                    [
                        {"role": message.role, "content": message_to_json(message)}
                        for message in session.history.snapshot()
                    ],
                )
            except Exception:  # noqa: BLE001 - persistence must not fail a turn
                log.debug("could not persist history for %s", session.id, exc_info=True)
        try:
            await compaction.emit_context(core, session, discover=False)
        except Exception:  # noqa: BLE001 - accounting must not fail a turn
            log.debug("could not emit the context event for %s", session.id, exc_info=True)
    await core.hub.emit_event(session.id, events.turn_done(turn_id, reason))
    return reason


def backend_for(core: Core, session: Session) -> Any:
    """Execution backend for a session; ``backend.set`` / ``/backend`` swaps it."""
    return session.backend or LocalBackend(session.workdir)


def agent_config(core: Core, session: Session | None = None) -> AgentConfig:
    """Tool rounds, output budget and the thinking switch for one turn.

    The budget and the switch are per-vendor, so they are read off the
    provider registry (which owns ``settings.providers.<vendor>``) whenever a
    session says which vendor it talks to.  ``"auto"`` thinking resolves here
    and nowhere else: on for a session someone is watching, off for a
    delegated one, where the report *is* the output and hidden reasoning only
    eats the budget (CORE-reasoning-budget).
    """
    settings = core.settings
    max_tokens = settings.agent.max_tokens
    thinking = settings.agent.thinking
    registry = getattr(core, "providers", None)
    if registry is not None:
        try:
            vendor = (session.provider if session else None) or registry.default_vendor()
            max_tokens = registry.max_tokens_for(vendor, session.model if session else None)
            thinking = registry.thinking_for(vendor)
        except ProviderError:
            # An unknown or unconfigured vendor is the turn's problem to
            # report, not the budget's; the global settings still apply.
            log.debug("could not resolve the output budget", exc_info=True)
    definition_choice = getattr(session, "thinking", None) if session else None
    if definition_choice in THINKING_CHOICES:
        thinking = definition_choice
    if thinking not in ("on", "off"):
        thinking = "off" if session is not None and session.is_subagent else "on"
    return AgentConfig(
        max_tool_rounds=max(1, settings.agent.max_tool_rounds),
        max_tokens=max(1, int(max_tokens)),
        thinking=thinking,
    )


@dataclass
class _Attempt:
    """What one (possibly resumed) assistant turn produced."""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    #: Characters of hidden reasoning; published, never stored.
    reasoning_chars: int = 0
    reasoning_tokens: int = 0
    #: How many times the answer was resumed after an output-limit stop.
    continuations: int = 0
    interrupted: bool = False

    @property
    def truncated(self) -> bool:
        """True when the answer still ends at the output limit."""
        return self.stop_reason == "max_tokens"


async def _stream_once(
    core: Core,
    session: Session,
    provider: Any,
    messages: list[ChatMessage],
    specs: list[Any],
    *,
    max_tokens: int,
    thinking: str,
    reasoning_base: int = 0,
) -> _Attempt:
    """One provider call, drained into an :class:`_Attempt`."""
    hub = core.hub
    attempt = _Attempt()
    chunks: list[str] = []
    # ``thinking`` is offered only to an adapter that declares a switch, so a
    # provider written before the option existed (or a test double) keeps its
    # two-argument signature.
    extra: dict[str, Any] = (
        {"thinking": thinking} if getattr(provider, "supports_thinking_option", False) else {}
    )

    # Thinking is streamed a fragment at a time, and every fragment published
    # is a repaint in every attached surface — for a minutes-long think, a
    # hundred a second, all to move one character count. They are batched into
    # ``REASONING_EMIT_INTERVAL`` windows instead: the first fragment goes out
    # at once so "Thinking…" appears immediately, the rest are concatenated,
    # and the count each window carries is the running total, so a surface that
    # only reads ``chars`` is never behind.
    pending_reasoning: list[str] = []
    last_reasoning_at = 0.0

    async def flush_reasoning() -> None:
        nonlocal last_reasoning_at
        if not pending_reasoning:
            return
        text = "".join(pending_reasoning)
        pending_reasoning.clear()
        last_reasoning_at = monotonic()
        await hub.emit_event(
            session.id,
            events.message_reasoning(text, reasoning_base + attempt.reasoning_chars),
        )

    async for event in provider.stream(messages, specs, max_tokens=max_tokens, **extra):
        if session.interrupt.is_set():
            attempt.interrupted = True
            break
        if event.kind == "text_delta" and event.text:
            # The answer has started, so whatever thinking led to it is over.
            await flush_reasoning()
            chunks.append(event.text)
            await hub.emit_event(session.id, events.message_delta(event.text))
        elif event.kind == "reasoning_delta" and event.text:
            # Hidden thinking joins neither the answer nor the history: it is
            # published so a surface can say the model is working, and how
            # much of the budget the working has already taken.
            attempt.reasoning_chars += len(event.text)
            pending_reasoning.append(event.text)
            if monotonic() - last_reasoning_at >= REASONING_EMIT_INTERVAL:
                await flush_reasoning()
        elif event.kind == "tool_call" and event.tool_call is not None:
            await flush_reasoning()
            attempt.calls.append(event.tool_call)
        elif event.kind == "usage" and event.usage is not None:
            # The vendor's own prompt count beats any local estimate.
            attempt.reasoning_tokens += event.usage.reasoning_tokens
            compaction.record_provider_usage(session, event.usage.input_tokens)
            await hub.emit_event(
                session.id,
                events.usage(event.usage.input_tokens, event.usage.output_tokens),
            )
        elif event.kind == "done":
            if event.error:
                await hub.emit_event(session.id, events.error(errors.INTERNAL, event.error))
            attempt.stop_reason = event.stop_reason or "end_turn"
    # Nothing thought is dropped, including by an interrupt: the last window is
    # always published, so the totals a surface shows match what was counted.
    await flush_reasoning()
    attempt.text = "".join(chunks)
    return attempt


async def _model_turn(
    core: Core,
    session: Session,
    provider: Any,
    messages: list[ChatMessage],
    specs: list[Any],
    config: AgentConfig,
) -> _Attempt:
    """One assistant turn, including the recovery from an output-limit stop.

    A reasoning model can spend the whole budget thinking and answer nothing,
    and a long review can stop mid-sentence.  Neither used to be visible: the
    turn simply ended with an empty or half-written message (CORE-reasoning-
    budget).  Two recoveries, in this order:

    * nothing visible and reasoning tokens burned -> ask again once with
      thinking off, or, when the provider has no such switch, with twice the
      budget;
    * something visible -> resume it, at most :data:`MAX_CONTINUATIONS` times,
      and join the pieces.
    """
    attempt = await _stream_once(
        core,
        session,
        provider,
        messages,
        specs,
        max_tokens=config.max_tokens,
        thinking=config.thinking,
    )
    if attempt.interrupted or not attempt.truncated or attempt.calls:
        return attempt

    if not attempt.text.strip() and attempt.reasoning_tokens > 0:
        spent = attempt.reasoning_tokens
        can_stop_thinking = (
            bool(getattr(provider, "supports_thinking_option", False))
            and config.thinking != "off"
        )
        if can_stop_thinking:
            log.info(
                "turn spent its whole %d-token budget on reasoning (%d tokens); "
                "retrying with thinking off",
                config.max_tokens,
                spent,
            )
            budget, thinking = config.max_tokens, "off"
        else:
            budget = context_windows.clamp_output_tokens(
                session.model, config.max_tokens * 2
            )
            thinking = config.thinking
            log.info(
                "turn spent its whole %d-token budget on reasoning (%d tokens); "
                "retrying with %d",
                config.max_tokens,
                spent,
                budget,
            )
        retry = await _stream_once(
            core,
            session,
            provider,
            messages,
            specs,
            max_tokens=budget,
            thinking=thinking,
            reasoning_base=attempt.reasoning_chars,
        )
        retry.reasoning_chars += attempt.reasoning_chars
        retry.reasoning_tokens += attempt.reasoning_tokens
        attempt = retry
        if attempt.interrupted or not attempt.truncated or attempt.calls:
            return attempt

    while (
        attempt.truncated
        and attempt.text.strip()
        and not attempt.calls
        and not attempt.interrupted
        and attempt.continuations < MAX_CONTINUATIONS
    ):
        log.info("answer hit the output limit; continuing (%d)", attempt.continuations + 1)
        # The partial answer is handed back as the assistant turn it was, with
        # the instruction as the next user message.  It is a local list: the
        # session history only ever sees the joined text, so an interrupted
        # continuation cannot leave half an answer behind.
        resumed = await _stream_once(
            core,
            session,
            provider,
            [
                *messages,
                ChatMessage(role="assistant", content=attempt.text),
                ChatMessage(role="user", content=CONTINUE_INSTRUCTION),
            ],
            specs,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
            reasoning_base=attempt.reasoning_chars,
        )
        attempt = _Attempt(
            text=attempt.text + resumed.text,
            calls=resumed.calls,
            stop_reason=resumed.stop_reason,
            reasoning_chars=attempt.reasoning_chars + resumed.reasoning_chars,
            reasoning_tokens=attempt.reasoning_tokens + resumed.reasoning_tokens,
            continuations=attempt.continuations + 1,
            interrupted=resumed.interrupted,
        )
    return attempt


def start_turn(core: Core, session: Session, text: str, *, unattended: bool = False) -> str:
    """Schedule a turn, or queue it behind the session's active turn.

    A user can keep typing while tools or subagents are running.  Those
    follow-ups must not start overlapping provider loops against the same
    history.  Capture attachments synchronously, enqueue the complete prompt,
    and let one task drain the session FIFO.
    """
    turn_id = new_turn_id()
    queued = QueuedTurn(
        turn_id=turn_id,
        text=text,
        unattended=unattended,
        attachments=pending.take(session.id),
    )
    task = session.turn_task
    if task is not None and not task.done():
        session.queued_turns.append(queued)
        # The prompt was accepted but will not start yet; say so, or the user
        # has no way to tell it from a dropped keystroke (CORE-fixes-v017 R5).
        waiting = len(session.queued_turns)
        _emit_soon(core, session, events.turn_queued(turn_id, waiting, waiting))
        return turn_id
    session.turn_task = asyncio.ensure_future(_drain_turns(core, session, queued))
    return turn_id


def _emit_soon(core: Core, session: Session, event: events.Event) -> None:
    """Emit from a synchronous caller, preserving submission order."""
    asyncio.ensure_future(core.hub.emit_event(session.id, event))


async def flush_queued_turns(core: Core, session: Session) -> list[str]:
    """Drop every queued prompt and tell the surfaces which ones went.

    ``session.interrupt`` means "stop what I asked for", and that has to
    include the follow-ups still waiting behind the running turn — otherwise
    Stop is followed by the queue draining anyway (CORE-fixes-v017 R3).  Each
    dropped prompt also gets its own ``turn.done`` so a client awaiting that
    turn id is not left hanging.

    The copy-and-clear is synchronous, before the first ``await``, so exactly
    the prompts that were waiting when Stop was pressed are dropped and one
    typed a moment later is not.
    """
    dropped = list(session.queued_turns)
    session.queued_turns.clear()
    for index, queued in enumerate(dropped):
        remaining = len(dropped) - index - 1
        await core.hub.emit_event(
            session.id, events.turn_dequeued(queued.turn_id, "dropped", remaining)
        )
        await core.hub.emit_event(session.id, events.turn_done(queued.turn_id, "interrupted"))
    return [queued.turn_id for queued in dropped]


async def _drain_turns(core: Core, session: Session, first: QueuedTurn) -> None:
    """Run ``first`` and every follow-up received during it, in FIFO order."""
    queued = first
    try:
        while True:
            session.interrupt.clear()
            session.current_turn = queued.turn_id
            await run_turn(
                core,
                session,
                queued.text,
                turn_id=queued.turn_id,
                unattended=queued.unattended,
                attachments=queued.attachments,
            )
            # The queue is *not* re-flushed here.  ``session.interrupt`` already
            # emptied it synchronously, at the instant Stop was pressed; a
            # prompt that arrived after that is new user intent and must still
            # run, not be swallowed by the interrupt that preceded it.
            if not session.queued_turns:
                break
            queued = session.queued_turns.pop(0)
            await core.hub.emit_event(
                session.id,
                events.turn_dequeued(queued.turn_id, "started", len(session.queued_turns)),
            )
    finally:
        session.current_turn = None


async def run_turn(
    core: Core,
    session: Session,
    text: str,
    *,
    turn_id: str | None = None,
    unattended: bool = False,
    attachments: list[Any] | None = None,
) -> str:
    """Run one full turn; returns its turn id once ``turn.done`` was emitted."""
    turn_id = turn_id or new_turn_id()
    session.current_turn = turn_id
    hub = core.hub
    try:
        reason = await _drive(core, session, text, turn_id, unattended, attachments)
    except asyncio.CancelledError:
        # A shutdown in progress (``Daemon.stop`` -> ``SessionManager.close_all``,
        # CORE-session-race) cancels every in-flight turn task; by the time that
        # happens the session store is about to close (or already has), so the
        # final event write is skipped rather than raced against it. A normal
        # cancellation (e.g. ``session.close`` mid-turn) still emits it.
        if not getattr(core, "stopping", False):
            await finish_turn(core, session, turn_id, "interrupted")
        raise
    except ProviderError as exc:
        await hub.emit_event(session.id, events.error(exc.code, str(exc)))
        reason = "error"
        await finish_turn(core, session, turn_id, reason)
    except Exception as exc:  # noqa: BLE001 - a bug ends the turn, never the daemon
        log.exception("turn %s failed", turn_id)
        await hub.emit_event(
            session.id, events.error(errors.INTERNAL, f"{type(exc).__name__}: {exc}")
        )
        reason = "error"
        await finish_turn(core, session, turn_id, reason)
    finally:
        # The queue runner owns the transition between adjacent turns.  A
        # direct ``run_turn`` caller still gets the traditional cleanup.
        if session.turn_task is not asyncio.current_task():
            session.current_turn = None
    return turn_id


async def speak_reply(core: Core, session: Session, text: str) -> None:
    """Say the reply out loud when ``audio.tts.autoSpeak`` is on.

    Never raises and never blocks the turn's outcome: a missing backend, a
    broken player or a synthesiser that times out all end as a log line and an
    unspoken reply.  The ``audio.spoken`` event carries the file either way, so
    a surface can play it when the daemon itself has no speakers.
    """
    body = (text or "").strip()
    if not body:
        return
    from snowpea_core.audio.player import AudioError
    from snowpea_core.audio.player import play as play_audio
    from snowpea_core.server.audio_handlers import audio_config, audio_dir_for, speech_caller

    try:
        config = audio_config(core)
        if not config.auto_speak:
            return
        caller = speech_caller(core)
        provider = config.tts(caller)
        if provider is None:
            log.debug("autoSpeak is on but no speech backend is available")
            return
        speech = await provider.synthesize(
            body, out_dir=audio_dir_for(core, session.id), voice=config.voice
        )
        played = False
        try:
            await play_audio(speech.path, preferred=config.player)
            played = True
        except AudioError as exc:
            log.info("autoSpeak could not play locally: %s", exc)
        await core.hub.emit_event(
            session.id,
            events.audio_spoken(
                path=str(speech.path),
                mime=speech.mime,
                provider=speech.provider,
                played=played,
                voice=speech.voice,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - speaking must never fail a turn
        log.info("autoSpeak failed: %s", exc)


async def _drive(
    core: Core,
    session: Session,
    text: str,
    turn_id: str,
    unattended: bool,
    attachments: list[Any] | None = None,
) -> str:
    """The loop proper; emits ``turn.done`` itself and returns its reason."""
    hub = core.hub
    config = agent_config(core, session)
    policy: PermissionPolicy = core.policy
    backend = backend_for(core, session)
    provider = core.providers.get(session.provider, session.model)

    # Auto-compaction happens here and nowhere else: between turns, before the
    # new user message joins the history, and never inside the tool loop
    # (CORE-context).
    await compaction.maybe_auto_compact(core, session)

    # Whatever ``session.prompt`` validated and stored for this turn; taking it
    # here (rather than passing it down) keeps an interrupted turn from leaking
    # its images into the next one (CORE-multimodal).
    attachments = pending.take(session.id) if attachments is None else attachments
    if text or attachments:
        session.history.append(
            ChatMessage(
                role="user",
                content=content_parts.history_blocks(text, attachments) if attachments else text,
            )
        )
        session.history.compact()

    # Recall once per turn, on the user's own words (M5 contract §1).
    memory_block = await context_for_turn(core, session, text)
    denials = 0

    for _round in range(config.max_tool_rounds):
        if session.interrupt.is_set():
            await finish_turn(core, session, turn_id, "interrupted")
            return "interrupted"

        specs = core.tools.specs(session)
        messages = build_messages(session, specs, memory_block, core=core)
        attempt = await _model_turn(core, session, provider, messages, specs, config)
        calls = attempt.calls

        if attempt.interrupted:
            await finish_turn(core, session, turn_id, "interrupted")
            return "interrupted"

        assistant_text = attempt.text
        if not calls:
            session.history.append(ChatMessage(role="assistant", content=assistant_text))
            await hub.emit_event(
                session.id,
                events.message_done(
                    assistant_text,
                    truncated=attempt.truncated,
                    continuations=attempt.continuations,
                ),
            )
            await speak_reply(core, session, assistant_text)
            await finish_turn(core, session, turn_id, "complete")
            await nudge_after_turn(core, session, text)
            await plugin_hooks.stop(core, session)
            return "complete"

        session.history.append(
            ChatMessage(role="assistant", content=assistant_text, tool_calls=list(calls))
        )
        for call in calls:
            outcome = await _run_one_call(core, session, backend, policy, call, turn_id, unattended)
            if outcome == "denied":
                # The refusal went back to the model as a tool result; it gets
                # to choose something else.  A model that only ever retries the
                # refused call still cannot burn the round budget.
                denials += 1
                if denials >= MAX_DENIALS_PER_TURN:
                    await finish_turn(core, session, turn_id, "denied")
                    return "denied"
                continue
            if outcome is not None:
                return outcome
        session.history.compact()

    await hub.emit_event(
        session.id,
        events.error(errors.INTERNAL, f"stopped after {config.max_tool_rounds} tool rounds"),
    )
    await finish_turn(core, session, turn_id, "error")
    return "error"


async def _run_one_call(
    core: Core,
    session: Session,
    backend: Any,
    policy: PermissionPolicy,
    call: ToolCall,
    turn_id: str,
    unattended: bool,
) -> str | None:
    """Permission-check and run one tool call.

    Returns ``None`` to continue the turn, or the ``turn.done`` reason when the
    call ended it (a denial).
    """
    hub = core.hub
    tool: Tool | None = core.tools.get(call.name)
    if tool is None:
        await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
        await _fail_call(core, session, call, f"unknown tool: {call.name}")
        return None
    if tool.state != "active":
        await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
        await _fail_call(core, session, call, f"tool {call.name} is inactive")
        return None
    allowed = getattr(session, "allowed_tools", None)
    if allowed is not None and call.name not in allowed:
        await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
        await _fail_call(core, session, call, f"{call.name} is not in this skill's allowed-tools")
        return None

    # A write that lands on snowpea's own settings is judged as ``config``,
    # not ``write`` (CORE-search-fix): the tag, not the tool, decides.
    tag = effective_permission(tool, call.arguments, session, core)
    verdict = policy.decide(session.mode, tag, tool, call.arguments, session)
    if verdict == "deny":
        message = f"{tool.name} ({tag}) is not allowed in {session.mode} mode"
        await hub.emit_event(session.id, events.error(errors.MODE_DENIED, message))
        await _deny_call(core, session, call, message)
        return "denied"
    if verdict == "ask":
        decision = await core.approvals.request(
            session,
            tool.name,
            call.arguments,
            risk=policy.risk(tag),
            unattended=unattended,
            cancel_event=session.interrupt,
            note=policy.note(tag),
            cacheable=tag not in UNPROMOTABLE,
        )
        if session.interrupt.is_set():
            await finish_turn(core, session, turn_id, "interrupted")
            return "interrupted"
        if not decision.allowed:
            code = decision.code or errors.APPROVAL_DENIED
            await hub.emit_event(
                session.id,
                events.error(code, f"{tool.name} was not approved ({decision.by})"),
            )
            await _deny_call(core, session, call, f"the user declined {tool.name}")
            return "denied"

    await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
    # Plugin hooks (M6 contract §1): PreToolUse may refuse the call with exit 2.
    blocked = await plugin_hooks.pre_tool_use(core, session, call.name, dict(call.arguments))
    if blocked is not None:
        await _fail_call(core, session, call, f"{plugin_hooks.BLOCKED_PREFIX}: {blocked}")
        return None
    ctx = ToolContext(session=session, core=core, backend=backend)
    try:
        result = await tool.run(ctx, dict(call.arguments))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken tool is a failed call
        log.exception("tool %s raised", tool.name)
        result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    await plugin_hooks.post_tool_use(core, session, call.name, dict(call.arguments))

    await hub.emit_event(
        session.id,
        events.tool_result(call.id, call.name, result.ok, result.output, result.error),
    )
    if result.diff:
        await hub.emit_event(session.id, events.diff(result.path or "", result.diff))
    session.history.append(
        ChatMessage(
            role="tool",
            content=result.output if result.ok else (result.error or "tool failed"),
            tool_call_id=call.id,
            name=call.name,
        )
    )
    return None


async def _deny_call(core: Core, session: Session, call: ToolCall, reason: str) -> None:
    """Tell the model a call was refused, so it can adapt inside the same turn.

    A refusal used to end the turn outright, which left the model unable to
    learn anything from it and made plan mode merely restrictive rather than
    usable (CORE-prompts, gap 3).  It now comes back as a tool result, the way
    any other failed call does.
    """
    message = f"Denied: {reason}. Choose a different action; do not retry the same call."
    if session.mode == "plan":
        message += " In plan mode, finish by describing what you would do instead."
    await _fail_call(core, session, call, message)


async def _fail_call(core: Core, session: Session, call: ToolCall, message: str) -> None:
    """Report a call that never ran and feed the reason back to the model."""
    await core.hub.emit_event(
        session.id, events.tool_result(call.id, call.name, False, "", message)
    )
    session.history.append(
        ChatMessage(role="tool", content=message, tool_call_id=call.id, name=call.name)
    )


__all__ = [
    "agent_config",
    "backend_for",
    "new_turn_id",
    "run_turn",
    "speak_reply",
    "start_turn",
]
