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
import re
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent import context_files
from snowpea_core.agent.agent import AgentConfig, build_messages
from snowpea_core.attachments import pending
from snowpea_core.config.settings import THINKING_CHOICES
from snowpea_core.exec.local import LocalBackend
from snowpea_core.memory import context_for_turn, nudge_after_turn
from snowpea_core.permissions import plan_paths
from snowpea_core.permissions.policy import (
    PLAN_WRITE_TOOLS,
    UNPROMOTABLE,
    PermissionPolicy,
)
from snowpea_core.prompts import environment as prompt_env
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import context_windows
from snowpea_core.providers import effort as effort_scale
from snowpea_core.providers.base import ChatMessage, ProviderError, ToolCall
from snowpea_core.server import errors
from snowpea_core.session import compaction, events
from snowpea_core.session.history import message_to_json
from snowpea_core.skills import hooks as plugin_hooks
from snowpea_core.tools import output_spill
from snowpea_core.tools.registry import (
    ProgressEmitter,
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

#: Tool rounds a delegated child gets when nothing was configured for it.  A
#: worker reads far more than it writes, and the parent only ever sees its
#: final report, so a child's floor is higher than the session default
#: (CORE-subagent-budget).
SUBAGENT_TOOL_ROUNDS = 80

#: What the model is asked for when the round budget runs out.  The call that
#: carries it is made with **no tools**, so the only thing it can produce is
#: the report (CORE-subagent-budget).
BUDGET_INSTRUCTION = (
    "You have used the tool budget for this turn. If the work is finished, give your "
    "final answer now, with no tool call. If it is not, do not call a tool either: report "
    "in plain prose what you did, what you found, what remains and which files you "
    "changed — a checkpoint follows. Do not promise further work in this turn."
)

#: What is fed back after the person chose to keep going at the checkpoint, so
#: the model resumes the work instead of answering its own report.
BUDGET_CONTINUE_INSTRUCTION = (
    "You have another {n} tool rounds. Continue the work from where you stopped."
)

#: Used as the report when the model answered the budget prompt with nothing.
#: A delegation must never come back empty (CORE-subagent-budget).
BUDGET_EMPTY_REPORT = "Stopped after {n} tool rounds without writing a report."

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
    if turn_id in session.finished_turns:
        # Already closed by ``SessionManager.finish_open_turns`` at shutdown;
        # the interrupt it then sets must not make this turn report itself done
        # a second time (CORE-dangling-turns).
        return reason
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


def tool_rounds_for(core: Core, session: Session | None = None) -> int:
    """How many tool rounds one turn of ``session`` may make.

    Highest rung first (CORE-subagent-budget):

    1. the agent definition's ``tool_rounds:`` (carried on the session);
    2. ``agents.toolRounds[<agent name>]`` when the setting is a mapping;
    3. ``agents.toolRounds`` as a number, or its ``"default"`` key;
    4. ``agent.max_tool_rounds``, floored at :data:`SUBAGENT_TOOL_ROUNDS` for a
       delegated child, which reads far more than the session it came from.
    """
    settings = core.settings
    base = max(1, int(settings.agent.max_tool_rounds))
    if session is not None and session.is_subagent:
        base = max(base, SUBAGENT_TOOL_ROUNDS)
    configured: Any = getattr(settings.agents, "toolRounds", None)
    if isinstance(configured, dict):
        name = (getattr(session, "agent", None) or "") if session else ""
        for key in (name, "default", "*"):
            if key and key in configured:
                configured = configured[key]
                break
        else:
            configured = None
    override = getattr(session, "tool_rounds", None) if session else None
    for candidate in (override, configured):
        try:
            rounds = int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if rounds >= 1:
            return rounds
    return base


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
    effort, effort_source = effort_scale.resolve(
        settings, None, None, session_effort=getattr(session, "effort", None) if session else None
    )
    registry = getattr(core, "providers", None)
    if registry is not None:
        try:
            vendor = (session.provider if session else None) or registry.default_vendor()
            max_tokens = registry.max_tokens_for(vendor, session.model if session else None)
            thinking = registry.thinking_for(vendor)
            effort, effort_source = registry.effort_for(
                vendor,
                session.model if session else None,
                session_effort=getattr(session, "effort", None) if session else None,
            )
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
        max_tool_rounds=tool_rounds_for(core, session),
        max_tokens=max(1, int(max_tokens)),
        thinking=thinking,
        effort=effort,
        effort_source=effort_source,
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
    effort: str | None = None,
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
    # Same contract for the effort tier: only an adapter that declares it is
    # sent one, so a provider written before the option existed (or a test
    # double) keeps its old signature (CORE-effort).
    if effort and getattr(provider, "supports_effort_option", False):
        extra["effort"] = effort

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
        effort=config.effort,
    )
    if attempt.interrupted or not attempt.truncated or attempt.calls:
        return attempt

    if not attempt.text.strip() and attempt.reasoning_tokens > 0:
        spent = attempt.reasoning_tokens
        can_stop_thinking = (
            bool(getattr(provider, "supports_thinking_option", False)) and config.thinking != "off"
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
            budget = context_windows.clamp_output_tokens(session.model, config.max_tokens * 2)
            thinking = config.thinking
            log.info(
                "turn spent its whole %d-token budget on reasoning (%d tokens); retrying with %d",
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
            # A turn that burned its budget on thinking is retried with *less*
            # of it, not the same tier again.
            effort="low" if thinking != "off" else config.effort,
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
            effort=config.effort,
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
    waited = False
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
                queued=waited,
            )
            # The queue is *not* re-flushed here.  ``session.interrupt`` already
            # emptied it synchronously, at the instant Stop was pressed; a
            # prompt that arrived after that is new user intent and must still
            # run, not be swallowed by the interrupt that preceded it.
            if not session.queued_turns:
                break
            queued = session.queued_turns.pop(0)
            waited = True
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
    queued: bool = False,
) -> str:
    """Run one full turn; returns its turn id once ``turn.done`` was emitted."""
    turn_id = turn_id or new_turn_id()
    session.current_turn = turn_id
    hub = core.hub
    # The turn is running *now* — after whatever wait it did in the FIFO, and
    # before anything it produces.  Without this a surface has to start its
    # clock on the first delta it happens to overhear (IDE-PROGRESS D1).
    await hub.emit_event(session.id, events.turn_started(turn_id, text or None, queued=queued))
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


#: The acknowledgement is spoken at most this long: it is "yes, I am on it",
#: not a summary, and a paragraph read aloud while the work has already started
#: is worse than silence.
ACK_MAX_CHARS = 240
ACK_MAX_SENTENCES = 2

#: What ``audio.spoken`` calls each kind of utterance.
UTTERANCE_REPLY = "reply"
UTTERANCE_ACK = "ack"

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+|(?<=[다요][.!?])\s*")


def opening_ack(text: str) -> str:
    """The first sentence or two of an opening line, trimmed for speaking.

    The model's prose before its first tool call is already an acknowledgement
    — "네, 알겠습니다. 파일을 읽어서 …하겠습니다" — so the useful part is the
    front of it. Anything past two sentences is the plan, which the user will
    see written down anyway.
    """
    body = " ".join((text or "").split())
    if not body:
        return ""
    parts = [part for part in _SENTENCE_END.split(body) if part]
    spoken = " ".join(parts[:ACK_MAX_SENTENCES]) if parts else body
    if len(spoken) > ACK_MAX_CHARS:
        spoken = spoken[:ACK_MAX_CHARS].rstrip() + "…"
    return spoken


async def speak_ack(core: Core, session: Session, text: str) -> None:
    """Say the turn's opening acknowledgement, once, before the work starts.

    A spoken request answered by a silent minute feels dead, and the agent has
    already written what it is about to do. Only the **first** such line of a
    turn is spoken: the ones between later tool calls are thinking aloud, and
    narrating a whole turn is not what anyone asked for.

    Off with ``audio.tts.speakAck``. Never in a delegated or unattended turn,
    which has nobody in the room to hear it.
    """
    if session.is_subagent or getattr(session, "unattended", False):
        return
    if getattr(session, "ack_spoken", False):
        return
    session.ack_spoken = True  # type: ignore[attr-defined]
    from snowpea_core.server.audio_handlers import audio_config

    try:
        if not bool(_tts_setting(core, "speakAck", True)):
            return
        if not audio_config(core).auto_speak:
            return
    except Exception:  # noqa: BLE001 - reading a setting must not fail a turn
        return
    await speak_reply(core, session, opening_ack(text), utterance=UTTERANCE_ACK)


def _reply_language_of(core: Core, session: Session) -> str | None:
    """What language this session is answering in, for the voice and the engine."""
    from snowpea_core.tools.delegate import delegation_language
    from snowpea_core.tools.registry import ToolContext

    try:
        return delegation_language(
            ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001 - a language guess never fails a turn
        return None


def _tts_setting(core: Core, name: str, default: Any) -> Any:
    """One ``audio.tts.<name>``, read the same lenient way the handlers read it."""
    from snowpea_core.server.audio_handlers import _block, _get

    return _get(_block(_block(core.settings, "audio"), "tts"), name, default)


async def speak_reply(
    core: Core, session: Session, text: str, *, utterance: str = UTTERANCE_REPLY
) -> None:
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
        # The reply's own language picks the voice: one engine can sound like
        # a different person per language, which is the point of the mapping.
        language = _reply_language_of(core, session)
        speech = await provider.synthesize(
            body,
            out_dir=audio_dir_for(core, session.id),
            voice=config.voice_for(language),
            language=language,
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
                utterance=utterance,
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
        # The prompt is published as well as stored: ``session.resume`` replays
        # the event log, so a transcript rebuilt without this shows every answer
        # and none of the questions.  Once per prompt, and never for the
        # continuation nudge the loop appends to itself further down.
        await hub.emit_event(session.id, events.message_user(text, attachments))

    # Recall once per turn, on the user's own words (M5 contract §1).
    memory_block = await context_for_turn(core, session, text)
    denials = 0

    rounds_left = config.max_tool_rounds
    session.rounds_used = 0
    # One acknowledgement per turn, not per tool round.
    session.ack_spoken = False
    while True:
        if rounds_left <= 0:
            # The budget is a checkpoint, not a wall: a long implementing turn
            # legitimately makes hundreds of calls.  Whatever happens next, the
            # turn first writes a report — it used to end on an ``error`` event
            # with no ``message.done`` at all, which left a delegating parent
            # holding an empty summary (CORE-subagent-budget).
            # First a probe with the tools still on the table: a model that is
            # simply done answers without calling anything, and that answer is
            # the turn — no checkpoint (the popup used to appear under a
            # finished reply).  Only a model that still reaches for a tool gets
            # the report-then-ask path.
            probe = await _budget_probe(core, session, provider, config, memory_block)
            if probe is not None and not probe.calls and not probe.interrupted:
                assistant_text = probe.text
                session.history.append(ChatMessage(role="assistant", content=assistant_text))
                await hub.emit_event(
                    session.id,
                    events.message_done(
                        assistant_text,
                        truncated=probe.truncated,
                        continuations=probe.continuations,
                    ),
                )
                await speak_reply(core, session, assistant_text)
                await finish_turn(core, session, turn_id, "complete")
                await nudge_after_turn(core, session, text)
                await plugin_hooks.stop(core, session)
                return "complete"
            await _budget_report(
                core,
                session,
                provider,
                config,
                memory_block,
                probe_text=(probe.text if probe else ""),
            )
            if session.interrupt.is_set():
                # Stop pressed while the report was being written: the report
                # was still published, but nobody is waiting for a question.
                await finish_turn(core, session, turn_id, "interrupted")
                return "interrupted"
            # Only a session someone is watching gets the choice; a delegated
            # or scheduled turn has nobody to ask and ends on its report.
            asks = not unattended and not session.is_subagent
            if not asks or not await _ask_to_continue(core, session, config):
                await finish_turn(core, session, turn_id, "budget")
                return "budget"
            session.history.append(
                ChatMessage(
                    role="user",
                    content=BUDGET_CONTINUE_INSTRUCTION.format(n=config.max_tool_rounds),
                )
            )
            rounds_left = config.max_tool_rounds
        rounds_left -= 1
        session.rounds_used += 1
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

        # The opening line is spoken here, before the tools run: a spoken
        # request answered by a silent minute feels dead, and the model has
        # just written what it is about to do (CORE-multimodal).
        await speak_ack(core, session, assistant_text)

        # No ``message.done`` here, tempting as it is.  The prose *is* finished
        # once the model reaches for a tool, but ``message.done`` is also what
        # the chat gateways forward to Telegram and friends, so one per tool
        # round would start sending a model's intermediate muttering to people's
        # phones.  A surface that needs to know the prose has settled can see it
        # for itself: a tool call after a message means that message will not
        # grow again (see ``tool.call`` in the TUI's store).
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


async def _budget_probe(
    core: Core,
    session: Session,
    provider: Any,
    config: AgentConfig,
    memory_block: str,
) -> _Attempt | None:
    """The model's own verdict at the budget: finished (no calls) or not.

    The instruction is local, like the report's; the tools stay available so
    a model that is done can simply answer.  Never raises.
    """
    try:
        specs = core.tools.specs(session)
        messages = [
            *build_messages(session, specs, memory_block, core=core),
            ChatMessage(role="user", content=BUDGET_INSTRUCTION),
        ]
        return await _model_turn(core, session, provider, messages, specs, config)
    except Exception:  # noqa: BLE001 - fall through to the tools-free report
        log.exception("the tool-budget probe failed for %s", session.id)
        return None


async def _budget_report(
    core: Core,
    session: Session,
    provider: Any,
    config: AgentConfig,
    memory_block: str,
    *,
    probe_text: str = "",
) -> str:
    """One last model call, with no tools, so the turn always says something.

    Running out of rounds used to end the turn on an ``error`` event alone: no
    ``message.done``, so a delegating parent got an empty summary and a person
    watching got a red line instead of an account of the work
    (CORE-subagent-budget).  The instruction is a *local* message, the way the
    output-limit continuation is: only the report itself joins the history, so
    the turn's last user message stays the one the user actually wrote — the
    checkpoint question and the reply language are both read off it.

    Never raises: a provider that fails here still leaves a written report.
    """
    # The probe's prose is the report when it wrote one; a second, tools-free
    # call only when it reached for a tool without saying anything.
    text = probe_text.strip()
    truncated = False
    continuations = 0
    if not text:
        try:
            messages = [
                *build_messages(session, [], memory_block, core=core),
                ChatMessage(role="user", content=BUDGET_INSTRUCTION),
            ]
            attempt = await _model_turn(core, session, provider, messages, [], config)
            text = attempt.text.strip()
            truncated = attempt.truncated
            continuations = attempt.continuations
        except Exception:  # noqa: BLE001 - the report is a courtesy, never a failure
            log.exception("the tool-budget report failed for %s", session.id)
    if not text:
        text = BUDGET_EMPTY_REPORT.format(n=config.max_tool_rounds)
    session.history.append(ChatMessage(role="assistant", content=text))
    await core.hub.emit_event(
        session.id,
        events.message_done(text, truncated=truncated, continuations=continuations),
    )
    await speak_reply(core, session, text)
    return text


#: The continue / stop rows of the tool-round checkpoint, per reply language.
_CONTINUE_ROWS: dict[str, tuple[str, str, str, str]] = {
    "ko": (
        "도구 호출 한도",
        "도구 호출 {n}회에 도달했습니다. 계속할까요?",
        "계속 (추천) — {n}회 더 진행합니다",
        "여기서 멈춤 — 지금까지의 작업만 남깁니다",
    ),
    "en": (
        "Tool-call budget",
        "The turn has made {n} tool calls. Keep going?",
        "Continue (recommended) — another {n} calls",
        "Stop here — keep what is done",
    ),
}


async def _ask_to_continue(core: Core, session: Session, config: AgentConfig) -> bool:
    """Put the round-budget checkpoint to the person; True means go on."""
    from snowpea_core.server.protocol import QuestionItem, QuestionOption
    from snowpea_core.tools.delegate import detected_language

    questions = getattr(core, "questions", None)
    if questions is None:
        return False
    lang = "ko" if detected_language(session) == "ko" else "en"
    header, question, go_on, stop = _CONTINUE_ROWS[lang]
    n = config.max_tool_rounds
    try:
        answers = await questions.ask(
            session,
            [
                QuestionItem(
                    header=header,
                    question=question.format(n=n),
                    options=[
                        QuestionOption(label=go_on.format(n=n)),
                        QuestionOption(label=stop),
                    ],
                    allowOther=False,
                )
            ],
        )
    except Exception:  # noqa: BLE001 - a surface that cannot ask means stop
        return False
    answer = answers[0] if answers else None
    return bool(
        answer
        and not answer.declined
        and not answer.timed_out
        and answer.selected
        and answer.selected[0] == go_on.format(n=n)
    )


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
        if session.mode == "plan" and tool.name in PLAN_WRITE_TOOLS and tag == "write":
            # The model has to know a *different path* would have worked, or
            # it retries the same write and reads the same refusal (M2 §9).
            message = f"{message}; {plan_paths.REFUSAL_NOTE}"
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
            refusal = f"the user declined {tool.name}"
            if decision.reason:
                # The refusal the human typed is the whole point of "deny with
                # a reason": the model must read it, not guess at it.
                refusal = f"{refusal}: {decision.reason}"
            await _deny_call(core, session, call, refusal)
            return "denied"

    await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
    # Plugin hooks (M6 contract §1): PreToolUse may refuse the call with exit 2.
    blocked = await plugin_hooks.pre_tool_use(core, session, call.name, dict(call.arguments))
    if blocked is not None:
        await _fail_call(core, session, call, f"{plugin_hooks.BLOCKED_PREFIX}: {blocked}")
        return None
    ctx = ToolContext(
        session=session,
        core=core,
        backend=backend,
        call_id=call.id,
        progress=ProgressEmitter(core, session.id, call.id, call.name),
    )
    try:
        result = await tool.run(ctx, dict(call.arguments))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken tool is a failed call
        log.exception("tool %s raised", tool.name)
        result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    await plugin_hooks.post_tool_use(core, session, call.name, dict(call.arguments))
    result = _spill_long_result(core, call.name, result)
    # Next to the LSP ``Diagnostics`` block (tools/fs.py ``_with_diagnostics``):
    # a call that touches a directory with its own AGENTS.md gets that file
    # once, and a call that *writes* one drops the cached prompt that no longer
    # matches the tree (CORE-context-files).
    override, ignore_context = agent_mod.context_file_settings(core)
    context_files.note_write(session, call.name, dict(call.arguments))
    if not ignore_context:
        result = context_files.attach_nested(
            session,
            call.name,
            dict(call.arguments),
            result,
            limit=prompt_env.context_file_max_chars(session.context_window, override),
        )

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


def _spill_long_result(core: Core, name: str, result: ToolResult) -> ToolResult:
    """Head/tail trim a scanning tool's output past ``tools.maxResultLines``.

    The full text is written to ``$SNOWPEA_HOME/cache/tool-output`` and the
    result carries a ``read_file`` pointer to it (M15 §A4), so nothing is lost
    and the conversation stops paying for the middle on every later turn.
    """
    if not result.ok or name not in output_spill.SPILLED_TOOLS or not result.output:
        return result
    budget = output_spill.max_result_lines(core)
    if result.output.count("\n") < budget:
        return result
    head = max(1, budget * 3 // 4)
    spilled = output_spill.spill(
        result.output, head_lines=head, tail_lines=budget - head, kind=name
    )
    if not spilled.trimmed:
        return result
    result.output = spilled.text
    return result


async def _deny_call(core: Core, session: Session, call: ToolCall, reason: str) -> None:
    """Tell the model a call was refused, so it can adapt inside the same turn.

    A refusal used to end the turn outright, which left the model unable to
    learn anything from it and made plan mode merely restrictive rather than
    usable (CORE-prompts, gap 3).  It now comes back as a tool result, the way
    any other failed call does.
    """
    message = f"Denied: {reason}. Choose a different action; do not retry the same call."
    if session.mode == "plan":
        message += (
            ' In plan mode, finish the plan and call set_mode("accept") so the user can'
            " choose to start implementing; never ask them in prose to switch modes."
        )
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
    "tool_rounds_for",
]
