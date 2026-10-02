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
