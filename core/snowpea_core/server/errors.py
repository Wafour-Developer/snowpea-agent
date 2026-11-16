"""Snowpea RPC error codes.

The wire format is JSON-RPC 2.0: the numeric ``error.code`` stays in the
JSON-RPC range while the stable, machine-readable snowpea code lives in
``error.data.code`` (contract §1).
"""

from __future__ import annotations

from typing import Any

UNAUTHORIZED = "unauthorized"
PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
#: A session, job, memory, content or other entity does not exist.  JSON-RPC
#: code -32004, never -32601 (protocol 1.7.0).
NOT_FOUND = "not_found"
#: The method itself does not exist: the only error answered with the
#: standard -32601, so a client can feature-detect an older daemon by it.
METHOD_NOT_FOUND_CODE = "method_not_found"
INVALID_PARAMS = "invalid_params"
MODE_DENIED = "mode_denied"
APPROVAL_DENIED = "approval_denied"
APPROVAL_TIMEOUT = "approval_timeout"
TOOL_INACTIVE = "tool_inactive"
NOT_IMPLEMENTED = "not_implemented"
SESSION_BUSY = "session_busy"
LOGIN_UNSUPPORTED = "login_unsupported"
#: A stored OAuth session expired and could not be refreshed; the user has to
#: sign in again.  Distinct from ``invalid_params`` so a surface can offer the
#: login instead of blaming the request (CORE-codex-login).
AUTH_EXPIRED = "auth_expired"
#: MCP server management (M14 §3).  ``mcp_invalid`` names the offending field,
#: ``mcp_unsafe`` carries the security findings ``force`` would override.
MCP_EXISTS = "mcp_exists"
MCP_NOT_FOUND = "mcp_not_found"
MCP_READ_ONLY = "mcp_read_only"
MCP_INVALID = "mcp_invalid"
MCP_START_FAILED = "mcp_start_failed"
MCP_UNSAFE = "mcp_unsafe"
#: ``mcp.authorize`` / ``mcp.authorize.complete`` could not get a token
#: (1.34.0): ``details.stage`` is discovery, metadata, registration, client,
#: exchange, state or callback.  Never carries a token or a code.
MCP_AUTH_FAILED = "mcp_auth_failed"
#: ``skill.create`` without ``force`` on a target that already has a
#: ``SKILL.md`` (1.33.0); ``details.path`` names it.
SKILL_EXISTS = "skill_exists"
#: A service the call needs cannot answer right now (``web.search`` with no
#: usable search provider, protocol 1.11.0); ``details.reason`` says why.
UNAVAILABLE = "unavailable"
#: The model answered, but not in the shape asked for (``llm.sample`` with a
#: ``schema``, 1.14.0); ``details.text`` holds the answer.
INVALID_OUTPUT = "invalid_output"
INTERNAL = "internal"
#: The model ended its turn with no text and no tool call, twice (1.23.0).
EMPTY_RESPONSE = "empty_response"
#: The model server could not decode an image in the request (1.37.2); core
#: replaces the unreadable picture with text and asks once more before this.
IMAGE_REJECTED = "image_rejected"

#: ``fs.*`` (1.40.0): the path resolves outside every root of the session
#: (``..``, an absolute path or a symbolic link), or a write would go through a
#: link; the content is too big for one call; the path is already there
#: (``fs.write`` ``create``) or is a directory where a file is needed; or the
#: file is one of snowpea's own secrets or a credential file.
OUTSIDE_ROOT = "outside_root"
TOO_LARGE = "too_large"
EXISTS = "exists"
IS_DIR = "is_dir"
PROTECTED = "protected"

#: ``session.fork`` refusals (1.50.0): the turn is before the last compaction (or
#: from before forks were recorded); no finished turn has that id; the session has
#: no finished turn; the source is a delegated child; the latest turn cannot be
#: taken while a turn is still running.
FORK_AFTER_COMPACTION = "fork_after_compaction"
FORK_TURN_NOT_FOUND = "fork_turn_not_found"
FORK_NO_FINISHED_TURN = "fork_no_finished_turn"
FORK_CHILD_SESSION = "fork_child_session"
FORK_RUNNING = "fork_running"
#: ``session.steer`` text that is a slash command once trimmed (1.50.0): a
#: command goes through ``session.prompt`` or ``command.run``, never a steer.
STEER_SLASH_COMMAND = "steer_slash_command"
#: ``goal.set`` / ``goal.resume`` on a delegated child or a scheduled run (1.50.0).
GOAL_NOT_ALLOWED = "goal_not_allowed"
#: M19 Jarvis mode (1.53.0): a jarvis.* call that needs the mode on while it is
#: off; a delegation change from a connection that may not make it (a messenger
#: with jarvis.messenger.allowGrants off, a remote web login that has not
#: re-authenticated, a browser host); external.send to a session snowpea can only
#: view (host terminal, or tmux before spike S5); an external CLI that is
#: missing, disabled or unsupported.
JARVIS_DISABLED = "jarvis_disabled"
JARVIS_PERSON_ONLY = "jarvis_person_only"
EXTERNAL_VIEW_ONLY = "external_view_only"
EXTERNAL_UNAVAILABLE = "external_unavailable"
#: 1.54.0: a call refused by the security lists (blocked command or protected path).
SECURITY_BLOCKED = "security_blocked"
#: 1.55.0 (``error`` event codes registered with the protocol): a plugin hook
#: could not run or failed (``details {plugin, hook, event, reason, exitCode?,
#: timedOut?}``); the model's context window cannot hold snowpea's system
#: prompt and tool definitions, so the turn ended (``details {windowTokens,
#: neededTokens}``).
HOOK_FAILED = "hook_failed"
CONTEXT_WINDOW_TOO_SMALL = "context_window_too_small"
#: 1.55.0 (security review 6, M-5): ``project.trust.set`` was given digests that
#: are not what is on disk now — a file changed, appeared or went away between
#: the status the person looked at and the click.  Nothing was trusted;
#: ``details {changed: [path], added: [path], missing: [path]}``.  Read the
#: status again and show it.
TRUST_CHANGED = "trust_changed"

#: ``error.data.details.reason`` values a client may switch on (1.55.0
#: enumerates them): reason -> (the code it rides on, what it means).
DETAIL_REASONS: dict[str, tuple[str, str]] = {
    "person_only": (
        JARVIS_PERSON_ONLY, "only a person's connection may do this (details.kind says who asked)"
    ),
    "mode_raise": (
        JARVIS_PERSON_ONLY,
        "raising a session's mode (plan < accept < auto) needs a person; details.from/to. "
        "With details.max: a remote web login asked for, or to start work in, a mode above "
        "settings web.remote.maxMode (the value of details.max)",
    ),
    "jarvis_on": (
        JARVIS_PERSON_ONLY,
        "the change is part of Jarvis's authority and Jarvis mode is on: the app or the TUI only",
    ),
    "not_bundled_plugin": (
        JARVIS_PERSON_ONLY,
        "skill.install from a browser outside its Settings page, not the bundled plugin; "
        "details.why",
    ),
    "local_only": (
        JARVIS_PERSON_ONLY,
        "a person may do this only at a local connection (IDE, TUI, CLI, the local web page): "
        "it names a program to run, a messenger identity, a provider endpoint or an install "
        "source, changes where a saved MCP server's requests go or what they carry, uses a "
        "${VAR}, stores a rule or a setting, or writes a skill, agent or command file that "
        "widens what runs; details.what says which",
    ),
    "snowpea_home": (
        INVALID_PARAMS,
        "the folder is snowpea's own home (or its .snowpea directory is): not a project, for "
        "any caller; details.field names the parameter, details.what is 'home' or 'parent'",
    ),
    "project_settings_invalid": (
        INVALID_PARAMS,
        "the project's .snowpea/settings.json on disk does not validate: it is left as it is "
        "and not written over with defaults plus the change; fix or remove it first",
    ),
    "catch_all": (INVALID_PARAMS, "an allowlist pattern that would match every command"),
    "project_add_only": (
        INVALID_PARAMS,
        "a project's security lists may only add entries; details.removed names the rest",
    ),
    "global_only": (
        INVALID_PARAMS,
        "a project scope tried to set a key only the global settings hold "
        "(security.unknownCode); details.key",
    ),
    "untrusted_project": (
        EXTERNAL_UNAVAILABLE,
        "first external CLI session in this project: confirm with trust: true; details.project",
    ),
    "untrusted_project_person_only": (
        EXTERNAL_UNAVAILABLE,
        "a person must open an external session in this project first; details.project",
    ),
    "no_vision_model": (UNAVAILABLE, "llm.sample modelCategory 'vision' with none configured"),
    "model_cannot_see": (UNAVAILABLE, "llm.sample images for a model known not to take images"),
}

#: ``details.kind`` of a ``jarvis_person_only`` error: who asked.  The
#: connection kinds (``server/person_gate.connection_kind``) plus the mode
#: guard's caller kinds (``session/mode_guard.ModeCaller``); a refused answer to
#: a person-only question carries the question's kind instead (``mode`` or null).
PERSON_ONLY_KINDS: tuple[str, ...] = (
    "person", "remote_login", "browser", "connector", "closed", "none", "agent", "mode",
)

ERROR_CODES: tuple[str, ...] = (
    UNAUTHORIZED,
    PROTOCOL_INCOMPATIBLE,
    NOT_FOUND,
    METHOD_NOT_FOUND_CODE,
    INVALID_PARAMS,
    MODE_DENIED,
    APPROVAL_DENIED,
    APPROVAL_TIMEOUT,
    TOOL_INACTIVE,
    NOT_IMPLEMENTED,
    SESSION_BUSY,
    LOGIN_UNSUPPORTED,
    AUTH_EXPIRED,
    MCP_EXISTS,
    MCP_NOT_FOUND,
    MCP_READ_ONLY,
    MCP_INVALID,
    MCP_START_FAILED,
    MCP_UNSAFE,
    MCP_AUTH_FAILED,
    SKILL_EXISTS,
    UNAVAILABLE,
    INVALID_OUTPUT,
    INTERNAL,
    EMPTY_RESPONSE,
    IMAGE_REJECTED,
    OUTSIDE_ROOT,
    TOO_LARGE,
    EXISTS,
    IS_DIR,
    PROTECTED,
    FORK_AFTER_COMPACTION,
    FORK_TURN_NOT_FOUND,
    FORK_NO_FINISHED_TURN,
    FORK_CHILD_SESSION,
    FORK_RUNNING,
    STEER_SLASH_COMMAND,
    GOAL_NOT_ALLOWED,
    JARVIS_DISABLED,
    JARVIS_PERSON_ONLY,
    EXTERNAL_VIEW_ONLY,
    EXTERNAL_UNAVAILABLE,
    SECURITY_BLOCKED,
    HOOK_FAILED,
    CONTEXT_WINDOW_TOO_SMALL,
    TRUST_CHANGED,
)

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS_JSONRPC = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000
#: Application code for a missing entity (``not_found``, ``mcp_not_found``).
#: Before protocol 1.7.0 these shared -32601 with unknown methods.
ENTITY_NOT_FOUND = -32004

_JSONRPC_CODE: dict[str, int] = {
    METHOD_NOT_FOUND_CODE: METHOD_NOT_FOUND,
    NOT_FOUND: ENTITY_NOT_FOUND,
    INVALID_PARAMS: INVALID_PARAMS_JSONRPC,
    MCP_INVALID: INVALID_PARAMS_JSONRPC,
    MCP_NOT_FOUND: ENTITY_NOT_FOUND,
    INTERNAL: INTERNAL_ERROR,
    # The numbers these refusals had before they got their own codes (1.50.0).
    FORK_AFTER_COMPACTION: INVALID_PARAMS_JSONRPC,
    FORK_TURN_NOT_FOUND: ENTITY_NOT_FOUND,
    FORK_NO_FINISHED_TURN: INVALID_PARAMS_JSONRPC,
    FORK_CHILD_SESSION: INVALID_PARAMS_JSONRPC,
    FORK_RUNNING: INVALID_PARAMS_JSONRPC,
    STEER_SLASH_COMMAND: INVALID_PARAMS_JSONRPC,
    GOAL_NOT_ALLOWED: INVALID_PARAMS_JSONRPC,
}


def jsonrpc_code(code: str) -> int:
    """Map a snowpea error code onto its JSON-RPC numeric code."""
    return _JSONRPC_CODE.get(code, SERVER_ERROR)


def new_error_id() -> str:
    """An id to quote for a failure whose text stays in the daemon log.

    Exception text can carry paths, settings and other callers' data (security
    review 6; review 7, Low 5): what reaches a client that is not a local
    person, or an event every subscriber sees, is :func:`internal_text` with
    this id; the text is logged under it.
    """
    import uuid

    return uuid.uuid4().hex[:12]


def internal_text(error_id: str) -> str:
    return f"internal error (id {error_id})"


class RpcError(Exception):
    """Raised by handlers; serialised into a JSON-RPC error object."""

    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_jsonrpc(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code}
        if self.data is not None:
            payload["details"] = self.data
        return {"code": jsonrpc_code(self.code), "message": self.message, "data": payload}


__all__ = [
    "EXISTS",
    "IS_DIR",
    "OUTSIDE_ROOT",
    "PROTECTED",
    "TOO_LARGE",
    "APPROVAL_DENIED",
    "APPROVAL_TIMEOUT",
    "ERROR_CODES",
    "INTERNAL",
    "INTERNAL_ERROR",
    "INVALID_PARAMS_JSONRPC",
    "INVALID_REQUEST",
    "ENTITY_NOT_FOUND",
    "METHOD_NOT_FOUND",
    "METHOD_NOT_FOUND_CODE",
    "PARSE_ERROR",
    "LOGIN_UNSUPPORTED",
    "MCP_EXISTS",
    "MCP_INVALID",
    "MCP_NOT_FOUND",
    "MCP_READ_ONLY",
    "MCP_START_FAILED",
    "MCP_UNSAFE",
    "MODE_DENIED",
    "NOT_FOUND",
    "NOT_IMPLEMENTED",
    "PROTOCOL_INCOMPATIBLE",
    "SERVER_ERROR",
    "SESSION_BUSY",
    "SKILL_EXISTS",
    "TOOL_INACTIVE",
    "UNAUTHORIZED",
    "UNAVAILABLE",
    "INVALID_OUTPUT",
    "INVALID_PARAMS",
    "RpcError",
    "jsonrpc_code",
]
