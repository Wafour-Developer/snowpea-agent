"""R3 (1.37.0): an image that leaves the window is a stub in the history itself.

``prune_request_images`` rewrote the older images of each request on the
copy only, so no request continued the one before it.  The stub now lives in
the stored history, and an ``ephemeral`` screenshot is shown once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from snowpea_core.providers.base import ChatMessage
from snowpea_core.session.compaction import persist_image_stubs, prune_request_images
from snowpea_core.session.history import History


def _image(name: str, *, ephemeral: bool = False) -> ChatMessage:
    block: dict[str, Any] = {"type": "image", "text": f"[image: {name}]", "name": name}
    if ephemeral:
        block["ephemeral"] = True
    return ChatMessage(role="user", content=[{"type": "text", "text": "(images)"}, block])


def _session(*messages: ChatMessage) -> Any:
    history = History()
    history.extend(list(messages))
    return SimpleNamespace(history=history)


def _kinds(session: Any) -> list[str]:
    out = []
    for message in session.history.messages:
        for block in message.content:
            out.append(block["type"])
    return [k for k in out if k == "image"]


def test_the_oldest_image_becomes_a_stub_in_the_stored_history() -> None:
    session = _session(_image("a"), _image("b"), _image("c"))
    assert persist_image_stubs(session, 2, 3) is True
    first = session.history.messages[0].content[1]
    assert first["type"] == "text"
    assert first["text"] == "[image: a] (not sent — at most 2 images per request)"
    assert len(_kinds(session)) == 2


def test_the_request_text_is_the_same_as_the_old_per_request_pruning() -> None:
    plain = [_image("a"), _image("b"), _image("c")]
    old = prune_request_images(list(plain), 2)[0].content[1]
    session = _session(*plain)
    persist_image_stubs(session, 2, 3)
    assert session.history.messages[0].content[1] == old


def test_a_second_pass_changes_nothing_so_later_requests_share_the_prefix() -> None:
    session = _session(_image("a"), _image("b"), _image("c"))
    persist_image_stubs(session, 2, 3)
    before = [m.content for m in session.history.messages]
    version = session.history.rewrite_version
    assert persist_image_stubs(session, 2, 3) is False
    assert [m.content for m in session.history.messages] == before
    assert session.history.rewrite_version == version


def test_an_ephemeral_screenshot_is_shown_once() -> None:
    shot = _image("shot", ephemeral=True)
    session = _session(shot)
    # The request that carries it has not gone out yet: it stays.
    assert persist_image_stubs(session, 0, 0) is False
    assert shot.content[1]["type"] == "image"
    # The next round start: it was in the last request, so it becomes a stub.
    assert persist_image_stubs(session, 0, 1) is True
    stub = session.history.messages[0].content[1]
    assert stub == {"type": "text", "text": "[image: shot] (shown once; not kept)"}


def test_an_ordinary_image_is_not_touched_without_a_cap() -> None:
    session = _session(_image("a"), _image("b"), _image("c"))
    assert persist_image_stubs(session, 0, 3) is False
    assert len(_kinds(session)) == 3


def test_only_the_changed_message_differs_so_the_cache_keeps_the_rest() -> None:
    session = _session(
        ChatMessage(role="user", content="start"),
        _image("a"),
        ChatMessage(role="assistant", content="ok"),
        _image("b"),
        _image("c"),
    )
    before = [m.content for m in session.history.messages]
    persist_image_stubs(session, 2, 5)
    after = [m.content for m in session.history.messages]
    changed = [i for i, (x, y) in enumerate(zip(before, after, strict=True)) if x != y]
    assert changed == [1]
