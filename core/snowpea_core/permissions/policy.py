"""Mode x permission-tag policy (contract §7).

The matrix decides first.  The allowlist plugs in through
:meth:`PermissionPolicy.promote`, which may only turn ``ask`` into ``allow`` —
it can never weaken a ``deny``, because :meth:`PermissionPolicy.decide` only
calls it for an ``ask``.

**Plan mode's two exceptions** (M2 §9) are the one place a verdict is loosened,
and they are part of the *mode's own definition* rather than a hook.  That
distinction matters: m2 §2 says a per-call `permission_for` may only ever be at
least as strict as the declared tag, so a hook can never widen anything.  These
two are widened here, in the matrix's own module, where they are visible next
to the row they qualify:

* ``write`` on ``write_file``/``edit_file`` becomes ``allow`` for a document —
  markdown, text, ``docs/``, ``.snowpea/plans/``, ``$SNOWPEA_HOME/plans/``
  (:mod:`snowpea_core.permissions.plan_paths`).  Every other path stays denied.
* ``exec`` on ``shell`` becomes ``allow`` for a command that only inspects
  (:mod:`snowpea_core.permissions.safe_commands`).  Everything else still asks.

Neither touches ``config``: a write that lands on a settings file is re-tagged
``config`` before the policy sees it, and ``config`` is ``deny`` in plan mode
with no exception at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

Mode = Literal["plan", "accept", "auto"]
PermissionTag = Literal["read", "write", "exec", "network", "send", "config", "delegate"]
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
        # Denied except for documents — see PLAN_WRITE_TOOLS below and M2 §9.
        "write": "deny",
        # A planner that cannot run `node --version` or the test suite writes
        # a worse plan, so commands ask instead of being refused; the prompt
        # still says what plan-mode commands are for. Writes stay refused.
        "exec": "ask",
        "network": "allow",
        "send": "deny",
        "config": "deny",
        # A child inherits the session's mode, so delegating cannot do more
        # than the parent may; the child's own calls are what get asked.
        "delegate": "allow",
    },
    "accept": {
        "read": "allow",
        "write": "allow",
        "exec": "ask",
        "network": "ask",
        "send": "ask",
        "config": "ask",
        "delegate": "allow",
    },
    "auto": {
        "read": "allow",
        "write": "allow",
        "exec": "allow",
        "network": "allow",
        "send": "allow",
        "config": "ask",
        "delegate": "allow",
    },
}

#: Tags the allowlist may never promote from ``ask`` to ``allow``.
UNPROMOTABLE: frozenset[str] = frozenset({"config"})

#: Tools the plan-mode document exception applies to.  Only these two take a
#: ``path``; no other ``write``-tagged tool is loosened by it.
PLAN_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})

#: Tool the plan-mode read-only-command exception applies to.
PLAN_EXEC_TOOL = "shell"

#: Human-facing risk label used in ``approval.request``.
RISK_BY_TAG: dict[str, str] = {
    "read": "low",
    "network": "medium",
    "write": "medium",
    "send": "high",
    "exec": "high",
    "config": "high",
    "delegate": "low",
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
        if mode == "plan":
            verdict = self.plan_exception(verdict, tag, tool, args, session)
        if verdict == "ask" and tag not in UNPROMOTABLE:
            verdict = self.promote(verdict, tool, args, session)
        return verdict  # type: ignore[return-value]

    def plan_exception(
        self,
        verdict: str,
        tag: str,
        tool: Tool | None,
        args: dict[str, Any] | None,
        session: Any,
    ) -> str:
        """Plan mode's two documented widenings (M2 §9), and nothing else.

        Both are keyed on the tool *and* the tag, so a future ``write``-tagged
        tool without a ``path`` — or a ``shell`` call that somehow arrives
        tagged ``config`` — is untouched and keeps the matrix's answer.
        """
        if tool is None:
            return verdict
        if verdict == "deny" and tag == "write" and tool.name in PLAN_WRITE_TOOLS:
            from snowpea_core.permissions.plan_paths import is_plan_writable

            path = str((args or {}).get("path", "") or "")
            if is_plan_writable(
                path,
                workdir=getattr(session, "workdir", None),
                home=self._home(),
                settings=self._settings(),
            ):
                return "allow"
        if verdict == "ask" and tag == "exec" and tool.name == PLAN_EXEC_TOOL:
            from snowpea_core.permissions.safe_commands import is_read_only

            if is_read_only(str((args or {}).get("command", "") or "")):
                return "allow"
        return verdict

    def _settings(self) -> Any:
        return getattr(self.allowlist, "settings", None)

    def _home(self) -> Any:
        return getattr(getattr(self.allowlist, "paths", None), "home", None)

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


__all__ = [
    "MODE_MATRIX",
    "NOTE_BY_TAG",
    "PLAN_EXEC_TOOL",
    "PLAN_WRITE_TOOLS",
    "RISK_BY_TAG",
    "UNPROMOTABLE",
    "PermissionPolicy",
]
