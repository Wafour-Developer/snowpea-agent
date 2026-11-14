"""Runtime context goes to the model as one snapshot, only when it changed (M16 P1.2)."""

from __future__ import annotations

from pathlib import Path

from snowpea_core.agent import runtime_context as rc
from snowpea_core.session.session import Session


def _session(tmp_path: Path, **kw: object) -> Session:
    return Session(id="s-rc", workdir=tmp_path, **kw)  # type: ignore[arg-type]


def _registry(*providers: rc.ContextProvider) -> rc.ContextRegistry:
    registry = rc.ContextRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def test_sent_once_then_again_only_after_a_change(tmp_path: Path) -> None:
    value = {"text": "Branch: main"}
    registry = _registry(rc.ContextProvider("git", 50, lambda c, s: value["text"]))
    session = _session(tmp_path)
    first = rc.take_due(None, session, registry=registry)
    assert first.startswith(rc.OPEN_TAG) and "Branch: main" in first and rc.HEADING in first
    assert rc.take_due(None, session, registry=registry) == ""
    value["text"] = "Branch: feature"
    assert "Branch: feature" in rc.take_due(None, session, registry=registry)


def test_items_keep_their_order_not_their_registration_order(tmp_path: Path) -> None:
    registry = _registry(
        rc.ContextProvider("late", 900, lambda c, s: "LATE"),
        rc.ContextProvider("early", 10, lambda c, s: "EARLY"),
    )
    text = rc.take_due(None, _session(tmp_path), registry=registry)
    assert text.index("EARLY") < text.index("LATE")


def test_a_fact_that_goes_away_is_cleared_once(tmp_path: Path) -> None:
    value = {"text": "Current plan: X"}
    registry = _registry(rc.ContextProvider("plan", 100, lambda c, s: value["text"]))
    session = _session(tmp_path)
    rc.take_due(None, session, registry=registry)
    value["text"] = ""
    assert rc.CLEARED in rc.take_due(None, session, registry=registry)
    assert rc.take_due(None, session, registry=registry) == ""


def test_nothing_is_sent_for_a_context_the_model_never_had(tmp_path: Path) -> None:
    registry = _registry(rc.ContextProvider("plan", 100, lambda c, s: ""))
    assert rc.take_due(None, _session(tmp_path), registry=registry) == ""


def test_a_compaction_makes_it_due_again(tmp_path: Path) -> None:
    registry = _registry(rc.ContextProvider("git", 50, lambda c, s: "Branch: main"))
    session = _session(tmp_path)
    rc.take_due(None, session, registry=registry)
    session.history.compactions += 1
    assert "Branch: main" in rc.take_due(None, session, registry=registry)


def test_a_broken_provider_is_skipped(tmp_path: Path) -> None:
    def boom(core: object, session: object) -> str:
        raise RuntimeError("no")

    registry = _registry(
        rc.ContextProvider("bad", 1, boom), rc.ContextProvider("ok", 2, lambda c, s: "fine")
    )
    assert "fine" in rc.take_due(None, _session(tmp_path), registry=registry)


def test_a_plugin_provider_leaves_with_its_disposer(tmp_path: Path) -> None:
    registry = rc.default_registry()
    dispose = registry.register(rc.ContextProvider("plugin:ticket", 500, lambda c, s: "Ticket: 42"))
    session = _session(tmp_path)
    assert "Ticket: 42" in rc.take_due(None, session, registry=registry)
    dispose()
    assert rc.CLEARED in rc.take_due(None, session, registry=registry)


def test_the_built_in_plan_auto_note_follows_the_mode(tmp_path: Path) -> None:
    from snowpea_core.agent.agent import PLAN_AUTO_NOTE

    session = _session(tmp_path, mode="plan")
    session.auto_after_plan = True
    assert PLAN_AUTO_NOTE in rc.take_due(None, session)
    child = _session(tmp_path, mode="plan", is_subagent=True)
    child.auto_after_plan = True
    assert rc.take_due(None, child) == ""


def test_the_summary_does_not_count_a_snapshot_as_the_users_words(tmp_path: Path) -> None:
    from snowpea_core.providers.base import ChatMessage
    from snowpea_core.session.compaction import _verbatim_entries

    messages = [
        ChatMessage(role="user", content="fix the build"),
        ChatMessage(
            role="user",
            content=rc.render((("plan", "Current plan: X"),)),
            name=rc.MESSAGE_NAME,
        ),
    ]
    assert _verbatim_entries(messages) == ["fix the build"]
