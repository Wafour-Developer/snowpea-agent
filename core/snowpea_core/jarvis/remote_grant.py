"""A delegation grant from a remote web login (1.56.0, owner decision 2026-11-17).

Security review 8 (H8-2) made every grant a local person's: a remote web login
has not re-authenticated, and a grant persists.  The owner works mostly over the
remote web page, so a grant from there is possible again — behind a switch, and
inside fixed limits the ENGINE enforces (:meth:`DelegationEngine.set_grant`),
whatever a client shows:

* ``jarvis.delegation.remoteGrant`` is on (default off).  It is a local person's
  switch: ``settings.set`` of any ``jarvis.*`` key is refused to a remote login,
  a browser and a connector (``settings_gate.PERSON_LOCAL_ONLY``); an edit of
  ``settings.json`` on disk while the daemon runs waits for a local person
  (``hot_reload.GATED_SECTIONS``, ``settings.acceptDiskChange``); the agent's
  ``settings_set`` tool refuses it (:func:`tool_refusal`).  A degraded settings
  document reads it as off (:func:`enabled`).
* The request carries the remote password again (``reauth``), checked by the
  remote listener exactly like a sign-in — the same scrypt check, the same
  per-address and global failure counters and queue bounds
  (``web_remote.RemoteAuth.reauthenticate``).  A login made with the access link
  (no password exists) cannot re-authenticate and gives no grant.
* Scope ``session`` or ``project`` only; no guarded classes (deploy, outside or
  destructive, secrets or sending stay a local person's); an expiry within
  :data:`MAX_HOURS` (default :data:`MAX_HOURS`; up to :data:`SLACK_SEC` past it
  is a client clock running ahead and is clamped, beyond is refused).
* The grant is marked (``JarvisGrant.remoteLogin``) and ledgered as such.

The password itself is never stored, logged, put in the ledger or in an event,
and never echoed in an error: the RPC layer drops the input of a ``reauth``
field from validation errors (``rpc.RpcDispatcher._invoke``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

#: Longest a remote grant lasts.
MAX_HOURS = 24
#: A requested expiry up to this far past :data:`MAX_HOURS` is clamped to it (a
#: client whose clock runs a little ahead asks for "24 h" and gets it).
SLACK_SEC = 300
#: The setting's dotted name, for messages.
SETTING = "jarvis.delegation.remoteGrant"
#: Where a local person turns it on, in words.
WHERE = "Settings > Jarvis > Delegations on this computer"

#: Why a remote web login gives no grant while the switch is off (the text keeps
#: "local connection": security review 8, H8-2).
OFF_REFUSAL = (
    "giving Jarvis authority needs a local connection (IDE, TUI or the local web page), "
    "not a remote web login; a local person can allow it from a remote web login with "
    f"'Allow delegation from a remote web login' ({WHERE}, {SETTING})"
)
#: The same, for the dispatcher's ``local_only`` refusal of ``jarvis.delegation.set``.
OFF_HINT = (
    "a local person can allow it from a remote web login: 'Allow delegation from a remote "
    f"web login' ({WHERE})"
)


def enabled(settings: Any) -> bool:
    """``jarvis.delegation.remoteGrant`` is on, in a settings document that is not
    degraded in its ``jarvis`` section (a broken document reads it as off)."""
    if settings is None:
        return False
    try:
        value = settings.jarvis.delegation.remoteGrant
    except AttributeError:
        return False
    if value is not True:
        return False
    degraded = getattr(settings, "degraded_sections", None)
    try:
        sections = degraded() if callable(degraded) else []
    except Exception:  # noqa: BLE001 - unknown state is off
        return False
    return not any(section in ("*", "jarvis") or section.startswith("jarvis.")
                   for section in sections)


def enabled_for(core: Any) -> bool:
    return enabled(getattr(core, "settings", None))


def RemoteGrantRefused(message: str, reason: str, what: str | None = None,  # noqa: N802
                       extra: dict[str, Any] | None = None) -> Exception:
    """:class:`~snowpea_core.jarvis.delegation.RemoteGrantRefused` (a
    ``PersonOnly``; imported here lazily: the engine imports this module)."""
    from snowpea_core.jarvis.delegation import RemoteGrantRefused as Refused

    return Refused(message, reason, what, extra)


def as_rpc(exc: Any) -> RpcError:
    """A refusal as the wire error: ``jarvis_person_only`` with its details."""
    details = exc.details() if callable(getattr(exc, "details", None)) else None
    return RpcError(errors.JARVIS_PERSON_ONLY, str(exc), details)


def check_limits(scope: str, allow: Any, expires_at: datetime | None,
                 now: datetime) -> datetime:
    """The limits of a remote grant; returns the expiry it gets (UTC).

    ``expires_at`` ``None``: :data:`MAX_HOURS` from ``now``.  Raises
    :class:`RemoteGrantRefused` (``remote_grant_limit``) for a global scope, any
    guarded class, or an expiry later than the limit (past the slack)."""
    if scope not in ("session", "project"):
        raise RemoteGrantRefused(
            "a grant from a remote web login is for one session or one project, never "
            "every project; give that at this computer", "remote_grant_limit", "scope")
    if allow:
        raise RemoteGrantRefused(
            "handing guarded classes (deploy, outside or destructive, secrets or sending) to "
            "Jarvis needs a local connection (IDE, TUI or the local web page), also with "
            f"{SETTING} on", "remote_grant_limit", "allow")
    limit = now + timedelta(hours=MAX_HOURS)
    if expires_at is None:
        return limit
    if expires_at > limit + timedelta(seconds=SLACK_SEC):
        raise RemoteGrantRefused(
            f"a grant from a remote web login ends within {MAX_HOURS} h; choose an earlier "
            "expiry", "remote_grant_limit", "expiry")
    return min(expires_at, limit)


async def reauthenticate(conn: Any, password: Any) -> None:
    """Check ``password`` (the ``reauth`` param) for the remote login behind
    ``conn``, under the sign-in limits; raise :class:`RemoteGrantRefused` when it
    does not pass.  Never puts the password in a message or a log."""
    if not isinstance(password, str) or not password:
        raise RemoteGrantRefused(
            "giving Jarvis authority from a remote web login needs your remote password "
            "again (reauth)", "reauth_required")
    check = getattr(conn, "remote_reauth", None)
    if not callable(check):
        # Not a socket of the remote listener (a test double, a snapshot): nobody
        # can vouch for the password, so it does not pass.
        raise RemoteGrantRefused(
            "this connection cannot re-authenticate with the remote password",
            "reauth_unavailable")
    outcome = await check(password)
    if outcome == "ok":
        return
    if outcome == "no_password":
        raise RemoteGrantRefused(
            "this remote login was made with the access link, not a password: set a remote "
            "password at this computer and sign in with it to give Jarvis a grant from here",
            "reauth_unavailable")
    if isinstance(outcome, tuple) and outcome and outcome[0] == "rate_limited":
        wait = int(outcome[1]) if len(outcome) > 1 else 1
        raise RemoteGrantRefused(
            f"too many attempts; try again in {wait} s", "reauth_rate_limited",
            extra={"retryAfter": wait})
    if outcome == "logged_out":
        raise RemoteGrantRefused("this remote login has ended; sign in again",
                                 "reauth_unavailable")
    raise RemoteGrantRefused("the remote password was not right", "reauth_failed")


def tool_refusal(before: Any, after: Any) -> str | None:
    """Why the agent's ``settings_set`` tool may not make this change (``None``: it may).

    The tool runs in a session (an approval answered by whoever is there, a
    remote web login too), so it never changes the switch either way."""
    def value(settings: Any) -> Any:
        try:
            return settings.jarvis.delegation.remoteGrant
        except AttributeError:
            return None

    if value(before) != value(after):
        return (f"{SETTING} is a local person's switch: change it in {WHERE}, "
                "not from a session")
    return None


__all__ = [
    "MAX_HOURS",
    "OFF_HINT",
    "OFF_REFUSAL",
    "SETTING",
    "SLACK_SEC",
    "RemoteGrantRefused",
    "as_rpc",
    "check_limits",
    "enabled",
    "enabled_for",
    "reauthenticate",
    "tool_refusal",
]
