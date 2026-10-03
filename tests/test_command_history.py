from __future__ import annotations

from types import SimpleNamespace

import pytest

from snowpea_core.commands.registry import CommandContext
from snowpea_core.session.history import History


class _Session:
    id = "s"

    def __init__(self) -> None:
        self.history = History()


@pytest.mark.asyncio
async def test_command_history_redacts_secret_args_and_output() -> None:
    session = _Session()
    ctx = CommandContext(
        core=SimpleNamespace(store=None),
        session=session,  # type: ignore[arg-type]
        turn_id="t",
        command_name="setup",
        command_args="--api-key sk-secret --token=tok-secret",
    )
    ctx.set_history_summary("Authorization: Bearer abcdefghijklmnop\npassword: hunter2")

    await ctx.record_history()

    text = "\n".join(str(message.content) for message in session.history.messages)
    assert "sk-secret" not in text
    assert "tok-secret" not in text
    assert "abcdefghijklmnop" not in text
    assert "hunter2" not in text
    assert "api-key ***" in text
    assert "token=***" in text
    assert "Authorization: ***" in text


@pytest.mark.asyncio
async def test_command_history_truncation_keeps_final_status() -> None:
    session = _Session()
    ctx = CommandContext(
        core=SimpleNamespace(store=None),
        session=session,  # type: ignore[arg-type]
        turn_id="t",
        command_name="verbose",
        command_args="",
    )
    ctx.set_history_summary("start\n" + ("x" * 7000) + "\nFINAL STATUS: failed story S9")

    await ctx.record_history(reason="error")

    assistant = session.history.messages[-1].content
    assert "start" in assistant
    assert "…[command output truncated]" in assistant
    assert "FINAL STATUS: failed story S9" in assistant


@pytest.mark.asyncio
async def test_workflow_progress_is_in_context_before_command_finishes() -> None:
    class Hub:
        async def emit_event(self, *_args):
            pass

    session = _Session()
    session.pending_notices = ["child a1: error; test failed: expected 2, got 1"]
    ctx = CommandContext(
        core=SimpleNamespace(store=None, hub=Hub()),
        session=session,
        turn_id="t",
        command_name="ralph",
        command_args="fix tests",
    )
    await ctx.say("iteration 1: FAIL — assertion mismatch")
    await ctx.say("iteration 2: PASS — pytest reports 1 passed")
    text = "\n".join(str(m.content) for m in session.history.snapshot())
    assert "expected 2, got 1" in text
    assert "iteration 1: FAIL" in text
    assert "iteration 2: PASS" in text
    await ctx.record_history()
    assert (
        sum(
            m.role == "user" and m.content == "/ralph fix tests" for m in session.history.snapshot()
        )
        == 1
    )


def test_history_redacts_json_credentials_without_hiding_tool_evidence() -> None:
    from snowpea_core.commands.registry import _redact_command_text

    text = _redact_command_text(
        '{"api_key": "private-key", "password": "escaped\\\"secret", '
        '"path": "tests/check.py", "output": "expected 2, got 1"}'
    )
    assert "private-key" not in text
    assert "escaped" not in text
    assert "tests/check.py" in text
    assert "expected 2, got 1" in text
