"""Human-in-the-loop approvals (contract §7).

An interactive turn asks its *origin* connection and nobody else: the surface
that started the session is the one holding a human.  An unattended turn (a
scheduled job, a gateway message) has no origin to ask, so its request sits in
the queue until some authenticated client answers with ``approval.respond`` or
``approvals.timeoutSec`` expires and it is denied.

Every resolution, including timeouts, is appended to
``$SNOWPEA_HOME/logs/approvals.jsonl``.

Answering with a scope wider than ``once`` remembers the decision: ``session``
caches it for this session's ``(tool, first command token)``, while ``project``
and ``always`` also write an allowlist entry (contract §7) into the project or
global settings so it survives a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import posixpath
import re
import shlex
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from snowpea_core.config.paths import Paths, utc_now
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import (
    DIR_TARGET,
    SHELL_TARGET,
    Allowlist,
    command_of,
    first_token,
    local_dev_origin,
    once_only,
    outside_folder,
    pattern_for_command,
    pattern_for_dir,
    pattern_for_tool,
    site_of,
    tool_target,
)
from snowpea_core.permissions.allowlist import (
    Scope as AllowlistScope,
)
from snowpea_core.permissions.shell_remember import (
    EXACT_LINE_NOTE,
    chain_program_ok,
    exact_line,
    is_compound,
    matchable,
    plain_words,
    propose,
    runs_commands,
    segments,
)
from snowpea_core.security.check import UNCHECKED_REASON
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ApprovalRemember,
    ApprovalRequest,
    ExternalRef,
    ScriptRef,
)
from snowpea_core.session.session import (
    CLIENT_APPROVAL_CARD_SEC,
    asking_conn,
    client_timed_out,
    surface_name,
    withdraw,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.manager import EventHub
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.approvals")

#: Alias so the annotations below still mean the builtin ``list`` even though
#: :class:`ApprovalQueue` defines a method called ``list``.
ApprovalRequests = list[ApprovalRequest]

#: Scopes that cache an "allow" for the rest of the session.
#: ``site`` is not here: the session cache is keyed by tool, so caching a site
#: answer would allow that tool on every site; its allowlist entry is enough.
CACHING_SCOPES: frozenset[str] = frozenset({"session", "project", "always"})

#: Scopes that also persist an allowlist entry, and the store each one uses.
PERSISTING_SCOPES: dict[str, str] = {"project": "project", "always": "global", "site": "global"}

#: Extra seconds the outer wait gives the origin call to report its own timeout.
GRACE_SECONDS = 2.0


#: Dotted browser actions whose answer may be remembered beyond one site.
#: Every other ``repl.*`` action (send, upload, script, …) acts on a page, so
#: an "always"/"project" answer for it would become a rule for every site.
#: Reading a tab and attaching to one (``repl.read`` / ``repl.attach``,
#: protocol 1.13.0) only look: the browser's approval card offers "always on
#: every site" for them, stored as the profile's origin-less global rule.
REPL_GLOBAL_OK: frozenset[str] = frozenset(
    {"repl.tabs", "repl.clipboard", "repl.read", "repl.attach"}
)

#: Page actions the person may still allow "always", on every site (1.19.0,
#: capability ``pageActionAlways``).  The card says so in as many words, so
#: the answer is exactly as broad as it looks; "project" stays ``once`` for
#: them, since a project rule would quietly cover every site too.  A host
#: marks what must never be remembered (payments) ``onceOnly``.
REPL_ALWAYS_OK: frozenset[str] = frozenset(
    {"repl.send", "repl.upload", "repl.script", "repl.navigate", "repl.fetch", "repl.download"}
)

#: Dotted actions that act on the session's project folder, not on a page:
#: they may be remembered for that project, never globally (1.7.0).
REPL_PROJECT_OK: frozenset[str] = frozenset({"repl.files"})


def _shell_command(request: ApprovalRequest) -> str | None:
    """The command line of a request whose remembered answer is a shell rule."""
    if request.reasonCode == OUTSIDE_REASON or (request.tool or "").startswith("repl."):
        return None
    return command_of(request.args)


def remember_for(
    tool: str,
    args: dict[str, Any],
    *,
    cacheable: bool = True,
    guard: str | None = None,
    reason_code: str | None = None,
) -> ApprovalRemember | None:
    """``ApprovalRequest.remember`` for a call (1.34.0): the shell rule it would store.

    ``None`` off-shell, for an answer that is never remembered (``onceOnly``,
    an agent-switch guard, an uncacheable ``config``/``secret`` call, any
    ``reasonCode``), and whenever :func:`~snowpea_core.permissions.shell_remember.propose`
    finds no safe rule.
    """
    if not cacheable or guard or reason_code or once_only(args) or tool.startswith("repl."):
        return None
    command = command_of(args)
    if command and is_compound(command):
        # m21: a compound line gets no rule; "for this session" remembers the
        # exact line (whitespace normalised), in this folder, for this session.
        line = exact_line(command)
        if line is None:
            return None
        return ApprovalRemember(pattern="^" + re.escape(line) + "$", display=line,
                                scopes=[], note=EXACT_LINE_NOTE, exactLine=True)
    proposal = propose(command) if command else None
    if proposal is None:
        return None
    return ApprovalRemember(
        pattern=proposal.pattern,
        display=proposal.display,
        scopes=list(proposal.scopes),  # type: ignore[arg-type]
        note=proposal.note,
    )


def _folder_of(args: dict[str, Any], workdir: Any) -> str:
    """The folder a shell call runs in: the session's folder joined with ``args.cwd``."""
    cwd = str(args.get("cwd") or "").strip()
    if not (workdir or cwd):
        return ""
    return posixpath.normpath(posixpath.join(str(workdir or ""), cwd))


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def script_offer(
    tool: str, args: dict[str, Any], security: Any, workdir: Any
) -> ApprovalRemember | None:
    """``remember`` for a script grant (m21 D), or ``None``.

    Offered when the security verdict asks ONLY because of what the content of
    the script files it hashed does (``Verdict.script_grant``: each reason in
    ``UNRESOLVED_REASONS``, none raised by the line itself, no control-plane
    reason, no script the same line writes), the call is one plain command
    whose words are all literal, and every such file is inside the session's
    project folder.
    """
    if (
        not getattr(security, "script_grant", False)
        or getattr(security, "blocked", False)
        or tool.startswith("repl.")
        or once_only(args)
        or workdir is None
    ):
        return None
    command = command_of(args)
    words = plain_words(command) if command else None
    if words is None or runs_commands(words):
        return None
    project = os.path.realpath(str(workdir))
    pairs: list[ScriptRef] = []
    for path, digest in getattr(security, "scripts", ()) or ():
        if not str(digest).startswith("sha256:"):
            return None  # unreadable, missing or too large: nothing to hold it to
        real = os.path.realpath(str(path))
        if not _under(real, project):
            return None
        pairs.append(ScriptRef(path=str(path), sha256=str(digest)[len("sha256:"):]))
    if not pairs:
        return None
    line = " ".join(shlex.quote(word) for word in words)
    names = ", ".join(os.path.relpath(os.path.realpath(p.path), project) for p in pairs[:3])
    if len(pairs) > 3:
        names += f" (+{len(pairs) - 3})"
    return ApprovalRemember(
        pattern=line,
        display=f"{line} (while {names} {'is' if len(pairs) == 1 else 'are'} unchanged)",
        scopes=["project"],
        note=f"remembered only for this exact command while {names} "
        f"{'is' if len(pairs) == 1 else 'are'} unchanged; asked again when it changes",
        kind="script",
        script=pairs[0],
        scripts=pairs,
    )


def _script_key(
    session_id: str, tool: str, args: dict[str, Any], workdir: Any
) -> tuple[str, str, tuple[str, ...], str] | None:
    command = command_of(args)
    words = plain_words(command) if command else None
    if words is None:
        return None
    return (session_id, tool, tuple(words), _folder_of(args, workdir))


def _script_remember(request: ApprovalRequest) -> bool:
    """The request offers a script grant (m21 D)."""
    return request.remember is not None and request.remember.kind == "script"


def _remembered(request: ApprovalRequest) -> ApprovalRemember | None:
    """The request's offered rule; one built without it (older code) gets it now."""
    if request.remember is not None:
        return request.remember
    return remember_for(
        request.tool or "", request.args, guard=request.guard, reason_code=request.reasonCode
    )


#: Connection kinds whose allow never stores a rule (security review 7, M-3): a
#: remote web login (not re-authenticated), a connector, a connection that has
#: closed.  A browser keeps its own rule (a wider scope only from a person's
#: click, ``actor: "person"``); the owner-bound messenger answers in-process.
NO_STORED_RULE_KINDS: frozenset[str] = frozenset({"remote_login", "connector", "closed"})
#: What a persisting scope becomes for them: remembered until the session ends.
CLAMPED_SCOPE = "session"


def clamp_scope(conn: Any, scope: str) -> str:
    """``scope`` as ``conn``'s allow may apply it (security review 7, M-3).

    ``project``, ``always`` and ``site`` store an allowlist rule that outlives
    the answer and pre-approves for everyone: from a :data:`NO_STORED_RULE_KINDS`
    connection they become ``session``.  ``approval.respond`` and
    ``approval.resolved`` report the scope that applied.
    """
    if scope not in PERSISTING_SCOPES or conn is None:
        return scope
    try:
        from snowpea_core.server.person_gate import connection_kind

        kind = connection_kind(conn)
    except Exception:  # noqa: BLE001 - unknown is not a local person
        return CLAMPED_SCOPE
    return CLAMPED_SCOPE if kind in NO_STORED_RULE_KINDS else scope


def _remembering_person(conn: Any, actor: str | None = None) -> bool:
    """``conn`` is a person's own surface whose answer may remember an exact line
    or a script (m21): a local person's connection, or a browser's click
    (``actor: "person"``).  Never a remote web login, a connector, a closed
    connection or an in-process caller (Jarvis, a messenger)."""
    try:
        from snowpea_core.server.person_gate import connection_kind

        kind = connection_kind(conn)
        if kind == "person":
            return True
        if kind == "browser":
            from snowpea_core.server.settings_gate import acts_for_person

            return bool(acts_for_person(conn, actor))
    except Exception:  # noqa: BLE001 - unknown is nobody
        return False
    return False


def _local_person(conn: Any) -> bool:
    """``conn`` is a local person's own connection (not a remote web login, a
    browser host, a connector or no connection at all)."""
    try:
        from snowpea_core.server.person_gate import is_person

        return conn is not None and is_person(conn, local=True)
    except Exception:  # noqa: BLE001 - unknown is not a local person
        return False


def guard_scope(request: ApprovalRequest, decision: Decision, workdir: Any = None) -> Decision:
    """Downgrade a remembered answer that would be broader than it looks.

    Defense in depth for host actions (1.7.0): a page action (``repl.*``
    other than tabs/clipboard) answered "always"/"project" would be stored
    with no site and match everywhere — except "always" for the kinds in
    :data:`REPL_ALWAYS_OK`, which the person chose on every site (1.19.0);
    a "site" answer with no usable http(s) origin would be stored the same
    way.  Both become ``once``.
    The exception is a local-dev-server navigation (``repl.navigate`` with
    ``reasonCode: "localDevServer"``) answered "project": it is stored for
    the session's project *and* the URL's origin only.

    A shell command answered "project"/"always" (1.34.0) is remembered only
    as its ``remember`` rule: with none it is kept to ``once``, and "always"
    for a rule offered for the project only is kept to ``project``.
    """
    if not decision.allowed or decision.scope == "once":
        return decision
    if once_only(request.args):
        return replace(decision, scope="once", picked=decision.scope)
    tool = request.tool or ""
    local_dev = (
        decision.scope == "project"
        and local_dev_origin(tool, request.args, workdir) is not None
    )
    if (
        decision.scope in ("always", "project")
        and tool.startswith("repl.")
        and tool not in REPL_GLOBAL_OK
        and not (decision.scope == "project" and tool in REPL_PROJECT_OK)
        and not (decision.scope == "always" and tool in REPL_ALWAYS_OK)
        and not local_dev
    ):
        log.info("approval %s: %s for %s kept to once", request.requestId, decision.scope, tool)
        return replace(decision, scope="once")
    command = _shell_command(request)
    remember = request.remember
    if remember is not None and remember.kind == "script":
        # m21 D: a script grant, only from a person's own surface: "session",
        # or "project" (an "always" is kept for the project).
        if not decision.line_ok or not command:
            return replace(decision, scope="once")
        if decision.scope not in ("session", "project"):
            return replace(decision, scope="project" if decision.scope == "always" else "once")
        return decision
    if command and remember is not None and remember.exactLine and not matchable(command):
        # m21: a compound line is remembered as its exact text for this session,
        # and only from a person's own answer (never a remote login, a connector,
        # Jarvis or a messenger); nothing wider is ever stored for it.
        if not decision.line_ok:
            log.info("approval %s: %s for a compound line not from a person kept to once",
                     request.requestId, decision.scope)
            return replace(decision, scope="once")
        if decision.scope != "session":
            log.info("approval %s: %s for a compound line kept to session",
                     request.requestId, decision.scope)
            return replace(decision, scope="session")
        return decision
    if decision.scope == "session" and command and not matchable(command):
        # The session cache is keyed by the first word: "for this session" on
        # ``ls | sh`` must not quietly cover every ``ls`` after it.
        log.info("approval %s: session for a compound command kept to once",
                 request.requestId)
        return replace(decision, scope="once")
    if decision.scope in ("always", "project") and command:
        remember = _remembered(request)
        if remember is None:
            log.info("approval %s: %s for a command with no safe rule kept to once",
                     request.requestId, decision.scope)
            return replace(decision, scope="once")
        if decision.scope not in remember.scopes:
            log.info("approval %s: always for %r kept to project",
                     request.requestId, remember.display)
            return replace(decision, scope="project")
    if decision.scope == "site":
        origin = site_of(request.args, request.site)
        if origin is None or not origin.startswith(("http://", "https://")):
            log.info("approval %s: site scope without an http(s) site kept to once",
                     request.requestId)
            return replace(decision, scope="once")
    return decision


@dataclass
class Decision:
    """Resolution of one approval request."""

    decision: str
    scope: str = "once"
    by: str = "unknown"
    code: str | None = None
    #: Free text the human typed when refusing; the model is told exactly this.
    reason: str = ""
    #: The scope the person picked, when :func:`guard_scope` kept a ``onceOnly``
    #: answer to ``once``: ``approval.ask`` hands it back so the host can keep
    #: the choice on its side (addendum 22); nothing here stores it.
    picked: str = ""
    #: Who answered a ``personOnly`` request (a
    #: :class:`~snowpea_core.jarvis.delegation.Caller`), so the asker can name
    #: the person as grantor (1.53.0); ``None`` for every other request.
    person: Any = None
    #: The answer came from a local person's own connection (the app, the TUI):
    #: only such an answer keeps a project's settings trust when it stores a
    #: project rule (security review 6, L-2).
    local_person: bool = False
    #: 1.56.0: a remote web login allowed Jarvis's ``delegation_set`` proposal
    #: with its password again: when that grant ends (UTC ISO, within 24 h).
    grant_expires_at: str | None = None
    #: m21: a person answered on their own surface (the app, the TUI, a browser
    #: click): only such an answer remembers an exact compound line or a script
    #: grant.  False for a remote web login, a connector, Jarvis, a messenger.
    line_ok: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def waits_for_a_person(conn: Any) -> bool:
    """True for a live TUI or desktop connection: nobody is racing a clock there.

    A browser-driven run, a messenger or a scheduled job has no one watching,
    so those keep ``approvals.timeoutSec`` / ``questions.timeoutSec``.
    """
    return conn is not None and getattr(conn, "client_kind", None) != "browser"


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[Decision]
    unattended: bool
    #: A person at a TUI or desktop session is waited for without a clock.
    no_limit: bool = False
    origin_conn: Any = None
    task: asyncio.Task[None] | None = field(default=None)
    #: Session workdir, kept so a ``project`` answer knows where to write.
    workdir: Any = None
    #: False for ``config`` calls: a wide answer must not silence the next one.
    cacheable: bool = True
    #: The session's owning browser profile (addendum 8): a global rule stored
    #: from this answer belongs to it, and the pending card names it.
    host: str | None = None
    #: The browser clientId owning the job this run serves: only that
    #: browser's connection may answer (1.23.0).
    owner: str | None = None
    #: 0.2.91: the core's own clock for this request (``approvals.timeoutSec``
    #: when the asker is waited for without one): a deny that arrives after it
    #: is the asking client's own card timing out, not a person's "no".
    clock_sec: float = 0.0



#: ``ApprovalRequest.reasonCode`` of a write outside the project (addendum 29).
OUTSIDE_REASON = "outside_project"


def _outside_folder(request: ApprovalRequest) -> str | None:
    return request.folder if request.reasonCode == OUTSIDE_REASON else None


class ApprovalQueue:
    """Pending approvals plus the audit log."""

    def __init__(
        self,
        settings: Settings | None = None,
        paths: Paths | None = None,
        hub: EventHub | None = None,
        allowlist: Allowlist | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.paths = paths
        self.hub = hub
        self.allowlist = allowlist
        self._pending: dict[str, _Pending] = {}
        self._cache: set[tuple[str, str, str]] = set()
        #: m21: exact compound lines a person allowed "for this session":
        #: ``(session, tool, normalised line, folder, background)``.  In memory
        #: only, so a restart forgets them; never inherited by another session.
        self._lines: set[tuple[str, str, str, str, bool]] = set()
        #: m21 D: script grants a person gave "for this session": the call's
        #: ``(session, tool, words, folder)`` -> the ``(path, sha256)`` it holds to.
        self._script_grants: dict[tuple[str, str, tuple[str, ...], str],
                                  tuple[tuple[str, str], ...]] = {}
        #: Jarvis mode's router (1.53.0): ``router(request, session, tags,
        #: interactive)`` returns ``ApprovalRequest.route`` before the request
        #: goes on the wire, or ``None`` (Jarvis mode off).  Must not block.
        self.router: Any = None
        #: The daemon's core (set by ``app_server``): the security lists read the
        #: registered projects and Jarvis's ledger through it.
        self.core: Any = None

    def bind(
        self,
        settings: Settings,
        paths: Paths,
        hub: EventHub,
        allowlist: Allowlist | None = None,
    ) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.settings = settings
        self.paths = paths
        self.hub = hub
        if allowlist is not None:
            self.allowlist = allowlist

    # -- queries -------------------------------------------------------
    def list(self, session_id: str | None = None, conn: Any = None) -> ApprovalRequests:
        """The shared queue: unattended requests, plus ``conn``'s own.

        An interactive request belongs to the surface that started the session
        and is asked over ``approval.request``; it never becomes visible to a
        second client (contract §4, AC-20).  Passing the asking connection back
        in is what lets that surface still see its own pending request.
        """
        return [
            entry.request
            for entry in self._pending.values()
            if (entry.unattended or (conn is not None and entry.origin_conn is conn))
            and (session_id is None or entry.request.sessionId == session_id)
        ]

    def count(self, session_id: str) -> int:
        """Every pending request of ``session_id``, interactive ones included."""
        return sum(1 for e in self._pending.values() if e.request.sessionId == session_id)

    async def _announce_status(
        self, session_id: str, status: str, host: str | None = None
    ) -> None:
        """``sessions.changed`` with the session's new status (1.7.0 task list)."""
        if self.hub is None:
            return
        with contextlib.suppress(Exception):
            await self.hub.notify(
                "sessions.changed",
                {
                    "reason": "approval",
                    "sessionId": session_id,
                    "status": status,
                    "hostToolsFrom": host,
                },
            )

    def unattended(self, session_id: str | None = None) -> ApprovalRequests:
        """Pending requests with no interactive surface to answer them."""
        return [
            entry.request
            for entry in self._pending.values()
            if entry.unattended and (session_id is None or entry.request.sessionId == session_id)
        ]

    def get(self, request_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(request_id)
        return entry.request if entry else None

    @staticmethod
    def cache_key(
        session_id: str,
        tool: str,
        args: dict[str, Any] | None = None,
        folder: str | None = None,
    ) -> tuple[str, str, str]:
        """``(session, tool, first command token)``; the token is "" off-shell.

        A write outside the project is keyed by its folder instead, so "allow
        for this session" covers that folder, not every folder.
        """
        if folder:
            return (session_id, tool, f"dir:{folder}")
        command = command_of(args or {})
        return (session_id, tool, first_token(command) if command else "")

    @staticmethod
    def line_key(
        session_id: str, tool: str, args: dict[str, Any], workdir: Any = None
    ) -> tuple[str, str, str, str, bool] | None:
        """The exact-line memory's key for a compound line (m21), or ``None``.

        The line with its whitespace normalised, the folder it runs in (the
        session's folder joined with ``args.cwd``) and whether it runs in the
        background: another folder or another spelling is another line.
        """
        command = command_of(args)
        line = exact_line(command) if command and is_compound(command) else None
        if line is None:
            return None
        cwd = str(args.get("cwd") or "").strip()
        folder = posixpath.normpath(posixpath.join(str(workdir or ""), cwd)) if (
            workdir or cwd) else ""
        return (session_id, tool, line, folder, bool(args.get("background")))

    def _home(self) -> Any:
        return getattr(self.paths, "home", None)

    def script_grant_state(
        self, session: Any, tool: str, args: dict[str, Any], offer: ApprovalRemember
    ) -> tuple[str, str | None]:
        """``("granted", scope)``, ``("changed", names)`` or ``("none", None)`` (m21 D).

        A grant holds while every file it holds to has the digest it had when
        the person allowed it, and the call runs the same files: the session's
        own grant first, then the project's (kept in the owner's settings).
        """
        workdir = getattr(session, "workdir", None)
        key = _script_key(session.id, tool, args, workdir)
        if key is None or not offer.scripts:
            return "none", None
        now = tuple(sorted((s.path, s.sha256) for s in offer.scripts))
        held: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        if key in self._script_grants:
            held.append(("session", self._script_grants[key]))
        if self.allowlist is not None:
            for item in self.allowlist.script_grants(workdir):
                grant = item.grant or {}
                if (grant.get("tool"), tuple(grant.get("words") or ()),
                        grant.get("folder")) == (tool, key[2], key[3]):
                    held.append(("project", tuple(sorted(
                        (s["path"], s["sha256"]) for s in grant["scripts"]))))
        changed: list[str] = []
        for scope, pairs in held:
            if pairs == now:
                return "granted", scope
            if {p for p, _ in pairs} == {p for p, _ in now}:
                old = dict(pairs)
                changed.extend(p for p, d in now if old.get(p) != d)
        if changed:
            project = os.path.realpath(str(workdir)) if workdir else ""
            names = sorted({os.path.relpath(os.path.realpath(p), project) if project else p
                            for p in changed})
            return "changed", ", ".join(names[:3])
        return "none", None

    def _store_script_grant(
        self, request: ApprovalRequest, scope: str, workdir: Any
    ) -> None:
        """Remember a person's allow of a script grant (m21 D)."""
        offer = request.remember
        if offer is None or offer.kind != "script" or not offer.scripts:
            return
        key = _script_key(request.sessionId, request.tool, request.args, workdir)
        if key is None:
            return
        pairs = tuple(sorted((s.path, s.sha256) for s in offer.scripts))
        if scope == "session":
            self._script_grants[key] = pairs
            return
        if scope != "project" or self.allowlist is None or workdir is None:
            return
        try:
            self.allowlist.add_script(
                workdir=workdir, tool=request.tool, words=list(key[2]), folder=key[3],
                scripts=[{"path": s.path, "sha256": s.sha256} for s in offer.scripts],
                display=offer.display,
            )
        except (OSError, ValueError):  # pragma: no cover - a bad store never breaks a turn
            log.warning("could not store a script grant for %r", offer.pattern, exc_info=True)

    def cached(
        self,
        session_id: str,
        tool: str,
        args: dict[str, Any] | None = None,
        folder: str | None = None,
        workdir: Any = None,
    ) -> bool:
        """True when this session, or a session above it, approved this call shape.

        A subagent's call is covered by a "for this session" answer its parent
        (or grandparent) gave (addendum 18); the parent's ``always`` answers
        live in the allowlist and cover it already.  A command line that is
        not one plain command (``git status ; id``) is never covered: the
        cache is keyed by its first word only.
        """
        command = command_of(args or {})
        if command and not folder and not matchable(command):
            # m21: a compound line is covered by its own exact-line answer (in
            # this session only), or when every simple command of it is covered.
            key = self.line_key(session_id, tool, args or {}, workdir)
            if key is not None and key in self._lines:
                return True
            parts = segments(command)
            return parts is not None and all(
                chain_program_ok(part)
                and self.cached(session_id, tool, {**(args or {}), "command": part})
                for part in parts
            )
        for ancestor in self._lineage(session_id):
            if self.cache_key(ancestor, tool, args, folder) in self._cache:
                return True
        return False

    def _lineage(self, session_id: str) -> list[str]:
        """``session_id`` and the sessions it descends from, nearest first."""
        chain = [session_id]
        sessions = getattr(self.hub, "sessions", None)
        lookup = getattr(sessions, "get", None)
        current = session_id
        while callable(lookup) and len(chain) < 16:
            session = lookup(current)
            # Only a subagent inherits: a scheduled run also names the thread
            # that scheduled it as its parent, and must not borrow its answers.
            if session is None or getattr(session, "kind", "chat") != "subagent":
                break
            parent = getattr(session, "parent_session_id", None)
            if not parent or parent in chain:
                break
            chain.append(parent)
            current = parent
        return chain

    @property
    def timeout_sec(self) -> int:
        return max(1, int(self.settings.approvals.timeoutSec))

    # -- security lists and classification ---------------------------------
    def _projects(self) -> list[str]:
        from snowpea_core.security import enforce

        core = self.core
        if core is not None:
            return enforce.live_projects(core)
        sessions = getattr(self.hub, "sessions", None)
        found: list[str] = []
        with contextlib.suppress(Exception):
            for live in sessions.all() if sessions is not None else ():
                if getattr(live, "workdir", None):
                    found.append(str(live.workdir))
        return found

    def _security(
        self, session: Any, tool: str, args: dict[str, Any], tags: tuple[str, ...] | None,
        host_tool: bool = False,
    ) -> Any:
        from snowpea_core.security import enforce

        return enforce.judge(
            settings=self.settings, home=self._home(), workdir=getattr(session, "workdir", None),
            tool=tool, args=args, tags=tuple(tags or ()), projects=self._projects(),
            host_tool=host_tool,
        )

    async def _refuse_blocked(
        self, session: Any, tool: str, args: dict[str, Any], verdict: Any,
        external: ExternalRef | None,
    ) -> Decision:
        """Refuse a blocked call: announced, logged, never posted or cached."""
        from snowpea_core.security import enforce

        await enforce.announce(
            hub=self.hub, paths=self.paths, session_id=session.id, tool=tool, args=args,
            verdict=verdict, surface="external" if external is not None else "approval",
            core=self.core,
        )
        decision = Decision("deny", "once", "security", errors.SECURITY_BLOCKED,
                            reason=verdict.refusal)
        record = ApprovalRequest(requestId=f"ap-{uuid.uuid4().hex[:12]}", sessionId=session.id,
                                 tool=tool, args={}, external=external)
        self._append_log(record, decision)
        return decision

    def _classify(
        self, session: Any, tool: str, args: dict[str, Any], tags: tuple[str, ...] | None,
        external: ExternalRef | None,
    ) -> str:
        """The delegation engine's classification (``never`` when it cannot tell)."""
        try:
            from snowpea_core.jarvis.classify import ClassifyRequest, classify

            return classify(ClassifyRequest(
                tool=tool, args=dict(args or {}), tags=tuple(tags or ()),
                workdir=getattr(session, "workdir", None), session_id=session.id,
                home=str(self._home()) if self._home() else None,
                external=external is not None,
            )).classification
        except Exception:  # noqa: BLE001 - unknown is never-delegable
            return "never"

    # -- the ask -------------------------------------------------------
    async def request(
        self,
        session: Session,
        tool: str,
        args: dict[str, Any],
        *,
        risk: str = "medium",
        unattended: bool = False,
        timeout_sec: int | None = None,
        scope_hint: str = "once",
        cancel_event: asyncio.Event | None = None,
        note: str = "",
        cacheable: bool = True,
        site: str | None = None,
        guard: str | None = None,
        diff: str | None = None,
        reason_code: str | None = None,
        paths: list[str] | None = None,
        tags: tuple[str, ...] | None = None,
        person_only: bool = False,
        external: ExternalRef | None = None,
        host_tool: bool = False,
        external_terminal: dict[str, Any] | None = None,
    ) -> Decision:
        """Ask for permission to run ``tool``; block until answered or denied.

        ``note`` is the extra warning the surface shows with the prompt.
        ``cacheable`` is false for ``config`` calls, so answering "allow for the
        session" on one settings write does not silently cover the next one.
        ``tags`` are the call's effective permission tags, for Jarvis mode's
        router.  ``person_only`` marks a request only a person may answer
        (``ApprovalRequest.personOnly``): never cached, never remembered.
        ``external`` (1.53.0) marks a prompt bridged from an external CLI
        session (``session.id`` is then ``ext:<cli>:<id>``).
        ``host_tool``: the caller dispatches ``tool`` to the session's connected
        host (:func:`~snowpea_core.tools.host_tools.runs_on_host`), so the
        security check judges it as the agent loop did.
        """
        # The security lists first (1.54.0): a blocked call is refused here,
        # never shown, cached, routed or answerable; a call they cannot check is
        # the person's alone.
        security = self._security(session, tool, args, tags, host_tool)
        if security.blocked:
            return await self._refuse_blocked(session, tool, args, security, external)
        # m21: what only snowpea cannot tell ahead is an ordinary call in a session
        # in auto mode under security.unknownCode "auto" (Verdict.asks_in).
        asks_in = getattr(security, "asks_in", None)
        unchecked = bool(asks_in(getattr(session, "mode", None))) if callable(asks_in) \
            else bool(security.opaque)
        # m21 D: a call asked only for what its scripts' content does may be
        # remembered by a person for these files as they are now.
        offer = None if person_only or not unchecked else script_offer(
            tool, args, security, getattr(session, "workdir", None))
        if unchecked:
            person_only = True
            note = f"{security.why or UNCHECKED_REASON}. {note}".strip()
        if person_only:
            cacheable = False
        from snowpea_core.jarvis.classify import escape_text, unclean_text

        classification = self._classify(session, tool, args, tags, external)
        never = person_only or classification == "never"

        escaped = unclean_text(args) or unclean_text(tool)
        if escaped:
            # What runs holds characters a surface strips (security review 3, N3):
            # the card shows them written out, nothing remembers or caches the
            # answer, and Jarvis's router judges it opaque.
            cacheable = False
            offer = None
        if offer is not None:
            state, detail = self.script_grant_state(session, tool, args, offer)
            if state == "granted":
                return Decision("allow", detail or "session", "script-grant")
            if state == "changed":
                note = (f"the script changed since you allowed it: {detail}. {note}").strip()
        folder = (
            outside_folder(paths, workdir=getattr(session, "workdir", None), home=self._home())
            if reason_code == OUTSIDE_REASON
            else None
        )
        if reason_code == OUTSIDE_REASON and folder is None:
            # Too wide a folder to remember: this call only.
            cacheable = False
        # Security review 4, H3: the session cache is keyed by the first word, so
        # a guarded or never-delegable call is never served from it (it asks
        # again).  A write outside the project keyed by its folder (1.30.0) is
        # exactly what the person allowed and keeps its "for this session".
        first_word = folder is None
        line = self.line_key(session.id, tool, args, getattr(session, "workdir", None))
        if cacheable and not never and line is not None and line in self._lines:
            # m21: this very line, in this folder, allowed for this session by a
            # person: keyed by its whole text, so H3's first-word worry is moot.
            return Decision("allow", "session", "cache")
        if cacheable and not never and not (
            first_word and classification in ("guarded", "never")
        ) and self.cached(session.id, tool, args, folder):
            return Decision("allow", "session", "cache")

        # 0.2.91: the surface the person is prompting from (the turn's asker),
        # else the session's open origin, else nobody: the shared queue.
        origin = None if unattended else asking_conn(session, self._lookup())
        if origin is None:
            unattended = True
        # An explicit timeout_sec is a caller's own deadline and always counts
        # (0 expires at once).  Otherwise a person at the session is waited for.
        no_limit = timeout_sec is None and waits_for_a_person(origin)
        if timeout_sec is not None:
            timeout = timeout_sec
        else:
            timeout = 0 if no_limit else self.timeout_sec  # on the wire, 0: no limit
        request = ApprovalRequest(
            requestId=f"ap-{uuid.uuid4().hex[:12]}",
            sessionId=session.id,
            tool=escape_text(tool) if escaped else tool,
            args=escape_text(args) if escaped else args,
            risk=risk,
            timeoutSec=timeout,
            scopeHint=scope_hint,  # type: ignore[arg-type]
            note=note,
            site=site,
            guard=guard,
            diff=diff,
            reasonCode=reason_code,
            paths=list(paths) if paths else None,
            folder=folder,
            remember=offer if offer is not None else remember_for(
                tool, args, cacheable=cacheable, guard=guard, reason_code=reason_code
            ),
            personOnly=True if person_only else None,
            external=external,
            externalTerminal=external_terminal,
            argsEscaped=True if escaped else None,
            neverDelegable=True if never else None,
            reason=(security.reason or UNCHECKED_REASON) if unchecked else None,
            # m21: what could not be checked, part by part (the verdict's own words).
            uncheckedWhy=list(getattr(security, "unchecked_why", ()) or ()) or None
            if unchecked else None,
        )
        if self.router is not None:
            try:
                route = self.router(request, session, tuple(tags or ()), origin is not None)
            except Exception:  # noqa: BLE001 - a broken router leaves it to the person
                log.warning("approval router failed for %s", request.requestId, exc_info=True)
                route = "person"
            if unchecked and route is not None:
                route = "person"  # never Jarvis, never auto
            if route is not None:
                request = request.model_copy(update={"route": route})
        loop = asyncio.get_running_loop()
        entry = _Pending(
            request=request,
            future=loop.create_future(),
            unattended=unattended,
            no_limit=no_limit,
            origin_conn=origin,
            workdir=getattr(session, "workdir", None),
            cacheable=cacheable,
            host=getattr(session, "host_tools_from", None),
            owner=getattr(session, "owned_by", None),
            clock_sec=float(timeout if timeout else self.timeout_sec),
        )
        self._pending[request.requestId] = entry
        await self._announce_status(session.id, "awaiting_approval", entry.host)
        if origin is not None:
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
        else:
            await self._broadcast_pending(request, entry.host)
        await self._tell_observers(
            "approval.pending",
            {"request": request.model_dump(mode="json"), "interactive": origin is not None},
        )
        outer = float(timeout) + (GRACE_SECONDS if entry.task is not None else 0.0)
        try:
            decision = await self._await_decision(entry, outer, cancel_event)
        except RpcError as exc:  # pragma: no cover - transport failure
            decision = Decision("deny", "once", "error", exc.code)
        finally:
            self._pending.pop(request.requestId, None)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.task
        if not self.count(session.id):
            await self._announce_status(session.id, "running", entry.host)
        decision = guard_scope(request, decision, entry.workdir)
        if decision.decision == "allow" and getattr(security, "scripts", ()):
            # Security review 5, H4: the answer covers the scripts as they were
            # judged; one swapped while the question was open is not run.
            from snowpea_core.security.check import scripts_changed

            changed = scripts_changed(security)
            if changed:
                decision = Decision("deny", "once", "security", errors.SECURITY_BLOCKED,
                                    reason="the script changed after you were asked: "
                                    + ", ".join(changed[:3]))
        await self._resolve(
            request,
            decision,
            # The origin answered its own dialog and needs no echo; a decision
            # made elsewhere (the owner's messenger) must close that dialog.
            notify_exclude=entry.origin_conn if decision.by == "origin" else None,
            workdir=entry.workdir,
            unattended=entry.unattended,
            cacheable=entry.cacheable,
            host=entry.host,
        )
        return decision

    async def _broadcast_pending(self, request: ApprovalRequest, host: str | None = None) -> None:
        """Announce an unattended request to every authenticated surface.

        The gateway listens on the same hub, so a binding whose session raised
        the request also pushes it to the bound conversation with allow/deny
        buttons — one notification, two deliveries (contract §4).
        """
        if self.hub is None:
            return
        await self.hub.notify(
            "approval.pending",
            {"request": request.model_dump(mode="json"), "hostToolsFrom": host},
        )

    async def _tell_observers(self, method: str, params: dict[str, Any]) -> None:
        """Tell in-process observers (the gateway) about every request, attended or not."""
        tell = getattr(self.hub, "tell_observers", None)
        if tell is not None:
            await tell(method, params)

    async def _await_decision(
        self, entry: _Pending, timeout: float, cancel_event: asyncio.Event | None
    ) -> Decision:
        """Wait for the answer, the timeout, or an interrupt — whichever comes first."""
        waiters: list[asyncio.Future[Any]] = [entry.future]
        watcher: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            watcher = asyncio.ensure_future(cancel_event.wait())
            waiters.append(watcher)
        try:
            await asyncio.wait(
                waiters,
                timeout=None if entry.no_limit else timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        if entry.future.done():
            return entry.future.result()
        if cancel_event is not None and cancel_event.is_set():
            return Decision("deny", "once", "interrupted", errors.APPROVAL_DENIED)
        return Decision("deny", "once", "timeout", errors.APPROVAL_TIMEOUT)

    async def _to_shared_queue(self, entry: _Pending, why: str) -> None:
        """The origin's allow did not count: tell it why and open the request to
        the other surfaces (``approval.pending``), where a person can answer it."""
        entry.unattended = True
        await self._refused_to_origin(entry, why)
        if self.hub is not None:
            await self.hub.notify(
                "approval.pending",
                {"request": entry.request.model_dump(mode="json"),
                 "hostToolsFrom": entry.host},
                exclude=entry.origin_conn,
            )

    async def _ask_origin(self, entry: _Pending) -> None:
        """Send ``approval.request`` to the origin connection and record the answer."""
        request = entry.request
        started = asyncio.get_running_loop().time()
        try:
            answer = await entry.origin_conn.call(
                "approval.request",
                request.model_dump(mode="json"),
                timeout=None if entry.no_limit else float(request.timeoutSec),
            )
            waited = asyncio.get_running_loop().time() - started
            if client_timed_out(answer, waited, entry.clock_sec, CLIENT_APPROVAL_CARD_SEC):
                # 0.2.91: the asking client's own card gave up (the desktop app
                # answers {decision: "deny", scope: "once"} after 300 s), so
                # nobody answered: not a person's "no".
                surface = surface_name(entry.origin_conn)
                log.warning("approval %s: %s answered a bare deny after %.1f s: its own "
                            "timeout", request.requestId, surface, waited)
                if not entry.future.done():
                    entry.future.set_result(Decision(
                        "deny", "once", "origin-timeout", errors.APPROVAL_DENIED,
                        reason=f"nobody answered on {surface} within {int(round(waited))} s"))
                return
            if answer.get("decision") not in ("allow", "deny"):
                # 0.2.90: a reply with no decision (``null``, ``{}``, an older or
                # broken host) is not a person's "no": nobody saw a card.  Say
                # what refused it instead of "(origin)".
                shown = str(answer.get("result") if set(answer) == {"result"} else answer)
                log.warning("approval %s: the origin answered without a decision: %s",
                            request.requestId, shown[:200])
                if not entry.future.done():
                    entry.future.set_result(Decision(
                        "deny", "once", "origin-no-answer", errors.APPROVAL_DENIED,
                        reason=("the surface that started this conversation could not show "
                                f"the approval (it replied without a decision: {shown[:120]})")))
                return
            allowed = answer.get("decision") == "allow"
            scope = str(answer.get("scope", "once"))
            from snowpea_core.server.person_gate import connection_kind

            if allowed and connection_kind(entry.origin_conn) == "connector":
                # Security review 6, L-1: a connector (or an unknown client kind)
                # never approves, origin or not: its allow is no answer.  The
                # request stays open for a person's surface (approval.respond).
                log.warning("approval %s: a connector origin's allow is not an answer",
                            request.requestId)
                await self._to_shared_queue(entry, "a connector cannot approve")
                return
            if allowed and scope != "once":
                from snowpea_core.server.settings_gate import acts_for_person

                actor = answer.get("actor")
                if connection_kind(entry.origin_conn) == "browser" and not acts_for_person(
                    entry.origin_conn, actor if isinstance(actor, str) else None
                ):
                    # A browser's wider answer counts only from a click (``actor:
                    # "person"``); otherwise it allows this call alone.
                    scope = "once"
                scope = clamp_scope(entry.origin_conn, scope)
            decision = Decision(
                decision="allow" if allowed else "deny",
                scope=scope,
                by="origin",
                reason=str(answer.get("reason") or ""),
                local_person=_local_person(entry.origin_conn),
                line_ok=_remembering_person(
                    entry.origin_conn,
                    answer.get("actor") if isinstance(answer.get("actor"), str) else None,
                ),
            )
            if allowed and request.personOnly:
                # The origin is asked like anyone else: a person's connection only.
                try:
                    decision.person = _person_answer(request, entry.origin_conn, "origin",
                                                     self.settings, "allow")
                    if not (_script_remember(request) and decision.line_ok):
                        decision.scope = "once"
                except RpcError as exc:
                    # Not a person's allow (1.55.0): never a silent ignore and no
                    # longer a silent deny either.  The request stays open and
                    # moves to the shared queue, so a person's surface can answer
                    # it with approval.respond; the origin is told why and gets
                    # the card back (it may still decline).  Else it times out.
                    log.warning("approval %s: origin allow refused (%s)", request.requestId,
                                exc.message)
                    await self._to_shared_queue(entry, exc.message)
                    return
        except TimeoutError:
            decision = Decision("deny", "once", "timeout", errors.APPROVAL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead surface denies, it never hangs
            log.warning("approval %s could not reach its origin: %s", request.requestId, exc)
            decision = Decision("deny", "once", "origin-unreachable", errors.APPROVAL_DENIED,
                                reason=("the surface that started this conversation could not "
                                        f"show the approval: {str(exc)[:160]}"))
        if not entry.future.done():
            entry.future.set_result(decision)

    def _lookup(self) -> Any:
        """Session id -> live session (``asking_conn`` walks a child's parents)."""
        sessions = getattr(self.hub, "sessions", None)
        lookup = getattr(sessions, "get", None)
        return lookup if callable(lookup) else None

    async def follow_asker(self) -> int:
        """Move waiting requests to their session's new asker (0.2.91); how many moved.

        The person started typing on another surface while a request waited on
        the old one: the request is asked there again, and the old surface's
        card is withdrawn (``approval.resolved {outcome: "withdrawn"}``, as for
        a request routed to Jarvis).  A shared-queue request, one routed to
        Jarvis, or one whose session has no new open asker stays where it is.
        """
        lookup = self._lookup()
        if lookup is None:
            return 0
        moved = 0
        for entry in list(self._pending.values()):
            if entry.unattended or entry.origin_conn is None or entry.future.done():
                continue
            if entry.request.route == "jarvis" or entry.owner is not None:
                continue
            target = asking_conn(lookup(entry.request.sessionId), lookup)
            if target is None or target is entry.origin_conn:
                continue
            old, task = entry.origin_conn, entry.task
            entry.origin_conn = target
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
            if task is not None and not task.done():
                task.cancel()
            await withdraw(old, "approval", entry.request.requestId)
            moved += 1
        return moved

    async def _refused_to_origin(self, entry: _Pending, message: str) -> None:
        """Re-announce a still-open request to the origin whose reply was refused (1.55.0)."""
        params = {
            "request": entry.request.model_dump(mode="json"),
            "hostToolsFrom": entry.host,
            "refused": {
                "code": errors.JARVIS_PERSON_ONLY,
                "reason": "person_only",
                "message": message,
            },
        }
        try:
            await entry.origin_conn.notify("approval.pending", params)
        except Exception:  # noqa: BLE001 - a closed origin: the request times out
            log.debug("could not re-announce approval %s", entry.request.requestId,
                      exc_info=True)

    # -- the answer ----------------------------------------------------
    async def respond(
        self,
        request_id: str,
        decision: str,
        scope: str = "once",
        by: str = "client",
        conn: Any = None,
        reason: str = "",
        actor: str | None = None,
        reauth: str | None = None,
        grant_expires_at: str | None = None,
    ) -> Decision | None:
        """Resolve a pending request (``approval.respond``).

        Returns the decision as it will apply (its ``scope`` after
        :func:`guard_scope`), or ``None`` when the request was already answered
        and this call changed nothing.  ``reauth`` / ``grant_expires_at``
        (1.56.0): a remote web login allowing Jarvis's ``delegation_set``
        proposal (:func:`_remote_grant_answer`); ignored otherwise, never kept.
        """
        entry = self._pending.get(request_id)
        if entry is None:
            raise RpcError(errors.NOT_FOUND, f"no pending approval {request_id}")
        if conn is not None:
            # A browser or connector answers its own sessions' requests; another
            # session's only for its person; a person-only one never (review 3).
            from snowpea_core.server.settings_gate import answer_refusal

            sessions = getattr(self.hub, "sessions", None)
            lookup = getattr(sessions, "get", None)
            session = lookup(entry.request.sessionId) if callable(lookup) else None
            answer_refusal(conn, session, request_id, "approval",
                           origin_conn=entry.origin_conn, owner=(entry.owner, entry.host),
                           person_only=bool(entry.request.personOnly), actor=actor,
                           route=entry.request.route)
        if not entry.unattended and entry.origin_conn is not None and conn is not None:
            if conn is not entry.origin_conn:
                raise RpcError(
                    errors.UNAUTHORIZED,
                    "interactive approvals may only be answered by the originating surface",
                )
        if entry.owner is not None and not (
            conn is not None
            and getattr(conn, "client_kind", None) == "browser"
            and getattr(conn, "client_id", None) == entry.owner
        ):
            raise RpcError(
                errors.UNAUTHORIZED,
                "a browser-owned job's approvals may only be answered by that browser",
            )
        remote = await _remote_grant_answer(entry.request, conn, decision, self.settings,
                                            reauth, grant_expires_at)
        del reauth  # checked (or not needed); never kept past this point
        if remote is not None:
            person, grant_expires = remote
        else:
            person = _person_answer(entry.request, conn, by, self.settings, decision)
            grant_expires = None
        if entry.future.done():
            return None
        line_ok = conn is not None and not str(by).startswith("jarvis") \
            and _remembering_person(conn, actor)
        # A person-only answer applies once, except a person's own allow of a
        # script grant (m21 D), which keeps its scope (guard_scope narrows it).
        keeps = person is not None and line_ok and _script_remember(entry.request)
        answer = Decision(
            decision="allow" if decision == "allow" else "deny",
            scope="once" if person is not None and not keeps else clamp_scope(conn, scope),
            by=by,
            reason=reason,
            person=person,
            local_person=_local_person(conn),
            grant_expires_at=grant_expires,
            line_ok=line_ok,
        )
        entry.future.set_result(answer)
        # The scope that will apply (``guard_scope`` runs the same rules again
        # where the request is resolved): approval.respond reports it (1.54.0).
        return guard_scope(entry.request, answer, entry.workdir)

    async def _resolve(
        self,
        request: ApprovalRequest,
        decision: Decision,
        *,
        notify_exclude: Any = None,
        workdir: Any = None,
        unattended: bool = False,
        cacheable: bool = True,
        host: str | None = None,
    ) -> None:
        """Cache, persist, log and announce a finished request."""
        # A local-dev project rule is per origin; the session cache is keyed by
        # tool alone and would allow every navigation, so only the rule counts.
        per_origin = local_dev_origin(request.tool, request.args, workdir) is not None
        exact = request.remember is not None and bool(request.remember.exactLine)
        if _script_remember(request):
            # m21 D: a person's allow of a script grant; nothing else is cached
            # (the call is person-only: never its first word, never a rule).
            if decision.allowed and decision.line_ok and decision.person is not None:
                self._store_script_grant(request, decision.scope, workdir)
        elif exact:
            # m21: the exact line, for this session, in memory: never the first
            # word (it would cover every plain ``ls`` after ``ls | sort``), never
            # an allowlist entry.
            key = self.line_key(request.sessionId, request.tool, request.args, workdir)
            if (cacheable and decision.allowed and decision.scope == "session"
                    and decision.line_ok and key is not None):
                self._lines.add(key)
        elif (
            cacheable
            and decision.allowed
            and decision.scope in CACHING_SCOPES
            and not (per_origin and decision.scope == "project")
        ):
            self._cache.add(
                self.cache_key(
                    request.sessionId, request.tool, request.args, _outside_folder(request)
                )
            )
        if (cacheable and not exact and not _script_remember(request) and decision.allowed
                and decision.scope in PERSISTING_SCOPES):
            self._persist(request, decision.scope, workdir, host,
                          local_person=decision.local_person)
        self._append_log(request, decision, unattended=unattended)
        if self.hub is not None:
            resolved = {
                "requestId": request.requestId,
                "decision": decision.decision,
                "by": decision.by,
                # The scope actually applied (1.54.0): a widening reply kept to
                # "once" says so; null for a deny.
                "scope": decision.scope if decision.allowed else None,
            }
            await self.hub.notify("approval.resolved", resolved, exclude=notify_exclude)
            await self._tell_observers("approval.resolved", dict(resolved))

    def _persist(
        self, request: ApprovalRequest, scope: str, workdir: Any, host: str | None = None,
        *, local_person: bool = False,
    ) -> None:
        """Turn a ``project``/``always`` answer into an allowlist entry.

        ``local_person``: a local person's own connection answered.  Any other
        answer (a remote web login, a browser, a messenger, a connector) still
        stores the rule, but does not re-pin the project's settings trust: the
        changed file waits for a local person (security review 6, L-2).
        """
        if self.allowlist is None:
            return
        store = cast(AllowlistScope, PERSISTING_SCOPES[scope])
        if request.reasonCode == OUTSIDE_REASON:
            # A folder rule lives with the owner, never in the project's file.
            store = "global"
        if store == "project" and workdir is None:
            log.warning("cannot store a project allowlist entry without a workdir")
            return
        origin = None
        reason_code = None
        local_dev = local_dev_origin(request.tool, request.args, workdir)
        if scope == "project" and local_dev is not None:
            # "Always for this project" on a local dev server: this tool, this
            # origin, and only asks that name the same reason (security review).
            origin, reason_code = local_dev, str(request.args.get("reasonCode"))
        elif scope == "site":
            # "Always allow on this site": the tool, limited to the page's origin.
            origin = site_of(request.args, request.site)
            if origin is None:
                log.warning("a site-scoped approval for %s had no site; not stored", request.tool)
                return
        command = command_of(request.args)
        if request.reasonCode == OUTSIDE_REASON:
            # "Always for this folder", never "always write_file anywhere".
            if not request.folder:
                return
            pattern, target = pattern_for_dir(request.folder), DIR_TARGET
            reason_code = OUTSIDE_REASON
        elif command and origin is None and _shell_command(request):
            # The rule the card offered (1.34.0), never the bare first word.
            remember = _remembered(request)
            if remember is None:
                log.info("approval %s: no safe rule for %r; not stored", request.requestId, command)
                return
            pattern, target = remember.pattern, SHELL_TARGET
        elif command and origin is None:
            pattern, target = pattern_for_command(command), SHELL_TARGET
        else:
            pattern, target = pattern_for_tool(request.tool), tool_target(request.tool)
        try:
            # A global rule answered in a browser profile's session is that
            # profile's rule (addendum 8); project rules stay shared.
            self.allowlist.add(
                pattern,
                store,
                target,
                workdir=workdir,
                origin=origin,
                host=host if store == "global" else None,
                reason_code=reason_code,
                keeps_trust=local_person,
            )
        except (OSError, ValueError):  # pragma: no cover - a bad store never breaks a turn
            log.warning("could not store allowlist entry %r", pattern, exc_info=True)

    def _append_log(
        self, request: ApprovalRequest, decision: Decision, *, unattended: bool = False
    ) -> None:
        if self.paths is None:
            return
        record = {
            "ts": utc_now(),
            "requestId": request.requestId,
            "sessionId": request.sessionId,
            "tool": request.tool,
            "args": request.args,
            "risk": request.risk,
            "decision": decision.decision,
            "scope": decision.scope,
            "by": decision.by,
            "code": decision.code,
            "unattended": unattended,
        }
        try:
            self.paths.ensure()
            from snowpea_core.util.atomic import append_text

            append_text(self.paths.approvals_log, json.dumps(record) + "\n")
        except OSError:  # pragma: no cover - audit log must never break a turn
            log.warning("could not append to %s", self.paths.approvals_log, exc_info=True)


async def _remote_grant_answer(
    request: ApprovalRequest, conn: Any, decision: str, settings: Any,
    reauth: str | None, grant_expires_at: str | None,
) -> tuple[Any, str] | None:
    """A remote web login allowing Jarvis's ``delegation_set`` proposal while
    ``jarvis.delegation.remoteGrant`` is on (1.56.0): ``(person, expiry)``.

    ``None`` when this is not that (anything else keeps :func:`_person_answer`'s
    rule; with the switch off the engine refuses the grant as before).  The
    proposal's limits are judged first (scope session or project, no guarded
    class; the expiry the person chose within 24 h, never later than the
    proposal's), then the password like a sign-in.  A refusal is an RPC error and
    the request stays pending, so a local person can still answer it.
    """
    if not request.personOnly or decision != "allow" or request.tool != "delegation_set":
        return None
    from snowpea_core.jarvis import remote_grant
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "remote_login" or not remote_grant.enabled(settings):
        return None
    from datetime import UTC, datetime

    from snowpea_core.jarvis.delegation import (
        Caller,
        DelegationError,
        PersonOnly,
        parse_time,
        person_refusal,
    )

    args = request.args if isinstance(request.args, dict) else {}
    now = datetime.now(UTC)
    try:
        chosen = parse_time(grant_expires_at) if grant_expires_at else None
        if chosen is not None and chosen <= now:
            raise DelegationError("grantExpiresAt is in the past")
        expiry = remote_grant.check_limits(str(args.get("scope") or ""),
                                           args.get("allow") or [], chosen, now)
        proposed = args.get("expiresAt")
        if isinstance(proposed, str) and proposed:
            with contextlib.suppress(DelegationError):
                expiry = min(expiry, parse_time(proposed))
        caller = Caller.from_connection(conn)
        refusal = person_refusal(caller, settings, guarded=False)
        if refusal is not None:
            raise PersonOnly(refusal)
        await remote_grant.reauthenticate(conn, reauth)
    except DelegationError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from None
    except PersonOnly as exc:
        raise remote_grant.as_rpc(exc) from None
    stamp = expiry.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return replace(caller, reauthenticated=True), stamp


def _person_answer(
    request: ApprovalRequest, conn: Any, by: str, settings: Any, decision: str
) -> Any:
    """The person answering a ``personOnly`` request; refuses anyone else.

    ``None`` for an ordinary request or a decline.  A person is a human client connection
    (:func:`~snowpea_core.jarvis.delegation.require_person`), or the bound
    person of a messenger chat (the gateway answers as ``gateway:<platform>:<user>``
    only after checking the approver) while ``jarvis.messenger.allowGrants``
    is on.  Jarvis, the engine and in-process callers are refused.
    """
    if not request.personOnly or decision != "allow":
        # Declining changes nothing, so anyone who sees it may decline.
        return None
    from snowpea_core.jarvis.delegation import Caller, person_refusal

    if conn is not None:
        caller = Caller.from_connection(conn)
    elif str(by).startswith("gateway:"):
        caller = Caller.messenger(str(by).split(":")[1] or "messenger", bound_person=True)
    else:
        caller = Caller(kind="jarvis" if str(by).startswith("jarvis") else "system")
    allow = (request.args or {}).get("allow") if isinstance(request.args, dict) else None
    refusal = person_refusal(caller, settings, guarded=bool(allow))
    if refusal is not None:
        raise RpcError(errors.JARVIS_PERSON_ONLY, f"only a person may answer this: {refusal}")
    return caller


__all__ = [
    "CACHING_SCOPES",
    "ApprovalRequests",
    "GRACE_SECONDS",
    "PERSISTING_SCOPES",
    "ApprovalQueue",
    "Decision",
    "remember_for",
]
