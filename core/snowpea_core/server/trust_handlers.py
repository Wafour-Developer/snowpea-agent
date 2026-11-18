"""``project.trust.*`` (1.55.0, capability ``projectTrust``; security review 5, C1).

``project.trust.status`` tells a person what a project folder would make
snowpea do and what of it is active; ``project.trust.set`` / ``.revoke`` are a
LOCAL person's (``person_gate.PERSON_METHODS``: never a remote web login or
the connector) — and, 1.62.0 (``browserProjectTrust``), a local browser 0.4.0+'s
for its own session's folder after its native confirmation, default scopes
only (``settings_gate.browser_trust_refusal``).  Every change is ``project.trust.changed`` to
the people's surfaces.  The store is :mod:`snowpea_core.trust.projects`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ProjectTrustParams,
    ProjectTrustRevokeParams,
    ProjectTrustSetParams,
    ProjectTrustStatus,
)
from snowpea_core.trust.projects import TrustStore, set_default_home, store_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger("snowpea.trust")

#: The session event code of the once-per-session notice (a surface may localise it).
UNTRUSTED_CODE = "project_untrusted"

#: Sessions already told (so a resume does not repeat it).
_TOLD: set[str] = set()
#: The projects the one-time migration trusted, told with the first notice.
_MIGRATED: list[str] = []
#: Why no project is trusted although some were (the record was lost), told with
#: the first notice and as ``project.trust.changed {reason: "store_lost"}``.
_STORE_PROBLEM: list[str] = []


def store_problem(core: Any) -> str | None:
    """The trust record's problem, for ``security.get.problems`` and the doctor."""
    try:
        return store_of(core).problem()
    except Exception:  # noqa: BLE001 - a problem line is best effort
        return None


def store_of(core: Any) -> TrustStore:
    return store_for(core.paths.home)


def wire_trust(core: Any) -> None:
    """Point the readers at this daemon's store and run the one-time migration (d).

    Projects that already have sessions in the store are trusted for what
    they bring now (their hooks already ran before this version); a new project
    starts untrusted.  Logged once, and told with the first session's notice.
    """
    set_default_home(core.paths.home)
    # Round-4 M3, re-checked in review 5: no PATH entry inside any known project.
    from snowpea_core.exec.local import set_project_roots
    from snowpea_core.security.enforce import live_projects

    set_project_roots(lambda: live_projects(core))
    store = store_of(core)
    # Security review 7, P2: from here on this daemon is the only writer of the
    # record, its marker and the legacy file.  This is the daemon's start: what is
    # on disk now is taken as it is (the daemon was down); every later change that
    # is not the store's own is undone and told (``project.trust.changed {reason:
    # "tamper"}``).  Armed before the migration, whose writes are the store's own.
    _guard(core, store)
    _migrate_once(core, store)


#: The session store's record that the trust migration ran (review 7, M7-8).
MIGRATED_KEY = "trust.migrated"
#: Changes made behind the daemon's back and undone since it started.
_TAMPERED: list[str] = []


def _guard(core: Any, store: TrustStore) -> None:
    import asyncio

    try:
        wired: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        wired = None
    tasks: set[asyncio.Task[None]] = set()

    def tell() -> None:
        task = asyncio.get_running_loop().create_task(
            _changed(core, "", {"scopes": []}, reason="tamper", by=None))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def on_tamper(path: str, happened: str, note: dict[str, Any] | None = None) -> None:
        # Security review 8, L8-1: told once at once, then at most once a minute
        # with a count; and only what was done is claimed.
        count = int((note or {}).get("count") or 1)
        restored = bool((note or {}).get("restored", True))
        again = f" ({count} times since the last notice)" if count > 1 else ""
        back = ("the file was put back" if restored else
                "putting the file back failed, so the file on disk differs until snowpea "
                "writes it again")
        _TAMPERED.append(
            f"something outside snowpea {happened} the trusted-projects record "
            f"($SNOWPEA_HOME/trust/{os.path.basename(path)}) while snowpea was running{again}; "
            f"snowpea did not use the change and kept its own copy; {back}")
        del _TAMPERED[:-5]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Read from a worker thread: hand the notice to the daemon's loop.
            target = wired or getattr(core, "loop", None)
            if target is not None and not target.is_closed():
                target.call_soon_threadsafe(tell)
            return
        tell()

    # One listener per home: the newest core is the one that is told.
    store.tamper_listeners[:] = [on_tamper]
    store.guard()


def tamper_problems() -> list[str]:
    """What the guard undid since the daemon started (``security.get.problems``)."""
    return list(_TAMPERED)


def _migrate_once(core: Any, store: TrustStore) -> None:
    session_store = getattr(core, "store", None)
    meta_get = getattr(session_store, "meta_get", None)
    meta_set = getattr(session_store, "meta_set", None)
    recorded = meta_get(MIGRATED_KEY) if meta_get is not None else None
    if store.migrated():
        if recorded is None and meta_set is not None:
            # The marker's fact, kept a second time in the session store: losing the
            # marker and the record no longer looks like a first start (M7-8).
            meta_set(MIGRATED_KEY, store.migrated_at() or "unknown")
        # Security review 6, M-4: the migration ran once and never again.  A record
        # that is gone or unreadable now means nothing is trusted; say so.
        problem = store.problem()
        if problem:
            _STORE_PROBLEM[:] = [problem]
            log.warning("project trust: %s", problem)
        return
    if recorded is not None:
        # The session store says the migration ran, yet the marker (and with it
        # perhaps the record) is gone: lost or removed while the daemon was down.
        # Nothing is trusted again by itself — the marker is written back, the
        # record is read as it is, and the person is told.
        store.restore_marker(recorded)
        problem = store.problem() or (
            "the trust migration marker ($SNOWPEA_HOME/trust/migrated.json) was missing and "
            "was written again; check the projects you trust")
        _STORE_PROBLEM[:] = [problem]
        log.warning("project trust: %s", problem)
        return
    from snowpea_core.config.paths import utc_now

    stamp = utc_now()
    roots: list[str] = []
    session_workdirs = getattr(session_store, "session_workdirs", None)
    if session_workdirs is not None:
        try:
            # Only projects whose sessions were created before this upgrade.
            roots = list(session_workdirs(limit=1000, before=stamp))
        except TypeError:
            roots = list(session_workdirs(limit=1000))
        except Exception:  # noqa: BLE001 - no history: nothing to migrate
            log.debug("could not list session workdirs for the trust migration", exc_info=True)
    trusted = store.migrate(roots, at=stamp)
    if meta_set is not None:
        meta_set(MIGRATED_KEY, stamp)
    if trusted:
        _MIGRATED[:] = trusted
        log.warning(
            "project trust: %d project(s) that already had sessions were trusted as they are "
            "now (their hooks, MCP servers, commands, agents and settings stay active): %s",
            len(trusted), ", ".join(trusted),
        )


async def _changed(core: Any, workdir: str, status: dict[str, Any], *, reason: str,
                   by: str | None) -> None:
    hub = getattr(core, "hub", None)
    if hub is None:
        return
    try:
        await hub.notify("project.trust.changed", {
            "workdir": workdir, "scopes": status.get("scopes") or [], "by": by,
            "reason": reason, "projects": [],
        })
    except Exception:  # noqa: BLE001 - the change stands
        log.debug("could not broadcast project.trust.changed", exc_info=True)


def _root(workdir: str) -> Path:
    text = str(workdir or "").strip()
    if not text or not os.path.isabs(os.path.expanduser(text)):
        raise RpcError(errors.INVALID_PARAMS, "workdir must be an absolute path")
    path = Path(os.path.realpath(os.path.expanduser(text)))
    if not path.is_dir():
        raise RpcError(errors.INVALID_PARAMS, f"no such folder: {text}")
    return path


async def _after_change(core: Any, root: Path) -> None:
    """What the change switches on or off: hooks, commands, agents, MCP servers, prompts."""
    # Security review 6, L-3: a project's prompt override that was read while it
    # was trusted must not outlive the trust in a cache.
    try:
        from snowpea_core.prompts import loader as prompt_loader

        prompt_loader.clear_cache()
    except Exception:  # noqa: BLE001 - the trust record stands
        log.debug("could not clear the prompt cache after a trust change", exc_info=True)
    _STORE_PROBLEM.clear()
    skills = getattr(core, "skills", None)
    if skills is not None and hasattr(skills, "reload"):
        try:
            await skills.reload()
        except Exception:  # noqa: BLE001 - the trust record stands
            log.warning("could not reload skills after a trust change", exc_info=True)
    try:
        from snowpea_core.tools import mcp_client

        await mcp_client.sync_tools_bounded(core, root)
    except Exception:  # noqa: BLE001 - the trust record stands
        log.debug("could not resync MCP servers after a trust change", exc_info=True)


async def project_trust_status_handler(conn: RpcConnection, params: ProjectTrustParams,
                                       core: Core) -> ProjectTrustStatus:
    _judge(conn, core, params.workdir, "read a project's trust", read=True)
    root = _root(params.workdir)
    return ProjectTrustStatus.model_validate(store_of(core).status(root))


def _browser(conn: Any) -> bool:
    from snowpea_core.server.person_gate import connection_kind

    return connection_kind(conn) == "browser"


def _judge(conn: Any, core: Any, workdir: str, what: str, *, actor: str | None = None,
           person_confirm: str | None = None, scopes: Any = None, read: bool = False) -> str:
    """Who may change (or, ``read``, see) a project's trust; returns ``by``.

    A local person (a remote web login reads only); a browser connection by
    ``settings_gate.browser_trust_refusal`` (1.62.0, ``browserProjectTrust``),
    refused with its ``details.reason``; nobody else.
    """
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import require_person

    if _browser(conn):
        why = settings_gate.browser_trust_refusal(conn, core, workdir, actor, person_confirm,
                                                  scopes, read=read)
        if why is not None:
            settings_gate.refuse(conn, what, reason=why)
        return f"browser:{getattr(conn, 'client_id', None) or '?'}"
    if not read:  # (the read's other callers are the dispatcher's: PERSON_METHODS)
        require_person(conn, what, local=True)
    return str(getattr(conn, "client_kind", None) or "local")


def _audit(core: Any, conn: Any, event: str, root: Path, scopes: Any, by: str,
           person_confirm: str | None, digests: int | None) -> None:
    """A line in ``logs/security.jsonl`` for a trust change (1.62.0); never raises."""
    import json
    import time

    from snowpea_core.server.person_gate import connection_kind

    try:
        core.paths.ensure()
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "event": event, "workdir": str(root),
                  "scopes": list(scopes) if scopes is not None else None, "by": by,
                  "connection": connection_kind(conn),
                  "clientId": getattr(conn, "client_id", None),
                  "personConfirmWindow": person_confirm == "window",
                  "digests": digests}
        from snowpea_core.util.atomic import append_text

        append_text(Path(core.paths.logs_dir) / "security.jsonl", json.dumps(record) + "\n")
    except (OSError, AttributeError, TypeError):  # pragma: no cover - audit is best effort
        log.warning("could not write the project trust audit line", exc_info=True)


async def project_trust_set_handler(conn: RpcConnection, params: ProjectTrustSetParams,
                                    core: Core) -> ProjectTrustStatus:
    by = _judge(conn, core, params.workdir, "trust a project", actor=params.actor,
                person_confirm=params.personConfirm, scopes=params.scopes)
    from snowpea_core.server.project_gate import require_project

    require_project(core, params.workdir)  # review 7, H-2: snowpea's home is no project
    root = _root(params.workdir)
    from snowpea_core.trust.projects import TrustChanged

    try:
        # Security review 6, M-5: what is pinned is what the person was shown.
        store_of(core).set(root, params.scopes, by=by, digests=dict(params.digests))
    except TrustChanged as exc:
        raise RpcError(
            errors.TRUST_CHANGED,
            "the project's control files are not what you were shown; look at them again",
            {"changed": exc.changed, "added": exc.added, "missing": exc.missing},
        ) from exc
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    _audit(core, conn, "project_trusted", root, params.scopes, by, params.personConfirm,
           len(params.digests))
    await _after_change(core, root)
    status = store_of(core).status(root)
    await _changed(core, str(root), status, reason="set", by=by)
    return ProjectTrustStatus.model_validate(status)


async def project_trust_revoke_handler(conn: RpcConnection, params: ProjectTrustRevokeParams,
                                       core: Core) -> ProjectTrustStatus:
    by = _judge(conn, core, params.workdir, "take back a project's trust", actor=params.actor,
                person_confirm=params.personConfirm, scopes=params.scopes)
    root = _root(params.workdir)
    from snowpea_core.server.settings_gate import browser_trust_scopes

    # A browser's omitted scopes are the ones it may take back (1.62.0).
    scopes = params.scopes if params.scopes is not None or not _browser(conn) else list(
        browser_trust_scopes())
    store_of(core).revoke(root, scopes, by=by)
    _audit(core, conn, "project_trust_revoked", root, scopes, by, params.personConfirm, None)
    await _after_change(core, root)
    status = store_of(core).status(root)
    await _changed(core, str(root), status, reason="revoke", by=by)
    return ProjectTrustStatus.model_validate(status)


async def session_notice(core: Any, session_id: str, workdir: Any) -> None:
    """Once per session: what the project brings that is not active, as a session event."""
    if not session_id or session_id in _TOLD:
        return
    _TOLD.add(session_id)
    try:
        line = store_of(core).notice(workdir)
        details = _safe_details(core, workdir) if line else None
        if _STORE_PROBLEM:
            lost = _STORE_PROBLEM[0]
            _STORE_PROBLEM.clear()
            lost = f"{lost[:1].upper()}{lost[1:]}."
            line = f"{lost} {line}" if line else lost
            await _changed(core, "", {"scopes": []}, reason="store_lost", by=None)
        if _MIGRATED:
            moved = ", ".join(_MIGRATED[:5]) + (" …" if len(_MIGRATED) > 5 else "")
            first = ("snowpea now asks before a project's own hooks, MCP servers, commands, "
                     "agents and settings take effect; the projects you already worked in "
                     f"were trusted as they are: {moved}.")
            _MIGRATED.clear()
            line = f"{first} {line}" if line else first
        if not line:
            return
        from snowpea_core.session import events

        await core.hub.emit_event(session_id, events.error(UNTRUSTED_CODE, line, details))
    except Exception:  # noqa: BLE001 - a notice is best effort
        log.debug("could not tell the session about project trust", exc_info=True)


#: The ``project_untrusted`` notice's lines for a browser (1.62.0): one that can
#: trust the folder itself (``canTrustHere``) and one that cannot.
BROWSER_TRUST_TEXT = ("Trust this folder from the browser to turn on its MCP servers, hooks "
                      "and commands")
LEGACY_BROWSER_TRUST_TEXT = ("This folder's own MCP servers, hooks and commands are off until "
                             "it is trusted in the snowpea app or terminal. Servers you add "
                             "globally in Settings › MCP work everywhere")


def _safe_details(core: Any, workdir: Any) -> dict[str, Any] | None:
    try:
        return notice_details(core, workdir)
    except Exception:  # noqa: BLE001 - the plain line is still told
        log.debug("could not build the trust notice's details", exc_info=True)
        return None


def notice_details(core: Any, workdir: Any) -> dict[str, Any] | None:
    """The ``project_untrusted`` notice's ``details`` (1.62.0): the event goes to
    every surface, so each picks its own line — the TUI and IDE keep ``message``,
    a browser 0.4.0+ shows ``browserText`` and a trust button when
    ``canTrustHere``, an older browser ``legacyBrowserText``."""
    from snowpea_core.server.settings_gate import browser_trust_scopes

    status = store_of(core).status(workdir)
    inactive = [f for f in status["files"] if f["state"] != "active"]
    if not inactive:
        return None
    scopes = sorted({f["scope"] for f in inactive})
    browser_scopes = [s for s in scopes if s in browser_trust_scopes()]
    return {"workdir": status["workdir"], "scopes": scopes,
            "changed": [f["path"] for f in inactive if f["state"] in ("changed", "new")],
            "browserScopes": browser_scopes, "canTrustHere": bool(browser_scopes),
            "browserText": BROWSER_TRUST_TEXT, "legacyBrowserText": LEGACY_BROWSER_TRUST_TEXT}


def register_trust_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("project.trust.status", project_trust_status_handler)
    dispatcher.register("project.trust.set", project_trust_set_handler)
    dispatcher.register("project.trust.revoke", project_trust_revoke_handler)
    return dispatcher


__all__ = [
    "UNTRUSTED_CODE",
    "notice_details",
    "project_trust_revoke_handler",
    "project_trust_set_handler",
    "project_trust_status_handler",
    "register_trust_handlers",
    "session_notice",
    "wire_trust",
]
