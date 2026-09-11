"""Mode x permission-tag policy (contract §7).

The matrix decides first.  The allowlist plugs in through
:meth:`PermissionPolicy.promote`, which may only turn ``ask`` into ``allow`` —
it can never weaken a ``deny``, because :meth:`PermissionPolicy.decide` only
calls it for an ``ask``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

Mode = Literal["plan", "accept", "auto"]
PermissionTag = Literal["read", "write", "exec", "network", "send"]
Verdict = Literal["allow", "deny", "ask"]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.permissions.allowlist import Allowlist
    from snowpea_core.tools.registry import Tool

#: Contract §7 table.
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

#: Human-facing risk label used in ``approval.request``.
RISK_BY_TAG: dict[str, str] = {
    "read": "low",
    "network": "medium",
    "write": "medium",
    "send": "high",
    "exec": "high",
}


class PermissionPolicy:
    """Decides allow / deny / ask for one tool call."""

    def __init__(self, allowlist: Allowlist | None = None) -> None:
        self.allowlist = allowlist

    def bind(self, allowlist: Allowlist) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.allowlist = allowlist

    def decide(
        self,
        mode: Mode,
        tag: PermissionTag,
        tool: Tool | None = None,
        args: dict[str, Any] | None = None,
        session: Any = None,
    ) -> Verdict:
        """Look the pair up in :data:`MODE_MATRIX`, then apply the allowlist."""
        verdict = MODE_MATRIX.get(mode, {}).get(tag, "ask")
        if verdict == "ask":
            verdict = self.promote(verdict, tool, args, session)
        return verdict  # type: ignore[return-value]

    def promote(
        self,
        verdict: str,
        tool: Tool | None = None,
        args: dict[str, Any] | None = None,
        session: Any = None,
    ) -> str:
        """Promote ``ask`` to ``allow`` when the allowlist covers this call."""
        if verdict != "ask" or self.allowlist is None or tool is None:
            return verdict
        workdir = getattr(session, "workdir", None)
        if self.allowlist.matches(tool, args or {}, workdir=workdir):
            return "allow"
        return verdict

    def risk(self, tag: PermissionTag) -> str:
        return RISK_BY_TAG.get(tag, "medium")


__all__ = ["MODE_MATRIX", "RISK_BY_TAG", "PermissionPolicy"]
