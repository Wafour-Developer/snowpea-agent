"""A cut-off screenshot must not wedge a session (browser report, core 0.2.72).

``adb exec-out screencap -p > x.png`` on Windows can leave a file with a valid
PNG signature and nothing decodable after it.  The magic bytes passed, the
model server refused the request, and the picture stayed in the history.
"""

from __future__ import annotations

import base64
import struct
import zlib
from types import SimpleNamespace
from typing import Any

from snowpea_core.attachments.model import validate_image
from snowpea_core.providers import content
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.openai_compat import _is_image_decode_rejection
from snowpea_core.session.compaction import stub_unreadable_images
from snowpea_core.session.history import History
from snowpea_core.tools.host_tools import result_from_answer


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def good_png(width: int = 2, height: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def test_a_whole_png_passes_and_the_cut_one_does_not() -> None:
    png = good_png()
    assert validate_image(png, "image/png") is None
    assert validate_image(png[:8], "image/png") is not None  # the report: signature only
    assert validate_image(png[:40], "image/png") is not None  # IHDR, then cut inside IDAT
    assert validate_image(png[:-12], "image/png") is not None  # no IEND
    assert validate_image(b"", "image/png") == "empty file"


def test_a_corrupt_header_checksum_is_caught() -> None:
    png = bytearray(good_png())
    png[16] ^= 0x01  # width changes, the CRC no longer matches
    assert "checksum" in (validate_image(bytes(png), "image/png") or "")


def test_jpeg_gif_webp_cut_short() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xc0\x00\x11" + b"\x00" * 30 + b"\xff\xd9"
    assert validate_image(jpeg, "image/jpeg") is None
    assert validate_image(jpeg[:-2], "image/jpeg") is not None
    gif = b"GIF89a" + b"\x00" * 20 + b";"
    assert validate_image(gif, "image/gif") is None
    assert validate_image(gif[:-1], "image/gif") is not None
    webp = b"RIFF" + struct.pack("<I", 100) + b"WEBPVP8 " + b"\x00" * 20
    assert validate_image(webp, "image/webp") is not None
    assert validate_image(b"BM" + b"\x00" * 20, "image/bmp") is None  # unknown types pass


def test_a_broken_stored_image_is_sent_as_words() -> None:
    block = {
        "type": "image", "name": "c2_search.png", "mime": "image/png",
        "data": base64.b64encode(good_png()[:8]).decode(),
    }
    parts = content.parts_from_blocks([block])
    assert all(isinstance(part, content.TextPart) for part in parts)
    assert "c2_search.png not sent" in parts[0].text and "8 bytes" in parts[0].text


def test_a_good_stored_image_is_still_sent() -> None:
    block = {
        "type": "image", "name": "ok.png", "mime": "image/png",
        "data": base64.b64encode(good_png()).decode(),
    }
    assert isinstance(content.parts_from_blocks([block])[0], content.ImagePart)


def test_a_host_tool_image_that_is_cut_off_is_dropped_with_a_note() -> None:
    bad = base64.b64encode(good_png()[:30]).decode()
    good = base64.b64encode(good_png()).decode()
    result = result_from_answer(
        "browser_screenshot",
        {"ok": True, "content": [
            {"type": "image", "data": bad, "mediaType": "image/png"},
            {"type": "image", "data": good, "mediaType": "image/png"},
        ]},
    )
    assert len(result.meta["images"]) == 1
    assert "not used" in result.output


def test_the_servers_complaint_is_recognised() -> None:
    message = '{"message":"Failed to load image: cannot identify image file"}'
    assert _is_image_decode_rejection(message)
    assert not _is_image_decode_rejection("maximum context length exceeded")


def _session(*blocks: dict[str, Any]) -> Any:
    history = History()
    history.extend([ChatMessage(role="user", content=[{"type": "text", "text": "x"}, *blocks])])
    return SimpleNamespace(history=history)


def test_stubbing_replaces_only_the_unreadable_image() -> None:
    bad = {"type": "image", "name": "bad.png", "mime": "image/png", "size": 8,
           "data": base64.b64encode(good_png()[:8]).decode()}
    ok = {"type": "image", "name": "ok.png", "mime": "image/png",
          "data": base64.b64encode(good_png()).decode()}
    session = _session(bad, ok)
    assert stub_unreadable_images(session) == 1
    blocks = session.history.messages[0].content
    assert blocks[1]["type"] == "text" and "bad.png" in blocks[1]["text"]
    assert blocks[2]["type"] == "image"
    assert stub_unreadable_images(session) == 1  # nothing broken left: the newest goes


def test_with_nothing_identifiable_the_newest_image_goes() -> None:
    a = {"type": "image", "name": "a.png", "mime": "image/png",
         "data": base64.b64encode(good_png()).decode()}
    b = {"type": "image", "name": "b.png", "mime": "image/png",
         "data": base64.b64encode(good_png()).decode()}
    session = _session(a, b)
    assert stub_unreadable_images(session) == 1
    blocks = session.history.messages[0].content
    assert blocks[1]["type"] == "image" and blocks[2]["type"] == "text"


class _Rejecting:
    vendor = "test-image"

    def __init__(self, times: int) -> None:
        self.times = times
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        from snowpea_core.providers.base import ProviderError, StreamEvent

        self.calls += 1
        if self.calls <= self.times:
            raise ProviderError("image_rejected", "HTTP 400: Failed to load image")
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _turn(tmp_path: Any, provider: Any) -> tuple[Any, Any, Any]:
    import asyncio

    from _support import Recorder, make_daemon

    from snowpea_core.agent import loop as agent_loop

    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    core.providers.get = lambda _p, _m: provider
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = await core.sessions.create(workdir, mode="auto")
    # Valid to look at, refused by the server anyway.
    session.history.append(ChatMessage(role="user", content=[
        {"type": "text", "text": "look"},
        {"type": "image", "name": "p.png", "mime": "image/png",
         "data": base64.b64encode(good_png()).decode()},
    ]))
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    agent_loop.start_turn(core, session, "hello")
    await asyncio.wait_for(session.turn_task, timeout=20)
    return daemon, session, recorder


async def test_a_decode_rejection_stubs_the_image_and_the_turn_goes_on(tmp_path: Any) -> None:
    provider = _Rejecting(1)
    daemon, session, recorder = await _turn(tmp_path, provider)
    try:
        assert provider.calls == 2
        assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
        assert not any(
            isinstance(b, dict) and b.get("type") == "image"
            for m in session.history.messages if isinstance(m.content, list)
            for b in m.content
        )
    finally:
        await daemon.stop()


async def test_a_second_rejection_surfaces_with_its_own_code(tmp_path: Any) -> None:
    provider = _Rejecting(5)
    daemon, _, recorder = await _turn(tmp_path, provider)
    try:
        assert provider.calls == 2
        assert recorder.of_kind("error")[-1]["payload"]["code"] == "image_rejected"
    finally:
        await daemon.stop()
