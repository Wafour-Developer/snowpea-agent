"""Unit coverage for per-turn filesystem checkpoints."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.session.checkpoints import (
    MAX_FILE_SIZE,
    CheckpointStore,
    changed_paths,
    is_excluded,
    merge_manifest,
    parse_porcelain,
)


class StubSession:
    def __init__(self, workdir: Path, *, session_id: str = "session-1") -> None:
        self.id = session_id
        self.workdir = workdir
        self.backend = LocalBackend(workdir)
        self.current_prompt = "restore this turn"
        self.events: list[Any] = []

    async def event(self, event: Any) -> None:
        self.events.append(event)


def make_store(tmp_path: Path, **checkpoint_settings: Any) -> CheckpointStore:
    settings = Settings.model_validate({"checkpoints": checkpoint_settings})
    return CheckpointStore(Paths.create(tmp_path / "home"), settings)


def checkpoint_id(checkpoint: dict[str, Any]) -> str:
    return str(checkpoint["id"])


def file_entry(checkpoint: dict[str, Any], path: str) -> dict[str, Any]:
    return next(file for file in checkpoint["files"] if file["path"] == path)


async def record_write(
    store: CheckpointStore,
    session: StubSession,
    turn_id: str,
    path: str,
    content: str | bytes | None,
) -> dict[str, Any]:
    await store.before_write(session, turn_id, path, source="tool")
    target = session.workdir / path
    if content is None:
        target.unlink(missing_ok=True)
    elif isinstance(content, bytes):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    await store.note_after(session, turn_id, path)
    manifest = await store.finalize_turn(session, turn_id)
    assert manifest is not None
    return manifest


def test_parse_porcelain_reads_modified_entries_from_z_output() -> None:
    assert parse_porcelain(b" M src/app.py\0") == {"src/app.py": " M"}


def test_parse_porcelain_reads_added_entries_from_z_output() -> None:
    assert parse_porcelain(b"A  src/new.py\0") == {"src/new.py": "A "}


def test_parse_porcelain_reads_untracked_entries_from_z_output() -> None:
    assert parse_porcelain(b"?? scratch.txt\0") == {"scratch.txt": "??"}


def test_parse_porcelain_records_both_sides_of_a_rename_from_z_output() -> None:
    assert parse_porcelain(b"R  new-name.txt\0old-name.txt\0") == {
        "new-name.txt": "R ",
        "old-name.txt": "R ",
    }


def test_changed_paths_returns_union_of_paths_whose_status_changed() -> None:
    before = {"clean.txt": " M", "same.txt": "A ", "removed.txt": "??"}
    after = {"clean.txt": "", "same.txt": "A ", "added.txt": "??"}

    assert changed_paths(before, after) == {"clean.txt", "removed.txt", "added.txt"}


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "node_modules/pkg/index.js",
        ".snowpea/checkpoints/state.json",
        "__pycache__/mod.pyc",
        "dist/app.js",
        "out/app.js",
    ],
)
def test_is_excluded_rejects_non_restorable_directories(path: str) -> None:
    assert is_excluded(path) is True


def test_is_excluded_allows_regular_project_files() -> None:
    assert is_excluded("src/app.py") is False


def test_checkpoint_ids_cannot_escape_the_checkpoint_root(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.delete("..")
    with pytest.raises(ValueError):
        store.delete("session-1", "../index")


async def test_before_write_marks_files_larger_than_the_size_cap_as_skipped(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "large.bin").write_bytes(b"x" * (MAX_FILE_SIZE + 1))
    store = make_store(tmp_path)
    session = StubSession(workdir)

    await store.before_write(session, "turn-1", "large.bin", source="tool")
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    entry = file_entry(manifest, "large.bin")
    assert entry["skipped"] == "too_large"
    assert entry["restorable"] is False


def test_merge_manifest_ignores_a_second_write_of_the_same_path() -> None:
    manifest: dict[str, Any] = {"files": []}
    first = {"path": "a.txt", "beforeSha": "old", "afterSha": None, "status": "modified"}
    second = {"path": "a.txt", "beforeSha": "ignored", "afterSha": "new", "status": "deleted"}

    assert merge_manifest(manifest, first) is True
    assert merge_manifest(manifest, second) is False

    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["beforeSha"] == "old"


def test_merge_manifest_updates_after_state_for_a_repeated_path() -> None:
    manifest: dict[str, Any] = {"files": []}

    merge_manifest(manifest, {"path": "a.txt", "afterSha": None, "size": 3})
    merge_manifest(manifest, {"path": "a.txt", "afterSha": "new", "size": 4})

    assert manifest["files"] == [{"path": "a.txt", "afterSha": "new", "size": 4}]


async def test_store_round_trip_preserves_checkpoint_files(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("before\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    manifest = await record_write(store, session, "turn-1", "a.txt", "after\n")
    loaded = await store.get_checkpoint(session.id, checkpoint_id(manifest))

    assert loaded == manifest


async def test_store_deduplicates_identical_blobs_across_turns(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    store = make_store(tmp_path)
    session = StubSession(workdir)

    (workdir / "a.txt").write_text("shared\n", encoding="utf-8")
    first = await record_write(store, session, "turn-1", "a.txt", "after-one\n")
    (workdir / "b.txt").write_text("shared\n", encoding="utf-8")
    second = await record_write(store, session, "turn-2", "b.txt", "after-two\n")

    assert file_entry(first, "a.txt")["beforeSha"] == file_entry(second, "b.txt")["beforeSha"]


async def test_diff_through_uses_the_earliest_before_state_across_later_turns(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("zero\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    first = await record_write(store, session, "turn-1", "a.txt", "one\n")
    await record_write(store, session, "turn-2", "a.txt", "two\n")

    result = await store.diff(session, checkpoint_id(first), through=True)

    patch = result["files"][0]["patch"]
    assert "-two" in patch and "+zero" in patch


async def test_restore_skips_files_changed_since_the_checkpoint_without_force(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("before\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)
    manifest = await record_write(store, session, "turn-1", "a.txt", "after\n")
    (workdir / "a.txt").write_text("manual\n", encoding="utf-8")

    result = await store.restore(session, checkpoint_id(manifest))

    assert result["restored"] == []
    assert result["skipped"] == [{"path": "a.txt", "reason": "changed_since"}]
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "manual\n"


async def test_restore_applies_files_changed_since_the_checkpoint_with_force(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("before\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)
    manifest = await record_write(store, session, "turn-1", "a.txt", "after\n")
    (workdir / "a.txt").write_text("manual\n", encoding="utf-8")

    result = await store.restore(session, checkpoint_id(manifest), force=True)

    assert result["restored"] == ["a.txt"]
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "before\n"


async def test_restore_deletes_files_created_by_the_checkpoint(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    store = make_store(tmp_path)
    session = StubSession(workdir)
    manifest = await record_write(store, session, "turn-1", "created.txt", "new\n")

    result = await store.restore(session, checkpoint_id(manifest))

    assert result["restored"] == ["created.txt"]
    assert not (workdir / "created.txt").exists()


async def test_restore_records_a_restore_checkpoint_before_writing(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("before\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)
    manifest = await record_write(store, session, "turn-1", "a.txt", "after\n")

    result = await store.restore(session, checkpoint_id(manifest))
    restore_checkpoint = await store.get_checkpoint(session.id, result["checkpointId"])

    assert restore_checkpoint["kind"] == "restore"


async def test_prune_by_max_turns_removes_the_oldest_turn(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    store = make_store(tmp_path, maxTurns=1)
    session = StubSession(workdir)
    (workdir / "a.txt").write_text("zero\n", encoding="utf-8")

    first = await record_write(store, session, "turn-1", "a.txt", "one\n")
    await record_write(store, session, "turn-2", "a.txt", "two\n")

    assert await store.get_checkpoint(session.id, checkpoint_id(first)) is None


async def test_prune_by_max_bytes_drops_unreferenced_blobs(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    store = make_store(tmp_path, maxBytes=1)
    session = StubSession(workdir)
    (workdir / "a.txt").write_text("a", encoding="utf-8")
    first = await record_write(store, session, "turn-1", "a.txt", "b")
    first_before = file_entry(first, "a.txt")["beforeSha"]

    await record_write(store, session, "turn-2", "a.txt", "c")

    assert await store.get_checkpoint(session.id, checkpoint_id(first)) is None
    assert not list((tmp_path / "home" / "checkpoints").glob(f"**/{first_before}"))


async def test_shell_delta_records_clean_git_files_as_restorable_with_head_bytes(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    subprocess.run(["git", "init"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "clean.txt").write_text("head\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workdir, check=True, capture_output=True)
    store = make_store(tmp_path)
    session = StubSession(workdir)

    baseline = await store.begin_shell(session)
    (workdir / "clean.txt").write_text("shell\n", encoding="utf-8")
    await store.end_shell(session, "turn-1", baseline)
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    entry = file_entry(manifest, "clean.txt")
    assert entry["source"] == "shell"
    assert entry["restorable"] is True
    assert entry["beforeSha"] is not None


async def test_shell_delta_records_dirty_git_files_as_not_restorable(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    subprocess.run(["git", "init"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "dirty.txt").write_text("head\n", encoding="utf-8")
    subprocess.run(["git", "add", "dirty.txt"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workdir, check=True, capture_output=True)
    (workdir / "dirty.txt").write_text("already dirty\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    baseline = await store.begin_shell(session)
    (workdir / "dirty.txt").write_text("shell\n", encoding="utf-8")
    await store.end_shell(session, "turn-1", baseline)
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    entry = file_entry(manifest, "dirty.txt")
    assert entry["source"] == "shell"
    assert entry["restorable"] is False
    assert entry["beforeSha"] is None
