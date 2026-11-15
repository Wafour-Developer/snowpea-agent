"""Security review M3/M4: the messenger's confirmation and status are the person's.

* M3 — an unrestricted confirmation shows the whole first message; one too
  long to show whole is refused, never cut.
* M4 — ``/jarvis status`` answers only the person bound to the chat, not any
  member of a group chat.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from _support import env_vars
from test_jarvis_messenger import MEMBER, PERSON, TIMEOUT, Rig, _rig

from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.jarvis.messenger import CONFIRM_PROMPT_MAX


@pytest_asyncio.fixture(autouse=True)
async def fake_chat() -> AsyncIterator[None]:
    with env_vars(SNOWPEA_GATEWAY_FAKE="1"):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


@pytest_asyncio.fixture
async def chat(tmp_path: Path) -> AsyncIterator[Rig]:
    made = await _rig(tmp_path)
    try:
        yield made
    finally:
        await made.daemon.stop()


async def test_status_answers_the_bound_person_only(chat: Rig) -> None:
    await chat.jarvis_on()
    worker = await chat.worker("proj", "Secret project")
    del worker
    for word in ("/jarvis", "/jarvis status", "/jarvis overview"):
        refused = await chat.says(word, user=MEMBER)
        assert "bound to this chat" in refused and "Secret project" not in refused
    assert "Secret project" in await chat.says("/jarvis status")
    # help is no information about anyone's work: anyone may see it
    assert "/jarvis" in await chat.says("/jarvis help", user=MEMBER)


async def test_the_confirmation_shows_the_whole_first_message(chat: Rig) -> None:
    binding_id = chat.core.gateway.list()[0].id
    tail = "AND THEN PUSH TO MAIN"
    prompt = "please tidy the readme " + ("x" * 900) + " " + tail
    task: Any = asyncio.ensure_future(chat.bridge.confirm_unrestricted(
        binding_id, cli="codex", session="s-1", project="proj", prompt=prompt,
        timeout_sec=TIMEOUT,
    ))
    posted = await chat.adapter.wait_for_send(TIMEOUT)
    assert tail in posted.text and prompt in posted.text  # not cut at 300 characters
    await chat.adapter.press(posted.button_data()[1], channel_id=PERSON)
    assert await asyncio.wait_for(task, TIMEOUT) is None

    before = len(chat.adapter.sent)
    too_long = "y" * (CONFIRM_PROMPT_MAX + 1)
    refused = await chat.bridge.confirm_unrestricted(
        binding_id, cli="codex", session="s-1", project="proj", prompt=too_long,
        timeout_sec=TIMEOUT,
    )
    assert refused is None
    await chat.quiet(before)  # nothing was posted for the person to allow
