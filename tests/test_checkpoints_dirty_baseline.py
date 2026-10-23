"""Checkpoints keep real before-states for dirty and untracked files (addendum 29, C2)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.session.checkpoints import CheckpointStore


class StubSession:
    def __init__(self, workdir: Path) -> None:
        self.id = "session-1"
        self.workdir = workdir
        self.backend = LocalBackend(workdir)
        self.current_prompt = "edit a file"


def git(workdir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    git(workdir, "init")
    git(workdir, "config", "user.email", "test@example.com")
    git(workdir, "config", "user.name", "Test")
    return workdir


def make_store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(Paths.create(tmp_path / "home"), Settings.model_validate({}))


async def run_shell(
    store: CheckpointStore, session: StubSession, turn_id: str, action: Any
) -> None:
    baseline = await store.begin_shell(session)
    action()
    await store.end_shell(session, turn_id, baseline)


async def test_unborn_head_shell_then_write_lists_only_the_written_file(tmp_path: Path) -> None:
    workdir = make_repo(tmp_path)
    (workdir / "src").mkdir()
    (workdir / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (workdir / "src" / "b.ts").write_text("export const b = 2;\n", encoding="utf-8")
    (workdir / "README.md").write_text("# demo\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    await run_shell(store, session, "turn-1", lambda: None)  # ls
    await store.before_write(session, "turn-1", "src/a.ts", source="tool")
    (workdir / "src" / "a.ts").write_text("export const a = 42;\n", encoding="utf-8")
    await store.note_after(session, "turn-1", "src/a.ts")
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    assert [row["path"] for row in manifest["files"]] == ["src/a.ts"]
    entry = manifest["files"][0]
    assert entry["status"] == "modified"
    assert entry["restorable"] is True
    assert entry["beforeSha"] is not None

    result = await store.restore(session, manifest["id"])
    assert result["restored"] == ["src/a.ts"]
    assert (workdir / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 1;\n"


async def test_unborn_head_shell_edit_of_an_untracked_file_is_modified_and_restorable(
    tmp_path: Path,
) -> None:
    workdir = make_repo(tmp_path)
    (workdir / "a.txt").write_text("original\n", encoding="utf-8")
    (workdir / "b.txt").write_text("untouched\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    await run_shell(
        store,
        session,
        "turn-1",
        lambda: (
            (workdir / "a.txt").write_text("shell\n", encoding="utf-8"),
            (workdir / "new.txt").write_text("new\n", encoding="utf-8"),
        ),
    )
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    rows = {row["path"]: row for row in manifest["files"]}
    assert set(rows) == {"a.txt", "new.txt"}
    assert rows["a.txt"]["status"] == "modified"
    assert rows["a.txt"]["restorable"] is True
    assert rows["new.txt"]["status"] == "created"

    await store.restore(session, manifest["id"])
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "original\n"
    assert not (workdir / "new.txt").exists()


async def test_committed_repo_dirty_file_changed_by_shell_restores_the_dirty_content(
    tmp_path: Path,
) -> None:
    workdir = make_repo(tmp_path)
    (workdir / "dirty.txt").write_text("head\n", encoding="utf-8")
    (workdir / "other.txt").write_text("head\n", encoding="utf-8")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "base")
    (workdir / "dirty.txt").write_text("already dirty\n", encoding="utf-8")
    (workdir / "other.txt").write_text("also dirty\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)

    await run_shell(
        store,
        session,
        "turn-1",
        lambda: (workdir / "dirty.txt").write_text("shell\n", encoding="utf-8"),
    )
    manifest = await store.finalize_turn(session, "turn-1")

    assert manifest is not None
    assert [row["path"] for row in manifest["files"]] == ["dirty.txt"]
    entry = manifest["files"][0]
    assert entry["status"] == "modified"
    assert entry["restorable"] is True

    result = await store.restore(session, manifest["id"])
    assert result["restored"] == ["dirty.txt"]
    assert (workdir / "dirty.txt").read_text(encoding="utf-8") == "already dirty\n"


async def test_write_upgrades_a_shell_row_without_a_before_state(tmp_path: Path) -> None:
    workdir = make_repo(tmp_path)
    (workdir / "a.txt").write_text("original\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = StubSession(workdir)
    await run_shell(store, session, "turn-1", lambda: None)
    manifest = store._pending.setdefault(
        (session.id, "turn-1"), store._new_manifest(session, "turn-1")
    )
    manifest["files"].append(
        {
            "path": "a.txt",
            "status": "modified",
            "beforeSha": None,
            "afterSha": None,
            "size": 0,
            "source": "shell",
            "restorable": False,
            "skipped": None,
        }
    )

    await store.before_write(session, "turn-1", "a.txt", source="tool")
    (workdir / "a.txt").write_text("written\n", encoding="utf-8")
    await store.note_after(session, "turn-1", "a.txt")
    final = await store.finalize_turn(session, "turn-1")

    assert final is not None
    entry = final["files"][0]
    assert entry["source"] == "tool"
    assert entry["status"] == "modified"
    assert entry["restorable"] is True
    await store.restore(session, final["id"])
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "original\n"
