"""The on-disk store behind session.toolContent (tools/tool_content_store.py)."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest

from snowpea_core.tools.tool_content_store import MAX_EDGE, ToolContentStore

Image = pytest.importorskip("PIL.Image")


def screenshot(width: int = 2560, height: int = 1440) -> str:
    """A noisy PNG, so it is big enough for a JPEG to win."""
    image = Image.effect_noise((width, height), 64).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def blocks(data: str) -> list[dict[str, str]]:
    return [
        {"type": "text", "text": "the page"},
        {"type": "image", "mediaType": "image/png", "data": data},
    ]


def test_a_screenshot_is_kept_as_a_smaller_jpeg(tmp_path: Path) -> None:
    store = ToolContentStore(tmp_path)
    original = screenshot()
    assert store.save("s-1", "call_1", blocks(original))
    loaded = store.load("s-1", "call_1")
    assert loaded is not None
    text, image = loaded
    assert text == {"type": "text", "text": "the page"}
    assert image["mediaType"] == "image/jpeg"
    data = base64.b64decode(image["data"])
    assert len(data) < len(base64.b64decode(original))
    with Image.open(io.BytesIO(data)) as decoded:
        assert max(decoded.size) == MAX_EDGE


def test_results_without_an_image_are_not_kept(tmp_path: Path) -> None:
    store = ToolContentStore(tmp_path)
    assert not store.save("s-1", "call_1", [{"type": "text", "text": "only text"}])
    assert store.load("s-1", "call_1") is None


def test_ids_that_are_not_plain_names_are_refused(tmp_path: Path) -> None:
    store = ToolContentStore(tmp_path / "store")
    assert not store.save("../escape", "call_1", blocks(screenshot(64, 64)))
    assert not store.save("s-1", "../call", blocks(screenshot(64, 64)))
    assert store.load("..", "call_1") is None
    assert not (tmp_path / "escape").exists()


def test_the_oldest_results_go_first_past_the_limit(tmp_path: Path) -> None:
    probe = ToolContentStore(tmp_path / "probe")
    probe.save("s-1", "call_1", blocks(screenshot()))
    one = sum(path.stat().st_size for path in (tmp_path / "probe").rglob("*") if path.is_file())

    store = ToolContentStore(tmp_path / "store", limit_bytes=int(one * 2.5))
    # call_1-2 shares call_1's prefix: evicting call_1 must not touch it.
    for index, call_id in enumerate(["call_1", "call_1-2", "call_3"]):
        assert store.save("s-1", call_id, blocks(screenshot()))
        record = tmp_path / "store" / "s-1" / f"{call_id}.json"
        os.utime(record, (1_000_000 + index, 1_000_000 + index))
    assert store.save("s-2", "call_4", blocks(screenshot()))

    assert store.load("s-1", "call_1") is None
    assert store.load("s-1", "call_1-2") is None
    assert store.load("s-1", "call_3") is not None
    assert store.load("s-2", "call_4") is not None
    assert not list((tmp_path / "store" / "s-1").glob("call_1-0.*"))


def test_purge_drops_a_whole_session(tmp_path: Path) -> None:
    store = ToolContentStore(tmp_path)
    store.save("s-1", "call_1", blocks(screenshot(64, 64)))
    store.save("s-2", "call_1", blocks(screenshot(64, 64)))
    store.purge("s-1")
    assert store.load("s-1", "call_1") is None
    assert store.load("s-2", "call_1") is not None
    # A write that was still in flight when the session went stays out.
    assert not store.save("s-1", "call_2", blocks(screenshot(64, 64)))
    assert not (tmp_path / "s-1").exists()


def test_a_call_id_with_a_colon_is_kept(tmp_path: Path) -> None:
    store = ToolContentStore(tmp_path)
    assert store.save("s-1", "toolu:01", blocks(screenshot(64, 64)))
    assert store.load("s-1", "toolu:01") is not None
