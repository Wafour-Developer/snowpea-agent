"""Drop older image blocks from local VL requests (2-image server limits)."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent.agent import build_messages
from snowpea_core.attachments.model import Attachment
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import content
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server.app_server import Core
from snowpea_core.session import compaction
from snowpea_core.session.session import Session

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454E44ae426082"
)


def _image_message(label: str, path: Path) -> ChatMessage:
    attachment = Attachment.from_path(path)
    return ChatMessage(
        role="user",
        content=content.history_blocks(f"see {label}", [attachment]),
    )


def test_only_the_two_newest_images_are_sent(tmp_path: Path) -> None:
    paths = [tmp_path / f"{name}.png" for name in ("a", "b", "c")]
    for path in paths:
        path.write_bytes(PNG)
    history = [
        _image_message("a", paths[0]),
        ChatMessage(role="assistant", content="ok"),
        _image_message("b", paths[1]),
        ChatMessage(role="assistant", content="ok"),
        _image_message("c", paths[2]),
    ]
    pruned = compaction.prune_request_images(history, max_images=2)
    images = 0
    markers = 0
    for message in pruned:
        if not content.has_blocks(message.content):
            continue
        for block in message.content:  # type: ignore[union-attr]
            if block.get("type") == "image":
                images += 1
            if block.get("type") == "text" and "not sent" in str(block.get("text", "")):
                markers += 1
    assert images == 2
    assert markers == 1


@pytest.fixture
def core(tmp_path: Path) -> Core:
    settings = Settings(
        providers={
            "local": {
                "baseUrl": "http://127.0.0.1:8000/v1",
                "models": ["qwen38-flash-next"],
            }
        }
    )
    return Core(settings=settings, paths=Paths.create(tmp_path / "home"), token="t")


def test_build_messages_prunes_for_local_vision_sessions(tmp_path: Path, core: Core) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    for name in ("a.png", "b.png", "c.png"):
        (workdir / name).write_bytes(PNG)
    session = Session(
        id="s-prune",
        workdir=workdir,
        provider="local",
        model="qwen38-flash-next",
    )
    session.history.append(_image_message("a", workdir / "a.png"))
    session.history.append(ChatMessage(role="assistant", content="ok"))
    session.history.append(_image_message("b", workdir / "b.png"))
    session.history.append(ChatMessage(role="assistant", content="ok"))
    session.history.append(_image_message("c", workdir / "c.png"))

    messages = build_messages(session, [], core=core)
    sent = 0
    for message in messages:
        if not content.has_blocks(message.content):
            continue
        for block in message.content:  # type: ignore[union-attr]
            if block.get("type") == "image":
                sent += 1
    assert sent == 2
    # Stored history still has every image block.
    stored = sum(
        1
        for message in session.history.snapshot()
        if content.has_blocks(message.content)
        for block in message.content  # type: ignore[union-attr]
        if block.get("type") == "image"
    )
    assert stored == 3
