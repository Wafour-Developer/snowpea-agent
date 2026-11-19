"""PDF to plain text for ``read_file`` and ``web_extract``.

Both tools used to hand the model the raw ``%PDF-1.x`` bytes.  The text is
extracted in the core's own Python environment with ``pypdf`` (pure Python,
so it ships inside the frozen desktop core as well).
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("snowpea.tools.pdf_text")

#: What every PDF file starts with.
PDF_MAGIC = b"%PDF-"
#: Default cap on the extracted text.
DEFAULT_MAX_CHARS = 100_000


def looks_like_pdf(data: bytes | str) -> bool:
    """``True`` when ``data`` starts like a PDF file (leading whitespace allowed)."""
    head = data[:1024]
    if isinstance(head, str):
        head = head.encode("latin-1", "replace")
    return head.lstrip()[: len(PDF_MAGIC)] == PDF_MAGIC


def pdf_to_text(data: bytes, *, source: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The text of each page under ``--- page N ---``, behind a header line.

    Never raises: a broken, encrypted or unreadable PDF gives a one-line
    explanation instead, so the model can tell the user rather than crash.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a core dependency
        return f"[{source} is a PDF; text extraction is unavailable (pypdf is not installed)]"
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many "encrypted" PDFs only restrict printing and open with an
            # empty password; only a real password stops here.
            try:
                if not reader.decrypt(""):
                    return f"[{source} is an encrypted PDF; it cannot be read without a password]"
            except Exception:  # noqa: BLE001 - unsupported encryption
                return f"[{source} is an encrypted PDF; it cannot be read without a password]"
        pages = reader.pages
        total = len(pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on bad input
        log.debug("pdf open failed for %s", source, exc_info=True)
        return f"[{source} looks like a PDF but could not be read: {type(exc).__name__}: {exc}]"

    header = f"[{source}: PDF converted to text, {total} page{'s' if total != 1 else ''}]"
    parts: list[str] = [header]
    used = len(header)
    for index in range(total):
        try:
            text = pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page must not lose the rest
            text = f"[page could not be read: {type(exc).__name__}]"
        block = f"--- page {index + 1} ---\n{text.strip()}"
        if used + len(block) + 1 > max_chars:
            room = max(0, max_chars - used - 1)
            if room > 200:
                parts.append(block[:room])
            parts.append(
                f"… [truncated at {max_chars} characters; pages {index + 1}–{total} "
                "not shown in full]"
            )
            break
        parts.append(block)
        used += len(block) + 1
    return "\n".join(parts)


__all__ = ["DEFAULT_MAX_CHARS", "PDF_MAGIC", "looks_like_pdf", "pdf_to_text"]
