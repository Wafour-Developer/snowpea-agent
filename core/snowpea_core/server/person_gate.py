"""Who may call an RPC that changes Jarvis's safety or reach (security review H2/M9/L6).

One rule for every handler, the delegation engine's (:func:`snowpea_core.jarvis.
delegation.person_refusal`): a person is an open, authenticated connection
whose ``clientKind`` is a human surface (IDE, web, TUI, CLI, the desktop app,
no ``clientKind``).  A browser host, the remote connector, an unknown client
and in-process callers (``conn is None``) are not.  ``local=True`` also refuses
a remote web login (``RpcConnection.remote_login``): it has not
re-authenticated, so it may not widen what runs unattended.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

ConnectionKind = Literal["person", "remote_login", "browser", "connector", "closed", "none"]


def _open(conn: Any) -> bool:
    return (
        conn is not None
        and bool(getattr(conn, "authenticated", False))
        and not bool(getattr(conn, "closed", False))
    )


@dataclass(frozen=True, eq=False)
class CallerSnapshot:
    """Who asked, as it was when the work was accepted (security review 7, H-1).

    Work that runs later than the call that asked for it — a queued command or
    prompt, a tool call of the turn, a scheduled job — is judged by this, never
    by the live connection: a connection that has closed since, or that says
    something else now, changes nothing.  Every gate that takes a connection
    takes a snapshot in its place (:func:`connection_kind` reads ``kind`` from
    it; the identity attributes are copied).  ``conn`` is the connection
    itself, kept for identity (``session.origin_conn is caller.conn``) only.
    """

    kind: ConnectionKind
    #: A browser's ``actor: "person"`` marker on the call.
    actor: str | None = None
    client_kind: str | None = None
    client_id: str | None = None
    surface_id: str | None = None
    client_version: str | None = None
    #: With no connection: the in-process identity (``"messenger"``, ``"internal"``).
    caller: str | None = None
    #: Whose words started the work (``agent.loop.ORIGINS``), when known.
    origin: str | None = None
    conn: Any = None

    # -- what the gates read from a connection --------------------------
    @property
    def authenticated(self) -> bool:
        return self.kind not in ("none", "closed")

    @property
    def closed(self) -> bool:
        return self.kind == "closed"

    @property
    def remote_login(self) -> bool:
        return self.kind == "remote_login"

    @property
    def local_person(self) -> bool:
        return self.kind == "person"


#: No connection and no named in-process identity.
NOBODY = CallerSnapshot(kind="none")


def snapshot(conn: Any, actor: str | None = None, *, caller: str | None = None,
             origin: str | None = None) -> CallerSnapshot:
    """The :class:`CallerSnapshot` of ``conn`` now (a snapshot stays what it is).

    ``actor``, ``caller`` and ``origin`` fill what a given snapshot left unset.
    """
    if isinstance(conn, CallerSnapshot):
        updates = {
            name: value
            for name, value in (("actor", actor), ("caller", caller), ("origin", origin))
            if value is not None and getattr(conn, name) is None
        }
        return replace(conn, **updates) if updates else conn
    if conn is None:
        return CallerSnapshot(kind="none", actor=actor, caller=caller, origin=origin)
    return CallerSnapshot(
        kind=connection_kind(conn),
        actor=actor,
        client_kind=getattr(conn, "client_kind", None),
        client_id=getattr(conn, "client_id", None),
        surface_id=getattr(conn, "surface_id", None),
        client_version=getattr(conn, "client_version", None),
        caller=caller,
        origin=origin,
        conn=conn,
    )


def accept_work(conn: Any, core: Any, session: Any, what: str) -> CallerSnapshot:
    """An RPC that starts work in ``session`` or adds to it: who asks, as of now.

    Returns the caller's snapshot for the turn to carry (security review 7,
    H-1), after refusing a remote web login that would start work in a session
    above ``web.remote.maxMode`` (``jarvis_person_only``, ``mode_raise``,
    ``details.max``).  The one place ``session.continue``, ``goal.set|resume``,
    ``agent.spawn`` and ``team.start`` take their caller from.
    """
    from snowpea_core.server.rpc import current_actor
    from snowpea_core.session import mode_guard

    who = snapshot(conn, current_actor(), origin="human")
    mode_guard.check_remote_max(who.kind, mode_guard.effective_mode(session),
                                getattr(core, "settings", None), what)
    return who


def live_conn(conn: Any) -> Any:
    """The connection behind ``conn`` (itself, or a snapshot's), for identity checks."""
    return conn.conn if isinstance(conn, CallerSnapshot) else conn


def connection_kind(conn: Any) -> ConnectionKind:
    """``person``, ``remote_login``, ``browser``, ``connector`` (other non-person),
    ``closed`` or ``none``.

    ``none`` is an in-process caller only (``conn is None``).  A connection that
    has closed, or never authenticated, is ``closed``: it is nobody, and never
    an in-process caller (security review 7, H-1).  A :class:`CallerSnapshot`
    answers with the kind it was taken with.
    """
    from snowpea_core.jarvis.delegation import HUMAN_CLIENT_KINDS

    if isinstance(conn, CallerSnapshot):
        return conn.kind
    if conn is None:
        return "none"
    if not _open(conn):
        return "closed"
    if bool(getattr(conn, "remote_login", False)):
        # Security review 5, M3: the remote listener's connections are remote
        # web logins whatever ``clientKind`` they declared.
        return "remote_login"
    kind = getattr(conn, "client_kind", None)
    if kind in HUMAN_CLIENT_KINDS:
        return "person"
    return "browser" if kind == "browser" else "connector"


def asker_for(session: Any, who: Any) -> Any:
    """The connection a turn's approvals and questions go to (0.2.91), or ``None``.

    The person's own connection whose words start (or steer) the turn: a local
    person or a remote web login (it keeps its own limits when it answers), or
    the browser that hosts or opened the session (its panel is where the person
    types).  Never a connector, a closed connection, a messenger, Jarvis, a
    scheduled job or another in-process caller (``who.conn`` is ``None``), and
    never for a browser-owned job's run.  ``None`` leaves the session's origin
    in charge, as before.
    """
    if not isinstance(who, CallerSnapshot) or who.conn is None:
        return None
    if getattr(session, "owned_by", None):
        return None
    conn = who.conn
    if connection_kind(conn) in ("closed", "none"):
        return None
    if who.kind in ("person", "remote_login"):
        return conn
    if who.kind == "browser":
        client_id = getattr(conn, "client_id", None)
        if client_id and client_id in (getattr(session, "host_tools_from", None),
                                       getattr(session, "origin_client_id", None)):
            return conn
    return None


def is_person(conn: Any, *, local: bool = False) -> bool:
    """True for a person's connection (``local``: not a remote web login)."""
    kind = connection_kind(conn)
    return kind == "person" or (kind == "remote_login" and not local)


def require_person(conn: Any, what: str, *, local: bool = False) -> None:
    """Raise ``jarvis_person_only`` unless ``conn`` is the person (see the module doc)."""
    kind = connection_kind(conn)
    if kind == "person" or (kind == "remote_login" and not local):
        return
    if kind == "remote_login":
        reason = f"{what} needs a local connection (IDE, TUI or the local web page)"
    elif kind in ("none", "closed"):
        reason = f"only a person may {what}"
    else:
        reason = f"a {getattr(conn, 'client_kind', None) or kind} connection cannot {what}"
    raise RpcError(errors.JARVIS_PERSON_ONLY, reason, {"reason": "person_only", "kind": kind})


#: Methods only a person may call (security review M9/L6), checked by the
#: dispatcher before the handler runs.  ``True``: a local connection only (a
#: remote web login is refused too, it widens what runs unattended);
#: ``False``: a remote web login is a person here.  Methods not listed keep
#: their own rules (the delegation engine's, the external manager's).
PERSON_METHODS: dict[str, bool] = {
    # Jarvis reads: people only (browser hosts and the connector get nothing).
    "jarvis.status": False,
    "jarvis.overview": False,
    "jarvis.project.list": False,
    "jarvis.escalations.list": False,
    "jarvis.ledger": False,
    "jarvis.delegation.list": False,
    "jarvis.briefing.get": False,
    # Jarvis changes.
    "jarvis.enable": True,
    "jarvis.disable": False,
    "jarvis.project.rename": False,
    "jarvis.project.alias": False,
    "jarvis.escalations.dismiss": False,
    "jarvis.briefing.set": False,
    # 1.56.0: a session's Jarvis switch — a person's, a remote web login too
    # (per-session state that grants nothing by itself); never Jarvis, a
    # browser, a connector or an in-process caller.
    "jarvis.session.set": False,
    # Presets write credentials, MCP servers, read tags and jobs.
    "mcp.preset.apply": True,
    "mcp.preset.remove": True,
    "mcp.preset.check": False,
    # A CLI path override runs that executable.
    "external.clis.confirmPath": True,
    # The person's own terminal screen (1.56.0): people only.
    "external.terminalSnapshot": False,
    # Security lists (1.54.0): people read and try them (a remote web login
    # too, read-only: security.get says writable false); only a local person
    # changes them, through settings.set (settings_gate.PERSON_LOCAL_ONLY).
    # 1.58.0: a browser reads them too and its Settings page changes the switch
    # and the lists (BROWSER_READABLE, settings_gate.BROWSER_PERSON_SECURITY_KEYS).
    "security.presets.list": False,
    "security.get": False,
    "security.check": False,
    # A typed rule pre-approves before any routing (security review 4, H1): a
    # local person's only (never a browser, a connector or a remote web login).
    "permission.allowlist.add": True,
    # Remote web access (1.54.0): who may reach this machine from elsewhere and
    # with what — a local person's only (a remote login may not widen its own door).
    "web.remote.status": True,
    "web.remote.listen": True,
    "web.remote.stop": True,
    "web.remote.setPassword": True,
    "web.remote.clearPassword": True,
    "web.remote.rotateToken": True,
    # M37 (1.73.0): CLI hook integrations edit the person's own CLI config files;
    # the web tunnel opens this machine to the internet and stores its token.
    "integration.list": True,
    "integration.install": True,
    "integration.uninstall": True,
    "web.tunnel.status": True,
    "web.tunnel.start": True,
    "web.tunnel.stop": True,
    "web.tunnel.setToken": True,
    "web.tunnel.install": True,
  # M40 (1.76.0): managing ssh machines stays on this computer.
    "machine.list": True,
    "machine.add": True,
    "machine.update": True,
    "machine.remove": True,
    "machine.connect": True,
    "machine.disconnect": True,
    "machine.answer": True,
    # Security review 5, M1: what runs code in the daemon or replaces it, and
    # what a project folder may make snowpea do — a local person's only.
    "extension.enable": True,
    "extension.disable": True,
    "extension.configure": True,
    "extension.reload": True,
    "extension.list": True,
    "system.update": True,
    "system.restart": True,
    "project.trust.set": True,
    "project.trust.revoke": True,
    "project.trust.status": False,
    # Security review 7, P2: applying security settings that were edited on disk.
    "settings.acceptDiskChange": True,
    # 개선하기 (1.59.0): a person sends a conversation to the Snowpea team — a
    # remote web login is a person here too; never a browser host (it has its
    # own sender), a connector, Jarvis or any other in-process caller.
    "feedback.preview": False,
    "feedback.send": False,
}


#: Of :data:`PERSON_METHODS`, the reads a browser connection may call too
#: (1.58.0): its Settings page shows the security master switch and the lists
#: it may change (``settings_gate.BROWSER_PERSON_SECURITY_KEYS``).
BROWSER_READABLE: frozenset[str] = frozenset(
    {"security.presets.list", "security.get", "security.check"}
)
#: Of :data:`PERSON_METHODS`, what a browser connection may call and its handler
#: judges (1.62.0, ``browserProjectTrust``: ``settings_gate.browser_trust_refusal``).
BROWSER_JUDGED: frozenset[str] = frozenset(
    {"project.trust.status", "project.trust.set", "project.trust.revoke"}
)


def method_refusal(method: str, conn: Any) -> None:
    """Raise ``jarvis_person_only`` when ``method`` is a person's and ``conn`` is not."""
    local = PERSON_METHODS.get(method)
    if local is not None:
        if (method in BROWSER_READABLE or method in BROWSER_JUDGED) and connection_kind(
                conn) == "browser":
            return
        require_person(conn, f"call {method}", local=local)


async def is_task_worker(core: Any, session_id: str) -> bool:
    """True for a ``jarvis-task`` worker session (live or stored)."""
    from snowpea_core.jarvis.ports import TASK_WORKER_SURFACE

    sessions = getattr(core, "sessions", None)
    live = sessions.get(session_id) if sessions is not None else None
    if live is not None:
        return getattr(live, "origin_surface", None) == TASK_WORKER_SURFACE
    store = getattr(core, "store", None)
    if store is None:
        return False
    try:
        row = await store.session(session_id)
    except Exception:  # noqa: BLE001 - unknown is not a worker
        return False
    return bool(row) and row.get("origin_surface") == TASK_WORKER_SURFACE


async def hide_task_worker(conn: Any, core: Any, session_id: str) -> None:
    """``not_found`` for a non-person reading a ``jarvis-task`` worker (security review M6).

    A mail or calendar worker's transcript holds what it read; only the
    person's own surfaces may open it while it exists (it is purged after the
    hand-over).
    """
    if is_person(conn):
        return
    if await is_task_worker(core, session_id):
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")


__all__ = [
    "NOBODY",
    "CallerSnapshot",
    "asker_for",
    "ConnectionKind",
    "connection_kind",
    "live_conn",
    "snapshot",
    "hide_task_worker",
    "is_person",
    "is_task_worker",
    "require_person",
]
