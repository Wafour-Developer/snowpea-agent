"""Read-only web tools in accept mode, and approvals a subagent inherits (addendum 18).

A subagent's three-city population lookup raised 37 approval cards in the
browser's safe (accept) mode. web_search / web_extract only read, so accept
mode runs them without asking unless ``approvals.readOnlyWebAsk`` is on; a
"for this session" answer in the parent covers its subagents; and a
definition cannot widen its child's mode past the parent's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import Allowlist
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.permissions.policy import PermissionPolicy, stricter_mode
from snowpea_core.session.session import Session

WEB_SEARCH = SimpleNamespace(name="web_search", permission="network")
WEB_EXTRACT = SimpleNamespace(name="web_extract", permission="network")
BROWSER_CLICK = SimpleNamespace(name="browser_click", permission="network")


def _policy(ask: bool = False) -> PermissionPolicy:
    settings = Settings()
    settings.approvals.readOnlyWebAsk = ask
    return PermissionPolicy(Allowlist(settings=settings))


def test_accept_mode_runs_read_only_web_tools_without_asking() -> None:
    policy = _policy()
    assert policy.decide("accept", "network", WEB_SEARCH, {}) == "allow"  # type: ignore[arg-type]
    assert policy.decide("accept", "network", WEB_EXTRACT, {}) == "allow"  # type: ignore[arg-type]
    # Other network tools still ask; plan and auto keep their matrix answer.
    assert policy.decide("accept", "network", BROWSER_CLICK, {}) == "ask"  # type: ignore[arg-type]
    assert policy.decide("plan", "network", WEB_SEARCH, {}) == "allow"  # type: ignore[arg-type]
    assert policy.decide("auto", "network", WEB_SEARCH, {}) == "allow"  # type: ignore[arg-type]


def test_read_only_web_ask_brings_the_prompt_back() -> None:
    policy = _policy(ask=True)
    assert policy.decide("accept", "network", WEB_SEARCH, {}) == "ask"  # type: ignore[arg-type]


def test_a_subagent_is_covered_by_its_parents_session_answer(tmp_path: Path) -> None:
    parent = Session(id="s-parent", workdir=tmp_path)
    child = Session(id="s-child", workdir=tmp_path, parent_session_id="s-parent", kind="subagent")
    grandchild = Session(
        id="s-grand", workdir=tmp_path, parent_session_id="s-child", kind="subagent"
    )
    routine = Session(
        id="s-routine", workdir=tmp_path, parent_session_id="s-parent", kind="scheduled"
    )
    by_id = {s.id: s for s in (parent, child, grandchild, routine)}
    queue = ApprovalQueue(hub=SimpleNamespace(sessions=SimpleNamespace(get=by_id.get)))  # type: ignore[arg-type]
    queue._cache.add(queue.cache_key("s-parent", "browser_click", {}))
    assert queue.cached("s-child", "browser_click", {})
    assert queue.cached("s-grand", "browser_click", {})
    assert not queue.cached("s-routine", "browser_click", {})
    assert not queue.cached("s-child", "shell", {"command": "rm -rf x"})


def test_a_definition_cannot_widen_its_childs_mode() -> None:
    assert stricter_mode("plan", "accept") == "plan"
    assert stricter_mode("accept", "auto") == "accept"
    assert stricter_mode("auto", "accept") == "accept"


def test_a_delegation_lead_gets_three_direct_calls_then_must_delegate(tmp_path: Path) -> None:
    """The lead kept implementing everything itself under /delegation on."""
    from snowpea_core.agent import loop

    settings = Settings()
    core = SimpleNamespace(settings=settings)
    lead = Session(id="s-lead", workdir=tmp_path)
    lead.delegation = True
    verdicts = [loop._lead_direct_refusal(core, lead, "patch", "write") for _ in range(4)]  # type: ignore[arg-type]
    assert verdicts[:3] == [None, None, None]
    assert verdicts[3] and "delegate_task" in verdicts[3]
    # Reads, delegation off, and subagents are never limited.
    assert loop._lead_direct_refusal(core, lead, "read_file", "read") is None  # type: ignore[arg-type]
    plain = Session(id="s-plain", workdir=tmp_path)
    assert all(
        loop._lead_direct_refusal(core, plain, "shell", "exec") is None for _ in range(5)  # type: ignore[arg-type]
    )
    child = Session(id="s-child", workdir=tmp_path, parent_session_id="s-lead", kind="subagent")
    child.delegation = True
    assert all(
        loop._lead_direct_refusal(core, child, "patch", "write") is None for _ in range(5)  # type: ignore[arg-type]
    )
