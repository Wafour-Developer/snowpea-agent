"""Proxy allowlist and remote RPC forwarding (M40)."""

from __future__ import annotations

from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

PROXY_METHODS: frozenset[str] = frozenset(
    {
        "session.list",
        "session.create",
        "session.resume",
        "session.prompt",
        "session.steer",
        "session.interrupt",
        "session.close",
        "session.rename",
        "session.events",
        "session.trajectory",
        "session.setMode",
        "session.setModel",
        "session.setEffort",
        "session.artifacts",
        "jarvis.session.set",
        "terminal.setJarvis",
        "external.clis",
        "external.list",
        "external.open",
        "external.create",
        "terminal.profiles",
        "terminal.list",
        "terminal.create",
        "terminal.attach",
        "terminal.detach",
        "terminal.write",
        "terminal.resize",
        "terminal.ack",
        "terminal.read",
        "terminal.send",
        "terminal.keys",
        "terminal.interrupt",
        "terminal.wait",
        "terminal.close",
        "terminal.seen",
        "workspace.list",
    }
)

#: Methods only Jarvis's in-process proxy (``jarvis_proxy_call``) may forward; a client's
#: ``machine`` param never reaches them (``refuse_unknown_proxy`` keeps to PROXY_METHODS).
JARVIS_PROXY_METHODS: frozenset[str] = frozenset(
    {"external.read", "external.send", "external.interrupt", "external.get"}
)

RELAY_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"terminal.data", "terminal.status", "terminal.created", "terminal.exit", "terminal.title"}
)

RELAY_PERSON_EVENTS: frozenset[str] = frozenset({"approval.resolved", "question.resolved"})

RELAY_SESSION_EVENTS: frozenset[str] = frozenset({"session.event"})


def strip_machine(params: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    machine = params.get("machine")
    if machine is None or machine == "":
        return None, params
    if not isinstance(machine, str):
        raise RpcError(errors.INVALID_PARAMS, "machine must be a string id")
    cleaned = {k: v for k, v in params.items() if k != "machine"}
    return machine, cleaned


def refuse_unknown_proxy(method: str) -> None:
    if method not in PROXY_METHODS:
        raise RpcError(
            errors.NOT_IMPLEMENTED,
            f"{method} cannot be proxied to a remote machine",
            {"method": method},
        )