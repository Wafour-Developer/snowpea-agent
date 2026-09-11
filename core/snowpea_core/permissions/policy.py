"""Mode x permission-tag policy — M1 stub (US-006 fills it in)."""

from __future__ import annotations

from typing import Any, Literal

Mode = Literal["plan", "accept", "auto"]
PermissionTag = Literal["read", "write", "exec", "network", "send"]
Verdict = Literal["allow", "deny", "ask"]

#: Contract §7 table. US-006 owns the allowlist promotion on top of it.
MODE_MATRIX: dict[str, dict[str, str]] = {
    "plan": {"read": "allow", "write": "deny", "exec": "deny", "network": "allow", "send": "deny"},
    "accept": {"read": "allow", "write": "allow", "exec": "ask", "network": "ask", "send": "ask"},
    "auto": {
        "read": "allow",
        "write": "allow",
        "exec": "allow",
        "network": "allow",
        "send": "allow",
    },
}


class PermissionPolicy:
    """Decides allow/deny/ask for a tool call. Stub: table lookup only."""

    def decide(
        self,
        mode: Mode,
        tag: PermissionTag,
        tool: Any = None,
        args: dict[str, Any] | None = None,
        session: Any = None,
    ) -> Verdict:
        verdict = MODE_MATRIX.get(mode, {}).get(tag, "ask")
        return verdict  # type: ignore[return-value]


__all__ = ["MODE_MATRIX", "PermissionPolicy"]
