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
)
#: The same for the project scope (every other project key is refused to
#: non-local callers too: no client but a local one writes the project scope).
PROJECT_PERSON_LOCAL_ONLY: tuple[Pattern, ...] = (("jarvis",), ("allowlist",))

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
           extra: dict[str, Any] | None = None) -> None:
    """``jarvis_person_only`` with ``details.reason`` (``person_only``, ``jarvis_on``,
    ``not_bundled_plugin``, …) and ``extra`` details."""
    from snowpea_core.server.person_gate import connection_kind

    kind = connection_kind(conn)
    if reason == "jarvis_on":
        message = (
            f"Jarvis mode is on: {what} defines what Jarvis may do; change it in the "
            "snowpea app or the TUI"
        )
    elif kind == "remote_login":
        message = f"{what} needs a local connection (IDE, TUI or the local web page)"
    elif kind == "none":
        message = f"only a person may {what}"
    else:
        message = f"a {getattr(conn, 'client_kind', None) or kind} connection cannot {what}"
    raise RpcError(errors.JARVIS_PERSON_ONLY, message,
                   {"reason": reason, "kind": kind, **(extra or {})})


def judge_patch(
    kind: str,
    patch: dict[str, Any],
    *,
    project: bool,
    actor: str | None = None,
    authority: Iterable[Pattern] = (),
) -> tuple[list[str], list[str], list[str]]:
    """``(refused, refused while Jarvis is on, Jarvis-relevant)`` dotted keys of ``patch``.

    For a local person everything is allowed (only the Jarvis-relevant list is
    filled, for the ledger callers may keep).
    """
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
        local_only = (
            (*PERSON_LOCAL_ONLY, *PROJECT_PERSON_LOCAL_ONLY) if project else PERSON_LOCAL_ONLY
        )
        if any(touches(path, pattern) for pattern in local_only):
            refused.append(dotted)
            continue
        if project:
            # The project scope: a remote web login (a person) keeps the rest of it;
            # no browser host or connector writes it.
            if kind != "remote_login":
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
    if kind == "connector" or (kind == "remote_login" and method not in REMOTE_LOGIN_METHODS):
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
BROWSER_NEVER_EXEMPT: frozenset[str] = frozenset(
    {"jarvis.takeover", "jarvis.delegation.revoke", "mcp.preset.list"}
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
    "gateway.bind", "lsp.install", "system.reloadSettings",
})
#: Never from a browser connection, any version, marker or not (2026-11-16: no
#: browser page or host calls it; a typed rule pre-approves before routing).
BROWSER_NEVER_METHODS: frozenset[str] = frozenset({"permission.allowlist.add"})
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
    "provider.configure", "provider.loginWeb", "provider.remove", "provider.visionReset",
    "mcp.add", "mcp.update", "mcp.remove", "mcp.reload", "mcp.authorize", "mcp.deauthorize",
    "mcp.test", "skill.create", "skill.learn", "skill.reload", "skill.remove", "skill.write",
    "skill.install", "memory.update", "audio.install", "gateway.bind", "lsp.install",
    "system.reloadSettings",
})
#: What the remote connector (``snowpea remote up``, clientKind ``connector``)
#: calls on its own connection: ``system.hello`` only (it hands the daemon's
#: token to the browser, which connects as itself).  No config method.
CONNECTOR_METHODS: frozenset[str] = frozenset()


def kind_method_refusal(method: str, params: dict[str, Any], conn: Any, core: Any) -> None:
    """The dispatcher's rule for connectors and remote web logins (security review 4, H4).

    A connector may call no config-writing method; a remote web login only
    :data:`REMOTE_LOGIN_METHODS`.  A local person and a browser (judged by
    :func:`browser_method_refusal`) are not judged here.
    """
    from snowpea_core.server.person_gate import connection_kind

    if method not in CONFIG_METHODS:
        return
    kind = connection_kind(conn)
    if kind == "connector" and method not in CONNECTOR_METHODS:
        ledger_settings(core, conn, [method], refused=True, method=method)
        refuse(conn, f"call {method}")
    if kind == "remote_login" and method not in REMOTE_LOGIN_METHODS:
        ledger_settings(core, conn, [method], refused=True, method=method)
        refuse(conn, f"call {method}")
    del params


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


def list_visible(conn: Any, session: Any, *, person_only: bool = False, host: Any = None,
                 route: str | None = None) -> bool:
    """A browser or connector sees requests of the sessions it hosts and of
    unhosted ones (IDE, CLI, scheduler); never another browser client's, never a
    person-only one, and a browser never a Jarvis-routed one.  A legacy browser
    (< 0.3.0) sees what it saw before, but Jarvis's routing."""
    kind = _kind(conn)
    if kind not in _WATCHERS:
        return True
    if kind == "browser" and route == "jarvis":
        return False
    if kind == "browser" and legacy_browser(conn):
        return True
    if not FILTER_ANSWER_LISTS:
        return True
    if person_only:
        return False
    owner = _host_of(session, host)
    return owner is None or owner == getattr(conn, "client_id", None)


def may_answer(conn: Any, session: Any, *, origin_conn: Any = None, owner: Any = None,
               person_only: bool = False, actor: str | None = None,
               route: str | None = None) -> bool:
    """Browser (0.3.0+) and connector answers: its own hosted session's requests
    (or one delivered to it) with or without the marker; an unhosted session's
    only with ``actor: "person"`` (a connector never); another browser client's
    never; a person-only one never; a Jarvis-routed one not until it is the
    person's.  A legacy browser answers as before."""
    kind = _kind(conn)
    if kind not in _WATCHERS:
        return True
    # Security review 4, M5: a person-only or Jarvis-routed request is never a
    # browser's, legacy or not (the marker guards against accidents, not a
    # hostile host — the browser host is trusted code).
    if person_only or route == "jarvis":
        return False
    if kind == "browser" and legacy_browser(conn):
        return True
    client_id = getattr(conn, "client_id", None)
    owners = owner if isinstance(owner, tuple) else (owner,)
    if origin_conn is not None and origin_conn is conn:
        return True
    host = next((str(o) for o in owners if o), None) or _host_of(session)
    if host is not None:
        return bool(client_id) and host == client_id
    return kind == "browser" and actor == ACTOR_PERSON


def answer_refusal(conn: Any, session: Any, request_id: str, what: str, *,
                   origin_conn: Any = None, owner: Any = None, person_only: bool = False,
                   actor: str | None = None, route: str | None = None) -> None:
    """Raise when ``conn`` may not answer: ``jarvis_person_only`` for a person-only
    request or an unhosted session's request without the marker, ``not_found`` for
    another browser's session or a Jarvis-routed request (nothing is revealed)."""
    if may_answer(conn, session, origin_conn=origin_conn, owner=owner,
                  person_only=person_only, actor=actor, route=route):
        return
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
                            host=host, route=None):
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
                        route=request.get("route")):
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
