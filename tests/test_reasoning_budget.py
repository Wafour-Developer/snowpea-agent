"""Hidden reasoning, the output budget and what happens when it runs out.

A Qwen3-family server behind an OpenAI-compatible endpoint streams its
thinking as ``delta.reasoning`` and charges it to ``max_tokens``.  With the old
4096-token budget and no reader for those deltas, a review-shaped prompt came
back empty (the whole budget went on thinking) or stopped mid-sentence, and
nothing downstream noticed (CORE-reasoning-budget).

The parsing half runs against the normaliser directly; the loop half runs a
real in-process daemon with the scripted fake provider, so the assertions are
about what a TUI would actually see.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import Recorder, env_vars

from snowpea_core.agent.definition import (
    AgentDefinition,
    parse_agent_md,
    parse_agent_text,
    write_definition,
)
from snowpea_core.agent.loop import CONTINUE_INSTRUCTION, MAX_CONTINUATIONS, agent_config, run_turn
from snowpea_core.agent.subagent import TRUNCATED_MARK, SubagentManager, get_manager
from snowpea_core.config.settings import DEFAULT_MAX_TOKENS, Settings
from snowpea_core.providers import context_windows
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.normalize import OpenAIStreamNormalizer, build_openai_request
from snowpea_core.providers.presets import PRESETS
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.session.session import Session

# ---------------------------------------------------------------------------
# the wire: reasoning deltas and reasoning tokens
# ---------------------------------------------------------------------------


def _feed(chunks: list[dict[str, Any]]) -> list[Any]:
    normalizer = OpenAIStreamNormalizer(PRESETS["local"])
    events: list[Any] = []
    for chunk in chunks:
        events.extend(normalizer.feed(chunk))
    events.extend(normalizer.finish())
    return events


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
def test_reasoning_deltas_are_their_own_event(key: str) -> None:
    """Both spellings become ``reasoning_delta``, never ``text_delta``."""
    events = _feed(
        [
            {"choices": [{"delta": {key: "let me think"}}]},
            {"choices": [{"delta": {"content": "the answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    kinds = [event.kind for event in events]
    assert kinds[:2] == ["reasoning_delta", "text_delta"]
    assert [e.text for e in events if e.kind == "reasoning_delta"] == ["let me think"]
    assert [e.text for e in events if e.kind == "text_delta"] == ["the answer"]


def test_usage_carries_reasoning_tokens() -> None:
    """``completion_tokens_details.reasoning_tokens`` reaches ``Usage``."""
    events = _feed(
        [
            {"choices": [{"delta": {"reasoning": "…"}, "finish_reason": "length"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 400,
                    "completion_tokens_details": {"reasoning_tokens": 400},
                },
            },
        ]
    )
    usage = next(event.usage for event in events if event.kind == "usage")
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (1200, 400, 400)
    done = next(event for event in events if event.kind == "done")
    assert done.stop_reason == "max_tokens"


def test_thinking_off_is_the_only_thing_on_the_wire() -> None:
    """``chat_template_kwargs`` appears for ``off`` and for nothing else."""
    args = (PRESETS["local"], "flash-next-mtp", [], [])
    assert "chat_template_kwargs" not in build_openai_request(*args, max_tokens=16384)
    assert "chat_template_kwargs" not in build_openai_request(
        *args, max_tokens=16384, thinking="on"
    )
    body = build_openai_request(*args, max_tokens=16384, thinking="off")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------


def test_the_default_budget_is_sixteen_k() -> None:
    assert Settings().agent.max_tokens == DEFAULT_MAX_TOKENS == 16384


def test_the_budget_is_clamped_to_what_a_model_accepts() -> None:
    """A model with a published ceiling below the budget gets the ceiling."""
    assert context_windows.clamp_output_tokens("gpt-4o", 16384) == 16384
    assert context_windows.clamp_output_tokens("gpt-4", 16384) == 8192
    assert context_windows.clamp_output_tokens("deepseek-chat", 16384) == 8192
    # An unknown model is not clamped at all: a guess would truncate answers.
    assert context_windows.clamp_output_tokens("flash-next-mtp", 16384) == 16384
    assert context_windows.max_output_tokens("flash-next-mtp") is None


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    """A daemon with no vendor configured; each test registers its own fake."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    with env_vars(SNOWPEA_PROVIDER=None):
        instance = Daemon(port=0, home=home)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.stop()


async def _session(core: Core, tmp_path: Path, script: dict[str, Any], **kwargs: Any) -> Any:
    """A session whose provider is one scripted :class:`FakeProvider` instance.

    Registering the instance (rather than pointing ``SNOWPEA_PROVIDER`` at a
    file) is what lets a test read back the ``max_tokens`` and ``thinking`` of
    every call the loop made.
    """
    workdir = tmp_path / "project"
    workdir.mkdir(parents=True, exist_ok=True)
    core.providers.register("local", FakeProvider(script))
    return await core.sessions.create(workdir, mode="auto", provider="local", **kwargs)


def _watch(core: Core, session: Any) -> Recorder:
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    return recorder


async def test_reasoning_is_published_and_never_stored(daemon: Daemon, tmp_path: Path) -> None:
    """The thinking reaches the surface as ``message.reasoning`` and stops there."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {
            "steps": [{"match": "review", "reasoning": "weighing it up", "text": "LGTM"}],
            "default": {"text": ""},
        },
    )
    recorder = _watch(core, session)

    await run_turn(core, session, "review this")

    reasoning = recorder.of_kind("message.reasoning")
    assert reasoning, "the thinking was dropped"
    assert "".join(str(e["payload"]["text"]) for e in reasoning) == "weighing it up"
    assert reasoning[-1]["payload"]["chars"] == len("weighing it up")

    history = "\n".join(str(m.content) for m in session.history.snapshot())
    assert "weighing it up" not in history
    assert recorder.texts().strip() == "LGTM"


async def test_a_long_think_is_published_in_windows_not_per_fragment(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Fragments inside one window become one event, and nothing is lost.

    The provider streams thinking eight characters at a time, and every event
    published is a repaint in every attached surface.  A minutes-long think
    therefore has to reach the wire as a handful of events carrying the same
    text and the same running total, not as one event per fragment — that rate
    is what made the TUI flicker through a long turn.
    """
    core = daemon.core
    assert core is not None
    thought = "so then I considered the alternative and rejected it. " * 12
    session = await _session(
        core,
        tmp_path,
        {
            "steps": [{"match": "plan", "reasoning": thought, "text": "done"}],
            "default": {"text": ""},
        },
    )
    recorder = _watch(core, session)

    await run_turn(core, session, "plan this")

    published = recorder.of_kind("message.reasoning")
    fragments = -(-len(thought) // 8)  # what the fake provider streams
    assert fragments > 50, "the fixture is too short to say anything about the rate"
    # Nothing dropped, nothing reordered, and the total is still the total.
    assert "".join(str(e["payload"]["text"]) for e in published) == thought
    assert published[-1]["payload"]["chars"] == len(thought)
    # Every fragment lands inside one window here, so this is the floor: the
    # leading fragment, then one flush.
    assert len(published) <= 5, f"{len(published)} events for {fragments} fragments"


async def test_a_budget_spent_on_thinking_is_retried_without_it(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Empty text + reasoning tokens + ``max_tokens`` -> one retry, thinking off."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {
            "steps": [
                {
                    "match": "review",
                    "reasoning": "…",
                    "reasoningTokens": 400,
                    "stopReason": "length",
                    "text": "",
                },
                {"match": "review", "thinking": "off", "text": "the review, in words"},
            ],
            "default": {"text": ""},
        },
    )
    recorder = _watch(core, session)

    await run_turn(core, session, "review this")

    provider = core.providers.get("local")
    assert [thinking for _, thinking in provider.calls] == ["on", "off"]
    assert [budget for budget, _ in provider.calls] == [DEFAULT_MAX_TOKENS] * 2
    done = recorder.of_kind("message.done")[-1]["payload"]
    assert done["text"] == "the review, in words"
    assert done["truncated"] is False


async def test_without_a_thinking_switch_the_budget_is_doubled(
    daemon: Daemon, tmp_path: Path
) -> None:
    """A provider with no switch gets the other recovery: twice the budget."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {
            "steps": [
                {
                    "match": "review",
                    "reasoningTokens": 400,
                    "stopReason": "length",
                    "text": "",
                }
            ],
            "default": {"text": "the review"},
        },
    )
    provider = core.providers.get("local")
    provider.supports_thinking_option = False

    await run_turn(core, session, "review this")

    assert [budget for budget, _ in provider.calls] == [
        DEFAULT_MAX_TOKENS,
        DEFAULT_MAX_TOKENS * 2,
    ]


async def test_a_cut_off_answer_is_continued_and_joined(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Partial text + ``max_tokens`` -> one continuation, and the halves join."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {
            "steps": [{"match": "review", "stopReason": "length", "text": "first half "}],
            "default": {"text": "second half"},
        },
    )
    recorder = _watch(core, session)

    await run_turn(core, session, "review this")

    done = recorder.of_kind("message.done")[-1]["payload"]
    assert done["text"] == "first half second half"
    assert done["continuations"] == 1
    assert done["truncated"] is False
    assert str(session.history.snapshot()[-1].content) == "first half second half"


async def test_continuations_are_capped_and_the_stop_is_reported(
    daemon: Daemon, tmp_path: Path
) -> None:
    """A model that never stops is given up on, and the message says so."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {"steps": [], "default": {"stopReason": "length", "text": "on and on "}},
    )
    recorder = _watch(core, session)

    await run_turn(core, session, "review this")

    done = recorder.of_kind("message.done")[-1]["payload"]
    assert done["continuations"] == MAX_CONTINUATIONS
    assert done["truncated"] is True
    assert done["text"] == "on and on " * (MAX_CONTINUATIONS + 1)


async def test_the_continuation_instruction_is_what_the_model_is_told(
    daemon: Daemon, tmp_path: Path
) -> None:
    """The resumed request carries the partial answer and the instruction."""
    core = daemon.core
    assert core is not None
    session = await _session(
        core,
        tmp_path,
        {"steps": [{"match": "review", "stopReason": "length", "text": "half "}]},
    )
    provider = core.providers.get("local")
    seen: list[list[Any]] = []
    original = provider.stream

    def record(messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
        seen.append(list(messages))
        return original(messages, tools, **kwargs)

    provider.stream = record  # type: ignore[method-assign]
    await run_turn(core, session, "review this")

    assert len(seen) >= 2
    resumed = seen[1]
    assert resumed[-1].role == "user"
    assert str(resumed[-1].content) == CONTINUE_INSTRUCTION
    assert resumed[-2].role == "assistant"
    assert str(resumed[-2].content) == "half "
    # One assistant message, holding the joined answer — the partial one was
    # sent to the model but never stored.
    stored = [m for m in session.history.snapshot() if m.role == "assistant"]
    assert len(stored) == 1


async def test_a_truncated_delegation_says_so_in_its_summary(
    daemon: Daemon, tmp_path: Path
) -> None:
    """``delegate_task`` never hands back a cut-off review as a whole one."""
    core = daemon.core
    assert core is not None
    parent = await _session(
        core,
        tmp_path,
        {"steps": [], "default": {"stopReason": "length", "text": "partial "}},
    )

    result = await get_manager(core).run(parent, "review the plan")

    assert result.ok
    assert result.summary.startswith("partial ")
    assert result.summary.endswith(TRUNCATED_MARK.format(count=MAX_CONTINUATIONS))


# ---------------------------------------------------------------------------
# who thinks
# ---------------------------------------------------------------------------


async def test_auto_thinks_for_a_person_and_not_for_a_delegate(
    daemon: Daemon, tmp_path: Path
) -> None:
    """``"auto"`` is on in the session someone is watching, off in a child."""
    core = daemon.core
    assert core is not None
    session = await _session(core, tmp_path, {"steps": [], "default": {"text": "ok"}})
    assert core.settings.agent.thinking == "auto"
    assert agent_config(core, session).thinking == "on"

    session.is_subagent = True
    assert agent_config(core, session).thinking == "off"

    # …unless the definition asked for it by name.
    session.thinking = "on"
    assert agent_config(core, session).thinking == "on"


def test_a_definition_can_ask_a_delegate_to_think(tmp_path: Path) -> None:
    """``thinking: on`` survives the agent .md round trip and reaches the child."""
    defn = AgentDefinition(name="thinker", description="thinks out loud", thinking="on")
    path = write_definition(defn, tmp_path)
    assert "thinking: on" in path.read_text(encoding="utf-8")
    assert parse_agent_md(path).thinking == "on"
    # A definition that says nothing inherits, which for a child means off.
    assert parse_agent_text("---\nname: quiet\n---\nbody").thinking == "inherit"

    child = Session(id="s-1", workdir=tmp_path)
    SubagentManager.__new__(SubagentManager)._apply_definition(child, defn, None)
    assert child.is_subagent is True
    assert child.thinking == "on"


def test_the_budget_follows_the_vendor_block() -> None:
    """``providers.<vendor>.max_tokens`` beats the global setting, then clamps."""
    settings = Settings()
    settings.providers["local"] = {"model": "flash-next-mtp", "max_tokens": 32768}
    settings.providers["openai"] = {"model": "gpt-4"}
    registry = ProviderRegistry(settings)

    assert registry.max_tokens_for("local") == 32768
    # gpt-4 caps at 8192, whatever the setting says.
    assert registry.max_tokens_for("openai") == 8192
    assert registry.thinking_for("local") == "auto"

    settings.providers["local"]["thinking"] = "off"
    assert registry.thinking_for("local") == "off"
    settings.providers["local"]["thinking"] = "nonsense"
    assert registry.thinking_for("local") == "auto"
