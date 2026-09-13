"""Mode x permission-tag policy (contract §7).

The matrix decides first.  The allowlist plugs in through
:meth:`PermissionPolicy.promote`, which may only turn ``ask`` into ``allow`` —
it can never weaken a ``deny``, because :meth:`PermissionPolicy.decide` only
calls it for an ``ask``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

Mode = Literal["plan", "accept", "auto"]
PermissionTag = Literal["read", "write", "exec", "network", "send", "config"]
Verdict = Literal["allow", "deny", "ask"]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.permissions.allowlist import Allowlist
    from snowpea_core.tools.registry import Tool

#: Contract §7 table, plus the ``config`` row (CORE-search-fix).  ``config``
#: never resolves to ``allow``: auto mode asks for it too, because an agent
#: quietly rewriting settings.json is exactly what the tag exists to stop.
MODE_MATRIX: dict[str, dict[str, str]] = {
    "plan": {
        "read": "allow",
        "write": "deny",
        # A planner that cannot run `node --version` or the test suite writes
        # a worse plan, so commands ask instead of being refused; the prompt
        # still says what plan-mode commands are for. Writes stay refused.
        "exec": "ask",
        "network": "allow",
        "send": "deny",
        "config": "deny",
    },
    "accept": {
        "read": "allow",
        "write": "allow",
        "exec": "ask",
        "network": "ask",
        "send": "ask",
        "config": "ask",
    },
    "auto": {
        "read": "allow",
        "write": "allow",
        "exec": "allow",
        "network": "allow",
        "send": "allow",
        "config": "ask",
    },
}

#: Tags the allowlist may never promote from ``ask`` to ``allow``.
UNPROMOTABLE: frozenset[str] = frozenset({"config"})

#: Human-facing risk label used in ``approval.request``.
RISK_BY_TAG: dict[str, str] = {
    "read": "low",
    "network": "medium",
    "write": "medium",
    "send": "high",
    "exec": "high",
    "config": "high",
}

#: Extra sentence shown with the approval prompt for a tag that needs one.
NOTE_BY_TAG: dict[str, str] = {"config": "modifies snowpea configuration"}


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
        if verdict == "ask" and tag not in UNPROMOTABLE:
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

    def note(self, tag: PermissionTag) -> str:
        """The warning the approval prompt carries for this tag, if any."""
        return NOTE_BY_TAG.get(tag, "")


__all__ = ["MODE_MATRIX", "NOTE_BY_TAG", "RISK_BY_TAG", "UNPROMOTABLE", "PermissionPolicy"]
