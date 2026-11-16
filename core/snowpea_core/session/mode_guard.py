"""Who may RAISE a session's mode (security review 4, C1/H2).

Modes, narrowest first: ``plan`` < ``accept`` < ``auto``
(:data:`snowpea_core.permissions.policy.MODE_STRICTNESS`; there is no
separate "ask" mode).  A new session starts in the project's ``defaultMode``
(``<project>/.snowpea/settings.json``), else ``accept``
(:data:`snowpea_core.session.manager.DEFAULT_MODE`).

A call *raises* when the session would run in a mode wider than the one it has
(for a new session or job: wider than that default).  Raising needs a person:

* a local person's connection, or a remote web login (the person, for running
  work);
* a browser connection for a session it hosts, or one with no host that it
  opened — 0.3.0 and later only with ``actor: "person"`` (a click); a legacy
  browser as before;
* never a connector, never Jarvis, a worker, the model, or an in-process caller
  without a person behind it.

The few in-process callers that legitimately set a mode say so with
:meth:`ModeCaller.internal` (a scheduled run applying the mode its job was
given by a person, a delegated child inheriting its parent's mode, a fork, a
messenger binding's configured mode, a Jarvis task worker's fixed ``accept``).
A person-verified answer to a mode question is :data:`PERSON_ANSWER`.
Lowering a mode is never refused.

One guard, :func:`check`, sits in :meth:`SessionManager.set_mode` and
:meth:`SessionManager.create`; every refusal is ``jarvis_person_only`` with
``details.reason: "mode_raise"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

#: Narrowest first.
MODE_ORDER: tuple[str, ...] = ("plan", "accept", "auto")
REASON = "mode_raise"


def rank(mode: Any) -> int:
    """An unknown mode counts as the widest (so setting it is a raise)."""
    try:
        return MODE_ORDER.index(str(mode))
    except ValueError:
        return len(MODE_ORDER)


def raises(current: Any, wanted: Any) -> bool:
    return rank(wanted) > rank(current)


@dataclass(frozen=True)
class ModeCaller:
    """Who asks for a mode: a connection (with the browser's marker) or a named in-process path."""

    kind: str
    conn: Any = None
    actor: str | None = None
    #: For ``internal``: which legitimate path it is (for the log and the tests).
    why: str = ""

    @classmethod
    def from_conn(cls, conn: Any, actor: str | None = None) -> ModeCaller:
        if conn is None:
            return NOBODY
        from snowpea_core.server.person_gate import connection_kind
        from snowpea_core.server.rpc import current_actor

        # The dispatcher records every call's ``actor`` (any method): read it
        # from there when the handler's model does not carry it.
        return cls(kind=connection_kind(conn), conn=conn, actor=actor or current_actor())

    @classmethod
    def internal(cls, why: str) -> ModeCaller:
        return cls(kind="internal", why=why)


#: An in-process caller with no person behind it: may keep or lower only.
NOBODY = ModeCaller(kind="none")
#: The model (a tool) or Jarvis: may keep or lower only.
AGENT = ModeCaller(kind="agent")
#: The answer to a person-only mode question, verified as a person's by the question queue.
PERSON_ANSWER = ModeCaller(kind="answer")


def _browser_may(caller: ModeCaller, session: Any) -> bool:
    from snowpea_core.server.settings_gate import acts_for_person, legacy_browser

    if legacy_browser(caller.conn):
        return True  # a browser from before the marker: as before
    if not acts_for_person(caller.conn, caller.actor):
        return False
    if session is None:
        return True  # a session it is creating now
    mine = getattr(caller.conn, "client_id", None)
    host = getattr(session, "host_tools_from", None)
    if host is not None:
        return host == mine
    return getattr(session, "origin_conn", None) is caller.conn


def refusal(caller: ModeCaller | None, session: Any, current: Any, wanted: Any) -> str | None:
    """Why ``caller`` may not move ``session`` from ``current`` to ``wanted`` (``None``: it may)."""
    if not raises(current, wanted):
        return None
    caller = caller or NOBODY
    if caller.kind in ("person", "remote_login", "answer", "internal"):
        return None
    if caller.kind == "browser" and _browser_may(caller, session):
        return None
    if caller.kind == "browser":
        return (f"raising the mode to {wanted} needs a person's click on this page "
                "(actor: \"person\"), for a session this browser hosts")
    if caller.kind == "connector":
        return f"a {getattr(caller.conn, 'client_kind', None) or 'connector'} connection " \
               f"cannot raise a session's mode to {wanted}"
    return f"only a person may raise a session's mode to {wanted}"


def check(caller: ModeCaller | None, session: Any, current: Any, wanted: Any) -> None:
    """Raise ``jarvis_person_only`` (``details.reason: "mode_raise"``) for a refused raise."""
    reason = refusal(caller, session, current, wanted)
    if reason is None:
        return
    kind = (caller or NOBODY).kind
    raise RpcError(errors.JARVIS_PERSON_ONLY, reason,
                   {"reason": REASON, "kind": kind, "from": str(current), "to": str(wanted)})


__all__ = [
    "AGENT",
    "MODE_ORDER",
    "NOBODY",
    "PERSON_ANSWER",
    "REASON",
    "ModeCaller",
    "check",
    "raises",
    "rank",
    "refusal",
]
