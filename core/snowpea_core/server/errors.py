"""Snowpea RPC error codes.

The wire format is JSON-RPC 2.0: the numeric ``error.code`` stays in the
JSON-RPC range while the stable, machine-readable snowpea code lives in
``error.data.code`` (contract §1).
"""

from __future__ import annotations

from typing import Any

UNAUTHORIZED = "unauthorized"
PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
NOT_FOUND = "not_found"
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
INTERNAL = "internal"

ERROR_CODES: tuple[str, ...] = (
    UNAUTHORIZED,
    PROTOCOL_INCOMPATIBLE,
    NOT_FOUND,
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
    INTERNAL,
)

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS_JSONRPC = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000

_JSONRPC_CODE: dict[str, int] = {
    NOT_FOUND: METHOD_NOT_FOUND,
    INVALID_PARAMS: INVALID_PARAMS_JSONRPC,
    MCP_INVALID: INVALID_PARAMS_JSONRPC,
    MCP_NOT_FOUND: METHOD_NOT_FOUND,
    INTERNAL: INTERNAL_ERROR,
}


def jsonrpc_code(code: str) -> int:
    """Map a snowpea error code onto its JSON-RPC numeric code."""
    return _JSONRPC_CODE.get(code, SERVER_ERROR)


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
    "APPROVAL_DENIED",
    "APPROVAL_TIMEOUT",
    "ERROR_CODES",
    "INTERNAL",
    "INTERNAL_ERROR",
    "INVALID_PARAMS_JSONRPC",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
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
    "TOOL_INACTIVE",
    "UNAUTHORIZED",
    "INVALID_PARAMS",
    "RpcError",
    "jsonrpc_code",
]
