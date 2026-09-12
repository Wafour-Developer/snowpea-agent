"""Attachment normalisation and the on-disk attachment store (CORE-multimodal)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from snowpea_core.attachments.model import (
    MAX_BYTES,
    OCTET_STREAM,
    Attachment,
    AttachmentError,
    decode_base64,
    downscale_image,
    extension_for,
    is_image,
    is_text,
    safe_name,
    sniff_mime,
)
from snowpea_core.attachments.store import AttachmentStore

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


# ---------------------------------------------------------------------------
# mime sniffing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (PDF, "application/pdf"),
        (b"hello world", "text/plain"),
        (b"\x00\x01\x02\xff\xfe", OCTET_STREAM),
        (b"", "text/plain"),
    ],
)
def test_sniff_mime_reads_the_magic_bytes(data: bytes, expected: str) -> None:
    assert sniff_mime(data) == expected


def test_sniff_mime_uses_the_name_only_to_pick_markdown() -> None:
    assert sniff_mime(b"# title", "notes.md") == "text/markdown"
    # A binary called .txt is still a binary.
    assert sniff_mime(b"\x00\xff\xfe\x01", "notes.txt") == OCTET_STREAM


def test_helpers() -> None:
    assert is_image("image/png") and not is_image("application/pdf")
    assert is_text("text/markdown") and not is_text("image/png")
    assert extension_for("image/jpeg") == "jpg"
    assert extension_for("application/zip", "bundle.zip") == "zip"
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("") == "attachment"


def test_decode_base64_accepts_a_data_uri() -> None:
    payload = base64.b64encode(b"hi").decode()
    assert decode_base64(payload) == b"hi"
    assert decode_base64(f"data:text/plain;base64,{payload}") == b"hi"
    with pytest.raises(AttachmentError):
        decode_base64("!!!not base64!!!!")


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_from_bytes_sniffs_and_hashes() -> None:
    item = Attachment.from_bytes("shot.png", PNG)
    assert item.mime == "image/png"
    assert item.size == len(PNG)
    assert item.is_image
    assert item.extension == "png"
    assert item.data == PNG
    assert item.path is None
    assert len(item.sha256) == 64
    assert item.describe() == "[image: shot.png]"


def test_from_bytes_ignores_a_lying_client_mime() -> None:
    item = Attachment.from_bytes("evil.png", b"\x00\x01\x02\xff", mime="image/png")
    assert item.mime == OCTET_STREAM


def test_declared_mime_survives_when_nothing_can_be_sniffed() -> None:
    item = Attachment.from_bytes("thing.bin", b"\x00\xff\xfe", mime="application/zip")
    assert item.mime == "application/zip"


def test_from_base64_round_trips() -> None:
    item = Attachment.from_base64("note.txt", base64.b64encode(b"hello").decode())
    assert item.mime == "text/plain"
    assert item.to_text() == "hello"
    assert item.to_base64() == base64.b64encode(b"hello").decode()
    assert item.describe() == "[file: note.txt]"


def test_from_path_stays_path_backed(tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF)
    item = Attachment.from_path(source)
    assert item.path == source
    assert item.data is None
    assert item.mime == "application/pdf"
    assert item.read_bytes() == PDF
    assert item.to_ref()["path"] == str(source)


def test_from_path_reports_a_missing_file() -> None:
    with pytest.raises(AttachmentError) as excinfo:
        Attachment.from_path("/definitely/not/here.png")
    assert excinfo.value.code == "invalid_params"


def test_size_cap_is_enforced_for_inline_bytes() -> None:
    with pytest.raises(AttachmentError) as excinfo:
        Attachment.from_bytes("huge.bin", b"x" * (MAX_BYTES + 1))
    assert excinfo.value.code == "attachment_too_large"


def test_size_cap_is_enforced_for_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("snowpea_core.attachments.model.MAX_BYTES", 8)
    source = tmp_path / "big.txt"
    source.write_bytes(b"0123456789")
    with pytest.raises(AttachmentError) as excinfo:
        Attachment.from_path(source)
    assert excinfo.value.code == "attachment_too_large"


def test_from_payload_takes_either_shape(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("body")
    by_path = Attachment.from_payload({"name": "a.txt", "path": str(source)})
    assert by_path.path == source
    inline = Attachment.from_payload(
        {"name": "b.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}
    )
    assert inline.mime == "image/png"
    with pytest.raises(AttachmentError):
        Attachment.from_payload({"name": "c.txt"})


def test_to_text_truncates() -> None:
    item = Attachment.from_bytes("long.txt", b"a" * 100)
    body = item.to_text(limit=10)
    assert body.startswith("a" * 10)
    assert "truncated" in body


def test_to_ref_never_carries_bytes() -> None:
    ref = Attachment.from_bytes("shot.png", PNG).to_ref()
    assert set(ref) == {"name", "mime", "size", "sha256"}


def test_downscale_is_a_pass_through_without_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_pillow(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("PIL"):
            raise ImportError("no pillow here")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    data, mime = downscale_image(PNG, "image/png")
    assert data is PNG
    assert mime == "image/png"


def test_downscale_shrinks_a_big_image_when_pillow_is_present() -> None:
    pillow = pytest.importorskip("PIL.Image")
    import io

    buffer = io.BytesIO()
    pillow.new("RGB", (4000, 1000), "red").save(buffer, format="PNG")
    original = buffer.getvalue()
    shrunk, mime = downscale_image(original, "image/png", max_edge=100)
    assert mime == "image/png"
    with pillow.open(io.BytesIO(shrunk)) as image:
        assert max(image.size) == 100


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_writes_under_the_session_and_dedupes(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    first = store.save("sess-1", Attachment.from_bytes("shot.png", PNG))
    assert first.path is not None
    assert first.data is None
    assert first.path.parent.name == "sess-1"
    assert first.path.name == f"{first.sha256}.png"
    assert first.path.read_bytes() == PNG

    again = store.save("sess-1", Attachment.from_bytes("other-name.png", PNG))
    assert again.path == first.path
    assert len(list(store.iter_files("sess-1"))) == 1


def test_store_leaves_path_backed_attachments_alone(tmp_path: Path) -> None:
    source = tmp_path / "on-disk.txt"
    source.write_text("hello")
    store = AttachmentStore(tmp_path / "attachments")
    saved = store.save("sess-1", Attachment.from_path(source))
    assert saved.path == source
    assert not (tmp_path / "attachments").exists()


def test_store_save_all_purge_and_size(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    saved = store.save_all(
        "sess-2",
        [Attachment.from_bytes("a.png", PNG), Attachment.from_bytes("b.txt", b"hello")],
    )
    assert len(saved) == 2
    assert store.total_bytes() == len(PNG) + 5
    assert store.purge("sess-2") == 2
    assert list(store.iter_files("sess-2")) == []
    assert store.purge("sess-2") == 0


def test_store_from_paths_lands_under_home(tmp_path: Path) -> None:
    from snowpea_core.config.paths import Paths

    store = AttachmentStore.from_paths(Paths(home=tmp_path))
    assert store.root == tmp_path / "attachments"
