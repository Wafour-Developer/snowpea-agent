"""Who may change which settings over RPC (security review 3, N7; browser-team design).

Every rule is a table, so a list can be adjusted without touching code.  A
``settings.set`` patch is judged leaf by leaf: each key it sets or deletes is a
path (``{"mcp": null}`` is the path ``mcp``), and a table entry ``P`` covers a
path ``L`` when ``L`` is at least as long as ``P`` and agrees with it part by
part (``*`` stands for any one key).  Deleting a parent of a listed key is not
covered by it.

Who calls (``person_gate.connection_kind``):

``person``
    A local person (IDE, TUI, CLI, desktop, the local web page): anything,
    subject to the value checks in ``settings_handlers.guard_patch``.
``browser`` with ``actor: "person"``
    The snowpea browser's own Settings pages, under a user gesture
    (:data:`BROWSER_PERSON_KEYS`).
``browser`` without it
    The browser host's own runtime writes (:data:`BROWSER_HOST_KEYS`).
``remote_login``
    A person on the remote web page, not re-authenticated
    (:data:`REMOTE_LOGIN_KEYS`).
``connector`` / anything else
    :data:`CONNECTOR_KEYS`.
``none``
    An in-process caller: nothing.

:data:`PERSON_LOCAL_ONLY` (and :data:`PROJECT_PERSON_LOCAL_ONLY` in the project
scope) is a local person's whatever the lists say.  While Jarvis mode is on,
the keys that make up Jarvis's authority chain (:func:`jarvis_authority`) are a
local person's too, refused with ``details.reason: "jarvis_on"``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

log = logging.getLogger("snowpea.server.settings_gate")

Pattern = tuple[str, ...]

#: A local person's only, from any other connection kind, with or without the marker.
PERSON_LOCAL_ONLY: tuple[Pattern, ...] = (
    ("jarvis",),
    ("security",),  # the security lists (1.54.0): blocked commands, protected paths
    ("daemon",),
    ("web",),
    ("extensions",),
    ("allowlist",),
    ("mcp", "permissions"),
    ("mcp", "allowVars"),
    # Security review 7, H-2: ``media.mcp`` names a program (command, args, env)
    # or a URL the daemon starts or calls with a stored key.
    ("media",),
)
#: The same for the project scope (every other project key is refused to
#: non-local callers too: no client but a local one writes the project scope).
PROJECT_PERSON_LOCAL_ONLY: tuple[Pattern, ...] = (("jarvis",), ("allowlist",))
#: What a remote web login may write in the PROJECT scope (security review 7,
#: H-2: an allow table, not only the never-list): the keys a project document
#: has for running work — its default mode (never above ``web.remote.maxMode``,
#: and counted only once the person trusts the project), teams and model
#: routing.  Everything else — any key the project schema does not know
#: included — is a local person's.  No browser or connector writes this scope.
PROJECT_REMOTE_LOGIN_KEYS: tuple[Pattern, ...] = (
    ("defaultMode",),
    ("agents",),
    ("models",),
)

#: What the snowpea browser's Settings/setup pages write (the browser team's
#: inventory, 2026-11-16), with ``actor: "person"`` (global scope only).
BROWSER_PERSON_KEYS: tuple[Pattern, ...] = (
    ("agent",),
    ("browser",),
    ("gateway",),
    ("lsp",),
    ("memory",),
    ("models",),
    ("providers",),
    ("scheduler",),
    ("search",),
    ("skills",),
)
#: Of :data:`PERSON_LOCAL_ONLY`'s ``security``, what the snowpea browser's
#: Settings page may change too (1.58.0, owner decision 2026-11-18: the browser
#: turns the checks off as well as on): only with an explicit ``actor:
#: "person"`` (a legacy browser without the marker does not count), only in
#: the global scope.  Any other ``security`` key, and ``security`` as a whole
#: (``{"security": null}``), stays a local person's.  Remote web logins,
#: connectors and in-process callers (Jarvis) never write any of them.
BROWSER_PERSON_SECURITY_KEYS: tuple[Pattern, ...] = (
    ("security", "enabled"),
    ("security", "unknownCode"),
    ("security", "blockedCommands"),
    ("security", "protectedPaths"),
)
#: What a browser connection may write without the marker: nothing (its own
#: runtime writes go through ``site.*`` and the other host methods).
BROWSER_HOST_KEYS: tuple[Pattern, ...] = ()
#: The remote connector (and any unknown client kind): nothing it needs today.
CONNECTOR_KEYS: tuple[Pattern, ...] = ()
#: A remote web login (a person, not re-authenticated): the browser's person
#: keys, MCP servers and approval timeouts the IDE's pages write, and the
#: IDE's view state.
REMOTE_LOGIN_KEYS: tuple[Pattern, ...] = (
    *BROWSER_PERSON_KEYS,
    ("mcp", "servers"),
    ("approvals",),
    ("ide",),
    ("appearance",),
    ("audio",),
)

#: Fields of an MCP server entry that say which program runs and with what
#: (a stdio server): a remote web login may neither set nor change them
#: (security review 6, H-1) — url-only servers stay its to manage.
MCP_LAUNCH_FIELDS: tuple[str, ...] = ("command", "args", "env", "cwd")
#: ``gateway.<platform>`` fields that decide which bot speaks, to whom, and
#: whether it runs: access a remote login could leave behind (review 6, M-4).
REMOTE_GATEWAY_FIELDS: tuple[str, ...] = (
    "token", "app_token", "allowed_user_id", "user_id", "channel_id", "enabled",
)
#: Keys of :data:`REMOTE_LOGIN_KEYS` a remote web login still may not write,
#: Jarvis mode on or off (security review 6, H-1/M-4): what names a program to
#: run (stdio MCP servers, language-server commands, audio command engines,
#: players and recorders), a messenger's identity, where model traffic goes
#: (a provider's base URL) and where skills are installed from.  A key is
#: refused when the patch sets or deletes it or anything under it
#: (:func:`matches`); deleting a whole parent (an MCP server, a messenger
#: block) only takes away and stays allowed.  ``details.reason: "local_only"``.
#: Fields of a ``providers.<vendor>`` block that are the account it uses: a key,
#: an OAuth session or the record a browser login leaves (security review 8,
#: M-1 — a remote login setting one moves the owner's traffic, billing and
#: logs to its own account, and it survives a rotation of the remote link).
PROVIDER_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "api_key", "apiKey", "token", "oauth_token", "refresh_token", "access_token", "id_token",
    "auth_method", "expires_at", "account_id", "plan_type", "project_id",
)
REMOTE_LOGIN_NEVER_KEYS: tuple[Pattern, ...] = (
    *(("mcp", "servers", "*", name) for name in MCP_LAUNCH_FIELDS),
    *(("gateway", "*", name) for name in REMOTE_GATEWAY_FIELDS),
    ("providers", "*", "base_url"),
    *(("providers", "*", name) for name in PROVIDER_CREDENTIAL_FIELDS),
    ("lsp", "servers"),
    ("audio", "stt", "command"),
    ("audio", "tts", "command"),
    ("audio", "player"),
    ("audio", "recorder"),
    ("skills", "registry"),
)


def _never_by_shape(path: Pattern) -> bool:
    """``path`` sets an endpoint-shaped field of a free-form block (security review
    8, H8-1: ``search.credentials.<id>.url`` and the like — a provider posts the
    owner's key, from settings or the daemon's environment, to that address)."""
    from snowpea_core.server.settings_leaves import ENDPOINT_SHAPED, endpoint_key

    for block in ENDPOINT_SHAPED:
        pattern = tuple(block.split("."))
        if len(path) > len(pattern) and matches(path[:len(pattern)], pattern) and endpoint_key(
            path[len(pattern)]
        ):
            return True
    return False


LOCAL_ONLY = "local_only"

#: ``gateway.<platform>`` fields that say who the bound person is, which bot
#: speaks for them and what is shared there: Jarvis's chain while it is on.
GATEWAY_IDENTITY_FIELDS: tuple[str, ...] = (
    "allowed_user_id", "user_id", "channel_id", "token", "app_token", "shareActive",
)

ACTOR_PERSON = "person"

#: Browsers older than this (``system.hello`` ``clientVersion``; missing or
#: unparseable counts as older) predate the ``actor`` marker: their calls get
#: the marker's list without it.  Every shipped browser sends ``0.2.0``.
BROWSER_ACTOR_CUTOVER: tuple[int, int, int] = (0, 3, 0)


def _version(text: Any) -> tuple[int, int, int] | None:
    import re

    match = re.fullmatch(r"\s*v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:[-+][0-9A-Za-z.+-]*)?\s*",
                         str(text)) if isinstance(text, str) else None
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def legacy_browser(conn: Any) -> bool:
    """A browser connection from before the ``actor`` marker (the one cut-over rule)."""
    version = _version(getattr(conn, "client_version", None))
    return version is None or version < BROWSER_ACTOR_CUTOVER


def acts_for_person(conn: Any, actor: str | None) -> bool:
    """A browser call counts as its person's: ``actor: "person"``, or a legacy browser."""
    return actor == ACTOR_PERSON or legacy_browser(conn)


def matches(path: Pattern, pattern: Pattern) -> bool:
    """``pattern`` covers ``path`` (``path`` at least as long, ``*`` any key)."""
    return len(path) >= len(pattern) and all(
        want in ("*", got) for want, got in zip(pattern, path, strict=False)
    )


def touches(path: Pattern, pattern: Pattern) -> bool:
    """Setting or deleting ``path`` changes something under ``pattern`` (either is a prefix)."""
    return all(want in ("*", got) for want, got in zip(pattern, path, strict=False))


def leaves(node: Any, prefix: Pattern = ()) -> list[tuple[Pattern, Any]]:
    """Every key a patch sets or deletes, with its value."""
    if isinstance(node, dict) and node:
        out: list[tuple[Pattern, Any]] = []
        for key, value in node.items():
            out.extend(leaves(value, (*prefix, str(key))))
        return out
    return [(prefix, node)]


def table_for(kind: str, actor: str | None) -> tuple[Pattern, ...]:
    """The keys a caller of ``kind`` may change (``actor``: the browser's marker)."""
    if kind == "browser":
        return BROWSER_PERSON_KEYS if actor == ACTOR_PERSON else BROWSER_HOST_KEYS
    if kind == "remote_login":
        return REMOTE_LOGIN_KEYS
    if kind == "none":
        return ()
    return CONNECTOR_KEYS


def jarvis_authority(core: Any) -> tuple[Pattern, ...]:
    """Keys that define Jarvis's authority chain right now (empty while Jarvis is off).

    The messenger approver and bot identity, the model profile and provider
    Jarvis runs on (``jarvis.model``, else ``models.default``), and the MCP
    servers presets added for Jarvis and its workers.
    """
    settings = getattr(core, "settings", None)
    jarvis = getattr(settings, "jarvis", None)
    if not getattr(jarvis, "enabled", False):
        return ()
    found: list[Pattern] = [("gateway", "*", field) for field in GATEWAY_IDENTITY_FIELDS]
    models = getattr(settings, "models", None)
    ref = getattr(jarvis, "model", None)
    if not ref:
        found.append(("models", "default"))
        ref = getattr(models, "default", None)
    if isinstance(ref, str) and ref:
        found.append(("models", "profiles", ref))
        profiles = getattr(models, "profiles", None) or {}
        profile = profiles.get(ref) if isinstance(profiles, dict) else None
        provider = (
            profile.get("provider") if isinstance(profile, dict)
            else getattr(profile, "provider", None)
        )
        if not provider and ":" in ref:
            provider = ref.split(":", 1)[0]
        if isinstance(provider, str) and provider:
            found.append(("providers", provider))
    try:
        from snowpea_core.jarvis.presets import applied_servers

        found.extend(("mcp", "servers", name) for name in applied_servers(core))
    except Exception:  # noqa: BLE001 - no preset book: no preset servers
        log.debug("could not read the preset book", exc_info=True)
    return tuple(found)


def refuse(conn: Any, what: str, *, reason: str = "person_only",
           extra: dict[str, Any] | None = None, hint: str | None = None) -> None:
    """``jarvis_person_only`` with ``details.reason`` (``person_only``, ``jarvis_on``,
    ``not_bundled_plugin``, …) and ``extra`` details; ``hint`` ends the message."""
    from snowpea_core.server.person_gate import connection_kind
    from snowpea_core.util import i18n

    kind = connection_kind(conn)
    if reason.startswith("browser_") and reason in i18n.MESSAGES:
        # 1.65.0 (clientLocale): what a browser shows its person, in its language.
        message = i18n.text("refusal.browser", conn=conn, why=i18n.text(reason, conn=conn))
    elif reason == "jarvis_on":
        message = (
            f"Jarvis mode is on: {what} defines what Jarvis may do; change it in the "
            "snowpea app or the TUI"
        )
    elif kind == "remote_login":
        message = f"{what} needs a local connection (IDE, TUI or the local web page)"
    elif kind in ("none", "closed"):
        message = f"only a person may {what}"
    else:
        message = f"a {getattr(conn, 'client_kind', None) or kind} connection cannot {what}"
    if hint:
        message = f"{message}; {hint}"
    raise RpcError(errors.JARVIS_PERSON_ONLY, message,
                   {"reason": reason, "kind": kind, **(extra or {})})


def refuse_closed(conn: Any, what: str) -> None:
    """A connection that has closed (or never signed in) is nobody: refused,
    never read as an in-process caller (security review 7, H-1)."""
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) == "closed":
        refuse(conn, what)


def remote_login_local_keys(patch: dict[str, Any]) -> list[str]:
    """Dotted keys of ``patch`` under :data:`REMOTE_LOGIN_NEVER_KEYS`, and the
    endpoint-shaped fields of the free-form blocks (:func:`_never_by_shape`)."""
    return [
        ".".join(path)
        for path, _value in leaves(patch)
        if any(matches(path, pattern) for pattern in REMOTE_LOGIN_NEVER_KEYS)
        or _never_by_shape(path)
    ]


def refuse_local_only(conn: Any, core: Any, what: str, *, method: str, detail: str) -> None:
    """Refuse (and ledger) what a remote web login may only do at a local connection."""
    ledger_settings(core, conn, [f"{method} ({detail})"], refused=True, method=method)
    refuse(conn, what, reason=LOCAL_ONLY, extra={"what": detail})


def _launch(entry: Any) -> dict[str, Any]:
    """The part of an MCP entry that decides which process runs (empty: none)."""
    if not isinstance(entry, dict) or not entry.get("command"):
        return {}
    return {name: entry[name] for name in MCP_LAUNCH_FIELDS if entry.get(name)}


def remote_mcp_refusal(conn: Any, core: Any, *, method: str, entry: dict[str, Any],
                       saved: dict[str, Any] | None = None, accept: bool = False) -> None:
    """``mcp.add|update|test`` from a remote web login (security review 6, H-1).

    Refused: an entry that runs a program (``command``/``args``/``env``/``cwd``)
    unless it is the saved one's, unchanged; and ``force``/``acceptFindings``
    (the security findings are a local person's to wave through).  Url-only
    servers, and probing or re-saving a saved entry whose launch is unchanged,
    stay allowed.  Other kinds are not judged here.
    """
    from snowpea_core.server.person_gate import connection_kind

    refuse_closed(conn, f"call {method}")
    if connection_kind(conn) != "remote_login":
        return
    if accept:
        refuse_local_only(conn, core, f"call {method} with force or acceptFindings",
                          method=method, detail="acceptFindings")
    launch = _launch(entry)
    if launch and launch != _launch(saved):
        refuse_local_only(conn, core, f"{method} of a server that runs a command",
                          method=method, detail="command")
    # Security review 7, H-3: the daemon fills ``${NAME}`` from credentials.json
    # and its environment, so whoever chooses where a server's requests go, or
    # what they carry, chooses where a stored secret is sent.
    if saved is not None and mcp_target(entry) != mcp_target(saved):
        refuse_local_only(
            conn, core, f"{method} that changes a saved server's url, headers or oauth",
            method=method, detail="target")
    if has_variable(_fresh(entry, saved)):
        refuse_local_only(conn, core, f"{method} of an entry that uses a ${{VAR}}",
                          method=method, detail="variable")


#: Fields of a SAVED MCP entry a remote web login may not change (security
#: review 7, H-3): where its requests go and what they carry.
MCP_TARGET_FIELDS: tuple[str, ...] = ("url", "headers", "oauth")


def mcp_target(entry: Any) -> dict[str, Any]:
    """The part of an MCP entry that says where requests go and what they carry."""
    if not isinstance(entry, dict):
        return {}
    return {name: entry[name] for name in MCP_TARGET_FIELDS if entry.get(name)}


def has_variable(value: Any) -> bool:
    """``value`` (a string, or strings in lists and objects) holds a ``${NAME}``
    the MCP client would expand (``$${NAME}``, the literal text, does not count)."""
    if isinstance(value, str):
        from snowpea_core.tools.mcp_client import var_names

        return bool(var_names(value))
    if isinstance(value, dict):
        return any(has_variable(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(has_variable(item) for item in value)
    return False


def _fresh(entry: Any, saved: Any) -> dict[str, Any]:
    """The fields of ``entry`` that are not the saved entry's, unchanged."""
    if not isinstance(entry, dict):
        return {}
    known = saved if isinstance(saved, dict) else {}
    return {name: value for name, value in entry.items() if known.get(name) != value}


def remote_mcp_settings_keys(patch: dict[str, Any], current: Any) -> list[str]:
    """Dotted ``mcp.servers.*`` keys of a ``settings.set`` patch a remote web
    login may not write (security review 7, H-3): ``url``/``headers``/``oauth``
    of a server ``settings.mcp.servers`` already has, and any value with a
    ``${VAR}`` — the same rule as ``mcp.add|update`` (``details.reason:
    "local_only"``)."""
    servers = patch.get("mcp", {}).get("servers") if isinstance(patch.get("mcp"), dict) else None
    if not isinstance(servers, dict):
        return []
    saved = current if isinstance(current, dict) else {}
    found: list[str] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue  # null: the server goes, which only takes away
        if has_variable(entry):
            found.append(f"mcp.servers.{name}")
            continue
        if name in saved:
            found.extend(f"mcp.servers.{name}.{field}" for field in MCP_TARGET_FIELDS
                         if field in entry)
    return found


#: ``hostToolsFrom`` naming "whichever browser is connected" (the browser's
#: standing orders and Tasks store it in their jobs' session templates).
BROWSER_ALIAS = "browser"
#: Browser calls limited to the browser's own sessions (security review 6):
#: method -> whether ``actor: "person"`` (or a legacy browser) opens the other
#: sessions too.  ``checkpoint.restore``: its own sessions only, marker or not;
#: ``session.deleteSaved`` / ``session.close``: any session with the marker.
BROWSER_OWN_SESSION_METHODS: dict[str, bool] = {
    "checkpoint.restore": False,
    "session.deleteSaved": True,
    "session.close": True,
    "session.delete": True,
}


def _names_browser(conn: Any, host: Any) -> bool:
    """``host`` (a ``hostToolsFrom``) is this browser connection, or the alias."""
    return bool(host) and host in (
        getattr(conn, "client_id", None), getattr(conn, "surface_id", None), BROWSER_ALIAS,
    )


def _only_browser(conn: Any) -> bool:
    """``conn`` is the one browser host connected now (the alias means it)."""
    from snowpea_core.tools.host_tools import HOST_TOOLS

    try:
        hosts = HOST_TOOLS.browser_hosts()
    except Exception:  # noqa: BLE001 - unknown: not the only one
        return False
    mine = getattr(conn, "client_id", None)
    others = [
        host for host in hosts
        if host is not conn and not (mine and getattr(host, "client_id", None) == mine)
    ]
    return not others


def owns_session(conn: Any, host: Any, surface: Any, creator: Any) -> bool:
    """A browser's OWN session (security review 7, Low 6).

    One this browser hosts — ``hostToolsFrom`` is its clientId or its surface
    id — or one it created (the creating connection's clientId, recorded by the
    server, is its own).  The alias ``"browser"`` ("whichever browser is
    connected") and a browser-opened session with no recorded creator (stored
    before 1.55.0) mean this browser only while it is the single browser host
    connected; with two connected, the one that created the session.  It used
    to be any browser's: the alias and ``originSurface: "browser"`` (which the
    caller supplied) matched every browser.
    """
    mine = getattr(conn, "client_id", None)
    if host and host in (mine, getattr(conn, "surface_id", None)):
        return True
    if creator and mine and creator == mine:
        return True
    if host == BROWSER_ALIAS or (not host and not creator and surface == BROWSER_ALIAS):
        return _only_browser(conn)
    return False


async def browser_session_refusal(conn: Any, core: Any, session_id: Any, *, method: str,
                                  actor: str | None = None) -> None:
    """A browser's :data:`BROWSER_OWN_SESSION_METHODS` call: its own session, or refused.

    Its own: :func:`owns_session`, live or stored.  Any other session is
    ``jarvis_person_only`` (``person_only``) unless the method's entry lets the
    marker open it.  Other kinds, and a session nobody knows, are not judged.
    """
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "browser":
        return
    with_marker = BROWSER_OWN_SESSION_METHODS[method]
    host = surface = creator = None
    found = False
    sessions = getattr(core, "sessions", None)
    live = sessions.get(str(session_id)) if sessions is not None and session_id else None
    if live is not None:
        found = True
        host, surface = getattr(live, "host_tools_from", None), getattr(
            live, "origin_surface", None)
        creator = getattr(live, "origin_client_id", None)
    elif session_id and getattr(core, "store", None) is not None:
        try:
            row = await core.store.session(str(session_id))
        except Exception:  # noqa: BLE001 - unknown: the handler answers not_found
            row = None
        if row:
            found = True
            host, surface = row.get("host_tools_from"), row.get("origin_surface")
            creator = row.get("origin_client_id")
    if session_id and not found:
        return
    if found and owns_session(conn, host, surface, creator):
        return
    if with_marker and acts_for_person(conn, actor):
        return
    refuse(conn, f"call {method} on a session this browser does not host")


def host_choice_refusal(conn: Any, host: Any) -> None:
    """``hostToolsFrom`` in ``session.create``, ``session.attach`` and a job's
    ``sessionTemplate`` (security review 6, L-2).

    Whose browser a session drives is a local person's choice, or a browser's
    for itself (its ``clientId``, its surface id or the alias ``"browser"``) —
    never a remote login's, a connector's, or one browser's for another.  A
    scheduled run inherits it: the job's template was judged when its creator
    made it, and the scheduler creates the session in-process.
    """
    from snowpea_core.server.person_gate import connection_kind

    if not host:
        return
    kind = connection_kind(conn)
    own = _names_browser(conn, host)
    if kind in ("person", "none") or (kind == "browser" and own):
        return  # ``none``: an in-process caller, not judged here (as guard_call)
    refuse(conn, "create a session on another client's host tools",
           reason=LOCAL_ONLY if kind == "remote_login" else "person_only",
           extra={"what": "hostToolsFrom"})


def remote_skill_source_problem(source: Any, link: bool = False) -> str | None:
    """Why a remote web login may not ``skill.install`` ``source`` (``None``: it may).

    Only the registry (``registry:<id>``) and a known marketplace's plugin
    (``<marketplace>/<plugin>``) — never a link, a local path, a git URL, a
    ``github:`` or other scheme, or a shortcut (security review 6, H-1).
    """
    import os
    import re

    text = str(source or "").strip()
    if link:
        return "a link installs a local folder"
    if re.fullmatch(r"registry:[A-Za-z0-9][A-Za-z0-9._@/-]*", text):
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", text):
        # ``marketplace.install`` reads an existing folder of that name first.
        return "a local path" if os.path.exists(os.path.expanduser(text)) else None
    return "not a registry or marketplace source"


def judge_patch(
    kind: str,
    patch: dict[str, Any],
    *,
    project: bool,
    actor: str | None = None,
    authority: Iterable[Pattern] = (),
    marked: bool | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """``(refused, refused while Jarvis is on, Jarvis-relevant)`` dotted keys of ``patch``.

    For a local person everything is allowed (only the Jarvis-relevant list is
    filled, for the ledger callers may keep).  ``marked``: the call itself
    carried ``actor: "person"`` (not only a legacy browser's standing in for
    it); default: ``actor == "person"``.  Only a marked browser call writes
    :data:`BROWSER_PERSON_SECURITY_KEYS`.
    """
    if marked is None:
        marked = actor == ACTOR_PERSON
    authority = tuple(authority)
    refused: list[str] = []
    jarvis_on: list[str] = []
    relevant: list[str] = []
    allowed = table_for(kind, actor)
    for path, _value in leaves(patch):
        if not path:
            continue
        dotted = ".".join(path)
        if any(touches(path, pattern) for pattern in authority) or path[0] in (
            "gateway", "models", "providers", "mcp", "jarvis",
        ):
            relevant.append(dotted)
        if kind == "person":
            continue
        if kind == "browser" and marked and not project and any(
            matches(path, pattern) for pattern in BROWSER_PERSON_SECURITY_KEYS
        ):
            continue  # 1.58.0: the browser's Settings page, global scope
        local_only = (
            (*PERSON_LOCAL_ONLY, *PROJECT_PERSON_LOCAL_ONLY) if project else PERSON_LOCAL_ONLY
        )
        if any(touches(path, pattern) for pattern in local_only):
            refused.append(dotted)
            continue
        if project:
            # The project scope: a remote web login (a person) writes the keys of
            # PROJECT_REMOTE_LOGIN_KEYS; no browser host or connector writes it.
            if kind != "remote_login" or not any(
                matches(path, pattern) for pattern in PROJECT_REMOTE_LOGIN_KEYS
            ):
                refused.append(dotted)
            continue
        if not any(matches(path, pattern) for pattern in allowed):
            refused.append(dotted)
            continue
        if any(touches(path, pattern) for pattern in authority):
            jarvis_on.append(dotted)
    return refused, jarvis_on, relevant


def guard_call(
    conn: Any, actor: str | None, core: Any, *, method: str, key: Pattern | None = None
) -> None:
    """``provider.configure`` and ``mcp.add|update|remove``: the same rules as settings.

    A browser host's call needs ``actor: "person"`` (its Settings page); while
    Jarvis mode is on, no caller but a local person may change ``key`` when it is
    part of Jarvis's authority chain (its provider, a preset's MCP server).
    In-process callers (the agent's own tools) are not judged here.
    """
    from snowpea_core.server.person_gate import connection_kind

    kind = connection_kind(conn)
    if kind in ("person", "none"):
        return
    what = method + (f" {'.'.join(key[1:])}" if key else "")
    if kind in ("connector", "closed") or (
        kind == "remote_login" and method not in REMOTE_LOGIN_METHODS
    ):
        # Security review 4, H4 (also refused by the dispatcher: kind_method_refusal).
        ledger_settings(core, conn, [what], refused=True, method=method)
        refuse(conn, f"call {method}")
    if kind == "browser" and not acts_for_person(conn, actor):
        ledger_settings(core, conn, [what], refused=True, method=method)
        refuse(conn, f"call {method} outside its Settings page")
    if key is not None and any(touches(key, pattern) for pattern in jarvis_authority(core)):
        ledger_settings(core, conn, [what], refused=True, method=method)
        refuse(conn, f"change {what}", reason="jarvis_on")


# ---------------------------------------------------------------------------
# methods a browser connection may call (browser team's inventory, 2026-11-16)
# ---------------------------------------------------------------------------

#: Never from a browser connection, marker or not.
BROWSER_NEVER_PREFIXES: tuple[str, ...] = ("jarvis.", "mcp.preset.", "security.")
#: Of those, the calls that only take authority away from Jarvis (anyone may:
#: the delegation engine's own rule) and the preset catalogue, which only reads.
#: 1.58.0: the security reads too — the browser's Settings page shows the
#: master switch and the lists it may now change (``BROWSER_PERSON_SECURITY_KEYS``).
BROWSER_NEVER_EXEMPT: frozenset[str] = frozenset(
    {"jarvis.takeover", "jarvis.delegation.revoke", "mcp.preset.list",
     "security.get", "security.check", "security.presets.list"}
)
#: Config-writing methods a browser connection may call only for its person
#: (``actor: "person"``, or a legacy browser) — exactly what the browser sends
#: the marker on (browser team, 2026-11-16).
BROWSER_PERSON_METHODS: frozenset[str] = frozenset({
    "settings.set", "setup.applyDefaults",
    "provider.configure", "provider.loginWeb", "provider.remove", "provider.visionReset",
    "mcp.add", "mcp.update", "mcp.remove", "mcp.reload", "mcp.authorize", "mcp.deauthorize",
    "mcp.test",  # runs a draft server's command
    "skill.create", "skill.learn", "skill.reload", "skill.remove", "skill.write",
    "skill.install",  # but the browser's own bundled plugin: see bundled_plugin_problem
    "memory.update", "permission.allowlist.remove", "team.guide.set", "audio.install",
    "lsp.install", "system.reloadSettings",
    # Security review 7, Low 8: removing a team guide is the same class as
    # writing one (it was open to every browser call and to a remote login).
    "team.guide.delete",
    # 1.60.0 (capability hiddenModels): Settings › 모델's remove / show again.
    "provider.model.remove", "provider.model.unhide",
})
#: Of :data:`BROWSER_PERSON_METHODS`, the ones a legacy browser (< 0.3.0) may
#: not call even though its calls otherwise count as its person's: they came
#: after the marker, so only a browser that sends it calls them (1.60.0).
BROWSER_MARKED_ONLY_METHODS: frozenset[str] = frozenset(
    {"provider.model.remove", "provider.model.unhide"}
)
#: Never from a browser connection, any version, marker or not (2026-11-16: no
#: browser page or host calls them).  ``permission.allowlist.add``: a typed rule
#: pre-approves before routing.  ``gateway.bind`` (1.55.0): binding a messenger
#: chat to a session is the person's binding identity; the browser refuses it on
#: every page, and core now does too.
#: ``agent.create``, ``agent.delete`` (security review 6, the browser team's
#: source check): no browser calls them.  Of the calls re-decided with them,
#: ``checkpoint.restore``, ``session.deleteSaved`` and ``session.close`` stay
#: open to a browser for its own sessions (:data:`BROWSER_OWN_SESSION_METHODS`),
#: and ``fs.write`` (the host's saves into a remote session's workdir) only for
#: a session it hosts, never a control file (``fs_handlers._refuse_caller``).
#: ``agent.bindChannel`` (security review 7, M-1/Low 8): it routes a messenger
#: channel to a named agent, with a stored credential — a local person's, like
#: ``gateway.bind`` (the handler itself judged nobody).
BROWSER_NEVER_METHODS: frozenset[str] = frozenset({
    "permission.allowlist.add", "gateway.bind", "agent.create", "agent.delete",
    "agent.bindChannel",
})
#: What a browser connection may call without the marker (its own runtime and
#: the pages that send none).  ``job.*`` never touches a ``jarvis-task`` job
#: (every action on one is a person's: ``job_handlers._guard``); every
#: ``session.*``, ``goal.*`` and other unlisted method keeps its own rules.
BROWSER_HOST_METHODS: frozenset[str] = frozenset({
    "memory.delete", "gateway.sync", "mcp.authorize.complete",
    "site.put", "site.mark", "site.markPart", "site.delete",
    "job.schedule", "job.update", "job.runNow", "job.cancel", "job.delete",
    "workspace.add", "workspace.remove", "workspace.rename", "workspace.reorder",
    "command.run", "tool.register",
})
#: Answers: ``approval.respond`` that allows with a scope wider than ``once``
#: needs the marker (so does the reply to ``approval.request``,
#: ``ApprovalQueue._ask_origin``); a deny, a ``once`` and every
#: ``question.respond`` do not.
ANSWER_METHODS: frozenset[str] = frozenset({"approval.respond", "question.respond"})
#: Methods the browser may not call while Jarvis mode is on (its binding identity).
BROWSER_JARVIS_ON_METHODS: frozenset[str] = frozenset({"gateway.bind"})
#: Methods whose params carry the ``actor`` marker (the dispatcher takes it off
#: the params of a model that does not declare it).
ACTOR_METHODS: frozenset[str] = BROWSER_PERSON_METHODS | BROWSER_HOST_METHODS | ANSWER_METHODS


#: The browser's own plugin, the only thing an unmarked ``skill.install`` from a
#: browser connection may install (browser team, 2026-11-16).
BUNDLED_PLUGIN_NAME = "snowpea-browser"
BUNDLED_PLUGIN_DIR = "snowpea-plugin"
BUNDLED_PLUGIN_PARENT = "snowpea-browser-host"
#: ``SNOWPEA_BROWSER_DEV_PLUGIN=<abs path>``, read once when the daemon starts:
#: one more source a developer's browser may install unmarked.
DEV_PLUGIN_ENV = "SNOWPEA_BROWSER_DEV_PLUGIN"


def _dev_plugin_at_start() -> str | None:
    import os

    raw = os.environ.get(DEV_PLUGIN_ENV, "").strip()
    return os.path.realpath(raw) if raw and os.path.isabs(raw) else None


_DEV_PLUGIN: str | None = _dev_plugin_at_start()


def bundled_plugin_problem(source: Any) -> str | None:
    """Why ``source`` is not the browser's bundled plugin (``None`` when it is).

    All must hold, on the real path: its folder is named ``snowpea-plugin`` and
    sits in a folder named ``snowpea-browser-host`` (``/opt/…``, an AppImage
    mount, ``Snowpea.app/Contents/Resources/…``, a versioned Windows folder);
    it holds ``.claude-plugin/plugin.json`` as a regular file (not a symlink)
    whose ``name`` is exactly ``snowpea-browser``; and every manifest snowpea
    reads names that plugin (so the install creates or replaces only it).  The
    daemon's ``SNOWPEA_BROWSER_DEV_PLUGIN`` folder is the one exception.
    """
    import json
    import os
    import stat as stat_module
    from pathlib import Path

    text = str(source or "")
    if not text or "://" in text or not os.path.isabs(text):
        return "not a local absolute path"
    real = os.path.realpath(text)
    if _DEV_PLUGIN is not None and real == _DEV_PLUGIN:
        return None
    root = Path(real)
    if root.name != BUNDLED_PLUGIN_DIR or root.parent.name != BUNDLED_PLUGIN_PARENT:
        return f"not <…>/{BUNDLED_PLUGIN_PARENT}/{BUNDLED_PLUGIN_DIR}"
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        info = os.lstat(manifest)
    except OSError:
        return "no .claude-plugin/plugin.json"
    if not stat_module.S_ISREG(info.st_mode):
        return ".claude-plugin/plugin.json is not a regular file"
    try:
        from snowpea_core.skills.marketplace import MANIFEST_PATHS

        names = []
        for relative, _fmt in MANIFEST_PATHS:
            candidate = root / relative
            if candidate.exists() or candidate.is_symlink():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                names.append(data.get("name") if isinstance(data, dict) else None)
    except (OSError, ValueError, ImportError):
        return "the plugin manifest cannot be read"
    if not names or any(name != BUNDLED_PLUGIN_NAME for name in names):
        return f"the plugin is not named {BUNDLED_PLUGIN_NAME!r}"
    return None


def _host_may(method: str, params: dict[str, Any], core: Any) -> bool:
    """A browser connection's unmarked call that is allowed."""
    del core
    if method == "skill.install":
        return not params.get("link") and bundled_plugin_problem(params.get("source")) is None
    return method in BROWSER_HOST_METHODS


#: Config-writing methods judged by kind (security review 4, H4): every one a
#: browser needs its marker for, and the ones no browser may call at all.
CONFIG_METHODS: frozenset[str] = BROWSER_PERSON_METHODS | BROWSER_NEVER_METHODS
#: Of those, what a remote web login may call: the person, for running work —
#: the keys of :data:`REMOTE_LOGIN_KEYS` (providers, MCP servers, skills, memory,
#: language servers, audio, messengers), never security configuration
#: (jarvis.*, security.*, web.*, daemon, extensions, allowlist rules,
#: mcp.permissions, CLI paths, unrestricted mode, guarded grants).
REMOTE_LOGIN_METHODS: frozenset[str] = frozenset({
    "settings.set",  # judged key by key against REMOTE_LOGIN_KEYS by its handler
    # Security review 8, M-1: provider.loginWeb is a local person's (it stores
    # the account the owner's traffic and billing go to); provider.configure
    # with a credential is refused by its handler.
    "provider.configure", "provider.remove", "provider.visionReset",
    # 1.60.0: hiding a model is provider.remove's class (a person's pickers).
    "provider.model.remove", "provider.model.unhide",
    "mcp.add", "mcp.update", "mcp.remove", "mcp.reload", "mcp.authorize", "mcp.deauthorize",
    "mcp.test", "skill.create", "skill.learn", "skill.reload", "skill.remove", "skill.write",
    "skill.install", "memory.update", "audio.install", "lsp.install",
    "system.reloadSettings",
})
#: Of those, the ones whose handler narrows a remote login further (security
#: review 6, H-1/M-4): no program to run, no install from a path or a git URL,
#: no provider endpoint (``details.reason: "local_only"``).  ``gateway.bind``
#: is no longer a remote login's at all.
#: Security review 7: a skill only in the global scope and only one that widens
#: nothing (``server/skill_gate.py``, M-1); ``audio.transcribe`` of a path only
#: for a recording or an attachment (Info).
REMOTE_LOGIN_NARROWED: frozenset[str] = frozenset({
    "settings.set", "mcp.add", "mcp.update", "mcp.test", "skill.install",
    "provider.configure", "job.schedule",
    "skill.create", "skill.learn", "skill.write", "audio.transcribe",
})
#: What the remote connector (``snowpea remote up``, clientKind ``connector``)
#: calls on its own connection: ``system.hello`` only (it hands the daemon's
#: token to the browser, which connects as itself).  No config method.
CONNECTOR_METHODS: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# deny by default (security review 5, M1): every method a non-local-person
# connection kind may call is listed here; a method in no list is refused.
# ``tests/test_security_review5_methods.py`` holds the verdict of every
# registered method for every kind, so a new method fails until it is placed.
# ---------------------------------------------------------------------------

#: A connector (``snowpea remote up``, and any client kind snowpea does not
#: know): the handshake, liveness, shutting the daemon down (installers and
#: launchers use it) and the reads and streams its watchers use — nothing that
#: starts, changes or configures anything.  ``jarvis.takeover`` and
#: ``jarvis.delegation.revoke`` only take authority away.
CONNECTOR_ALLOWED: frozenset[str] = frozenset({
    "system.hello", "system.info", "system.health", "system.shutdown",
    "session.list", "session.resume", "session.events", "session.trajectory",
    "job.list", "job.preview", "job.runs", "job.nextRunForClient",
    "external.clis", "external.list", "external.get", "external.read", "external.unwatch",
    "mcp.preset.list", "jarvis.takeover", "jarvis.delegation.revoke",
})

#: A remote web login: a person operating sessions from another machine, not
#: re-authenticated.  Sessions, answers, files, Jarvis's day-to-day, the
#: config methods of :data:`REMOTE_LOGIN_METHODS`; never what widens what runs
#: unattended or reaches the daemon itself (extensions, updates, restarts,
#: shutdown, project trust, the remote door, allowlist rules), never a browser
#: host's runtime (host tools, site memory, its LLM and search helpers).
REMOTE_LOGIN_ALLOWED: frozenset[str] = REMOTE_LOGIN_METHODS | frozenset({
    "system.hello", "system.info", "system.health", "system.checkUpdate",
    # sessions
    "session.create", "session.prompt", "session.steer", "session.interrupt", "session.close",
    "session.list", "session.resume", "session.events", "session.trajectory", "session.rename",
    "session.setMode", "session.setModel", "session.setEffort", "session.setAgent",
    "session.setActive", "session.pin", "session.archive", "session.deleteSaved",
    "session.delete",
    "session.fork", "session.lineage", "session.compact", "session.continue",
    "session.notice", "session.artifacts", "session.attachments",
    "goal.set", "goal.get", "goal.pause", "goal.resume", "goal.clear",
    "checkpoint.list", "checkpoint.diff", "checkpoint.restore", "checkpoint.delete",
    "command.list", "command.run", "tool.list", "file.complete", "usage.summary",
    # answers
    "approval.list", "approval.respond", "question.list", "question.respond",
    # files
    "fs.list", "fs.glob", "fs.stat", "fs.read", "fs.hash", "fs.write",
    "workspace.list", "workspace.add", "workspace.remove", "workspace.rename",
    "workspace.reorder",
    # agents, teams, jobs.  Security review 7, M-1/Low 8: never ``agent.create``
    # (it writes a project's agent definition, a control file, and a named
    # agent runs unattended), ``agent.bindChannel`` (a messenger binding) or
    # ``team.guide.delete`` (the class of ``team.guide.set``, which was never here).
    "agent.list", "agent.delete", "agent.spawn",
    "team.start", "team.status", "team.guide.list", "team.guide.get",
    "job.schedule", "job.list", "job.cancel", "job.delete", "job.runNow", "job.update",
    "job.preview", "job.runs", "job.nextRunForClient",
    # reads of config and the services a person uses
    "settings.get", "setup.catalog", "setup.status", "provider.list", "provider.models",
    "provider.test", "mcp.list", "mcp.catalog", "skill.list",
    "skill.read", "skill.search", "memory.list", "memory.search", "memory.write",
    "memory.delete", "memory.confirm", "lsp.status", "lsp.catalog", "lsp.restart",
    "audio.capabilities", "audio.voices", "audio.speak", "audio.transcribe",
    "audio.record.start", "audio.record.stop", "gateway.list", "gateway.unbind",
    "gateway.sync", "permission.allowlist.list",
    "security.get", "security.check", "security.presets.list", "project.trust.status",
    # 개선하기 (1.59.0): a remote web login is a person (person_gate.PERSON_METHODS).
    "feedback.preview", "feedback.send",
    # external CLIs (the external manager's own trust and ownership rules apply)
    "external.clis", "external.list", "external.get", "external.read", "external.open",
    "external.send", "external.setMode", "external.interrupt", "external.create",
    "external.close", "external.unwatch", "external.terminalSnapshot", "external.setModel",
    # Jarvis (PERSON_METHODS keeps jarvis.enable a local person's)
    "jarvis.status", "jarvis.overview", "jarvis.disable", "jarvis.project.list",
    "jarvis.project.rename", "jarvis.project.alias", "jarvis.escalations.list",
    "jarvis.escalations.dismiss", "jarvis.ledger", "jarvis.delegation.list",
    "jarvis.delegation.revoke", "jarvis.takeover", "jarvis.handback",
    # 1.56.0: the per-session Jarvis switch (a person's; grants nothing by itself).
    "jarvis.session.set",
    # jarvis.briefing.set: only {enabled: false} (jarvis_assist_handlers).
    "jarvis.briefing.get", "jarvis.briefing.set", "mcp.preset.list", "mcp.preset.check",
})

#: Security review 8: what a remote web login may no longer call at all — a
#: person's at a local connection (``details.reason: "local_only"``,
#: ``details.what``: the method).  ``provider.loginWeb`` and
#: ``mcp.authorize.complete`` store the account an owner's provider or MCP
#: server uses (M-1); ``jarvis.delegation.set`` creates or widens a grant that
#: persists (H8-2) — revoking, taking over and handing back stay open.
REMOTE_LOGIN_LOCAL_ONLY: frozenset[str] = frozenset({
    "provider.loginWeb", "mcp.authorize.complete", "jarvis.delegation.set",
})
#: 1.56.0: ``jarvis.delegation.set`` is let through to its handler for a remote
#: web login while a local person has ``jarvis.delegation.remoteGrant`` on (the
#: handler and the engine then require the remote password again and keep the
#: grant to one session or project, no guarded classes, at most 24 h:
#: ``jarvis/remote_grant.py``).  Off, it is refused as before, and the message
#: says where a local person turns it on.
REMOTE_GRANT_METHOD = "jarvis.delegation.set"

#: What a browser connection may call beyond :data:`BROWSER_PERSON_METHODS`
#: (marker), :data:`BROWSER_HOST_METHODS`, :data:`ANSWER_METHODS` and the
#: :data:`BROWSER_NEVER_EXEMPT` calls: its own sessions and runtime.  Never
#: extensions, updates, restarts or anything person-only (project trust: only
#: by :data:`BROWSER_TRUST_METHODS`, 1.62.0).
BROWSER_OPEN_METHODS: frozenset[str] = frozenset({
    "system.hello", "system.info", "system.health", "system.shutdown", "system.checkUpdate",
    "session.create", "session.prompt", "session.steer", "session.interrupt", "session.close",
    "session.list", "session.resume", "session.events", "session.trajectory", "session.rename",
    "session.setMode", "session.setModel", "session.setEffort", "session.setAgent",
    "session.setActive", "session.pin", "session.archive", "session.deleteSaved",
    "session.delete",
    "session.fork", "session.lineage", "session.compact", "session.continue",
    "session.notice", "session.artifacts", "session.attachments", "session.attach",
    "session.setBrowserProvider", "session.toolContent",
    "goal.set", "goal.get", "goal.pause", "goal.resume", "goal.clear",
    "checkpoint.list", "checkpoint.diff", "checkpoint.restore", "checkpoint.delete",
    "command.list", "tool.list", "tool.unregister", "tool.progress", "turn.outcome",
    "file.complete", "usage.summary", "approval.list", "question.list", "approval.ask",
    "approval.evaluate",
    "fs.list", "fs.glob", "fs.stat", "fs.read", "fs.hash", "fs.write", "workspace.list",
    "agent.list", "agent.spawn",
    "team.start", "team.status", "team.guide.list", "team.guide.get",
    "job.list", "job.preview", "job.runs", "job.nextRunForClient",
    "settings.get", "setup.catalog", "setup.status", "provider.list", "provider.models",
    "provider.test", "mcp.list", "mcp.catalog", "skill.list", "skill.read", "skill.search",
    "memory.list", "memory.search", "memory.write", "memory.confirm", "memory.ingest",
    "lsp.status", "lsp.catalog", "lsp.restart", "audio.capabilities", "audio.voices",
    "audio.speak", "audio.transcribe", "audio.record.start", "audio.record.stop",
    "gateway.list", "gateway.unbind", "permission.allowlist.list", "llm.sample", "web.search",
    "site.get", "site.list", "site.stats", "site.playbook.get", "site.playbook.put",
    "site.playbook.edit", "site.playbook.delete", "site.playbook.mark",
    "external.clis", "external.list", "external.get", "external.read", "external.open",
    "external.send", "external.setMode", "external.interrupt", "external.create",
    "external.close", "external.unwatch", "external.setModel",
})


def browser_allowed(method: str) -> bool:
    """``method`` is in one of the browser tables (else a browser may not call it)."""
    return (
        method in BROWSER_OPEN_METHODS
        or method in BROWSER_PERSON_METHODS
        or method in BROWSER_HOST_METHODS
        or method in ANSWER_METHODS
        or method in BROWSER_NEVER_EXEMPT
        or method in BROWSER_TRUST_METHODS
    )


def kind_method_refusal(method: str, params: dict[str, Any], conn: Any, core: Any) -> None:
    """The dispatcher's deny-by-default rule for every kind but a local person.

    Security review 4, H4 / review 5, M1: a connector may call only
    :data:`CONNECTOR_ALLOWED`, a remote web login only
    :data:`REMOTE_LOGIN_ALLOWED`, a browser only what its tables list
    (:func:`browser_allowed`; the marker rules are
    :func:`browser_method_refusal`'s).  A local person is not judged here.
    """
    from snowpea_core.server.person_gate import connection_kind

    del params
    kind = connection_kind(conn)
    if kind == "connector":
        allowed = method in CONNECTOR_ALLOWED
    elif kind == "remote_login":
        if method in REMOTE_LOGIN_LOCAL_ONLY:
            if method == REMOTE_GRANT_METHOD:
                from snowpea_core.jarvis import remote_grant

                if remote_grant.enabled_for(core):
                    # 1.56.0: the local person turned remote grants on; the
                    # handler and the engine judge the password and the limits.
                    return
                ledger_settings(core, conn, [f"{method} ({method})"], refused=True,
                                method=method)
                refuse(conn, f"call {method}", reason=LOCAL_ONLY, extra={"what": method},
                       hint=remote_grant.OFF_HINT)
            refuse_local_only(conn, core, f"call {method}", method=method, detail=method)
        allowed = method in REMOTE_LOGIN_ALLOWED
    elif kind == "browser":
        allowed = browser_allowed(method)
    elif kind == "closed":
        allowed = False  # nobody: never an in-process caller (security review 7, H-1)
    else:
        return
    if allowed:
        return
    if method in CONFIG_METHODS:
        ledger_settings(core, conn, [method], refused=True, method=method)
    refuse(conn, f"call {method}")


def browser_method_refusal(method: str, params: dict[str, Any], conn: Any, core: Any) -> None:
    """The dispatcher's browser rule: refuse what this browser call may not do.

    Only ``clientKind: "browser"`` connections are judged; settings keys and
    provider/MCP targets are judged again by their handlers (the tables above).
    """
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "browser":
        return
    actor = params.get("actor")
    person = acts_for_person(conn, actor if isinstance(actor, str) else None)
    if (method.startswith(BROWSER_NEVER_PREFIXES) and method not in BROWSER_NEVER_EXEMPT) or (
        method.startswith("permission.allowlist.")
        and method not in ("permission.allowlist.remove", "permission.allowlist.list")
    ) or method in BROWSER_NEVER_METHODS:
        if not method.startswith("jarvis."):  # jarvis.* keeps its own ledger rules
            ledger_settings(core, conn, [method], refused=True, method=method)
        refuse(conn, f"call {method}")
    if method == "approval.respond":
        scope = params.get("scope") or "once"
        if params.get("decision") != "deny" and scope != "once" and not person:
            refuse(conn, f"allow with scope {scope!r} outside a person's click")
        return
    if method in ANSWER_METHODS:
        return
    if method in BROWSER_PERSON_METHODS and not person and not _host_may(method, params, core):
        ledger_settings(core, conn, [method], refused=True, method=method)
        if method == "skill.install":
            why = ("a link is never the bundled plugin" if params.get("link")
                   else bundled_plugin_problem(params.get("source")))
            refuse(conn, f"call {method} outside its Settings page", reason="not_bundled_plugin",
                   extra={"why": why})
        refuse(conn, f"call {method} outside its Settings page")
    if method in BROWSER_MARKED_ONLY_METHODS and (actor != ACTOR_PERSON or legacy_browser(conn)):
        ledger_settings(core, conn, [method], refused=True, method=method)
        refuse(conn, f"call {method} without actor 'person' from a browser 0.3.0 or later")
    authority = jarvis_authority(core)
    if not authority:
        return
    key: Pattern | None = None
    if method.startswith("provider.") and isinstance(params.get("vendor"), str):
        key = ("providers", params["vendor"])
    elif method.startswith("mcp.") and isinstance(params.get("name"), str):
        key = ("mcp", "servers", params["name"])
    patch = params.get("patch")
    read_tag = params.get("permission") == "read" or (
        isinstance(patch, dict) and patch.get("permission") == "read"
    )
    if (
        method in BROWSER_JARVIS_ON_METHODS
        or (method in ("mcp.add", "mcp.update") and read_tag)
        or (key is not None and any(touches(key, pattern) for pattern in authority))
    ):
        ledger_settings(core, conn, [method], refused=True, method=method)
        refuse(conn, f"call {method}", reason="jarvis_on")


def command_refusal(conn: Any, core: Any, name: str, args: str, *, source: str = "builtin",
                    actor: str | None = None) -> None:
    """``command.run`` from a browser: judged by what the command would persist
    (``commands/persists.py``), before it runs, with the RPC tables' rules."""
    from snowpea_core.commands.persists import persists
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "browser":
        return
    what = persists(name, args, source)
    if what.level == "local":
        # Security review 6, M-2: a local person's only, marker or not.
        ledger_settings(core, conn, [f"/{name}"], refused=True, method="command.run")
        refuse(conn, f"run /{name}", reason=LOCAL_ONLY, extra={"what": "command"})
    if what.level == "person" and not acts_for_person(conn, actor):
        ledger_settings(core, conn, [f"/{name}"], refused=True, method="command.run")
        refuse(conn, f"run /{name} outside its Settings page")
    authority = jarvis_authority(core)
    if authority and (what.jarvis_on or (
        what.key is not None and any(touches(what.key, pattern) for pattern in authority)
    )):
        ledger_settings(core, conn, [f"/{name}"], refused=True, method="command.run")
        refuse(conn, f"run /{name}", reason="jarvis_on")


# ---------------------------------------------------------------------------
# who may see and answer which approval or question (security review 3, browser team)
# ---------------------------------------------------------------------------

#: Filter ``approval.list`` / ``question.list`` and the request events for
#: browser and connector connections (the browser team's consumer list is in:
#: on).  Jarvis-routed requests are hidden from browsers whatever this says.
FILTER_ANSWER_LISTS = True

_WATCHERS = ("browser", "connector")


def _kind(conn: Any) -> str:
    from snowpea_core.server.person_gate import connection_kind

    return connection_kind(conn)


def _host_of(session: Any, host: Any = None) -> str | None:
    value = host or getattr(session, "host_tools_from", None) or getattr(session, "owned_by", None)
    return str(value) if value else None


#: Browsers from this ``clientVersion`` on confirm a person-only answer in their own
#: native window and say so (``personConfirm: "window"``, 1.61.0).  Browsers
#: 0.3.0 send ``actor: "person"`` on an ordinary page click, so the marker alone
#: never counts for a person-only approval.
BROWSER_PERSON_CONFIRM_MIN: tuple[int, int, int] = (0, 4, 0)
#: The one value of ``personConfirm``.
PERSON_CONFIRM_WINDOW = "window"

#: ``details.reason`` (and ``approval.pending {refused}.reason``) when a browser's
#: answer to a person-only approval does not count (1.61.0, ``browserPersonApprovals``).
BROWSER_NOT_ALLOWED = "browser_not_allowed"  # not a request a browser answers (below)
BROWSER_REMOTE = "browser_remote"            # a remote web login or a remote peer
BROWSER_LEGACY = "browser_legacy"            # clientVersion before 0.4.0
BROWSER_UNCONFIRMED = "browser_unconfirmed"  # no actor "person" + personConfirm "window"
BROWSER_NOT_OWNER = "browser_not_owner"      # not its session (nor an ancestor's)
#: Person-only approvals no browser answers whatever it sends: Jarvis's authority
#: (``delegation_set``) and taking over a terminal (``tmux.adopt``); with them
#: every external CLI's request, every Jarvis-routed one, and every person-only
#: request that is not a security ask (a mode raise, a schedule) — see
#: :func:`_browser_takes`.
BROWSER_NEVER_TOOLS: tuple[str, ...] = ("delegation_set", "tmux.adopt")


def _field(request: Any, name: str) -> Any:
    return request.get(name) if isinstance(request, dict) else getattr(request, name, None)


#: The tools whose security ask a browser may answer (security review L1: an
#: allow-list): a shell line, code, a file read, write, edit or patch, and the
#: host's own page tools (``repl.*``).  Everything else — a schedule, a mode
#: raise, a delegation, a terminal, an external CLI, Jarvis — is not a browser's.
BROWSER_TAKES_TOOLS: frozenset[str] = frozenset({
    "shell", "bash", "exec", "exec_command", "execute_code",
    "read_file", "write_file", "edit_file", "create_file", "edit", "multiedit", "write",
    "patch", "apply_patch", "str_replace", "str_replace_editor",
})


def _browser_takes(request: Any) -> bool:
    """A person-only approval a browser may answer at all (1.61.0): a security ask
    (a blocked call put to the person, ``blocked``, or one the check could not
    see, ``reason``) about one of :data:`BROWSER_TAKES_TOOLS` or a host page
    tool (``repl.*``) — never Jarvis's authority, a terminal, an external CLI's
    or a Jarvis-routed request."""
    if request is None:
        return False
    tool = str(_field(request, "tool") or "").lower()
    if tool in BROWSER_NEVER_TOOLS or not (tool in BROWSER_TAKES_TOOLS
                                           or tool.startswith("repl.")):
        return False
    return bool(_field(request, "blocked") or _field(request, "reason")) and not (
        _field(request, "external") or _field(request, "externalTerminal")
        or _field(request, "route") == "jarvis")


def _loopback(peer: Any) -> bool:
    import ipaddress

    text = str(peer or "").strip()
    if text in ("", "local", "localhost"):
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def browser_remote(conn: Any) -> bool:
    """The browser reaches this core from elsewhere: through the remote listener
    (``remote_login``) or from a peer address that is not this machine's."""
    return bool(getattr(conn, "remote_login", False)) or not _loopback(
        getattr(conn, "peer", None))


def browser_confirms(conn: Any) -> bool:
    """The browser's version confirms person-only answers in a native window."""
    version = _version(getattr(conn, "client_version", None))
    return version is not None and version >= BROWSER_PERSON_CONFIRM_MIN


def browser_owns(conn: Any, session: Any, *, owner: Any = None, lookup: Any = None) -> bool:
    """``conn`` (a browser) owns, hosts or opened ``session``, or is the surface the
    person is prompting from; a subagent's session counts by its parent chain.
    A request owned by another browser (``owner``) is never its."""
    client_id = getattr(conn, "client_id", None)
    owners = [str(o) for o in (owner if isinstance(owner, tuple) else (owner,)) if o]
    if any(o != client_id for o in owners):
        return False
    current, seen = session, 0
    while current is not None and seen < 16:
        # m31 (owner, 2026-11-19): a browser that attached to or sent in any
        # session (a TUI, IDE, CLI or desktop one too) owns it, as before 0.2.97.
        ids = {getattr(current, name, None) for name in
               ("host_tools_from", "owned_by", "origin_client_id")} - {None, BROWSER_ALIAS}
        conns = ("origin_conn", "asker_conn")
        if (client_id and client_id in ids) or any(
                conn is getattr(current, name, None) for name in conns):
            return True
        parent = getattr(current, "parent_session_id", None)
        current = lookup(parent) if parent and callable(lookup) else None
        seen += 1
    return False


def browser_person_refusal(conn: Any, session: Any, request: Any, actor: str | None,
                           person_confirm: str | None = None, *, owner: Any = None,
                           lookup: Any = None, asker: bool = False) -> str | None:
    """Why a browser's allow of the person-only approval ``request`` is not its
    person's (a ``details.reason`` above), or ``None`` when it is (1.61.0).

    All must hold: a security ask a browser may answer (:func:`_browser_takes`);
    a local browser (:func:`browser_remote`); ``clientVersion`` 0.4.0 or later;
    ``actor: "person"`` with ``personConfirm: "window"`` (the browser's own
    native confirmation); its own session, a parent's, or the asker
    (:func:`browser_owns`; ``asker``: the core asked this very connection).
    """
    if not _browser_takes(request):
        return BROWSER_NOT_ALLOWED
    if browser_remote(conn):
        return BROWSER_REMOTE
    if not browser_confirms(conn):
        return BROWSER_LEGACY
    if actor != ACTOR_PERSON or person_confirm != PERSON_CONFIRM_WINDOW:
        return BROWSER_UNCONFIRMED
    if not (asker or browser_owns(conn, session, owner=owner, lookup=lookup)):
        return BROWSER_NOT_OWNER
    return None


#: What a browser connection may call for project trust (1.62.0,
#: ``browserProjectTrust``): the dispatcher lets them through, the handler judges
#: the call by :func:`browser_trust_refusal` (the status read: local and its own
#: session's folder only).
BROWSER_TRUST_METHODS: frozenset[str] = frozenset(
    {"project.trust.status", "project.trust.set", "project.trust.revoke"})


def browser_trust_scopes() -> tuple[str, ...]:
    """The scopes a browser may grant or take back: the default ones, never
    ``mode`` (how sessions run) or ``externalCli`` (starting external CLIs)."""
    from snowpea_core.trust.projects import DEFAULT_SCOPES

    return DEFAULT_SCOPES


def browser_trust_refusal(conn: Any, core: Any, workdir: Any, actor: str | None,
                          person_confirm: str | None, scopes: Any, *,
                          read: bool = False) -> str | None:
    """Why a browser's ``project.trust.set`` / ``.revoke`` is not its person's (a
    ``details.reason`` of :func:`browser_person_refusal`), or ``None`` (1.62.0).

    All must hold: only :func:`browser_trust_scopes` (``scopes`` ``None`` is the
    caller's default, which for a browser is those); a local browser; version
    0.4.0+; ``actor: "person"`` with ``personConfirm: "window"``; ``workdir`` is
    the real workdir of a live session this browser owns, hosts or opened.
    ``read`` (``project.trust.status``): only local and its own session's folder.
    A :class:`~pathlib.Path` ``workdir`` is taken as already resolved (the
    handler resolves it once and changes the store under that same root).
    """
    import os
    from pathlib import Path

    allowed = set(browser_trust_scopes())
    if not read and scopes is not None and any(str(s) not in allowed for s in scopes):
        return BROWSER_NOT_ALLOWED
    if browser_remote(conn):
        return BROWSER_REMOTE
    if not read and not browser_confirms(conn):
        return BROWSER_LEGACY
    if not read and (actor != ACTOR_PERSON or person_confirm != PERSON_CONFIRM_WINDOW):
        return BROWSER_UNCONFIRMED
    try:
        wanted = str(workdir) if isinstance(workdir, Path) else os.path.realpath(
            os.path.expanduser(str(workdir)))
    except (TypeError, ValueError):
        return BROWSER_NOT_OWNER
    sessions = getattr(core, "sessions", None)
    live = list(sessions.all()) if sessions is not None and hasattr(sessions, "all") else []
    lookup = getattr(sessions, "get", None)
    for session in live:
        where = getattr(session, "workdir", None)
        if where is None or os.path.realpath(str(where)) != wanted:
            continue
        if browser_owns(conn, session, lookup=lookup):
            return None
    return BROWSER_NOT_OWNER


def list_visible(conn: Any, session: Any, *, person_only: bool = False, host: Any = None,
                 route: str | None = None, request: Any = None, lookup: Any = None) -> bool:
    """A browser or connector sees requests of the sessions it hosts and of
    unhosted ones (IDE, CLI, scheduler); never another browser client's, and a
    browser never a Jarvis-routed one.  A person-only one only a local browser
    0.4.0+ that owns the session sees (1.61.0, given the ``request``), and only
    one it may answer.  A legacy browser (< 0.3.0) sees what it saw before, but
    Jarvis's routing."""
    kind = _kind(conn)
    if kind == "closed":
        return False
    if kind not in _WATCHERS:
        return True
    if kind == "browser" and route == "jarvis":
        return False
    if kind == "browser" and legacy_browser(conn):
        return True
    if not FILTER_ANSWER_LISTS:
        return True
    if person_only:
        return kind == "browser" and _browser_takes(request) and not browser_remote(
            conn) and browser_confirms(conn) and browser_owns(conn, session, owner=host,
                                                              lookup=lookup)
    owner = _host_of(session, host)
    return owner is None or owner == getattr(conn, "client_id", None)


def may_answer(conn: Any, session: Any, *, origin_conn: Any = None, owner: Any = None,
               person_only: bool = False, actor: str | None = None,
               route: str | None = None, request: Any = None, lookup: Any = None,
               person_confirm: str | None = None) -> bool:
    """Browser (0.3.0+) and connector answers: its own hosted session's requests
    (or one delivered to it) with or without the marker; an unhosted session's
    only with ``actor: "person"`` (a connector never); another browser client's
    never; a person-only one only by :func:`browser_person_refusal` (1.61.0;
    never a connector); a Jarvis-routed one not until it is the person's.  A
    legacy browser answers as before (a person-only one never)."""
    kind = _kind(conn)
    if kind == "closed":
        return False
    if kind not in _WATCHERS:
        return True
    if person_only and kind == "browser" and route != "jarvis":
        return browser_person_refusal(conn, session, request, actor, person_confirm,
                                      owner=owner, lookup=lookup) is None
    # Security review 4, M5: a person-only or Jarvis-routed request is never a
    # connector's, nor a Jarvis-routed one a browser's.
    if person_only or route == "jarvis":
        return False
    if kind == "browser" and legacy_browser(conn):
        return True
    client_id = getattr(conn, "client_id", None)
    owners = owner if isinstance(owner, tuple) else (owner,)
    if origin_conn is not None and origin_conn is conn and kind == "browser":
        return True  # (a connector never answers, origin or not: review 6, L-1)
    host = next((str(o) for o in owners if o), None) or _host_of(session)
    if host is not None:
        return bool(client_id) and host == client_id
    return kind == "browser" and actor == ACTOR_PERSON


def answer_refusal(conn: Any, session: Any, request_id: str, what: str, *,
                   origin_conn: Any = None, owner: Any = None, person_only: bool = False,
                   actor: str | None = None, route: str | None = None,
                   request: Any = None, lookup: Any = None,
                   person_confirm: str | None = None) -> None:
    """Raise when ``conn`` may not answer: ``jarvis_person_only`` for a person-only
    request or an unhosted session's request without the marker, ``not_found`` for
    another browser's session or a Jarvis-routed request (nothing is revealed).
    A browser refused a person-only approval gets ``details.reason`` from
    :func:`browser_person_refusal` (1.61.0)."""
    if may_answer(conn, session, origin_conn=origin_conn, owner=owner,
                  person_only=person_only, actor=actor, route=route, request=request,
                  lookup=lookup, person_confirm=person_confirm):
        return
    if person_only and request is not None and _kind(conn) == "browser" and route != "jarvis":
        why = browser_person_refusal(conn, session, request, actor, person_confirm,
                                     owner=owner, lookup=lookup) or BROWSER_NOT_ALLOWED
        refuse(conn, f"answer {what} {request_id}", reason=why)
    hosted_elsewhere = route == "jarvis" or (
        (next((o for o in (owner if isinstance(owner, tuple) else (owner,)) if o), None)
         or _host_of(session)) not in (None, getattr(conn, "client_id", None))
    )
    if person_only or not hosted_elsewhere:
        refuse(conn, f"answer {what} {request_id}")
    raise RpcError(errors.NOT_FOUND, f"no pending {what} {request_id}")


def request_event_for(conn: Any, method: str, params: dict[str, Any], sessions: Any
                      ) -> tuple[str, dict[str, Any]] | None:
    """What a browser or connector gets of one request event (``None``: nothing).

    A browser never gets ``*.route.changed``: a request routed to Jarvis becomes
    ``*.resolved {by: "rerouted", outcome: "withdrawn"}`` (``decision`` null for
    an approval), one routed back to the person ``*.pending`` with the request,
    as if new.  ``*.pending`` of a Jarvis-routed request is not sent.
    """
    kind = _kind(conn)
    public = {k: v for k, v in params.items() if k != "_request"}
    if kind not in _WATCHERS:
        return method, public
    prefix = method.split(".", 1)[0]
    request = params.get("request") if isinstance(params.get("request"), dict) else None
    if method.endswith(".route.changed"):
        if kind != "browser":
            return method, public
        request = params.get("_request") if isinstance(params.get("_request"), dict) else None
        if request is None:
            return None
        lookup = getattr(sessions, "get", None)
        session = lookup(str(request.get("sessionId"))) if callable(lookup) else None
        host = params.get("hostToolsFrom")
        if not list_visible(conn, session, person_only=bool(request.get("personOnly")),
                            host=host, route=None,
                            request=request if prefix == "approval" else None,
                            lookup=lookup):
            return None
        if params.get("route") == "jarvis":
            body: dict[str, Any] = {"requestId": params.get("requestId"), "by": "rerouted",
                                    "outcome": "withdrawn"}
            if prefix == "approval":
                body["decision"] = None
            return f"{prefix}.resolved", body
        pending: dict[str, Any] = {"request": request}
        if prefix == "approval":
            pending["hostToolsFrom"] = host
        return f"{prefix}.pending", pending
    if request is None:
        # A resolution by id alone carries nothing of the request — but who
        # answered: a messenger's bound user id stays with the person's own
        # surfaces (security review 4, Low).
        if method.endswith(".resolved") and str(public.get("by") or "").startswith("gateway:"):
            public = {**public, "by": "messenger"}
        return method, public
    lookup = getattr(sessions, "get", None)
    session = lookup(str(request.get("sessionId"))) if callable(lookup) else None
    if not list_visible(conn, session, person_only=bool(request.get("personOnly")),
                        host=params.get("hostToolsFrom") or request.get("hostToolsFrom"),
                        route=request.get("route"),
                        request=request if prefix == "approval" else None, lookup=lookup):
        return None
    return method, public


def ledger_settings(core: Any, conn: Any, keys: list[str], *, refused: bool, method: str) -> None:
    """One ledger line for a refused or Jarvis-relevant change from a non-local person."""
    if not keys or core is None:
        return
    try:
        from snowpea_core.jarvis.delegation import engine_for
        from snowpea_core.server.person_gate import connection_kind

        kind = connection_kind(conn)
        by = str(getattr(conn, "client_kind", None) or kind)
        shown = ", ".join(keys[:8]) + (" …" if len(keys) > 8 else "")
        verb = "refused" if refused else "changed"
        engine_for(core).ledger.record_decision(
            kind="escalate",
            decided_by="person",
            summary=f"{method} from {by} ({kind}) {verb}: {shown}"[:300],
            reason="settings",
            importance="normal" if refused else "low",
        )
    except Exception:  # noqa: BLE001 - the decision itself stands
        log.warning("could not ledger a settings change", exc_info=True)


__all__ = [
    "BROWSER_HOST_KEYS",
    "BROWSER_PERSON_KEYS",
    "CONNECTOR_KEYS",
    "PERSON_LOCAL_ONLY",
    "PROJECT_PERSON_LOCAL_ONLY",
    "REMOTE_LOGIN_KEYS",
    "jarvis_authority",
    "judge_patch",
    "ledger_settings",
    "refuse",
]
