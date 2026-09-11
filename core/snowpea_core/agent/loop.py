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
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ChatMessage, ProviderError, ToolCall
from snowpea_core.server import errors
from snowpea_core.session import events
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent")


def new_turn_id() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


def backend_for(core: Core, session: Session) -> Any:
    """Execution backend for a session (M1: always local)."""
    return LocalBackend(session.workdir)


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
        await hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
        raise
    except ProviderError as exc:
        await hub.emit_event(session.id, events.error(exc.code, str(exc)))
        reason = "error"
        await hub.emit_event(session.id, events.turn_done(turn_id, reason))
    except Exception as exc:  # noqa: BLE001 - a bug ends the turn, never the daemon
        log.exception("turn %s failed", turn_id)
        await hub.emit_event(
            session.id, events.error(errors.INTERNAL, f"{type(exc).__name__}: {exc}")
        )
        reason = "error"
        await hub.emit_event(session.id, events.turn_done(turn_id, reason))
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

    if text:
        session.history.append(ChatMessage(role="user", content=text))
        session.history.compact()

    for _round in range(config.max_tool_rounds):
        if session.interrupt.is_set():
            await hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
            return "interrupted"

        specs = core.tools.specs(session)
        messages = build_messages(session, specs)
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
                await hub.emit_event(
                    session.id,
                    events.usage(event.usage.input_tokens, event.usage.output_tokens),
                )
            elif event.kind == "done" and event.error:
                await hub.emit_event(session.id, events.error(errors.INTERNAL, event.error))

        if interrupted:
            await hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
            return "interrupted"

        assistant_text = "".join(chunks)
        if not calls:
            session.history.append(ChatMessage(role="assistant", content=assistant_text))
            await hub.emit_event(session.id, events.message_done(assistant_text))
            await hub.emit_event(session.id, events.turn_done(turn_id, "complete"))
            return "complete"

        session.history.append(
            ChatMessage(role="assistant", content=assistant_text, tool_calls=list(calls))
        )
        for call in calls:
            outcome = await _run_one_call(core, session, backend, policy, call, turn_id, unattended)
            if outcome is not None:
                return outcome
        session.history.compact()

    await hub.emit_event(
        session.id,
        events.error(errors.INTERNAL, f"stopped after {config.max_tool_rounds} tool rounds"),
    )
    await hub.emit_event(session.id, events.turn_done(turn_id, "error"))
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

    verdict = policy.decide(session.mode, tool.permission, tool, call.arguments, session)
    if verdict == "deny":
        message = f"{tool.name} ({tool.permission}) is not allowed in {session.mode} mode"
        await hub.emit_event(session.id, events.error(errors.MODE_DENIED, message))
        await hub.emit_event(session.id, events.turn_done(turn_id, "denied"))
        return "denied"
    if verdict == "ask":
        decision = await core.approvals.request(
            session,
            tool.name,
            call.arguments,
            risk=policy.risk(tool.permission),
            unattended=unattended,
            cancel_event=session.interrupt,
        )
        if session.interrupt.is_set():
            await hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
            return "interrupted"
        if not decision.allowed:
            code = decision.code or errors.APPROVAL_DENIED
            await hub.emit_event(
                session.id,
                events.error(code, f"{tool.name} was not approved ({decision.by})"),
            )
            await hub.emit_event(session.id, events.turn_done(turn_id, "denied"))
            return "denied"

    await hub.emit_event(session.id, events.tool_call(call.id, call.name, call.arguments))
    ctx = ToolContext(session=session, core=core, backend=backend)
    try:
        result = await tool.run(ctx, dict(call.arguments))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken tool is a failed call
        log.exception("tool %s raised", tool.name)
        result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

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


async def _fail_call(core: Core, session: Session, call: ToolCall, message: str) -> None:
    """Report a call that never ran and feed the reason back to the model."""
    await core.hub.emit_event(
        session.id, events.tool_result(call.id, call.name, False, "", message)
    )
    session.history.append(
        ChatMessage(role="tool", content=message, tool_call_id=call.id, name=call.name)
    )


__all__ = ["agent_config", "backend_for", "new_turn_id", "run_turn", "start_turn"]
