"""Context accounting that keeps a long turn inside the window.

Covers what a 2-hour turn against a strict 262144-token server needed: the used
count includes what was added since the provider last reported, the threshold
leaves room for the reply, one result cannot swamp the window, and a refusal
teaches the real window (a hosted-family default never stands in for a server
nobody could ask).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.registry import UNVERIFIED_LOCAL_WINDOW
from snowpea_core.session import compaction
from snowpea_core.session.history import History, estimate_messages
from snowpea_core.session.session import Session


def _session(tmp_path: Any) -> Session:
    return Session(id="s-acct", workdir=tmp_path)


def test_tokens_added_since_the_report_are_counted(tmp_path: Any) -> None:
    session = _session(tmp_path)
    session.history.replace([ChatMessage(role="user", content="go")])
    compaction.record_provider_usage(session, 1000, sent_estimate=500)  # ratio 2.0
    big = ChatMessage(role="tool", content="x" * 40_000, tool_call_id="c", name="read_file")
    session.history.append(big)
    core = SimpleNamespace(providers=SimpleNamespace(context_window=lambda *a, **k: 262144))
    core.providers.default_vendor = lambda: "local"
    state = compaction.state_for(core, session)  # type: ignore[arg-type]
    added = estimate_messages([big])
    assert state.used == 1000 + int(added * 2.0)
    assert state.estimated is False


def test_the_ratio_is_clamped(tmp_path: Any) -> None:
    session = _session(tmp_path)
    compaction.record_provider_usage(session, 10_000, sent_estimate=10)
    assert session.context_ratio == 2.0
    compaction.record_provider_usage(session, 5, sent_estimate=1000)
    assert session.context_ratio == 1.0


def test_one_huge_result_is_trimmed_to_a_share_of_the_window() -> None:
    log = ChatMessage(role="tool", content="L" * 202_089, tool_call_id="c", name="read_file")
    small = ChatMessage(role="tool", content="ok", tool_call_id="d", name="shell")
    out = compaction.cap_single_results([log, small], 262144)
    assert len(out[0].content) <= int(262144 * 0.4) + 50
    assert out[1] is small
    # The stored message is untouched, and an unknown window changes nothing.
    assert len(log.content) == 202_089
    assert compaction.cap_single_results([log], None)[0] is log


def test_a_refusal_teaches_the_window_and_the_ratio(tmp_path: Any) -> None:
    learned: list[tuple[str, int]] = []
    providers = SimpleNamespace(
        default_vendor=lambda: "sg",
        learn_context_window=lambda vendor, model, window: learned.append((vendor, window)),
    )
    core = SimpleNamespace(providers=providers)
    session = _session(tmp_path)
    exc = agent_loop.ProviderError(
        "internal",
        "HTTP 400: prompt (246410) + max tokens (16384) exceeds the context (262144)",
    )
    sent = [ChatMessage(role="user", content="y" * 4 * 100_000)]
    agent_loop._learn_overflow(core, session, exc, sent)  # type: ignore[arg-type]
    assert learned == [("sg", 262144)]
    assert session.context_window == 262144
    assert session.context_ratio > 1.0


def test_an_unaskable_local_server_does_not_inherit_a_hosted_one_million() -> None:
    from snowpea_core.providers.registry import ProviderRegistry

    settings = Settings()
    settings.providers["sgw"] = {
        "preset": "local",
        "base_url": "http://127.0.0.1:9/v1",
        "model": "deepseek-v4.1-flash",
    }
    registry = ProviderRegistry(settings)
    assert registry.is_local_style("sgw")
    window = registry.context_window("sgw", "deepseek-v4.1-flash")
    assert window is not None and window <= UNVERIFIED_LOCAL_WINDOW


def test_the_threshold_leaves_room_for_the_reply(tmp_path: Any) -> None:
    settings = Settings()
    core = SimpleNamespace(
        settings=settings,
        providers=SimpleNamespace(
            default_vendor=lambda: "local", max_tokens_for=lambda *a, **k: 16384
        ),
    )
    session = _session(tmp_path)
    session.history = History()
    session.history.replace([ChatMessage(role="user", content="a")] * 10)
    # 83% used would not trip an 85% threshold alone; with 16k reserved it does.
    window = 262144
    state = compaction.ContextState(used=int(window * 0.80), window=window, estimated=False)
    assert compaction.should_auto_compact(core, session, state)  # type: ignore[arg-type]
    low = compaction.ContextState(used=int(window * 0.5), window=window, estimated=False)
    assert not compaction.should_auto_compact(core, session, low)  # type: ignore[arg-type]
