"""``session.fork`` (1.46.0, capability ``fork``; M17 2c).

Branch a session at the end of a finished turn into a new session: the same
workdir, mode, model and agent, the history as it stood when that turn ended,
and the event log up to that turn, so the new session's transcript shows what
it inherited.  The source is never touched; checkpoint restore stays the way
to go back *inside* a session.

1.50.0: the fork records ``forked_from``/``forked_at_turn_id`` on its row, an
untitled source gives an untitled fork, and each refusal has its own
``error.data.code`` (``fork_*`` in :mod:`server.errors`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import SessionForkParams, SessionForkResult
from snowpea_core.session import events
from snowpea_core.session.manager import persist_history
from snowpea_core.session.mode_guard import ModeCaller

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = ("session.fork",)


def _idle(session: Any) -> bool:
    task = getattr(session, "turn_task", None)
    return task is None or task.done()


async def session_fork_handler(
    conn: RpcConnection | None, params: SessionForkParams, core: Core
) -> SessionForkResult:
    store = getattr(core, "store", None)
    if store is None:
        raise RpcError(errors.INTERNAL, "no session store")
    source = core.sessions.get(params.sessionId) or await core.sessions.restore(params.sessionId)
    if source is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    if getattr(source, "is_subagent", False):
        raise RpcError(errors.FORK_CHILD_SESSION, "a delegated child is not forked")
    dones = [
        event
        for event in await store.events_of_kinds(source.id, ("turn.done",))
        if not (event["payload"] or {}).get("synthetic")
    ]
    if not dones:
        raise RpcError(
            errors.FORK_NO_FINISHED_TURN, "the session has no finished turn to fork at"
        )
    if params.atTurnId:
        matches = [e for e in dones if (e["payload"] or {}).get("turnId") == params.atTurnId]
        if not matches:
            raise RpcError(errors.FORK_TURN_NOT_FOUND, f"no finished turn {params.atTurnId}")
        target = matches[-1]
    else:
        target = dones[-1]
    payload = target["payload"] or {}
    latest = target is dones[-1]
    messages = source.history.snapshot()
    history_len = payload.get("historyLen")
    compactions = payload.get("compactions")
    if (
        isinstance(history_len, int)
        and compactions == source.history.compactions
        and history_len <= len(messages)
    ):
        kept = messages[:history_len]
    elif latest and _idle(source):
        kept = messages
    elif latest:
        # Without a recorded history length the live history is the only copy,
        # and a running turn is adding to it.
        raise RpcError(
            errors.FORK_RUNNING,
            "a turn is running; fork once it has ended, or at an earlier turn",
        )
    else:
        raise RpcError(
            errors.FORK_AFTER_COMPACTION,
            "that turn is before the session's last compaction (or from before forks were "
            "recorded); fork at a later turn",
        )
    fork = await core.sessions.create(
        source.workdir,
        mode=source.mode,
        provider=source.provider,
        model=source.model,
        agent=getattr(source, "agent", None),
        origin_surface=getattr(source, "origin_surface", None),
        origin_conn=conn,
        # The source's own mode: a copy, not a raise.
        caller=ModeCaller.internal("fork-keeps-mode"),
    )
    at_turn = payload.get("turnId")
    # The fork keeps the source's title unchanged (1.53.0: no " (fork)" suffix,
    # forkedFrom says what it is); an untitled source gives an untitled fork.
    fork.title = source.title or None
    if fork.title:
        try:
            await store.update_title(fork.id, fork.title)
        except Exception:  # noqa: BLE001 - a title is cosmetic
            pass
    fork.forked_from, fork.forked_at_turn_id = source.id, at_turn
    await store.update_fork(fork.id, source.id, at_turn)
    jarvis = getattr(core, "jarvis", None)
    if jarvis is not None:
        # 1.56.0: a fork takes its source's Jarvis switch.
        jarvis.inherit_switch(fork.id, source.id)
    fork.history.extend(list(kept))
    await persist_history(store, fork)
    top = await store.copy_events(source.id, fork.id, int(target["seq"]))
    fork.seq = max(fork.seq, top)
    await core.hub.emit_event(fork.id, events.session_forked(source.id, at_turn, len(kept)))
    sessions = getattr(core, "sessions", None)
    if sessions is not None and hasattr(sessions, "announce_sessions_changed"):
        await sessions.announce_sessions_changed("created", fork.id, status="idle")
    return SessionForkResult(
        sessionId=fork.id, fromSessionId=source.id, atTurnId=at_turn, historyLen=len(kept)
    )


def register_fork_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("session.fork", session_fork_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_fork_handlers", "session_fork_handler"]
