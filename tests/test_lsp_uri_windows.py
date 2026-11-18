"""LSP file URIs: right on Windows (pyright refused ``file://///C:/...``)."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from snowpea_core.lsp.client import path_to_uri, uri_to_path, windows_uri_path


def test_the_old_windows_uri_shape_is_gone() -> None:
    # pathname2url("C:\\x") is "///C:/x"; "file://" + that was the bug.
    import nturl2path

    assert ("file://" + nturl2path.pathname2url(r"C:\Users\sg\backend")).startswith("file://///")
    assert windows_uri_path("file:///C:/Users/sg/backend") == str(
        PureWindowsPath(r"C:\Users\sg\backend"))


def test_a_windows_uri_maps_back_to_the_drive_path() -> None:
    assert windows_uri_path("file:///C:/Users/sg/a%20b/x.py") == r"C:\Users\sg\a b\x.py"
    assert windows_uri_path("file:///c%3A/Users/x.py") == r"c:\Users\x.py"


def test_a_unc_uri_maps_to_a_share() -> None:
    assert windows_uri_path("file://server/share/dir/x.py") == r"\\server\share\dir\x.py"
    assert windows_uri_path("file://localhost/C:/x.py") == r"C:\x.py"


def test_other_schemes_are_none() -> None:
    assert windows_uri_path("untitled:Untitled-1") is None
    assert uri_to_path("untitled:Untitled-1") is None


def test_a_posix_path_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "a b" / "x.py"
    uri = path_to_uri(target)
    assert uri.startswith("file:///") and "%20" in uri
    assert uri_to_path(uri) == target.resolve()
