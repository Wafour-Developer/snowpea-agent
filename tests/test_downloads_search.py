"""glob and grep must see session downloads when path targets that folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.exec.local import LocalBackend
from snowpea_core.session.paths import DOWNLOADS
from snowpea_core.session.session import Session
from snowpea_core.tools import glob as glob_tools
from snowpea_core.tools import grep as grep_tools
from snowpea_core.tools.registry import ToolContext


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


@pytest.fixture
def ctx(workdir: Path) -> ToolContext:
    session = Session(id="s-dl", workdir=workdir)
    return ToolContext(session=session, core=None, backend=LocalBackend(workdir))  # type: ignore[arg-type]


def write(root: Path, rel: str, text: str = "", *, binary: bytes | None = None) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if binary is not None:
        target.write_bytes(binary)
    else:
        target.write_text(text, encoding="utf-8")


async def test_glob_in_downloads_finds_pdf(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, f"{DOWNLOADS}/report.pdf", binary=b"%PDF-1.4")
    write(workdir, f"{DOWNLOADS}/.gitignore", "*\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "*.pdf", "path": DOWNLOADS})
    assert result.ok, result.error
    assert f"{DOWNLOADS}/report.pdf" in result.output


async def test_grep_in_downloads_finds_text(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, f"{DOWNLOADS}/notes.txt", "invoice-number-42\n")
    write(workdir, f"{DOWNLOADS}/.gitignore", "*\n")
    result = await grep_tools.grep(ctx, {"pattern": "invoice-number", "path": DOWNLOADS})
    assert result.ok, result.error
    assert f"{DOWNLOADS}/notes.txt:1:invoice-number-42" in result.output


async def test_glob_in_downloads_subfolder(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, f"{DOWNLOADS}/sub/deck.pdf", binary=b"%PDF")
    write(workdir, f"{DOWNLOADS}/.gitignore", "*\n")
    result = await glob_tools.glob_tool(
        ctx, {"pattern": "*.pdf", "path": f"{DOWNLOADS}/sub"}
    )
    assert result.ok, result.error
    assert f"{DOWNLOADS}/sub/deck.pdf" in result.output


async def test_project_wide_glob_skips_downloads_and_names_folder(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, f"{DOWNLOADS}/hidden.pdf", binary=b"%PDF")
    write(workdir, f"{DOWNLOADS}/.gitignore", "*\n")
    write(workdir, "visible.txt", "ok\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "**/*", "path": "."})
    assert result.ok, result.error
    assert "hidden.pdf" not in result.output
    assert "visible.txt" in result.output
    assert ".snowpea-downloads/" in result.output
    assert "path=.snowpea-downloads" in result.output


async def test_glob_in_nested_results_downloads(ctx: ToolContext, workdir: Path) -> None:
    nested = f"results/{DOWNLOADS}"
    write(workdir, f"{nested}/deck.pdf", binary=b"%PDF")
    write(workdir, f"{nested}/.gitignore", "*\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "*.pdf", "path": nested})
    assert result.ok, result.error
    assert f"{nested}/deck.pdf" in result.output


async def test_grep_in_nested_results_downloads(ctx: ToolContext, workdir: Path) -> None:
    nested = f"results/{DOWNLOADS}"
    write(workdir, f"{nested}/memo.txt", "nested-download-token\n")
    write(workdir, f"{nested}/.gitignore", "*\n")
    result = await grep_tools.grep(ctx, {"pattern": "nested-download", "path": nested})
    assert result.ok, result.error
    assert f"{nested}/memo.txt:1:nested-download-token" in result.output


async def test_project_wide_glob_skips_nested_downloads_and_names_path(
    ctx: ToolContext, workdir: Path
) -> None:
    nested = f"results/{DOWNLOADS}"
    write(workdir, f"{nested}/secret.pdf", binary=b"%PDF")
    write(workdir, f"{nested}/.gitignore", "*\n")
    write(workdir, "ok.txt", "here\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "**/*", "path": "."})
    assert result.ok, result.error
    assert "secret.pdf" not in result.output
    assert "ok.txt" in result.output
    assert f"path={nested}" in result.output
    assert f"{nested}/" in result.output


@pytest.fixture
def external_downloads(tmp_path: Path) -> Path:
    folder = tmp_path / "Snowpea" / "Default" / DOWNLOADS
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def external_ctx(workdir: Path, external_downloads: Path) -> ToolContext:
    session = Session(id="s-ext", workdir=workdir)
    return ToolContext(session=session, core=None, backend=LocalBackend(workdir))  # type: ignore[arg-type]


async def test_glob_outside_workdir_downloads_absolute(
    external_ctx: ToolContext, external_downloads: Path
) -> None:
    (external_downloads / ".gitignore").write_text("*\n", encoding="utf-8")
    (external_downloads / "ext.pdf").write_bytes(b"%PDF")
    target = str(external_downloads)
    result = await glob_tools.glob_tool(external_ctx, {"pattern": "*.pdf", "path": target})
    assert result.ok, result.error
    assert f"{target}/ext.pdf" in result.output or "ext.pdf" in result.output


async def test_grep_outside_workdir_downloads_absolute(
    external_ctx: ToolContext, external_downloads: Path
) -> None:
    (external_downloads / ".gitignore").write_text("*\n", encoding="utf-8")
    (external_downloads / "line.txt").write_text("outside-workdir-hit\n", encoding="utf-8")
    target = str(external_downloads)
    result = await grep_tools.grep(
        external_ctx, {"pattern": "outside-workdir", "path": target}
    )
    assert result.ok, result.error
    assert "line.txt:1:outside-workdir-hit" in result.output


async def test_glob_outside_workdir_downloads_subfolder_absolute(
    external_ctx: ToolContext, external_downloads: Path
) -> None:
    sub = external_downloads / "inbox"
    sub.mkdir()
    (external_downloads / ".gitignore").write_text("*\n", encoding="utf-8")
    (sub / "item.pdf").write_bytes(b"%PDF")
    target = str(sub)
    result = await glob_tools.glob_tool(external_ctx, {"pattern": "*.pdf", "path": target})
    assert result.ok, result.error
    assert "item.pdf" in result.output


async def test_grep_outside_workdir_downloads_subfolder_absolute(
    external_ctx: ToolContext, external_downloads: Path
) -> None:
    sub = external_downloads / "inbox"
    sub.mkdir()
    (external_downloads / ".gitignore").write_text("*\n", encoding="utf-8")
    (sub / "deep.txt").write_text("subfolder-hit\n", encoding="utf-8")
    target = str(sub)
    result = await grep_tools.grep(external_ctx, {"pattern": "subfolder-hit", "path": target})
    assert result.ok, result.error
    assert "deep.txt:1:subfolder-hit" in result.output


async def test_nested_gitignore_inside_downloads_still_applies(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, f"{DOWNLOADS}/.gitignore", "*\n")
    write(workdir, f"{DOWNLOADS}/keep.txt", "show-me\n")
    write(workdir, f"{DOWNLOADS}/private/.gitignore", "secret/\n")
    write(workdir, f"{DOWNLOADS}/private/secret/quiet.txt", "hush\n")
    write(workdir, f"{DOWNLOADS}/private/visible.txt", "here\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "**/*.txt", "path": DOWNLOADS})
    assert result.ok, result.error
    assert f"{DOWNLOADS}/keep.txt" in result.output
    assert f"{DOWNLOADS}/private/visible.txt" in result.output
    assert "quiet.txt" not in result.output


async def test_custom_downloads_gitignore_is_not_treated_as_seal(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, f"{DOWNLOADS}/.gitignore", "drop/\n")
    write(workdir, f"{DOWNLOADS}/drop/x.txt", "gone\n")
    write(workdir, f"{DOWNLOADS}/stay.txt", "stay\n")
    result = await glob_tools.glob_tool(ctx, {"pattern": "**/*.txt", "path": DOWNLOADS})
    assert result.ok, result.error
    assert f"{DOWNLOADS}/stay.txt" in result.output
    assert "gone" not in result.output