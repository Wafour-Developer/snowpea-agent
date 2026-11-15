"""The delegation engine: who decides a request — the engine, Jarvis or the person.

This module is the security boundary of Jarvis mode.  The Jarvis model only
ever chooses among options the engine hands it, and every answer it gives is
re-checked here (:meth:`DelegationEngine.validate_decision`) before it reaches
:meth:`~snowpea_core.permissions.approval_queue.ApprovalQueue.respond`.

**Grants** live in ``$SNOWPEA_HOME/jarvis/delegation.db`` (SQLite), outside
``settings.json`` on purpose: a worker session's settings write cannot touch
them, and a write to the file itself is snowpea config (``never``).  A grant
has a scope (``global``, ``project:<root>``, ``session:<id>``), a level
(``staged`` / ``full``), the guarded classes it hands over, who confirmed it
and when, and an expiry.  The most specific live grant wins (session, then the
deepest project root containing the session's folder, then global, then
``jarvis.delegation.default``).  Only a person creates one
(:func:`require_person`): a human client connection, or the bound person in a
messenger while ``jarvis.messenger.allowGrants`` is on.  Nothing a tool or a
model produces can call it.

**Routing** (:meth:`DelegationEngine.route`):

========== ===============================================================
``never``  person
paused     person (the person typed into the session or took it over)
``safe``   ``auto`` — the engine allows it without a model call
``risky``  ``person`` under ``staged``, ``jarvis`` under ``full``
guarded    ``jarvis`` only when *every* guarded class of the request is in
           the effective grant's ``allow``; otherwise ``person``
========== ===============================================================

A project hidden from Jarvis (``jarvis.expose`` off) and an exhausted budget
route everything to the person.

**Pauses**: the person's own message into a managed session pauses Jarvis
there until ``now + jarvis.quietMinutes`` (extended by each further input);
``takeover`` pauses it until a hand-back or the given minutes; ``handback``
(person only) returns authority at once.  Pauses are stored in the same
database, so a takeover survives a daemon restart.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from snowpea_core.jarvis.classify import (
    GUARDED_CLASSES,
    ClassifyRequest,
    GuardedClass,
    Verdict,
    classify,
)
from snowpea_core.jarvis.ledger import Ledger
from snowpea_core.server.protocol import (
    ExternalRef,
    JarvisGrant,
    JarvisPause,
    SessionJarvisState,
)

log = logging.getLogger("snowpea.jarvis.delegation")

Scope = Literal["global", "project", "session"]
Level = Literal["staged", "full"]
Route = Literal["auto", "jarvis", "person"]

LEVELS: tuple[Level, ...] = ("staged", "full")
SCOPES: tuple[Scope, ...] = ("global", "project", "session")
DEFAULT_QUIET_MINUTES = 10
#: Longest takeover a person may ask for (a week), as on the wire.
MAX_TAKEOVER_MINUTES = 10080
#: Routes remembered for :meth:`DelegationEngine.validate_decision`.
ROUTE_MEMORY = 4096

#: Client kinds (``system.hello`` ``clientKind``) a person sits behind.  The
#: desktop app and the TUI send none.  Anything else — a browser host, the
#: remote connector, an unknown client — may not change delegation.
HUMAN_CLIENT_KINDS: frozenset[str | None] = frozenset(
    {None, "", "ide", "desktop", "tui", "cli", "web", "app"}
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class DelegationError(ValueError):
    """A malformed grant or target; the RPC layer answers ``invalid_params``."""

    code = "invalid_params"


class PersonOnly(PermissionError):
    """Only the person may do this; the RPC layer answers ``jarvis_person_only``."""

    code = "jarvis_person_only"


class DecisionRefused(PermissionError):
    """Jarvis tried to answer something the engine did not route to it."""

    code = "jarvis_person_only"

    def __init__(self, message: str, route: RouteResult | None = None) -> None:
        super().__init__(message)
        self.route = route


# ---------------------------------------------------------------------------
# who is asking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who asks for a delegation change.

    ``connection`` is a client over RPC (``surface`` its ``clientKind``,
    ``remote`` true for a login on the remote web listener); ``messenger`` the
    gateway acting for a chat (``bound_person`` false for anyone but the
    binding's own person); ``jarvis`` the Jarvis session or its tools;
    ``system`` core itself (timers, session ends).
    """

    kind: Literal["connection", "messenger", "jarvis", "system"]
    surface: str | None = None
    remote: bool = False
    bound_person: bool = False

    @classmethod
    def from_connection(cls, conn: Any) -> Caller:
        """The caller an RPC handler sees; ``None`` (in-process) is ``system``."""
        if conn is None:
            return cls(kind="system", surface="core")
        return cls(
            kind="connection",
            surface=getattr(conn, "client_kind", None),
            remote=bool(getattr(conn, "remote_login", False)),
        )

    @classmethod
    def messenger(cls, platform: str, *, bound_person: bool) -> Caller:
        return cls(kind="messenger", surface=platform, bound_person=bound_person)

    @property
    def label(self) -> str:
        """``grantedBy`` / ledger ``by``: the surface, or what kind of caller it was."""
        if self.kind == "connection":
            return str(self.surface or "local")
        if self.kind == "messenger":
            return str(self.surface or "messenger")
        return self.kind


JARVIS = Caller(kind="jarvis", surface="jarvis")
SYSTEM = Caller(kind="system", surface="core")


def person_refusal(caller: Caller, settings: Any, *, guarded: bool) -> str | None:
    """Why ``caller`` may not change delegation, or ``None`` when it may.

    ``guarded`` is true when the change hands over a guarded class (or, for a
    revoke, widens what Jarvis may decide): a remote web login may change
    levels but not that, since it has not re-authenticated.
    """
    if caller.kind == "jarvis":
        return "Jarvis cannot change its own authority; it can only ask the person"
    if caller.kind == "system":
        return "only a person may change Jarvis's authority"
    if caller.kind == "messenger":
        allow = bool(_setting(settings, ("jarvis", "messenger", "allowGrants"), True))
        if not allow:
            return "grants from a messenger are off (jarvis.messenger.allowGrants)"
        if not caller.bound_person:
            return "only the person bound to this chat may change Jarvis's authority"
        return None
    if caller.surface not in HUMAN_CLIENT_KINDS:
        return f"a {caller.surface} connection cannot change Jarvis's authority"
    if caller.remote and guarded:
        return (
            "handing guarded classes to Jarvis needs a local connection "
            "(IDE, TUI or the local web page), not a remote web login"
        )
    return None


def require_person(caller: Caller, settings: Any, *, guarded: bool) -> None:
    """Raise :class:`PersonOnly` unless ``caller`` is the person (see :func:`person_refusal`)."""
    reason = person_refusal(caller, settings, guarded=guarded)
    if reason is not None:
        raise PersonOnly(reason)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effective:
    """The delegation in force for one session."""

    level: Level
    allow: frozenset[GuardedClass]
    grant: JarvisGrant | None
    #: Which scope supplied it: ``session``, ``project``, ``global`` or ``default``.
    source: str


@dataclass(frozen=True)
class PauseState:
    """Whether the person holds a session back from Jarvis right now."""

    paused: bool = False
    reason: Literal["typing", "takeover"] | None = None
    #: UTC ISO time authority returns; ``None`` while not paused or for an
    #: open-ended takeover.
    until: str | None = None
    project: str | None = None

    @property
    def taken_over(self) -> bool:
        """An open-ended takeover (``SessionJarvisState.takenOver``)."""
        return self.paused and self.reason == "takeover" and self.until is None


@dataclass(frozen=True)
class RouteResult:
    """Where the engine sent one request, and why."""

    route: Route
    verdict: Verdict
    level: Level
    #: The grant that let Jarvis decide it (or would have), when one did.
    grant_id: str | None
    #: Short reason: ``safe``, ``risky:staged``, ``guarded:not-allowed``, ``paused``…
    reason: str
    #: Guarded classes the effective grant does not hand over.
    missing: tuple[GuardedClass, ...] = ()

    @property
    def wire(self) -> str:
        """``ApprovalRequest.route``."""
        return self.route


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setting(settings: Any, path: tuple[str, ...], default: Any) -> Any:
    value = settings
    for name in path:
        value = getattr(value, name, None)
        if value is None:
            return default
    return value


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(text: str) -> datetime:
    """A wire timestamp (ISO-8601, ``Z`` or an offset) as an aware UTC datetime."""
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise DelegationError(f"not an ISO time: {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def canonical_root(root: str) -> str:
    """A project root as stored and compared: absolute, ``~`` expanded, symlinks resolved."""
    text = str(root or "").strip()
    if not text:
        raise DelegationError("a project grant needs its project root")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DelegationError(f"a project root must be absolute: {root}")
    try:
        return str(path.resolve())
    except (OSError, RuntimeError) as exc:
        raise DelegationError(f"cannot resolve project root {root}: {exc}") from exc


def target_key(
    session_id: str | None = None, external: ExternalRef | dict[str, Any] | None = None
) -> str:
    """The key a session is stored under: its id, or ``ext:<cli>:<id>``."""
    if external is not None:
        ref = (
            external if isinstance(external, ExternalRef) else ExternalRef.model_validate(external)
        )
        return f"ext:{ref.cli}:{ref.id}"
    if not session_id or not str(session_id).strip():
        raise DelegationError("a session target needs a sessionId or an external ref")
    return str(session_id).strip()


def _check_session_target(target: str) -> str:
    text = str(target or "").strip()
    if not text:
        raise DelegationError("a session grant needs its session id")
    if len(text) > 256 or any(ch.isspace() for ch in text):
        raise DelegationError(f"not a session id: {target!r}")
    return text


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0  # type: ignore[arg-type]


def _allow_list(values: Iterable[str]) -> tuple[GuardedClass, ...]:
    wanted = set()
    for value in values:
        if value not in GUARDED_CLASSES:
            raise DelegationError(
                f"unknown guarded class {value!r}; expected one of {', '.join(GUARDED_CLASSES)}"
            )
        wanted.add(value)
    return tuple(cls for cls in GUARDED_CLASSES if cls in wanted)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    target TEXT,
    project TEXT,
    level TEXT NOT NULL,
    allow TEXT NOT NULL DEFAULT '',
    granted_by TEXT NOT NULL,
    proposed_by TEXT NOT NULL DEFAULT 'person',
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    revoked_at TEXT,
    revoked_by TEXT,
    expired_noticed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS grants_scope ON grants (scope, target);
CREATE TABLE IF NOT EXISTS pauses (
    target TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    until TEXT,
    project TEXT,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
SCHEMA_VERSION = "1"

GrantListener = Callable[[str, JarvisGrant], None]
PauseListener = Callable[[str, PauseState], None]


class DelegationEngine:
    """Grants, pauses, routing and the server-side re-check of Jarvis's answers.

    ``settings`` is a :class:`~snowpea_core.config.settings.Settings` or a
    callable returning the current one (so a settings reload applies at
    once).  ``now`` is injectable for tests.  Every method is synchronous and
    thread-safe; listeners run after the change is committed.
    """

    def __init__(
        self,
        home: Path | str,
        settings: Any = None,
        *,
        ledger: Ledger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.home = Path(home)
        self.dir = self.home / "jarvis"
        self.db_path = self.dir / "delegation.db"
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self.ledger = ledger if ledger is not None else Ledger(self.dir / "ledger.jsonl")
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._routes: OrderedDict[str, RouteResult] = OrderedDict()
        #: The Jarvis session's id (set by the Jarvis service): its own requests are ``never``.
        self.jarvis_session_id: str | None = None
        #: ``(reason, grant)`` for every set, revoke and expiry.
        self.grant_listeners: list[GrantListener] = []
        #: ``(target, state)`` whenever a pause starts or ends.
        self.pause_listeners: list[PauseListener] = []
        #: True once :func:`_wire_events` announces this engine's changes.
        self.events_wired = False

    # -- plumbing ------------------------------------------------------------
    @property
    def settings(self) -> Any:
        value = self._settings
        return value() if callable(value) else value

    def now(self) -> datetime:
        return self._now().astimezone(UTC)

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self.dir, 0o700)
            fresh = not self.db_path.exists()
            db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,)
            )
            if fresh:
                with contextlib.suppress(OSError):
                    os.chmod(self.db_path, 0o600)
            self._db = db
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _emit_grant(self, reason: str, grant: JarvisGrant) -> None:
        for listener in list(self.grant_listeners):
            try:
                listener(reason, grant)
            except Exception:  # noqa: BLE001
                log.warning("grant listener failed", exc_info=True)

    def _emit_pause(self, target: str, state: PauseState) -> None:
        for listener in list(self.pause_listeners):
            try:
                listener(target, state)
            except Exception:  # noqa: BLE001
                log.warning("pause listener failed", exc_info=True)

    @property
    def default_level(self) -> Level:
        value = _setting(self.settings, ("jarvis", "delegation", "default"), "staged")
        return value if value in LEVELS else "staged"

    @property
    def quiet_minutes(self) -> int:
        value = _setting(self.settings, ("jarvis", "quietMinutes"), DEFAULT_QUIET_MINUTES)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return DEFAULT_QUIET_MINUTES

    @property
    def project_ttl_hours(self) -> int:
        value = _setting(self.settings, ("jarvis", "delegation", "projectTtlHours"), 24)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 24

    # -- grants ----------------------------------------------------------------
    @staticmethod
    def _grant(row: sqlite3.Row) -> JarvisGrant:
        return JarvisGrant(
            id=row["id"],
            scope=row["scope"],
            target=row["target"],
            level=row["level"],
            allow=[cls for cls in (row["allow"] or "").split(",") if cls],  # type: ignore[misc]
            grantedBy=row["granted_by"],
            proposedBy=row["proposed_by"],
            grantedAt=row["granted_at"],
            expiresAt=row["expires_at"],
            note=row["note"] or "",
        )

    def _live(self, row: sqlite3.Row, now: datetime) -> bool:
        if row["revoked_at"]:
            return False
        expires = row["expires_at"]
        return not (expires and parse_time(expires) <= now)

    def sweep_expired(self) -> list[JarvisGrant]:
        """Note grants that expired since the last sweep; announce each once."""
        now = self.now()
        expired: list[JarvisGrant] = []
        with self._lock:
            db = self._conn()
            rows = db.execute(
                "SELECT * FROM grants WHERE revoked_at IS NULL AND expires_at IS NOT NULL "
                "AND expired_noticed = 0"
            ).fetchall()
            for row in rows:
                if parse_time(row["expires_at"]) <= now:
                    db.execute("UPDATE grants SET expired_noticed = 1 WHERE id = ?", (row["id"],))
                    expired.append(self._grant(row))
        for grant in expired:
            self.ledger.record_grant("expired", grant, by="core")
            self._emit_grant("expired", grant)
        return expired

    def list_grants(
        self,
        scope: Scope | None = None,
        target: str | None = None,
        include_expired: bool = False,
    ) -> list[JarvisGrant]:
        """Grants, newest first; live ones only unless ``include_expired``."""
        self.sweep_expired()
        now = self.now()
        query = "SELECT * FROM grants"
        clauses: list[str] = []
        values: list[Any] = []
        if scope is not None:
            clauses.append("scope = ?")
            values.append(scope)
        if target is not None:
            clauses.append("target = ?")
            values.append(canonical_root(target) if scope == "project" else str(target).strip())
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY granted_at DESC, rowid DESC"
        with self._lock:
            rows = self._conn().execute(query, values).fetchall()
        return [
            self._grant(row) for row in rows if include_expired or self._live(row, now)
        ]

    def get_grant(self, grant_id: str) -> JarvisGrant | None:
        with self._lock:
            row = self._conn().execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        return self._grant(row) if row is not None else None

    def set_grant(
        self,
        scope: str,
        target: str | None,
        level: str,
        allow: Iterable[str] = (),
        *,
        caller: Caller,
        expires_at: str | None = None,
        note: str = "",
        proposed_by: Literal["person", "jarvis"] = "person",
        project: str | None = None,
    ) -> tuple[JarvisGrant, str | None]:
        """Create or replace the grant for one scope and target; the person only.

        ``project`` (a session grant only) records the session's project root,
        which lets a later revoke tell whether it would widen anything.
        Returns the grant now in force and the id of the one it replaced.
        """
        if scope not in SCOPES:
            raise DelegationError(f"unknown scope {scope!r}; expected global, project or session")
        if level not in LEVELS:
            raise DelegationError(f"unknown level {level!r}; expected staged or full")
        allowed = _allow_list(allow)
        require_person(caller, self.settings, guarded=bool(allowed))
        stored_target: str | None
        if scope == "global":
            if target:
                raise DelegationError("a global grant takes no target")
            if allowed:
                raise DelegationError("a global grant cannot hand over guarded classes")
            stored_target = None
        elif scope == "project":
            stored_target = canonical_root(target or "")
        else:
            stored_target = _check_session_target(target or "")
        now = self.now()
        expires: str | None = None
        if expires_at:
            moment = parse_time(expires_at)
            if moment <= now:
                raise DelegationError("expiresAt is in the past")
            expires = _iso(moment)
        elif scope == "project":
            expires = _iso(now + timedelta(hours=self.project_ttl_hours))
        session_project = canonical_root(project) if (scope == "session" and project) else None
        grant_id = f"jg-{uuid.uuid4().hex[:12]}"
        replaced: JarvisGrant | None = None
        with self._lock:
            db = self._conn()
            rows = db.execute(
                "SELECT * FROM grants WHERE scope = ? AND target IS ? AND revoked_at IS NULL",
                (scope, stored_target),
            ).fetchall()
            live = [row for row in rows if self._live(row, now)]
            stamp = _iso(now)
            db.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    db.execute(
                        "UPDATE grants SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                        (stamp, f"replaced:{grant_id}", row["id"]),
                    )
                db.execute(
                    "INSERT INTO grants (id, scope, target, project, level, allow, granted_by, "
                    "proposed_by, granted_at, expires_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant_id, scope, stored_target, session_project, level,
                        ",".join(allowed), caller.label, proposed_by, stamp, expires,
                        str(note or "")[:500],
                    ),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            if live:
                replaced = self._grant(live[0])
            row = db.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
            grant = self._grant(row)
        if replaced is not None:
            self.ledger.record_grant("replaced", replaced, by=caller.label)
        self.ledger.record_grant("set", grant, by=caller.label)
        self._emit_grant("set", grant)
        return grant, (replaced.id if replaced is not None else None)

    def _fallback(self, row: sqlite3.Row, now: datetime) -> list[tuple[Level, frozenset[str]]]:
        """What could take over where ``row`` applies once it is gone (every candidate)."""
        candidates: list[tuple[Level, frozenset[str]]] = [(self.default_level, frozenset())]
        db = self._conn()
        for other in db.execute("SELECT * FROM grants WHERE revoked_at IS NULL").fetchall():
            if other["id"] == row["id"] or not self._live(other, now):
                continue
            if other["scope"] == "global":
                candidates.append((other["level"], frozenset()))
            elif other["scope"] == "project" and row["scope"] in ("project", "session"):
                if row["scope"] == "project":
                    inner = Path(row["target"])
                    outer = Path(other["target"])
                    if inner == outer or outer not in inner.parents:
                        continue
                elif row["project"]:
                    inner = Path(row["project"])
                    outer = Path(other["target"])
                    if inner != outer and outer not in inner.parents:
                        continue
                allow = frozenset(cls for cls in (other["allow"] or "").split(",") if cls)
                candidates.append((other["level"], allow))
        return candidates

    def revoke_widens(self, grant_id: str) -> bool:
        """True when revoking the grant could let Jarvis decide more than it does now.

        A ``staged`` session grant under a ``full`` project grant is a
        restriction; removing it hands authority back, so only a person may.
        """
        now = self.now()
        with self._lock:
            row = self._conn().execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
            if row is None or not self._live(row, now):
                return False
            own_allow = frozenset(cls for cls in (row["allow"] or "").split(",") if cls)
            for level, allow in self._fallback(row, now):
                if _rank(level) > _rank(row["level"]) or allow - own_allow:
                    return True
        return False

    def revoke(self, grant_id: str, *, caller: Caller) -> bool:
        """Revoke a grant at once; ``False`` when it was already gone or expired.

        Narrowing Jarvis's authority is open to any caller (Jarvis may give up
        a grant); a revoke that would widen it is the person's.
        """
        if self.revoke_widens(grant_id):
            require_person(caller, self.settings, guarded=True)
        now = self.now()
        with self._lock:
            db = self._conn()
            row = db.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
            if row is None or not self._live(row, now):
                return False
            db.execute(
                "UPDATE grants SET revoked_at = ?, revoked_by = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (_iso(now), caller.label, grant_id),
            )
            grant = self._grant(row)
        self.ledger.record_grant("revoked", grant, by=caller.label)
        self._emit_grant("revoked", grant)
        return True

    def end_session(self, session_id: str) -> list[str]:
        """A session ended: its grants end with it (core calls this; no person needed)."""
        now = self.now()
        with self._lock:
            db = self._conn()
            rows = db.execute(
                "SELECT * FROM grants WHERE scope = 'session' AND target = ? "
                "AND revoked_at IS NULL",
                (session_id,),
            ).fetchall()
            ended = [row for row in rows if self._live(row, now)]
            for row in rows:
                db.execute(
                    "UPDATE grants SET revoked_at = ?, revoked_by = 'session-ended' WHERE id = ?",
                    (_iso(now), row["id"]),
                )
            db.execute("DELETE FROM pauses WHERE target = ?", (session_id,))
        for row in ended:
            grant = self._grant(row)
            self.ledger.record_grant("revoked", grant, by="session-ended")
            self._emit_grant("revoked", grant)
        return [row["id"] for row in ended]

    def effective(
        self,
        session_id: str | None = None,
        project: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
    ) -> Effective:
        """The delegation in force: session, then the deepest project, then global, then default."""
        self.sweep_expired()
        now = self.now()
        key: str | None = None
        if session_id or external is not None:
            key = target_key(session_id, external)
        root: Path | None = None
        if project:
            with contextlib.suppress(DelegationError):
                root = Path(canonical_root(project))
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM grants WHERE revoked_at IS NULL"
            ).fetchall()
        live = [row for row in rows if self._live(row, now)]
        if key is not None:
            for row in live:
                if row["scope"] == "session" and row["target"] == key:
                    return self._effective(row, "session")
        if root is not None:
            best: sqlite3.Row | None = None
            best_depth = -1
            for row in live:
                if row["scope"] != "project":
                    continue
                grant_root = Path(row["target"])
                if root == grant_root or grant_root in root.parents:
                    depth = len(grant_root.parts)
                    if depth > best_depth:
                        best, best_depth = row, depth
            if best is not None:
                return self._effective(best, "project")
        for row in live:
            if row["scope"] == "global":
                return self._effective(row, "global")
        return Effective(level=self.default_level, allow=frozenset(), grant=None, source="default")

    def _effective(self, row: sqlite3.Row, source: str) -> Effective:
        grant = self._grant(row)
        return Effective(
            level=grant.level, allow=frozenset(grant.allow), grant=grant, source=source
        )

    # -- pauses ----------------------------------------------------------------
    def _pause_row(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn().execute("SELECT * FROM pauses WHERE target = ?", (key,)).fetchone()

    def pause_state(
        self, session_id: str | None = None, *, external: ExternalRef | dict[str, Any] | None = None
    ) -> PauseState:
        """Whether the person holds this session back; an elapsed pause ends here."""
        key = target_key(session_id, external)
        row = self._pause_row(key)
        if row is None:
            return PauseState()
        until = row["until"]
        if until and parse_time(until) <= self.now():
            with self._lock:
                self._conn().execute(
                    "DELETE FROM pauses WHERE target = ? AND until = ?", (key, until)
                )
            self.ledger.record_pause("resumed", key, by="core", project=row["project"])
            state = PauseState(project=row["project"])
            self._emit_pause(key, state)
            return state
        return PauseState(paused=True, reason=row["reason"], until=until, project=row["project"])

    def _set_pause(
        self, key: str, reason: str, until: str | None, project: str | None, by: str
    ) -> PauseState:
        with self._lock:
            self._conn().execute(
                "INSERT INTO pauses (target, reason, until, project, set_by, set_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(target) DO UPDATE SET reason = excluded.reason, "
                "until = excluded.until, project = COALESCE(excluded.project, pauses.project), "
                "set_by = excluded.set_by, set_at = excluded.set_at",
                (key, reason, until, project, by, _iso(self.now())),
            )
        state = self.pause_state(key)
        self.ledger.record_pause(reason, key, by=by, until=until, project=project)
        self._emit_pause(key, state)
        return state

    def _state_for(self, key: str) -> PauseState:
        row = self._pause_row(key)
        if row is None:
            return PauseState()
        return PauseState(paused=True, reason=row["reason"], until=row["until"],
                          project=row["project"])

    def note_human_input(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        project: str | None = None,
        surface: str | None = None,
    ) -> PauseState:
        """The person typed into a managed session: pause Jarvis for ``jarvis.quietMinutes``.

        Each further input moves the end later.  An open-ended takeover, or a
        timed one ending later, is left as it is.
        """
        key = target_key(session_id, external)
        current = self._state_for(key)
        until = self.now() + timedelta(minutes=self.quiet_minutes)
        if current.paused and current.reason == "takeover" and (
            current.until is None or parse_time(current.until) >= until
        ):
            return current
        if current.paused and current.reason == "typing":
            # Still typing: push the end out; the ledger already has the pause.
            with self._lock:
                self._conn().execute(
                    "UPDATE pauses SET until = ?, set_at = ? WHERE target = ?",
                    (_iso(until), _iso(self.now()), key),
                )
            return self._state_for(key)
        return self._set_pause(key, "typing", _iso(until), project, surface or "person")

    def takeover(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        minutes: int | None = None,
        project: str | None = None,
        caller: Caller = SYSTEM,
    ) -> PauseState:
        """The person takes a session back until :meth:`handback` or ``minutes``.

        Taking authority away is open to any caller; only giving it back is the person's.
        """
        key = target_key(session_id, external)
        until: str | None = None
        if minutes is not None:
            if not 1 <= int(minutes) <= MAX_TAKEOVER_MINUTES:
                raise DelegationError(f"minutes must be 1..{MAX_TAKEOVER_MINUTES}")
            until = _iso(self.now() + timedelta(minutes=int(minutes)))
        return self._set_pause(key, "takeover", until, project, caller.label)

    def handback(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        caller: Caller,
    ) -> PauseState:
        """Authority returns to Jarvis now (ends a takeover or a typing pause); the person only."""
        require_person(caller, self.settings, guarded=False)
        key = target_key(session_id, external)
        row = self._pause_row(key)
        with self._lock:
            self._conn().execute("DELETE FROM pauses WHERE target = ?", (key,))
        state = PauseState(project=row["project"] if row is not None else None)
        if row is not None:
            self.ledger.record_pause("handback", key, by=caller.label, project=row["project"])
            self._emit_pause(key, state)
        return state

    def paused(self) -> list[JarvisPause]:
        """Every session the person holds back right now (``jarvis.status`` ``paused``)."""
        with self._lock:
            keys = [row["target"] for row in self._conn().execute("SELECT target FROM pauses")]
        found: list[JarvisPause] = []
        for key in keys:
            state = self.pause_state(key)
            ref: ExternalRef | None = None
            session: str | None = key
            if key.startswith("ext:"):
                _, cli, ext_id = key.split(":", 2)
                with contextlib.suppress(ValueError):
                    ref = ExternalRef(cli=cli, id=ext_id)  # type: ignore[arg-type]
                session = None
            if not state.paused or state.reason is None:
                continue
            found.append(
                JarvisPause(
                    sessionId=session, external=ref, project=state.project,
                    reason=state.reason, until=state.until,
                )
            )
        return found

    def session_state(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        project: str | None = None,
        exposed: bool = True,
        enabled: bool | None = None,
    ) -> SessionJarvisState:
        """``SessionSummary.jarvis`` / ``JarvisPauseResult.state`` for one session."""
        if enabled is None:
            enabled = bool(_setting(self.settings, ("jarvis", "enabled"), False))
        state = self.pause_state(session_id, external=external)
        effective: Effective | None = None
        if enabled and exposed:
            effective = self.effective(session_id, project, external=external)
        return SessionJarvisState(
            exposed=exposed,
            level=effective.level if effective is not None else None,
            pausedUntil=None if state.taken_over else state.until,
            takenOver=state.taken_over,
            levelScope=effective.source if effective is not None else None,  # type: ignore[arg-type]
            grantId=(
                effective.grant.id
                if effective is not None and effective.grant is not None
                else None
            ),
        )

    # -- routing ---------------------------------------------------------------
    def _verdict(self, request: ClassifyRequest) -> Verdict:
        if request.home is None:
            # Judge paths against this daemon's home, not whatever $SNOWPEA_HOME says.
            request = replace(request, home=str(self.home))
        if (
            self.jarvis_session_id is not None
            and request.session_id == self.jarvis_session_id
            and not request.jarvis_session
        ):
            request = replace(request, jarvis_session=True)
        return classify(request)

    def route(
        self,
        request: ClassifyRequest,
        *,
        project: str | None = None,
        external: ExternalRef | dict[str, Any] | None = None,
        exposed: bool = True,
        budget_exhausted: bool = False,
        request_id: str | None = None,
    ) -> RouteResult:
        """Where an approval goes: ``auto``, ``jarvis`` or ``person``.

        ``project`` defaults to the request's ``workdir``.  With a
        ``request_id`` the result is remembered, so :meth:`validate_decision`
        can insist the request was routed to Jarvis in the first place.
        """
        result = self._route(request, project=project, external=external, exposed=exposed,
                             budget_exhausted=budget_exhausted)
        if request_id:
            with self._lock:
                self._routes[request_id] = result
                self._routes.move_to_end(request_id)
                while len(self._routes) > ROUTE_MEMORY:
                    self._routes.popitem(last=False)
        return result

    def _route(
        self,
        request: ClassifyRequest,
        *,
        project: str | None,
        external: ExternalRef | dict[str, Any] | None,
        exposed: bool,
        budget_exhausted: bool,
    ) -> RouteResult:
        verdict = self._verdict(request)
        project = project or request.workdir
        if verdict.classification == "never":
            return RouteResult("person", verdict, self.default_level, None, "never")
        if request.session_id is None and external is None:
            return RouteResult("person", verdict, self.default_level, None, "no-session")
        if not exposed:
            return RouteResult("person", verdict, self.default_level, None, "not-exposed")
        pause = self.pause_state(request.session_id, external=external)
        if pause.paused:
            return RouteResult("person", verdict, self.default_level, None,
                               f"paused:{pause.reason}")
        if budget_exhausted:
            return RouteResult("person", verdict, self.default_level, None, "budget")
        if verdict.classification == "safe":
            return RouteResult("auto", verdict, self.default_level, None, "safe")
        effective = self.effective(request.session_id, project, external=external)
        grant_id = effective.grant.id if effective.grant is not None else None
        if verdict.classification == "risky":
            if effective.level == "full":
                return RouteResult("jarvis", verdict, effective.level, grant_id, "risky:full")
            return RouteResult("person", verdict, effective.level, grant_id, "risky:staged")
        missing = tuple(cls for cls in verdict.guarded if cls not in effective.allow)
        if verdict.guarded and not missing:
            return RouteResult("jarvis", verdict, effective.level, grant_id, "guarded:allowed")
        return RouteResult("person", verdict, effective.level, grant_id, "guarded:not-allowed",
                           missing or GUARDED_CLASSES)

    def routed(self, request_id: str) -> RouteResult | None:
        """The route remembered for ``request_id`` by :meth:`route`."""
        with self._lock:
            return self._routes.get(request_id)

    def forget(self, request_id: str) -> None:
        """Drop a remembered route (the request was answered or withdrawn)."""
        with self._lock:
            self._routes.pop(request_id, None)

    def validate_decision(
        self,
        request: ClassifyRequest,
        decision: str,
        *,
        scope: str = "once",
        request_id: str | None = None,
        project: str | None = None,
        external: ExternalRef | dict[str, Any] | None = None,
        exposed: bool = True,
        budget_exhausted: bool = False,
        expect: Route = "jarvis",
    ) -> RouteResult:
        """Re-check, server-side, an answer Jarvis wants to give; raise :class:`DecisionRefused`.

        The request must have been routed to Jarvis when it arrived (when
        ``request_id`` is given) **and** still be now — a grant revoked, a
        pause started or a budget spent in between all take it back.  Jarvis
        answers ``once`` only: a remembered rule is the person's to make.
        ``expect="auto"`` re-checks the engine's own allow the same way (it
        only ever allows).
        """
        if decision not in ("allow", "deny"):
            raise DecisionRefused(f"unknown decision {decision!r}")
        if decision == "allow" and scope != "once":
            raise DecisionRefused("Jarvis may only allow once; remembering a rule is the person's")
        if expect == "auto" and decision != "allow":
            raise DecisionRefused("the engine alone only ever allows")
        if expect == "person":
            raise DecisionRefused("a person's request is never decided for them")
        if request_id:
            earlier = self.routed(request_id)
            if earlier is None:
                raise DecisionRefused("the engine has no record of routing this request")
            if earlier.route != expect:
                raise DecisionRefused(
                    f"this request was routed to {earlier.route} ({earlier.reason})", earlier
                )
        current = self._route(request, project=project, external=external, exposed=exposed,
                              budget_exhausted=budget_exhausted)
        if current.route != expect:
            raise DecisionRefused(
                f"this request is now routed to {current.route} ({current.reason})", current
            )
        return current

    def route_question(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        exposed: bool = True,
        budget_exhausted: bool = False,
        jarvis_session: bool = False,
        browser_owned: bool = False,
    ) -> Route:
        """Where a question goes: Jarvis answers how-to questions at either level."""
        if jarvis_session or browser_owned or not exposed or budget_exhausted:
            return "person"
        if session_id is None and external is None:
            return "person"
        if self.jarvis_session_id is not None and session_id == self.jarvis_session_id:
            return "person"
        state = self.pause_state(session_id, external=external)
        return "person" if state.paused else "jarvis"

    def validate_question_answer(
        self,
        session_id: str | None = None,
        *,
        external: ExternalRef | dict[str, Any] | None = None,
        exposed: bool = True,
        budget_exhausted: bool = False,
        jarvis_session: bool = False,
        browser_owned: bool = False,
    ) -> None:
        """Raise :class:`DecisionRefused` unless Jarvis may answer this session's question now."""
        route = self.route_question(
            session_id, external=external, exposed=exposed, budget_exhausted=budget_exhausted,
            jarvis_session=jarvis_session, browser_owned=browser_owned,
        )
        if route != "jarvis":
            raise DecisionRefused("this question is the person's to answer")


def engine_for(core: Any) -> DelegationEngine:
    """The daemon's one engine, created on first use and wired to the event hub.

    Grant changes go out as ``jarvis.delegation.changed`` and ledger decisions
    as ``jarvis.decision``, both daemon-wide.  Later work packages (the Jarvis
    service, its tools, the gateway) take the engine from here.
    """
    engine: DelegationEngine | None = getattr(core, "jarvis_delegation", None)
    if engine is not None:
        return engine
    engine = DelegationEngine(core.paths.home, lambda: core.settings)
    _wire_events(core, engine)
    core.jarvis_delegation = engine
    return engine


def _wire_events(core: Any, engine: DelegationEngine) -> None:
    import asyncio

    tasks: set[asyncio.Task[None]] = set()

    def schedule(method: str, params: dict[str, Any]) -> None:
        hub = getattr(core, "hub", None)
        if hub is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(hub.notify(method, params))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def on_grant(reason: str, grant: JarvisGrant) -> None:
        schedule("jarvis.delegation.changed",
                 {"reason": reason, "grant": grant.model_dump(mode="json")})

    def on_record(record: dict[str, Any]) -> None:
        if record.get("type") != "decision":
            return
        entry = {k: v for k, v in record.items() if k not in ("type", "audit")}
        schedule("jarvis.decision", {"entry": entry})

    engine.grant_listeners.append(on_grant)
    engine.ledger.listeners.append(on_record)
    engine.events_wired = True


__all__ = [
    "HUMAN_CLIENT_KINDS",
    "JARVIS",
    "SYSTEM",
    "Caller",
    "DecisionRefused",
    "DelegationEngine",
    "DelegationError",
    "Effective",
    "PauseState",
    "PersonOnly",
    "RouteResult",
    "canonical_root",
    "engine_for",
    "parse_time",
    "person_refusal",
    "require_person",
    "target_key",
]
