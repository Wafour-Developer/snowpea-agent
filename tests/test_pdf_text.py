"""read_file and web_extract return a PDF's text, not its raw ``%PDF-`` bytes."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import http_util, web
from snowpea_core.tools.fs import read_file
from snowpea_core.tools.pdf_text import pdf_to_text
from snowpea_core.tools.registry import ToolContext


def make_pdf(pages: list[str]) -> bytes:
    """A minimal, valid PDF with one line of Helvetica text per page."""
    count = len(pages)
    font_id = 3 + 2 * count
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
            + f"] /Count {count} >>"
        ).encode(),
    ]
    for index, text in enumerate(pages):
        content_id = 4 + 2 * index
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    workdir = tmp_path / "work"
    workdir.mkdir()
    return ToolContext(
        session=Session(id="pdf", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )


def test_pages_are_marked_under_a_header() -> None:
    text = pdf_to_text(make_pdf(["Hello first", "Second page"]), source="a.pdf")
    lines = text.splitlines()
    assert lines[0] == "[a.pdf: PDF converted to text, 2 pages]"
    assert "--- page 1 ---" in lines and "--- page 2 ---" in lines
    assert "Hello first" in text and "Second page" in text
    assert "%PDF" not in text


def test_long_text_is_capped_with_a_note() -> None:
    pdf = make_pdf([f"page text {i}" for i in range(40)])
    text = pdf_to_text(pdf, source="b.pdf", max_chars=300)
    assert len(text) < 450
    assert "truncated at 300 characters" in text
    assert "--- page 40 ---" not in text


def test_a_broken_pdf_gives_a_message_not_a_crash() -> None:
    text = pdf_to_text(b"%PDF-1.6\n\x00\x01garbage", source="bad.pdf")
    assert text.startswith("[bad.pdf looks like a PDF but could not be read")


def test_an_encrypted_pdf_says_it_needs_a_password() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf(["secret"]))).pages:
        writer.add_page(page)
    writer.encrypt(user_password="pw", owner_password="owner", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    text = pdf_to_text(out.getvalue(), source="locked.pdf")
    assert text == "[locked.pdf is an encrypted PDF; it cannot be read without a password]"


async def test_read_file_returns_pdf_text(ctx: ToolContext) -> None:
    (ctx.session.workdir / "doc.pdf").write_bytes(make_pdf(["Quarterly report"]))
    result = await read_file(ctx, {"path": "doc.pdf"})
    assert result.ok, result.error
    assert result.output.startswith("[doc.pdf: PDF converted to text, 1 page]")
    assert "--- page 1 ---\nQuarterly report" in result.output


async def test_read_file_sniffs_a_pdf_without_the_suffix(ctx: ToolContext) -> None:
    (ctx.session.workdir / "download").write_bytes(make_pdf(["No suffix"]))
    result = await read_file(ctx, {"path": "download"})
    assert result.ok, result.error
    assert "No suffix" in result.output and "%PDF" not in result.output


async def test_a_text_file_named_pdf_reads_as_text(ctx: ToolContext) -> None:
    (ctx.session.workdir / "notes.pdf").write_text("just text\n", encoding="utf-8")
    result = await read_file(ctx, {"path": "notes.pdf"})
    assert result.ok and result.output == "just text\n"


def _client(handler: Any) -> Any:
    def factory(*, timeout: float = 20.0, guard_ssrf: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


async def test_web_extract_returns_pdf_text(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_pdf(["Fetched over http"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    monkeypatch.setattr(http_util, "new_client", _client(handler))
    monkeypatch.setattr(http_util, "is_safe_url", lambda url: True)
    result = await web.web_extract(ctx, {"url": "https://example.com/r.pdf"})
    assert result.ok, result.error
    assert result.output.startswith("[https://example.com/r.pdf: PDF converted to text, 1 page]")
    assert "Fetched over http" in result.output


async def test_web_extract_sniffs_pdf_served_as_octet_stream(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_pdf(["Octet"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "application/octet-stream"}
        )

    monkeypatch.setattr(http_util, "new_client", _client(handler))
    monkeypatch.setattr(http_util, "is_safe_url", lambda url: True)
    result = await web.web_extract(ctx, {"url": "https://example.com/dl"})
    assert result.ok and "Octet" in result.output and "%PDF" not in result.output
