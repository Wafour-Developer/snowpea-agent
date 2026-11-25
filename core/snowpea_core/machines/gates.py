"""Local gates before proxying to a remote machine (M40 security)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from snowpea_core.machines.proxy import JARVIS_PROXY_METHODS, PROXY_METHODS
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import connection_kind

log = logging.getLogger("snowpea.machines.gates")

_ALLOWED_KINDS = frozenset({"person", "remote_login"})

_TERMINAL_WRITE_METHODS = frozenset(
    {"terminal.write", "terminal.send", "terminal.keys", "terminal.interrupt"}
)


def _refuse_kind(conn: Any) -> None:
    kind = connection_kind(conn)
    if kind not in _ALLOWED_KINDS:
        raise RpcError(
            errors.UNAUTHORIZED,
            "only a local person or an allowed remote web login may use remote machines",
            {"reason": "machine_proxy_kind", "kind": kind},
        )


def _refuse_remote_off(conn: Any, core: Any) -> None:
    if connection_kind(conn) == "remote_login" and not core.settings.web.remote.machines:
        raise RpcError(
            errors.UNAUTHORIZED,
            "remote web logins cannot use machines unless web.remote.machines is on",
            {"reason": "remote_machines_off"},
        )


def _require_local_person(conn: Any, what: str) -> None:
    if connection_kind(conn) != "person":
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            f"only a local person may {what} on a remote machine",
            {"reason": "person_only", "kind": connection_kind(conn)},
        )


def _gate_trust(conn: Any, trust: bool) -> None:
    if trust:
        _require_local_person(conn, "trust a project")


def _machine_ledger(core: Any, machine_id: str, summary: str) -> None:
    from snowpea_core.server.external_handlers import _ledger_fn

    _ledger_fn(core)(
        {
            "summary": f"{summary} on remote machine {machine_id}"[:300],
            "reason": "remote_machine",
            "importance": "high",
        }
    )


def _gate_person_confirm(conn: Any, core: Any, machine_id: str, summary: str) -> None:
    from snowpea_core.external import proc

    try:
        proc.confirm_person(conn, core.settings)
    except Exception as exc:
        from snowpea_core.external.proc import ExternalError

        if isinstance(exc, ExternalError):
            raise RpcError(exc.code, exc.message, exc.details or {}) from exc
        raise
    _machine_ledger(core, machine_id, summary)


def _external_privileged(conn: Any, core: Any, params: Any, machine_id: str, *, what: str) -> None:
    _gate_trust(conn, bool(getattr(params, "trust", False)))
    mode = getattr(params, "mode", None)
    if mode == "unrestricted":
        if not getattr(params, "confirmDangerous", False):
            raise RpcError(
                errors.INVALID_PARAMS,
                "unrestricted mode on a remote machine needs confirmDangerous",
                {"reason": "confirm_dangerous"},
            )
        _gate_person_confirm(conn, core, machine_id, f"{what} unrestricted")
    options = list(getattr(params, "options", None) or [])
    if options or getattr(params, "confirmDangerous", False):
        from snowpea_core.terminal import profiles

        refusal = profiles.dangerous_refusal(core, conn)
        if refusal is not None:
            raise profiles._refuse_dangerous(refusal)  # noqa: SLF001
        if not getattr(params, "confirmDangerous", False):
            raise RpcError(
                errors.INVALID_PARAMS,
                "dangerous options need confirmDangerous on a remote machine",
                {"reason": "confirm_dangerous"},
            )
        _gate_person_confirm(conn, core, machine_id, f"{what} with dangerous options")
    native = getattr(params, "nativeMode", None)
    if native and connection_kind(conn) != "person":
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            "only a local person may set nativeMode on a remote CLI",
            {"reason": "person_only"},
        )
    if native and connection_kind(conn) == "person":
        _machine_ledger(core, machine_id, f"{what} nativeMode {native}")


def _terminal_tunnel_kind(method: str, params: Any) -> str:
    if method == "terminal.create":
        return "create"
    if method == "terminal.attach":
        mode = getattr(params, "mode", None)
        return "attach" if mode == "interactive" else "observe"
    if method in _TERMINAL_WRITE_METHODS:
        return "write"
    if method == "terminal.resize":
        return "resize"
    return "observe"


def _terminal_dangerous(method: str, params: Any) -> bool:
    if method == "terminal.create":
        return bool(getattr(params, "confirmDangerous", False) or getattr(params, "options", None))
    return method in _TERMINAL_WRITE_METHODS and bool(
        getattr(params, "confirmDangerous", False) or getattr(params, "options", None)
    )


def gate_connection(conn: Any, core: Any) -> None:
    _refuse_kind(conn)
    _refuse_remote_off(conn, core)


def gate_session_create(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    from snowpea_core.server.settings_gate import host_choice_refusal
    from snowpea_core.session.mode_guard import ModeCaller, check_remote_max

    host_choice_refusal(conn, getattr(params, "hostToolsFrom", None))
    caller = ModeCaller.from_conn(conn, getattr(params, "actor", None))
    mode = getattr(params, "mode", None) or "accept"
    check_remote_max(
        caller.kind,
        mode,
        core.settings,
        "starting a session on a remote machine",
        current="accept",
    )
    if mode == "unrestricted":
        _gate_person_confirm(conn, core, machine_id, "session.create unrestricted")


def gate_external_create(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    _external_privileged(conn, core, params, machine_id, what="external.create")


def gate_external_open(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    _external_privileged(conn, core, params, machine_id, what="external.open")


def gate_terminal_create(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    from snowpea_core.server.terminal_handlers import _tunnel_gate

    if getattr(params, "profile", None) == "snowpea" and connection_kind(conn) == "remote_login":
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            "a remote web login cannot open the snowpea TUI on a remote machine",
            {"reason": "remote_snowpea"},
        )
    dangerous = _terminal_dangerous("terminal.create", params)
    _tunnel_gate(conn, core, dangerous=dangerous, kind="create")
    if _terminal_dangerous("terminal.create", params):
        _gate_person_confirm(conn, core, machine_id, f"terminal.create {params.profile}")


def gate_terminal_attach(conn: Any, core: Any, params: Any, _machine_id: str) -> None:
    from snowpea_core.server.terminal_handlers import _tunnel_gate

    kind = _terminal_tunnel_kind("terminal.attach", params)
    if kind == "attach":
        _tunnel_gate(conn, core, kind="attach")


def _gate_terminal_io(method: str):
    def gate(conn: Any, core: Any, params: Any, _machine_id: str) -> None:
        from snowpea_core.server.terminal_handlers import _tunnel_gate

        kind = _terminal_tunnel_kind(method, params)
        _tunnel_gate(conn, core, dangerous=_terminal_dangerous(method, params), kind=kind)

    return gate


def gate_terminal_resize(conn: Any, core: Any, params: Any, _machine_id: str) -> None:
    from snowpea_core.server.terminal_handlers import _tunnel_gate

    _tunnel_gate(conn, core, kind="resize")


def gate_terminal_close(conn: Any, core: Any, _params: Any, _machine_id: str) -> None:
    from snowpea_core.server.terminal_handlers import _tunnel_gate

    _tunnel_gate(conn, core, kind="other")


def _session_mode_from_cache(core: Any, machine_id: str, session_id: str) -> str:
    from snowpea_core.machines.jarvis_remote import qualify

    cache = getattr(getattr(core, "machines", None), "_jarvis_cache", None)
    row = cache.session_any(qualify(machine_id, session_id)) if cache is not None else None
    if row is None:
        raise RpcError(
            errors.UNAVAILABLE,
            "could not verify the remote session mode",
            {"reason": "remote_session_unknown"},
        )
    mode = row.mode or "accept"
    if mode == "plan" and getattr(row, "auto_after_plan", False):
        return "auto"
    return str(mode)


def gate_session_set_mode(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    from snowpea_core.server.person_gate import connection_kind
    from snowpea_core.session.mode_guard import check_remote_max, rank, remote_max

    if connection_kind(conn) != "remote_login":
        return
    wanted = getattr(params, "mode", None) or "accept"
    check_remote_max(
        "remote_login", wanted, core.settings, "raising a remote session", current="accept"
    )
    limit = remote_max(core.settings)
    if rank(wanted) > rank(limit):
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            f"raising the remote session to {wanted} is above web.remote.maxMode ({limit})",
            {"reason": "mode_raise", "max": limit},
        )
    if getattr(params, "actor", None):
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            "a remote sign-in may not set actor on a remote session",
            {"reason": "person_only"},
        )


def gate_session_prompt_or_steer(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "remote_login":
        return
    sid = str(getattr(params, "sessionId", "") or "")
    if not sid:
        return
    # Mode cap for remote logins is enforced from a fresh ``session.list`` in
    # :func:`ensure_remote_session_prompt_cap` (Jarvis off must not block this).


def gate_session_prompt(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    gate_session_prompt_or_steer(conn, core, params, machine_id)


def gate_session_steer(conn: Any, core: Any, params: Any, machine_id: str) -> None:
    gate_session_prompt_or_steer(conn, core, params, machine_id)


def gate_jarvis_switch(conn: Any, core: Any, params: Any, _machine_id: str) -> None:
    from snowpea_core.server.person_gate import connection_kind, require_person

    if connection_kind(conn) == "remote_login":
        require_person(conn, "change a Jarvis switch on a remote machine")


_GATES: dict[str, Any] = {
    "session.create": gate_session_create,
    "session.prompt": gate_session_prompt,
    "session.steer": gate_session_steer,
    "session.setMode": gate_session_set_mode,
    "external.create": gate_external_create,
    "external.open": gate_external_open,
    "terminal.create": gate_terminal_create,
    "terminal.attach": gate_terminal_attach,
    "terminal.write": _gate_terminal_io("terminal.write"),
    "terminal.send": _gate_terminal_io("terminal.send"),
    "terminal.keys": _gate_terminal_io("terminal.keys"),
    "terminal.interrupt": _gate_terminal_io("terminal.interrupt"),
    "terminal.resize": gate_terminal_resize,
    "terminal.close": gate_terminal_close,
    "jarvis.session.set": gate_jarvis_switch,
    "terminal.setJarvis": gate_jarvis_switch,
}


def _trust_gate_any(conn: Any, params: BaseModel) -> None:
    if getattr(params, "trust", False):
        _gate_trust(conn, True)


def proxy_params_dump(
    conn: Any, core: Any, params: BaseModel, *, method: str
) -> dict[str, Any]:
    """Serialize proxied params for the remote daemon."""
    from snowpea_core.server.person_gate import connection_kind
    from snowpea_core.session.mode_guard import rank, remote_max

    data = params.model_dump(mode="json", exclude_unset=True)
    if connection_kind(conn) != "remote_login":
        return data
    if "mode" not in type(params).model_fields:
        return data
    if method == "session.create":
        limit = remote_max(core.settings)
        chosen = str(data.get("mode") or "accept")
        if chosen == "unrestricted" or rank(chosen) > rank(limit):
            chosen = limit
        data["mode"] = chosen
        return data
    if method == "session.setMode" and connection_kind(conn) == "remote_login":
        limit = remote_max(core.settings)
        chosen = str(data.get("mode") or "accept")
        if rank(chosen) > rank(limit):
            chosen = limit
        data["mode"] = chosen
        data.pop("actor", None)
        return data
    if method in ("external.create", "external.open"):
        chosen = data.get("mode")
        if chosen is None or chosen == "unrestricted":
            data["mode"] = "default"
        return data
    # terminal.attach and every other method: leave mode as the caller sent it.
    return data


#: Every field on a proxied params model must be listed here as ``gated`` or
#: ``harmless``.  A new protocol field fails the guard test until classified.
PROXY_PARAM_CLASSIFICATION: dict[str, dict[str, str]] = {
    "external.clis": {"machine": "harmless"},
    "external.create": {
        "cli": "harmless",
        "confirmDangerous": "gated",
        "machine": "harmless",
        "mode": "gated",
        "model": "harmless",
        "nativeMode": "gated",
        "options": "gated",
        "project": "harmless",
        "transport": "harmless",
        "trust": "gated",
    },
    "external.list": {
        "allProjects": "harmless",
        "before": "harmless",
        "clis": "harmless",
        "cwd": "harmless",
        "limit": "harmless",
        "machine": "harmless",
        "sessionId": "harmless",
    },
    "external.open": {
        "confirmDangerous": "gated",
        "limit": "harmless",
        "machine": "harmless",
        "mode": "gated",
        "nativeMode": "gated",
        "options": "gated",
        "ref": "harmless",
        "transport": "harmless",
        "trust": "gated",
    },
    "session.create": {
        "actor": "gated",
        "agent": "harmless",
        "browserMemory": "harmless",
        "denyExec": "harmless",
        "effort": "harmless",
        "hostToolsFrom": "gated",
        "locale": "harmless",
        "machine": "harmless",
        "maxConcurrent": "harmless",
        "memory": "harmless",
        "mode": "gated",
        "model": "harmless",
        "noProject": "harmless",
        "originSurface": "harmless",
        "provider": "harmless",
        "siteMemory": "harmless",
        "workdir": "harmless",
    },
    "session.list": {
        "hostToolsFrom": "harmless",
        "includeClosed": "harmless",
        "includeUnhosted": "harmless",
        "kinds": "harmless",
        "machine": "harmless",
        "workdir": "harmless",
    },
    "session.resume": {
        "afterSeq": "harmless",
        "locale": "harmless",
        "machine": "harmless",
        "sessionId": "harmless",
    },
    "terminal.ack": {"bytes": "harmless", "machine": "harmless", "terminalId": "harmless"},
    "terminal.attach": {
        "cols": "harmless",
        "machine": "harmless",
        "mode": "gated",
        "rows": "harmless",
        "terminalId": "harmless",
    },
    "terminal.close": {"force": "gated", "machine": "harmless", "terminalId": "harmless"},
    "terminal.create": {
        "cols": "harmless",
        "confirmDangerous": "gated",
        "cwd": "harmless",
        "labels": "harmless",
        "machine": "harmless",
        "noProject": "harmless",
        "options": "gated",
        "parent": "harmless",
        "profile": "gated",
        "prompt": "harmless",
        "resumeSessionId": "harmless",
        "rows": "harmless",
        "snowpeaSessionId": "harmless",
        "title": "harmless",
        "trust": "gated",
    },
    "terminal.detach": {"machine": "harmless", "terminalId": "harmless"},
    "terminal.interrupt": {"machine": "harmless", "terminalId": "gated"},
    "terminal.keys": {
        "keys": "gated",
        "machine": "harmless",
        "terminalId": "gated",
        "expectState": "harmless",
    },
    "terminal.list": {"aliveOnly": "harmless", "machine": "harmless", "parent": "harmless"},
    "terminal.profiles": {"machine": "harmless"},
    "terminal.read": {"lines": "harmless", "machine": "harmless", "terminalId": "harmless"},
    "terminal.resize": {
        "cols": "harmless",
        "machine": "harmless",
        "rows": "harmless",
        "terminalId": "gated",
    },
    "terminal.seen": {"machine": "harmless", "terminalId": "harmless"},
    "terminal.send": {
        "force": "gated",
        "machine": "harmless",
        "submit": "gated",
        "terminalId": "gated",
        "text": "gated",
    },
    "terminal.wait": {
        "machine": "harmless",
        "output": "harmless",
        "terminalId": "harmless",
        "timeout": "harmless",
    },
    "terminal.write": {
        "data": "gated",
        "human": "gated",
        "machine": "harmless",
        "terminalId": "gated",
    },
    "workspace.list": {"machine": "harmless"},
    "jarvis.session.set": {
        "enabled": "gated",
        "external": "harmless",
        "sessionId": "harmless",
    },
    "session.artifacts": {"sessionId": "harmless"},
    "session.close": {"sessionId": "harmless"},
    "session.events": {
        "afterSeq": "harmless",
        "limit": "harmless",
        "sessionId": "harmless",
    },
    "session.interrupt": {"sessionId": "harmless"},
    "session.prompt": {
        "attachments": "harmless",
        "sessionId": "harmless",
        "text": "gated",
        "whenBusy": "harmless",
    },
    "session.rename": {"sessionId": "harmless", "title": "harmless"},
    "session.setEffort": {"effort": "harmless", "sessionId": "harmless"},
    "session.setMode": {
        "actor": "gated",
        "autoAfterPlan": "harmless",
        "mode": "gated",
        "sessionId": "harmless",
    },
    "session.setModel": {"model": "harmless", "sessionId": "harmless"},
    "session.steer": {"sessionId": "harmless", "text": "gated"},
    "session.trajectory": {
        "afterSeq": "harmless",
        "limit": "harmless",
        "sessionId": "harmless",
    },
    "terminal.setJarvis": {
        "enabled": "gated",
        "machine": "harmless",
        "terminalId": "harmless",
    },
}


def _refuse_jarvis_privileged(params: BaseModel) -> None:
    if getattr(params, "trust", False):
        raise RpcError(
            errors.UNAUTHORIZED,
            "Jarvis may not trust a project on a remote machine",
            {"reason": "jarvis_proxy_trust"},
        )
    if getattr(params, "confirmDangerous", False) or getattr(params, "options", None):
        raise RpcError(
            errors.UNAUTHORIZED,
            "Jarvis may not use dangerous options on a remote machine",
            {"reason": "jarvis_proxy_dangerous"},
        )
    mode = getattr(params, "mode", None)
    if mode == "unrestricted":
        raise RpcError(
            errors.UNAUTHORIZED,
            "Jarvis may not run unrestricted on a remote machine",
            {"reason": "jarvis_proxy_unrestricted"},
        )


def run_jarvis_proxy_gates(
    method: str,
    params: BaseModel,
    core: Any,
    machine_id: str,
) -> None:
    """Gates for Jarvis's in-process machine proxy (1.77.0, jarvisRemote)."""
    if method not in PROXY_METHODS and method not in JARVIS_PROXY_METHODS:
        raise RpcError(
            errors.NOT_IMPLEMENTED,
            f"{method} cannot be proxied to a remote machine",
            {"method": method},
        )
    _refuse_jarvis_privileged(params)
    gate = _GATES.get(method)
    if gate is not None:
        jarvis_conn = type("_J", (), {"client_kind": "jarvis"})()
        gate(jarvis_conn, core, params, machine_id)


def run_proxy_gates(
    method: str,
    conn: Any,
    params: BaseModel,
    core: Any,
    machine_id: str,
) -> None:
    """Synchronous gates every proxied call must pass before forwarding."""
    if method not in PROXY_METHODS:
        raise RpcError(
            errors.NOT_IMPLEMENTED,
            f"{method} cannot be proxied to a remote machine",
            {"method": method},
        )
    gate_connection(conn, core)
    _trust_gate_any(conn, params)
    gate = _GATES.get(method)
    if gate is not None:
        gate(conn, core, params, machine_id)


async def gate_remote_snowpea_terminal(
    conn: Any,
    core: Any,
    terminal_id: str,
    client: Any,
    *,
    interactive_attach: bool = False,
) -> None:
    """Remote web logins may not type into a remote snowpea TUI (review H2/H6)."""
    if connection_kind(conn) != "remote_login":
        return
    _ = interactive_attach
    try:
        listed = await client.call("terminal.list", {})
    except Exception:
        raise RpcError(
            errors.UNAVAILABLE,
            "could not verify the remote terminal profile",
            {"reason": "remote_profile_unknown"},
        ) from None
    profile = None
    for row in listed.get("terminals") or []:
        if isinstance(row, dict) and row.get("terminalId") == terminal_id:
            profile = row.get("profile")
            break
    if profile == "snowpea":
        raise RpcError(
            errors.JARVIS_PERSON_ONLY,
            "the snowpea TUI acts as the remote person; a remote web login may only watch it",
            {"reason": "remote_snowpea"},
        )


async def filter_proxy_result(
    conn: Any,
    core: Any,
    method: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Passthrough; connector/browser are refused at the gate (C1)."""
    _ = (conn, core, method)
    return result


async def ensure_remote_session_prompt_cap(
    conn: Any,
    core: Any,
    machine_id: str,
    session_id: str,
    *,
    client: Any,
) -> None:
    """Fresh remote session mode for a remote-login prompt or steer (H1)."""
    from snowpea_core.server.person_gate import connection_kind
    from snowpea_core.session.mode_guard import check_remote_max

    if connection_kind(conn) != "remote_login":
        return
    if client is None:
        raise RpcError(
            errors.UNAVAILABLE,
            "could not verify the remote session mode",
            {"reason": "remote_session_unknown"},
        )
    try:
        listed = await client.call("session.list", {})
    except Exception:
        raise RpcError(
            errors.UNAVAILABLE,
            "could not verify the remote session mode",
            {"reason": "remote_session_unknown"},
        ) from None
    mode: str | None = None
    auto_after_plan = False
    for item in listed.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("sessionId")) != session_id:
            continue
        mode = str(item.get("mode") or "accept")
        auto_after_plan = bool(item.get("autoAfterPlan"))
        if not auto_after_plan:
            jarvis = item.get("jarvis") or {}
            if isinstance(jarvis, dict):
                auto_after_plan = bool(jarvis.get("autoAfterPlan"))
        break
    if mode is None:
        raise RpcError(
            errors.UNAVAILABLE,
            "could not verify the remote session mode",
            {"reason": "remote_session_unknown"},
        )
    if mode == "plan" and auto_after_plan:
        mode = "auto"
    check_remote_max("remote_login", mode, core.settings, "prompting a remote session")


async def ensure_machine_proxy_async(
    method: str,
    conn: Any,
    params: BaseModel,
    core: Any,
    machine_id: str,
    *,
    client: Any = None,
    terminal_id: str | None = None,
) -> None:
    if client is None or not terminal_id:
        return
    writers = {*_TERMINAL_WRITE_METHODS, "terminal.attach"}
    if method not in writers:
        return
    interactive = (
        method == "terminal.attach"
        and getattr(params, "mode", None) == "interactive"
    )
    if method in _TERMINAL_WRITE_METHODS or interactive:
        await gate_remote_snowpea_terminal(
            conn, core, terminal_id, client, interactive_attach=interactive
        )


def gate_jarvis_machine_call(
    method: str, params: BaseModel, core: Any, machine_id: str
) -> None:
    """Jarvis in-process remote calls: no trust, no dangerous options, switch must be on."""
    if method not in PROXY_METHODS and method not in JARVIS_PROXY_METHODS:
        raise RpcError(
            errors.NOT_IMPLEMENTED,
            f"{method} cannot be proxied to a remote machine",
            {"method": method},
        )
    if getattr(params, "trust", False):
        raise RpcError(errors.NOT_FOUND, "not found")
    if getattr(params, "confirmDangerous", False) or getattr(params, "options", None):
        raise RpcError(errors.NOT_FOUND, "not found")
    mode = getattr(params, "mode", None)
    if mode == "unrestricted":
        raise RpcError(errors.NOT_FOUND, "not found")
    jarvis = getattr(core, "jarvis", None)
    if jarvis is None or not jarvis.running:
        raise RpcError(errors.JARVIS_DISABLED, "Jarvis mode is off")
    if method.startswith("terminal.") and getattr(params, "profile", None) == "snowpea":
        raise RpcError(errors.NOT_FOUND, "not found")
    term_id = getattr(params, "terminalId", None)
    manager = getattr(core, "machines", None)
    cache = getattr(manager, "_jarvis_cache", None) if manager is not None else None
    terminal_methods = {
        "terminal.send",
        "terminal.keys",
        "terminal.interrupt",
        "terminal.write",
        "terminal.close",
        "terminal.read",
        "terminal.wait",
        "terminal.attach",
        "terminal.resize",
    }
    if term_id and method in terminal_methods:
        if cache is None or not cache.jarvis_on(machine_id, str(term_id)):
            raise RpcError(errors.NOT_FOUND, "not found")
    if method == "terminal.create" and getattr(params, "profile", None) == "snowpea":
        raise RpcError(
            errors.NOT_FOUND,
            "Jarvis cannot type into a snowpea TUI terminal on a remote machine",
            {"reason": "remote_snowpea_tui"},
        )
    if method in ("jarvis.session.set", "terminal.setJarvis"):
        # The switch is the person's: Jarvis never turns one on (or off) over the proxy.
        raise RpcError(errors.JARVIS_PERSON_ONLY, "only the person changes a Jarvis switch")
    session_id = getattr(params, "sessionId", None)
    if method.startswith("session.") and session_id:
        if cache is None or not cache.jarvis_on_session(machine_id, str(session_id)):
            raise RpcError(errors.NOT_FOUND, "not found")
    ref = getattr(params, "ref", None)
    if method.startswith("external.") and ref is not None:
        if cache is None or not cache.jarvis_on_external(machine_id, ref):
            raise RpcError(errors.NOT_FOUND, "not found")


# Backward-compatible name (sync part only).
ensure_machine_proxy_allowed = run_proxy_gates