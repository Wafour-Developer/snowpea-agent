"""``file.complete`` path matching and safety."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, init_repo, make_daemon

from snowpea_core.agent import file_complete
from snowpea_core.server.app_server import Daemon


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


async def _session(client, workdir: Path) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    result = await client.ok("session.create", {"workdir": str(workdir)})
    return str(result["sessionId"])


@pytest.mark.asyncio
async def test_empty_query_lists_top_level(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "top"
    workdir.mkdir()
    (workdir / "alpha.txt").write_text("a", encoding="utf-8")
    (workdir / "src").mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            result = await client.ok(
                "file.complete", {"sessionId": session_id, "query": ""}
            )
        finally:
            await client.stop()
    paths = {entry["path"] for entry in result["entries"]}
    assert "alpha.txt" in paths
    assert "src/" in paths


@pytest.mark.asyncio
async def test_fuzzy_subsequence_order(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "fuzzy"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "app.py").write_text("x", encoding="utf-8")
    (workdir / "src" / "api.py").write_text("x", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            result = await client.ok(
                "file.complete", {"sessionId": session_id, "query": "ap"}
            )
        finally:
            await client.stop()
    paths = [entry["path"] for entry in result["entries"]]
    assert paths.index("src/api.py") < paths.index("src/app.py")


@pytest.mark.asyncio
async def test_dir_prefix_completion(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "prefix"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "main.py").write_text("x", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            result = await client.ok(
                "file.complete", {"sessionId": session_id, "query": "src/m"}
            )
        finally:
            await client.stop()
    assert any(entry["path"] == "src/main.py" for entry in result["entries"])


@pytest.mark.asyncio
async def test_gitignore_is_respected(daemon: Daemon, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "ignored").mkdir()
    repo = init_repo(
        repo_root,
        {
            ".gitignore": "ignored/\n",
            "tracked.txt": "ok",
        },
    )
    (repo_root / "ignored" / "hidden.txt").write_text("no", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, repo)
            result = await client.ok(
                "file.complete", {"sessionId": session_id, "query": ""}
            )
        finally:
            await client.stop()
    paths = {entry["path"] for entry in result["entries"]}
    assert "tracked.txt" in paths
    assert not any("ignored" in path for path in paths)


@pytest.mark.asyncio
async def test_hidden_only_when_query_starts_with_dot(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "hidden"
    workdir.mkdir()
    (workdir / ".env").write_text("secret", encoding="utf-8")
    (workdir / "visible.txt").write_text("ok", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            plain = await client.ok(
                "file.complete", {"sessionId": session_id, "query": ""}
            )
            dotted = await client.ok(
                "file.complete", {"sessionId": session_id, "query": ".e"}
            )
        finally:
            await client.stop()
    plain_paths = {entry["path"] for entry in plain["entries"]}
    dotted_paths = {entry["path"] for entry in dotted["entries"]}
    assert ".env" not in plain_paths
    assert ".env" in dotted_paths


@pytest.mark.asyncio
async def test_limit_and_truncated(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "many"
    workdir.mkdir()
    for index in range(10):
        (workdir / f"f{index}.txt").write_text("x", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            result = await client.ok(
                "file.complete",
                {"sessionId": session_id, "query": "", "limit": 3},
            )
        finally:
            await client.stop()
    assert len(result["entries"]) == 3
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_escape_refused(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "escape"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            frame = await client.call(
                "file.complete",
                {"sessionId": session_id, "query": "../outside"},
            )
        finally:
            await client.stop()
    error = frame.get("error") or {}
    code = error.get("data", {}).get("code") or error.get("code")
    assert code in {"invalid_params", -32602}


def test_refuse_escape_unit(tmp_path: Path) -> None:
    workdir = tmp_path / "w"
    workdir.mkdir()
    assert file_complete.refuse_escape(workdir, "../etc") is True
    assert file_complete.refuse_escape(workdir, "/etc/passwd") is False


def test_absolute_and_home_queries_complete_outside_the_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``@/abs/…`` and ``@~/…`` list that directory, spelled the way they were typed."""
    workdir = tmp_path / "w"
    workdir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "pics").mkdir(parents=True)
    (elsewhere / "notes.md").write_text("x", encoding="utf-8")
    (elsewhere / "id_rsa").write_text("secret", encoding="utf-8")
    (elsewhere / ".hidden").write_text("h", encoding="utf-8")

    entries, _ = file_complete.complete_paths(workdir, f"{elsewhere}/")
    paths = [e.path for e in entries]
    assert paths == [f"{elsewhere}/pics/", f"{elsewhere}/notes.md"]  # no key, no dotfile

    entries, _ = file_complete.complete_paths(workdir, f"{elsewhere}/no")
    assert [e.path for e in entries] == [f"{elsewhere}/notes.md"]

    monkeypatch.setenv("HOME", str(tmp_path))
    entries, _ = file_complete.complete_paths(workdir, "~/else")
    assert [e.path for e in entries] == ["~/elsewhere/"]
    entries, _ = file_complete.complete_paths(workdir, "~/elsewhere/.h")
    assert [e.path for e in entries] == ["~/elsewhere/.hidden"]

    assert file_complete.complete_paths(workdir, "/no/such/dir/")[0] == []
