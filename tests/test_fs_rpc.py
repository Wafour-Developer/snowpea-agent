"""``fs.*`` (protocol 1.40.0): files on the daemon's machine, confined to the session's roots."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import RpcClient, connect, make_daemon

from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

MIB = 1024 * 1024
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and modes")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Env:
    def __init__(self, daemon: Daemon, client: RpcClient, session: dict[str, Any], work: Path):
        self.daemon = daemon
        self.client = client
        self.session_id: str = session["sessionId"]
        self.workspace = Path(session["workspaceDir"])
        self.work = work

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        return await self.client.call(method, {"sessionId": self.session_id, **params})

    async def ok(self, method: str, **params: Any) -> dict[str, Any]:
        return await self.client.ok(method, {"sessionId": self.session_id, **params})

    async def error(self, method: str, **params: Any) -> str:
        frame = await self.call(method, **params)
        assert frame.get("error"), frame
        return str(frame["error"]["data"]["code"])


@pytest.fixture
async def env(tmp_path: Path, http: aiohttp.ClientSession) -> AsyncIterator[Env]:
    daemon = await make_daemon(tmp_path / "home", {"memory": {"enabled": False}})
    work = (tmp_path / "work").resolve()
    work.mkdir()
    client = await connect(http, daemon)
    try:
        session = await client.ok("session.create", {"workdir": str(work)})
        yield Env(daemon, client, session, work)
    finally:
        await client.stop()
        await daemon.stop()


async def test_the_capability_and_the_version_are_announced(env: Env) -> None:
    assert "fs" in CAPABILITIES
    assert PROTOCOL_VERSION == "1.40.0"
    for method in ("fs.stat", "fs.list", "fs.glob", "fs.read", "fs.write", "fs.hash"):
        frame = await env.call(method, path=".")
        assert frame.get("error", {}).get("data", {}).get("code") != "method_not_found"


async def test_an_unknown_session_is_not_found(env: Env) -> None:
    frame = await env.client.call("fs.stat", {"sessionId": "nope", "path": "."})
    assert frame["error"]["data"]["code"] == "not_found"


# -- stat ---------------------------------------------------------------------


async def test_stat_describes_files_dirs_and_missing_paths(env: Env) -> None:
    (env.work / "a.txt").write_bytes(b"hello")
    (env.work / "sub").mkdir()
    stat = await env.ok("fs.stat", path="a.txt")
    assert (stat["type"], stat["size"], stat["nlink"], stat["isLink"]) == ("file", 5, 1, False)
    assert stat["mtimeMs"] == pytest.approx((env.work / "a.txt").stat().st_mtime * 1000, abs=2)
    assert stat["path"] == str(env.work / "a.txt")
    assert (await env.ok("fs.stat", path=str(env.work / "sub")))["type"] == "dir"
    assert await env.error("fs.stat", path="missing.txt") == "not_found"


@posix_only
async def test_stat_reports_a_link_and_its_hard_links(env: Env) -> None:
    (env.work / "a.txt").write_bytes(b"x")
    os.symlink("a.txt", env.work / "inside")
    os.symlink("/etc", env.work / "away")
    os.link(env.work / "a.txt", env.work / "twin.txt")
    inside = await env.ok("fs.stat", path="inside")
    assert (inside["isLink"], inside["type"]) == (True, "file")  # target inside a root
    away = await env.ok("fs.stat", path="away")
    assert (away["isLink"], away["type"]) == (True, "other")  # target outside: not described
    assert (await env.ok("fs.stat", path="twin.txt"))["nlink"] == 2


# -- roots ----------------------------------------------------------------------


async def test_a_path_outside_every_root_is_refused(env: Env, tmp_path: Path) -> None:
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("no")
    for method in ("fs.stat", "fs.read", "fs.hash"):
        assert await env.error(method, path=str(secret)) == "outside_root"
        assert await env.error(method, path="../elsewhere.txt") == "outside_root"
        assert await env.error(method, path="sub/../../elsewhere.txt") == "outside_root"
    assert await env.error("fs.list", path="..") == "outside_root"
    assert await env.error("fs.write", path="../x.txt", dataBase64=b64(b"x"), mode="create") == (
        "outside_root"
    )
    assert not (tmp_path / "x.txt").exists()


@posix_only
async def test_a_symlink_that_leaves_the_root_is_refused(env: Env, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("secret")
    os.symlink(outside, env.work / "dirlink")
    os.symlink(outside / "f.txt", env.work / "filelink")
    assert await env.error("fs.read", path="filelink") == "outside_root"
    assert await env.error("fs.read", path="dirlink/f.txt") == "outside_root"
    assert await env.error("fs.list", path="dirlink") == "outside_root"
    assert await env.error("fs.hash", path="filelink") == "outside_root"
    # listing the work dir shows the links but follows none
    names = {e["path"]: e for e in (await env.ok("fs.list", path="."))["entries"]}
    assert names["filelink"]["isLink"] is True


async def test_the_workspace_and_the_attachments_are_roots(env: Env) -> None:
    artifact = env.workspace / "artifacts" / "r.md"
    artifact.write_text("# r")
    got = await env.ok("fs.read", path=str(artifact))
    assert base64.b64decode(got["dataBase64"]) == b"# r"
    # an attachment of this session's root conversation
    from snowpea_core.attachments.store import AttachmentStore

    files = AttachmentStore.from_paths(env.daemon.core.paths).files_dir(env.session_id)  # type: ignore[union-attr]
    files.mkdir(parents=True)
    (files / "up.png").write_bytes(b"png")
    assert (await env.ok("fs.stat", path=str(files / "up.png")))["size"] == 3
    # another conversation's attachments are not
    other = files.parent.parent / "someone-else" / "files"
    other.mkdir(parents=True)
    (other / "x.png").write_bytes(b"x")
    assert await env.error("fs.read", path=str(other / "x.png")) == "protected"
    assert await env.error("fs.stat", path=str(other / "x.png")) == "outside_root"


# -- protected ------------------------------------------------------------------


async def test_snowpea_state_and_secrets_are_protected_whatever_the_root(env: Env) -> None:
    home = env.daemon.paths.home
    (home / "settings.json").write_text("{}")
    (home / "credentials.json").write_text("{}")
    (home / "mcp-oauth.json").write_text("{}")
    for name in ("settings.json", "credentials.json", "mcp-oauth.json", "token"):
        assert await env.error("fs.read", path=str(home / name)) == "protected"
        assert await env.error("fs.hash", path=str(home / name)) == "protected"
        assert (
            await env.error(
                "fs.write", path=str(home / name), dataBase64=b64(b"x"), mode="overwrite"
            )
            == "protected"
        )
    assert "{}" == (home / "settings.json").read_text()
    # secret-looking files inside the project
    for name in (".env", ".env.local", "id_rsa", "server.pem", "api.key"):
        (env.work / name).write_text("s")
        assert await env.error("fs.read", path=name) == "protected"
        assert (
            await env.error("fs.write", path=name, dataBase64=b64(b"x"), mode="overwrite")
            == "protected"
        )
        assert (env.work / name).read_text() == "s"
    (env.work / ".ssh").mkdir()
    (env.work / ".ssh" / "config").write_text("s")
    assert await env.error("fs.read", path=".ssh/config") == "protected"
    # a project's own snowpea settings
    (env.work / ".snowpea").mkdir()
    (env.work / ".snowpea" / "settings.json").write_text("{}")
    assert await env.error("fs.read", path=".snowpea/settings.json") == "protected"
    # but its other files and ordinary files are fine
    (env.work / ".snowpea" / "notes.txt").write_text("n")
    assert (await env.ok("fs.stat", path=".snowpea/notes.txt"))["size"] == 1
    # metadata of a protected file is still available (names are not secret)
    assert (await env.ok("fs.stat", path=".env"))["type"] == "file"


# -- list / glob ----------------------------------------------------------------


def tree(work: Path) -> None:
    for rel in (
        "src/a.ts",
        "src/deep/b.ts",
        "src/deep/c.js",
        "README.md",
        "node_modules/x/i.ts",
        ".git/config",
    ):
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)


async def test_list_is_one_level_by_default_and_recursive_with_depth(env: Env) -> None:
    tree(env.work)
    top = await env.ok("fs.list", path=".")
    assert [e["path"] for e in top["entries"]] == [".git", "README.md", "node_modules", "src"]
    assert top["truncated"] is False and top["path"] == str(env.work)
    deep = await env.ok("fs.list", path="src", depth=3)
    assert [(e["path"], e["type"]) for e in deep["entries"]] == [
        ("a.ts", "file"),
        ("deep", "dir"),
        ("deep/b.ts", "file"),
        ("deep/c.js", "file"),
    ]
    assert deep["entries"][0]["size"] == len("src/a.ts")


async def test_list_is_capped_and_says_so(env: Env) -> None:
    for i in range(10):
        (env.work / f"f{i}").write_text("x")
    capped = await env.ok("fs.list", path=".", maxEntries=4)
    assert len(capped["entries"]) == 4 and capped["truncated"] is True
    exact = await env.ok("fs.list", path=".", maxEntries=10)
    assert len(exact["entries"]) == 10 and exact["truncated"] is False
    assert await env.error("fs.list", path="f1") == "invalid_params"  # not a directory
    assert await env.error("fs.list", path="nope") == "not_found"


async def test_glob_matches_names_and_double_star(env: Env) -> None:
    tree(env.work)
    ts = await env.ok("fs.glob", pattern="**/*.ts")
    assert [e["path"] for e in ts["entries"]] == ["src/a.ts", "src/deep/b.ts"]  # no node_modules
    here = await env.ok("fs.glob", pattern="src/*.ts")
    assert [e["path"] for e in here["entries"]] == ["src/a.ts"]
    rooted = await env.ok("fs.glob", path="src", pattern="deep/*")
    assert [e["path"] for e in rooted["entries"]] == ["deep/b.ts", "deep/c.js"]
    literal = await env.ok("fs.glob", pattern="node_modules/*/i.ts")
    assert [e["path"] for e in literal["entries"]] == ["node_modules/x/i.ts"]
    capped = await env.ok("fs.glob", pattern="**/*", maxEntries=2)
    assert len(capped["entries"]) == 2 and capped["truncated"] is True


@pytest.mark.parametrize("pattern", ["/etc/*", "../*", "a/../../b", ""])
async def test_glob_patterns_cannot_climb_or_be_absolute(env: Env, pattern: str) -> None:
    assert await env.error("fs.glob", pattern=pattern) == "invalid_params"


# -- read -------------------------------------------------------------------------


async def test_read_gives_bytes_ranges_and_eof(env: Env) -> None:
    data = bytes(range(256)) * 4
    (env.work / "bin").write_bytes(data)
    whole = await env.ok("fs.read", path="bin")
    assert base64.b64decode(whole["dataBase64"]) == data
    assert (whole["size"], whole["offset"], whole["length"], whole["eof"]) == (1024, 0, 1024, True)
    first = await env.ok("fs.read", path="bin", offset=0, length=100)
    assert (first["length"], first["eof"]) == (100, False)
    assert base64.b64decode(first["dataBase64"]) == data[:100]
    middle = await env.ok("fs.read", path="bin", offset=1000, length=100)
    assert base64.b64decode(middle["dataBase64"]) == data[1000:]
    assert (middle["length"], middle["eof"]) == (24, True)
    past = await env.ok("fs.read", path="bin", offset=5000)
    assert (past["length"], past["eof"], past["dataBase64"]) == (0, True, "")


async def test_read_over_the_limit_is_too_large_unless_ranged(env: Env) -> None:
    (env.work / "big").write_bytes(b"a" * (8 * MIB + 10))
    assert await env.error("fs.read", path="big") == "too_large"
    assert await env.error("fs.read", path="big", length=8 * MIB + 1) == "too_large"
    head = await env.ok("fs.read", path="big", length=8 * MIB)
    assert (head["length"], head["eof"]) == (8 * MIB, False)
    tail = await env.ok("fs.read", path="big", offset=8 * MIB)
    assert (tail["length"], tail["eof"]) == (10, True)
    (env.work / "mid").write_bytes(b"b" * 100)
    assert await env.error("fs.read", path="mid", maxBytes=50) == "too_large"
    assert (await env.ok("fs.read", path="mid", maxBytes=100))["length"] == 100
    # a bigger maxBytes is capped at 8 MiB, not honoured
    assert await env.error("fs.read", path="big", maxBytes=64 * MIB) == "too_large"


async def test_read_refuses_directories_and_missing_files(env: Env) -> None:
    (env.work / "d").mkdir()
    assert await env.error("fs.read", path="d") == "is_dir"
    assert await env.error("fs.hash", path="d") == "is_dir"
    assert await env.error("fs.read", path="gone") == "not_found"


@posix_only
async def test_read_does_not_hang_on_a_fifo(env: Env) -> None:
    os.mkfifo(env.work / "pipe")
    assert await env.error("fs.read", path="pipe") == "invalid_params"


# -- hash ---------------------------------------------------------------------------


async def test_hash_is_the_sha256_of_the_whole_file(env: Env) -> None:
    data = os.urandom(3 * MIB + 17)
    (env.work / "h.bin").write_bytes(data)
    got = await env.ok("fs.hash", path="h.bin")
    assert got["sha256"] == hashlib.sha256(data).hexdigest()
    assert got["size"] == len(data)


# -- write ----------------------------------------------------------------------------


async def test_create_makes_a_file_and_refuses_to_replace(env: Env) -> None:
    made = await env.ok("fs.write", path="n.txt", dataBase64=b64(b"one"), mode="create")
    assert (made["created"], made["size"]) == (True, 3)
    assert made["path"] == str(env.work / "n.txt")
    assert (env.work / "n.txt").read_bytes() == b"one"
    assert (
        await env.error("fs.write", path="n.txt", dataBase64=b64(b"two"), mode="create") == "exists"
    )
    assert (env.work / "n.txt").read_bytes() == b"one"


async def test_overwrite_replaces_and_keeps_the_mode(env: Env) -> None:
    path = env.work / "o.sh"
    path.write_bytes(b"old")
    if sys.platform != "win32":
        path.chmod(0o755)
    got = await env.ok("fs.write", path="o.sh", dataBase64=b64(b"new content"), mode="overwrite")
    assert (got["created"], got["size"]) == (False, 11)
    assert path.read_bytes() == b"new content"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o755
    fresh = await env.ok("fs.write", path="p.txt", dataBase64=b64(b"x"), mode="overwrite")
    assert fresh["created"] is True  # overwrite of nothing creates


async def test_write_creates_parents_inside_the_roots_only(env: Env, tmp_path: Path) -> None:
    await env.ok("fs.write", path="a/b/c.txt", dataBase64=b64(b"deep"), mode="create")
    assert (env.work / "a" / "b" / "c.txt").read_bytes() == b"deep"
    assert (
        await env.error("fs.write", path="a/../../esc/c.txt", dataBase64=b64(b"x"), mode="create")
        == "outside_root"
    )
    assert not (tmp_path / "esc").exists()
    # into the workspace
    target = env.workspace / "artifacts" / "sub" / "r.txt"
    await env.ok("fs.write", path=str(target), dataBase64=b64(b"r"), mode="create")
    assert target.read_bytes() == b"r"


async def test_write_into_a_directory_or_below_a_file_fails(env: Env) -> None:
    (env.work / "d").mkdir()
    (env.work / "f").write_text("x")
    assert await env.error("fs.write", path="d", dataBase64=b64(b"x"), mode="overwrite") == "is_dir"
    assert await env.error("fs.write", path="d", dataBase64=b64(b"x"), mode="create") == "is_dir"
    assert (
        await env.error("fs.write", path="f/child", dataBase64=b64(b"x"), mode="create")
        == "invalid_params"
    )


@posix_only
async def test_write_never_goes_through_a_symlink(env: Env, tmp_path: Path) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("untouched")
    os.symlink(target, env.work / "link")
    for mode in ("create", "overwrite"):
        assert (
            await env.error("fs.write", path="link", dataBase64=b64(b"pwn"), mode=mode)
            == "outside_root"
        )
    assert target.read_text() == "untouched"
    # a link that stays inside is refused as well: a write replaces the name, not the target
    (env.work / "real.txt").write_text("real")
    os.symlink("real.txt", env.work / "inner")
    assert (
        await env.error("fs.write", path="inner", dataBase64=b64(b"x"), mode="overwrite")
        == "outside_root"
    )
    assert (env.work / "real.txt").read_text() == "real"
    # a directory link that leaves the root
    os.symlink(tmp_path, env.work / "up")
    assert (
        await env.error("fs.write", path="up/new.txt", dataBase64=b64(b"x"), mode="create")
        == "outside_root"
    )
    assert not (tmp_path / "new.txt").exists()


@posix_only
async def test_write_refuses_a_file_with_other_hard_links(env: Env, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    os.link(victim, env.work / "hard.txt")  # a name in the project for a file outside it
    assert (
        await env.error("fs.write", path="hard.txt", dataBase64=b64(b"pwn"), mode="overwrite")
        == "outside_root"
    )
    assert victim.read_text() == "untouched"
    assert (env.work / "hard.txt").read_text() == "untouched"


async def test_write_size_limit(env: Env) -> None:
    ok = await env.ok("fs.write", path="edge.bin", dataBase64=b64(b"z" * (8 * MIB)), mode="create")
    assert ok["size"] == 8 * MIB
    assert (
        await env.error(
            "fs.write", path="over.bin", dataBase64=b64(b"z" * (8 * MIB + 1)), mode="create"
        )
        == "too_large"
    )
    assert (
        await env.error("fs.write", path="huge.bin", dataBase64="A" * (12 * MIB), mode="create")
        == "too_large"
    )
    assert not (env.work / "over.bin").exists() and not (env.work / "huge.bin").exists()
    assert (
        await env.error("fs.write", path="bad.bin", dataBase64="!!not base64!!", mode="create")
        == "invalid_params"
    )


async def test_write_is_atomic_no_temp_files_and_no_torn_content(env: Env) -> None:
    path = env.work / "atomic.txt"
    path.write_bytes(b"A" * 1000)
    # many overwrites racing with reads: a reader sees one whole version, never a mix
    import asyncio

    versions = [bytes([65 + i]) * 200_000 for i in range(6)]

    async def writer() -> None:
        for data in versions:
            await env.ok("fs.write", path="atomic.txt", dataBase64=b64(data), mode="overwrite")

    seen: set[bytes] = set()

    async def reader() -> None:
        for _ in range(40):
            got = await env.ok("fs.read", path="atomic.txt")
            data = base64.b64decode(got["dataBase64"])
            assert data == data[:1] * len(data)  # one letter throughout
            seen.add(data[:1])
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())
    assert path.read_bytes() == versions[-1]
    assert [p.name for p in env.work.iterdir()] == ["atomic.txt"]  # no leftover temp file


async def test_a_failed_write_leaves_the_old_content(env: Env) -> None:
    (env.work / "keep.txt").write_text("keep")
    assert (
        await env.error(
            "fs.write", path="keep.txt", dataBase64=b64(b"x" * (8 * MIB + 1)), mode="overwrite"
        )
        == "too_large"
    )
    assert (env.work / "keep.txt").read_text() == "keep"
    assert [p.name for p in env.work.iterdir()] == ["keep.txt"]


@posix_only
async def test_a_hard_link_to_a_snowpea_file_is_still_protected(env: Env) -> None:
    home = env.daemon.paths.home
    (home / "credentials.json").write_text('{"k": "secret"}')
    os.link(home / "credentials.json", env.work / "notes.txt")
    assert await env.error("fs.read", path="notes.txt") == "protected"
    assert await env.error("fs.hash", path="notes.txt") == "protected"
    (env.work / "plain.txt").write_text("x")
    os.link(env.work / "plain.txt", env.work / "plain2.txt")  # an ordinary link is readable
    assert (await env.ok("fs.read", path="plain2.txt"))["length"] == 1


async def test_other_spellings_of_a_protected_name_are_protected(env: Env) -> None:
    # a case-insensitive file system reaches `.env` as `.ENV`; the check judges both spellings
    for name in (".ENV", ".Env.Local", "ID_RSA", "Server.PEM", "API.Key"):
        (env.work / name).write_text("s")
        assert await env.error("fs.read", path=name) == "protected"
    (env.work / ".SSH").mkdir()
    (env.work / ".SSH" / "config").write_text("s")
    assert await env.error("fs.read", path=".SSH/config") == "protected"
    (env.work / ".Snowpea").mkdir()
    (env.work / ".Snowpea" / "Settings.JSON").write_text("{}")
    assert await env.error("fs.read", path=".Snowpea/Settings.JSON") == "protected"


async def test_glob_with_many_double_stars_is_bounded(env: Env) -> None:
    deep = env.work
    for _ in range(14):
        deep = deep / "d"
        deep.mkdir()
    started = time.monotonic()
    got = await env.ok("fs.glob", pattern="**/*/**/*/**/*/**/*/**/nomatch")
    assert got["entries"] == []
    assert time.monotonic() - started < 10


async def test_a_write_does_not_touch_the_process_umask(env: Env) -> None:
    seen = []
    real = os.umask

    def spy(mask: int) -> int:
        seen.append(mask)
        return real(mask)

    os.umask = spy  # type: ignore[assignment]
    try:
        await env.ok("fs.write", path="u.txt", dataBase64=b64(b"x"), mode="create")
    finally:
        os.umask = real
    assert seen == []
