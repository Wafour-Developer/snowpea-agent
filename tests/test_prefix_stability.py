"""Every request extends the one before it, byte for byte (prefix caching).

llm.snowpea.ai reused only the first 12,858 tokens of each ~95k-token request:
the git status sat in the system prompt and was re-rendered every round, so an
edit changed message 0 and vLLM prefilled the whole conversation again (44-54 s
a round).  The same happened, less often, when tool_search added a schema to
the tool list (a chat template renders the tools first), when a new turn
dropped the last turn's reasoning, when the per-turn recall/plan line changed
the system prompt, and on every round once old tool outputs began to be
stubbed one round at a time.  What turn N sends must be what turn N+1 replays.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent.loop import run_turn
from snowpea_core.providers.base import ChatMessage, ToolCall, ToolSpec
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.normalize import build_openai_request
from snowpea_core.providers.presets import PRESETS
from snowpea_core.server.app_server import Daemon
from snowpea_core.session import compaction


def _call(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "arguments": args}]


SCRIPT = {
    "steps": [
        {"match": "fix the bug", "reasoning": "look first",
         "tool_calls": _call("read_file", {"path": "app.py"}), "text": ""},
        {"after_tool": "read_file", "reasoning": "search",
         "tool_calls": _call("grep", {"pattern": "def ", "path": "."}), "text": ""},
        {"after_tool": "grep", "reasoning": "write it",
         "tool_calls": _call("write_file", {"path": "test_app.py", "content": "import app\n"}),
         "text": ""},
        {"after_tool": "write_file", "reasoning": "find more tools",
         "tool_calls": _call("tool_search", {"query": "browser"}), "text": ""},
        {"after_tool": "tool_search", "text": "Fixed."},
        {"match": "and again", "tool_calls": _call("shell", {"command": "true"}), "text": ""},
        {"after_tool": "shell", "text": "Done."},
    ],
    "default": {"text": "ok"},
}


class Recording(FakeProvider):
    """The scripted fake, keeping each request as a chat template would see it."""

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__(script)
        self.replays_reasoning = True
        self.requests: list[list[str]] = []

    def stream(self, messages: list[ChatMessage], tools: list[ToolSpec], **kwargs: Any):  # type: ignore[override]
        body = build_openai_request(PRESETS["local"], "m", messages, tools, max_tokens=64)
        # The template renders the tools first, then each message in order.
        self.requests.append(
            [json.dumps(body.get("tools", []), sort_keys=True)]
            + [json.dumps(m, sort_keys=True, ensure_ascii=False) for m in body["messages"]]
        )
        return super().stream(messages, tools, **kwargs)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    with env_vars(SNOWPEA_PROVIDER=None):
        instance = Daemon(port=0, home=home)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.stop()


async def test_each_request_is_a_prefix_of_the_next(daemon: Daemon, tmp_path: Path) -> None:
    core = daemon.core
    assert core is not None
    work = tmp_path / "project"
    work.mkdir()
    (work / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(
        "git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm init",
        shell=True, cwd=work, check=True,
    )
    fake = Recording(SCRIPT)
    core.providers.register("local", fake)
    # Registered as "local", so the session counts as a self-hosted server.
    session = await core.sessions.create(work, mode="auto", provider="local")
    await run_turn(core, session, "fix the bug in app.py")
    await run_turn(core, session, "and again")

    assert len(fake.requests) >= 7
    for n in range(1, len(fake.requests)):
        before, after = fake.requests[n - 1], fake.requests[n]
        changed = [i for i, (a, b) in enumerate(zip(before, after, strict=False)) if a != b]
        assert not changed, (
            f"request {n} rewrote unit {changed[0]} of the one before:\n"
            f"  was: {before[changed[0]][:300]}\n  now: {after[changed[0]][:300]}"
        )
        assert len(after) >= len(before)


def _rounds(count: int) -> list[ChatMessage]:
    history: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    for i in range(count):
        call_id = f"c{i}"
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(
                    id=call_id, name="shell", arguments={"command": f"echo {i}"}
                )],
            )
        )
        history.append(
            ChatMessage(
                role="tool", content=f"output {i} " * 50, tool_call_id=call_id, name="shell"
            )
        )
    return history


def test_old_outputs_are_stubbed_a_window_at_a_time() -> None:
    keep = 6
    previous: list[ChatMessage] | None = None
    moves = 0
    for count in range(1, 40):
        pruned = compaction.prune_old_tool_outputs(_rounds(count), keep)
        tools = [m for m in pruned if m.role == "tool"]
        kept = sum(1 for m in tools if m.content.startswith("output"))
        assert keep <= kept < 2 * keep or count < keep, (count, kept)
        if previous is not None:
            same = all(a.content == b.content for a, b in zip(previous, pruned, strict=False))
            moves += not same
        previous = pruned
    # The boundary moved only every ``keep`` rounds, not every round.
    assert moves <= 40 // keep
