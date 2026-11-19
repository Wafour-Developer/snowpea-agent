"""Every settings leaf, classified (security review 7, H-2).

``media.mcp.command`` was a key that names a program the daemon starts, and no
table knew it: a remote web login could write it in the project scope.  So that
a key like it cannot be added unnoticed again, EVERY leaf of the ``Settings``
schema is listed here, in one of two tables:

:data:`COMMAND_LIKE`
    The leaf names something the daemon RUNS or CALLS — a program, its
    arguments, environment or folder (``program``), an address requests go to
    (``endpoint``), a secret sent with them (``secret``), or code and
    authority of the daemon itself (``daemon``).  Each says which connection
    kinds besides a local person may write it over ``settings.set`` (in the
    global scope; nobody but a local person writes one in the project scope).
:data:`PLAIN`
    Everything else, by section.

``tests/test_security_review7_settings.py`` walks the schema
(:func:`schema_leaves`) and fails for a leaf that is in neither table, for a
plain leaf whose name looks like a command (:data:`COMMAND_WORDS`) and is not in
:data:`PLAIN_BY_REVIEW`, and for a :data:`COMMAND_LIKE` leaf the gate
(``settings_handlers.guard_patch``) answers differently from its row.  The gate's
own tables (``settings_gate``) stay the rule; this is the inventory that keeps
them complete.

A path uses ``*`` for any key of a mapping and ``[]`` for a list of objects.
"""

from __future__ import annotations

import types
import typing
from typing import Any

PROGRAM = "program"
ENDPOINT = "endpoint"
SECRET = "secret"
DAEMON = "daemon"

#: Who besides a local person may write a leaf (``settings.set``, global scope).
NOBODY: frozenset[str] = frozenset()
#: The snowpea browser's own Settings page, under a click (``actor: "person"``;
#: a browser from before the marker counts as it): trusted code on this computer.
BROWSER_PAGE: frozenset[str] = frozenset({"browser+actor", "legacy"})
#: ... and a remote web login.
BROWSER_PAGE_AND_REMOTE: frozenset[str] = BROWSER_PAGE | {"remote_login"}
REMOTE_ONLY: frozenset[str] = frozenset({"remote_login"})

#: leaf -> (what it is, who besides a local person may write it).
COMMAND_LIKE: dict[str, tuple[str, frozenset[str]]] = {
    # -- programs the daemon starts ------------------------------------------
    "audio.player": (PROGRAM, NOBODY),
    "audio.recorder": (PROGRAM, NOBODY),
    "audio.stt.command": (PROGRAM, NOBODY),
    "audio.tts.command": (PROGRAM, NOBODY),
    "media.mcp.command": (PROGRAM, NOBODY),
    "media.mcp.args": (PROGRAM, NOBODY),
    "media.mcp.env.*": (PROGRAM, NOBODY),
    "lsp.servers.*.*": (PROGRAM, BROWSER_PAGE),  # {command, args, env, ...}
    "mcp.servers.*.command": (PROGRAM, NOBODY),
    "mcp.servers.*.args": (PROGRAM, NOBODY),
    "mcp.servers.*.env": (PROGRAM, NOBODY),
    "mcp.servers.*.cwd": (PROGRAM, NOBODY),
    "jarvis.external.clis.*.path": (PROGRAM, NOBODY),
    "jarvis.external.clis.*.confirmedPath": (PROGRAM, NOBODY),
    "jarvis.external.clis.*.passEnv": (PROGRAM, NOBODY),
    # -- addresses the daemon calls, and what it sends there -----------------
    "media.mcp.url": (ENDPOINT, NOBODY),
    "media.mcp.api_key": (SECRET, NOBODY),
    # A NEW url-only server (no ${VAR}); a saved one's target is a local person's.
    "mcp.servers.*.url": (ENDPOINT, REMOTE_ONLY),
    "mcp.servers.*.headers": (SECRET, REMOTE_ONLY),
    "mcp.servers.*.oauth": (SECRET, REMOTE_ONLY),
    "mcp.allowVars": (SECRET, NOBODY),  # which variables an untrusted entry may send
    "providers.*.base_url": (ENDPOINT, BROWSER_PAGE),
    # Security review 8, M-1: a remote login no longer swaps the owner's key
    # (traffic, billing and logs would move to its account).
    "providers.*.api_key": (SECRET, BROWSER_PAGE),
    "skills.registry.url": (ENDPOINT, BROWSER_PAGE),
    "skills.registry.token": (SECRET, BROWSER_PAGE),
    "gateway.*.token": (SECRET, BROWSER_PAGE),
    "gateway.*.app_token": (SECRET, BROWSER_PAGE),
    "search.credentials.*.*": (SECRET, BROWSER_PAGE_AND_REMOTE),
    "browser.credentials.*.*": (SECRET, BROWSER_PAGE_AND_REMOTE),
    # -- the daemon itself: its code, its doors, what it installs ------------
    "extensions.*.*": (DAEMON, NOBODY),
    "web.remote.enabled": (DAEMON, NOBODY),
    "web.remote.host": (DAEMON, NOBODY),
    "web.remote.port": (DAEMON, NOBODY),
    "web.remote.username": (DAEMON, NOBODY),
    "web.remote.sessionHours": (DAEMON, NOBODY),
    "web.remote.allowedOrigins": (DAEMON, NOBODY),
    "web.remote.trustProxy": (DAEMON, NOBODY),
    "web.remote.maxMode": (DAEMON, NOBODY),
    "updates.channel": (DAEMON, NOBODY),
    # 1.56.0: lets a remote web login give Jarvis grants (with its password
    # again): authority of the daemon itself, a local person's switch only.
    "jarvis.delegation.remoteGrant": (DAEMON, NOBODY),
    # 1.56.0: whether a new session starts in Jarvis's sight (the per-session
    # switch's default): a local person's choice, like the switch above.
    "jarvis.sessions.default": (DAEMON, NOBODY),
}

#: Schema leaves that are open mappings (``Any``): the fields inside them that
#: are command-like are listed in :data:`COMMAND_LIKE` one level down.
OPEN: frozenset[str] = frozenset({"providers.*", "gateway.*", "mcp.servers.*.*"})

# -- free-form blocks: classified by KEY SHAPE (security review 8, H8-1) ------------

#: A field name that names an address requests go to: equal to or ending in one
#: of these, case-insensitively (``url``, ``base_url``, ``baseUrl``,
#: ``SEARXNG_URL``, ``endpoint``, ``host``, ``proxy``, ``verification_uri``).
ENDPOINT_SUFFIXES: tuple[str, ...] = ("url", "uri", "endpoint", "host", "proxy")


def endpoint_key(name: str) -> bool:
    """``name`` (one field of a free-form block) is shaped like an address."""
    lowered = str(name).strip().lower()
    return any(lowered.endswith(suffix) for suffix in ENDPOINT_SUFFIXES)


#: Free-form blocks (``dict[str, Any]`` in the schema) whose fields the daemon
#: reads BY NAME: an endpoint-shaped field (:func:`endpoint_key`) inside one is
#: an :data:`ENDPOINT`, whoever may write the rest of the block.  The block is
#: written as a pattern (``*``: any key); the field is the next key down.
#: Who besides a local person may write such a field — the browser's Settings
#: page keeps today's rule (its inventory lists ``search``, ``browser``,
#: ``providers`` and ``gateway``), a remote login never
#: (``settings_gate.REMOTE_LOGIN_NEVER_SHAPES``).
ENDPOINT_SHAPED: dict[str, frozenset[str]] = {
    # tools/search_providers/base.py credentials_for: base_url / baseUrl / url,
    # and <NAME>_URL from the daemon's environment.
    "search.credentials.*": BROWSER_PAGE,
    # tools/browser_providers/__init__.py credentials_for: the same, plus a
    # field named after an environment variable (FIRECRAWL_URL, firecrawl_url).
    "browser.credentials.*": BROWSER_PAGE,
    # providers/registry.py base_url_for, providers/presets.py (base_url).
    "providers.*": BROWSER_PAGE,
    # gateway/router.py reads token, app_token, ids, workdir, mode: no address
    # today; any address-shaped field a platform adds is covered in advance.
    "gateway.*": BROWSER_PAGE,
}

#: Every free-form leaf of the schema (an ``Any`` value), and what governs it.
#: ``tests/test_security_review8_settings.py`` fails for one not listed here.
FREE_FORM: dict[str, str] = {
    "search.credentials.*.*": "endpoint-shaped fields: ENDPOINT_SHAPED; the rest a secret",
    "browser.credentials.*.*": "endpoint-shaped fields: ENDPOINT_SHAPED; the rest a secret",
    "providers.*": "endpoint-shaped fields: ENDPOINT_SHAPED; credentials: COMMAND_LIKE",
    "gateway.*": "endpoint-shaped fields: ENDPOINT_SHAPED; identity: COMMAND_LIKE",
    "mcp.servers.*.*": "settings_gate.remote_mcp_* (launch fields, target of a saved entry)",
    "lsp.servers.*.*": "a program: COMMAND_LIKE (browser page only)",
    "extensions.*.*": "the daemon's own code: COMMAND_LIKE (nobody)",
    "agent.effortMap.*": "model -> effort level; read by providers/effort.py, no address",
}


def free_form_leaves(model: Any = None) -> set[str]:
    """Schema leaves whose value is free-form (``Any``): a block of fields the
    schema does not name, read by whatever component owns it."""
    from pydantic import BaseModel

    from snowpea_core.config.settings import Settings

    found: set[str] = set()

    def walk(annotation: Any, prefix: tuple[str, ...]) -> None:
        if annotation is Any:
            found.add(".".join(prefix))
            return
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            for name, field in annotation.model_fields.items():
                walk(field.annotation, (*prefix, name))
            return
        origin, args = typing.get_origin(annotation), typing.get_args(annotation)
        if origin in (dict, typing.Mapping) and len(args) == 2:
            walk(args[1], (*prefix, "*"))
        elif origin in (typing.Union, types.UnionType):
            for arg in args:
                walk(arg, prefix)
        elif origin in (list, tuple) and args:
            walk(args[0], (*prefix, "[]"))

    walk(model if model is not None else Settings, ())
    return found

#: Every other leaf, by section (names relative to it).
PLAIN: dict[str, str] = {
    "agent": (
        "answerNudgeRounds autoAttachImages auto_budget_continuations busy contextFileMaxChars "
        "contextFilesMaxChars effort effortBy.* effortMap.* ignoreContextFiles keepToolRounds "
        "max_tokens max_tool_rounds no_progress_rounds provider_connect_wait_sec "
        "provider_first_token_sec provider_idle_sec provider_retries provider_step_budget_sec "
        "provider_timeout_sec pruneToolOutputs replyLanguage thinking turn_max_minutes "
        "turn_max_tokens unfinished_continue verifyOnStop verify_continue_rounds"
    ),
    "agents": (
        "allowBroaderChildren childContext defaultToolRounds default_team delegateByDefault "
        "generalAgent incompleteRetries leadDirectCalls maxToolRounds maxToolRoundsBy.* "
        "max_concurrent missingRole models.* ownIdentity teams.* toolRounds.*"
    ),
    "allowlist": (
        "[].created_at [].host_tools_from [].id [].origin [].pattern [].reason_code [].target"
    ),
    "approvals": "readOnlyWebAsk timeoutSec",
    "audio": (
        "stt.language stt.model stt.provider tts.autoSpeak tts.enabled tts.model tts.provider "
        "tts.speakAck tts.voice tts.voices.*"
    ),
    "browser": "defaultAgent fastModel headless provider siteMemory",
    "checkpoints": "enabled maxBytes maxTurns scanShellWrites",
    "context": "autoCompact autoCompactPercent keepLastMessages",
    "daemon": "idleTimeoutSec keepAwake",
    "jarvis": (
        "briefing.enabled briefing.instructions briefing.schedule briefing.sections "
        "briefing.timezone budget.dailyTokens budget.wakesPerHour delegation.default "
        "delegation.projectTtlHours enabled exposeBrowser external.clis.*.defaultMode "
        "external.clis.*.enabled external.enabled external.idleSec external.maxProcesses "
        "folder messenger.allowGrants messenger.route model notify.inApp notify.messenger "
        "notify.os proactive.decided proactive.error proactive.finished proactive.needsYou "
        "quietMinutes stallMinutes"
    ),
    "lsp": "autoInstall disabled enable enabled idleTimeoutSec",
    "mcp": "enabled permissions.*",
    "memory": (
        "askScope autoExtract auto_remember_patterns digestChars digestEntries enabled "
        "recallBrowser top_k"
    ),
    "models": (
        "categories.* default profiles.*.model profiles.*.provider "
        "profiles.*.supportsParallelTools"
    ),
    "modes": "plan.writableGlobs",
    "planning": "gate",
    "questions": "timeoutSec",
    "ralph": "max_iterations",
    "scheduler": "approvalTimeoutSec catchUpSec enabled tickSec",
    "search": "provider",
    "security": (
        "blockedCommands.custom blockedCommands.presets enabled protectedPaths.custom.[].level "
        "protectedPaths.custom.[].path protectedPaths.presets unknownCode"
    ),
    "skills": "disabled importCodex indexInPrompt indexMaxEntries protectRecentViews",
    "team": "max_conflict_retries pipeline.maxTasks pipeline.review pipeline.test review",
    "tools": (
        "agentDeferred deferred eager enabled_categories listDescriptions maxResultChars "
        "maxResultLines max_output_chars mcpResultMaxChars readBeforeWrite repeatGuard "
        "snapshotMaxChars"
    ),
    "updates": "check",
}

#: A leaf name with one of these words in it must be in :data:`COMMAND_LIKE`, or
#: in :data:`PLAIN_BY_REVIEW` with the reason it is not one.
COMMAND_WORDS: tuple[str, ...] = (
    "command", "cmd", "args", "argv", "env", "url", "uri", "endpoint", "host", "port", "path",
    "cwd", "dir", "folder", "bin", "exec", "program", "script", "shell", "hook", "player",
    "recorder", "token", "secret", "password", "key", "header", "origin", "proxy", "registry",
    "channel", "server", "credential", "plugin", "extension",
)

#: Plain leaves whose name has a :data:`COMMAND_WORDS` word: why each is not a
#: program, an address or a secret the daemon uses.
PLAIN_BY_REVIEW: dict[str, str] = {
    "agent.max_tokens": "a number of tokens",
    "agent.turn_max_tokens": "a number of tokens",
    "agent.provider_first_token_sec": "a timeout",
    "allowlist.[].origin": "a site a rule is limited to; allowlist is a local person's",
    "allowlist.[].host_tools_from": "a browser profile a rule belongs to; a local person's",
    "jarvis.budget.dailyTokens": "a number of tokens",
    "jarvis.folder": "Jarvis's write root, validated; jarvis.* is a local person's",
    "security.protectedPaths.custom.[].path": "a path to protect; security.* is a local person's",
    "checkpoints.scanShellWrites": "a switch",
}


def schema_leaves(model: Any = None) -> set[str]:
    """Every leaf of the settings schema, dotted (``*``: a mapping key, ``[]``: a list)."""
    from pydantic import BaseModel

    from snowpea_core.config.settings import Settings

    found: set[str] = set()

    def is_model(annotation: Any) -> bool:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    def walk(annotation: Any, prefix: tuple[str, ...]) -> None:
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        if is_model(annotation):
            for name, field in annotation.model_fields.items():
                walk(field.annotation, (*prefix, name))
            return
        if origin in (dict, typing.Mapping) and len(args) == 2:
            walk(args[1], (*prefix, "*"))
            return
        if origin in (list, tuple, set, frozenset) and args and is_model(args[0]):
            walk(args[0], (*prefix, "[]"))
            return
        if origin in (typing.Union, types.UnionType):
            nested = [
                arg for arg in args
                if is_model(arg) or typing.get_origin(arg) in (dict, typing.Mapping)
                or (typing.get_origin(arg) in (list, tuple) and typing.get_args(arg)
                    and is_model(typing.get_args(arg)[0]))
            ]
            if nested:
                for arg in nested:
                    walk(arg, prefix)
                return
        found.add(".".join(prefix))

    walk(model if model is not None else Settings, ())
    return found


def plain_leaves() -> set[str]:
    return {f"{section}.{name}" for section, names in PLAIN.items() for name in names.split()}


def covered(leaf: str) -> bool:
    """``leaf`` (a schema leaf) is classified: plain, command-like, or an open
    mapping whose command-like fields are listed one level down."""
    return leaf in plain_leaves() or leaf in COMMAND_LIKE or leaf in OPEN


def looks_like_a_command(leaf: str) -> bool:
    """A word of ``leaf``'s last name (``passEnv`` -> pass, env; ``base_url`` ->
    base, url; plurals count) is one of :data:`COMMAND_WORDS`."""
    import re

    last = [part for part in leaf.split(".") if part not in ("*", "[]")][-1]
    words = re.findall(r"[A-Za-z][a-z0-9]*", last.replace("_", " ").replace("-", " "))
    lowered = {word.lower() for word in words}
    lowered |= {word[:-1] for word in lowered if word.endswith("s")}
    return not lowered.isdisjoint(COMMAND_WORDS)


__all__ = [
    "COMMAND_LIKE",
    "COMMAND_WORDS",
    "ENDPOINT_SHAPED",
    "FREE_FORM",
    "OPEN",
    "endpoint_key",
    "free_form_leaves",
    "PLAIN",
    "PLAIN_BY_REVIEW",
    "covered",
    "looks_like_a_command",
    "plain_leaves",
    "schema_leaves",
]
