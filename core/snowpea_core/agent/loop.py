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
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import AgentConfig, build_messages
from snowpea_core.exec.local import LocalBackend
from snowpea_core.memory import context_for_turn, nudge_after_turn
from snowpea_core.permissions.policy import UNPROMOTABLE, PermissionPolicy
from snowpea_core.providers.base import ChatMessage, ProviderError, ToolCall
from snowpea_core.server import errors
from snowpea_core.session import compaction, events
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
        try:
            await compaction.emit_context(core, session, discover=False)
        except Exception:  # noqa: BLE001 - accounting must not fail a turn
            log.debug("could not emit the context event for %s", session.id, exc_info=True)
    await core.hub.emit_event(session.id, events.turn_done(turn_id, reason))
    return reason


def backend_for(core: Core, session: Session) -> Any:
    """Execution backend for a session; ``backend.set`` / ``/backend`` swaps it."""
    return session.backend or LocalBackend(session.workdir)


def agent_config(core: Core) -> AgentConfig:
    return AgentConfig(max_tool_rounds=max(1, core.settings.agent.max_tool_rounds))


def start_turn(core: Core, session: Session, text: str, *, unattended: bool = False) -> str:
    """Schedule a turn in the background and return its id immediately."""
    turn_id = new_turn_id()
    session.interrupt.clear()
    session.current_turn = turn_id
    task = asyncio.ensure_future(
        run_turn(core, session, text, turn_id=turn_id, unattended=unattended)
    )
    session.turn_task = task
    return turn_id


async def run_turn(
    core: Core,
    session: Session,
    text: str,
    *,
    turn_id: str | None = None,
    unattended: bool = False,
) -> str:
    """Run one full turn; returns its turn id once ``turn.done`` was emitted."""
    turn_id = turn_id or new_turn_id()
    session.current_turn = turn_id
    hub = core.hub
    try:
        reason = await _drive(core, session, text, turn_id, unattended)
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
        session.current_turn = None
    return turn_id


async def _drive(core: Core, session: Session, text: str, turn_id: str, unattended: bool) -> str:
    """The loop proper; emits ``turn.done`` itself and returns its reason."""
    hub = core.hub
    config = agent_config(core)
    policy: PermissionPolicy = core.policy
    backend = backend_for(core, session)
    provider = core.providers.get(session.provider, session.model)

    # Auto-compaction happens here and nowhere else: between turns, before the
    # new user message joins the history, and never inside the tool loop
    # (CORE-context).
    await compaction.maybe_auto_compact(core, session)

    if text:
        session.history.append(ChatMessage(role="user", content=text))
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
        chunks: list[str] = []
        calls: list[ToolCall] = []
        interrupted = False

        async for event in provider.stream(messages, specs, max_tokens=config.max_tokens):
            if session.interrupt.is_set():
                interrupted = True
                break
            if event.kind == "text_delta" and event.text:
                chunks.append(event.text)
                await hub.emit_event(session.id, events.message_delta(event.text))
            elif event.kind == "tool_call" and event.tool_call is not None:
                calls.append(event.tool_call)
            elif event.kind == "usage" and event.usage is not None:
                # The vendor's own prompt count beats any local estimate.
                compaction.record_provider_usage(session, event.usage.input_tokens)
                await hub.emit_event(
                    session.id,
                    events.usage(event.usage.input_tokens, event.usage.output_tokens),
                )
            elif event.kind == "done" and event.error:
                await hub.emit_event(session.id, events.error(errors.INTERNAL, event.error))

        if interrupted:
            await finish_turn(core, session, turn_id, "interrupted")
            return "interrupted"

        assistant_text = "".join(chunks)
        if not calls:
            session.history.append(ChatMessage(role="assistant", content=assistant_text))
            await hub.emit_event(session.id, events.message_done(assistant_text))
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


__all__ = ["agent_config", "backend_for", "new_turn_id", "run_turn", "start_turn"]
