"""Content-addressed, per-turn filesystem checkpoints."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import difflib
import hashlib
import json
import logging
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from snowpea_core.config.paths import Paths, utc_now
from snowpea_core.exec.local import LocalBackend

log = logging.getLogger("snowpea.checkpoints")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILE_SIZE = MAX_FILE_BYTES
EXCLUDED_PARTS = {".git", "node_modules", ".snowpea", "__pycache__", "dist", "out"}
MAX_BASELINE_PATHS = 2000
#: Bytes one shell baseline reads at most; past it, files fall back to porcelain.
MAX_BASELINE_BYTES = 64 * 1024 * 1024
#: Directories a shell baseline never hashes: environments and build output
#: that an unborn-HEAD repo lists as untracked when nothing ignores them.
BASELINE_SKIPPED_PARTS = {".venv", "venv", "env", "target", "build", ".next", ".cache"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def _safe_id(value: str) -> str:
    if not SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("checkpoint identifiers may contain only letters, digits, '.', '_', '-'")
    return value


def parse_porcelain(raw: bytes) -> dict[str, str]:
    """Parse ``git status --porcelain=v1 -z`` into path -> XY status."""
    result: dict[str, str] = {}
    fields = raw.split(b"\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field or len(field) < 4:
            continue
        xy = field[:2].decode("ascii", "replace")
        path = field[3:].decode("utf-8", "surrogateescape")
        if "R" in xy or "C" in xy:
            if index < len(fields) and fields[index]:
                original = fields[index].decode("utf-8", "surrogateescape")
                index += 1  # original path; destination is the changed file
                result[original] = xy
        result[path] = xy
    return result


def changed_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Paths whose porcelain entry appeared, disappeared, or changed."""
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def is_excluded(rel_path: str) -> bool:
    """Whether a safe relative POSIX path is outside checkpoint scope."""
    path = PurePosixPath(rel_path.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts or any(p in EXCLUDED_PARTS for p in path.parts)


def merge_manifest(manifest: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Add one path once; later observations only refresh its after state."""
    for current in manifest.setdefault("files", []):
        if current["path"] == entry["path"]:
            if "afterSha" in entry:
                current["afterSha"] = entry["afterSha"]
                if "status" in entry:
                    current["status"] = entry["status"]
                current["size"] = entry.get("size", current.get("size", 0))
            return False
    manifest["files"].append(entry)
    return True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prompt(session: Any) -> str:
    current = getattr(session, "current_prompt", "")
    if current:
        return str(current)[:200]
    for message in reversed(getattr(getattr(session, "history", None), "messages", [])):
        if message.role == "user" and isinstance(message.content, str):
            from snowpea_core.agent.host_context import user_words

            return user_words(message.content)[:200]
    return ""


class CheckpointStore:
    """Persist checkpoint manifests and deduplicated byte blobs."""

    def __init__(self, paths: Paths, settings: Any) -> None:
        self.paths = paths
        self.settings = settings
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._shell_before: dict[str, dict[str, str] | None] = {}
        # Content hashes (and sizes) of the dirty/untracked files of a shell
        # baseline, whose bytes are kept as blobs before the shell runs.
        self._shell_hashes: dict[str, dict[str, tuple[str, int]]] = {}
        # Per turn: the first baseline hash seen for each dirty path, and the
        # rows whose path existed before the turn although no blob was kept.
        self._turn_baseline: dict[tuple[str, str], dict[str, str]] = {}
        self._existed: set[tuple[str, str, str]] = set()
        # Per session: ``rel -> (mtime_ns, size, sha)`` of baseline files already
        # hashed this turn, so the next shell call does not read them again.
        self._stat_cache: dict[str, dict[str, tuple[int, int, str]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.checkpoints.enabled)

    def _root(self, session_id: str) -> Path:
        return self.paths.checkpoints_dir / _safe_id(session_id)

    def _blob_path(self, session_id: str, sha: str) -> Path:
        return self._root(session_id) / "objects" / sha[:2] / sha

    def _turn_path(self, session_id: str, checkpoint_id: str) -> Path:
        return self._root(session_id) / "turns" / f"{_safe_id(checkpoint_id)}.json"

    def _new_manifest(self, session: Any, turn_id: str, *, kind: str = "turn") -> dict[str, Any]:
        return {
            "id": turn_id,
            "sessionId": session.id,
            "turnId": turn_id,
            "workdir": str(session.workdir),
            "createdAt": utc_now(),
            "prompt": _prompt(session),
            "kind": kind,
            "files": [],
        }

    async def _store_blob(self, session_id: str, data: bytes) -> str:
        digest = _sha(data)
        target = self._blob_path(session_id, digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return digest

    async def _exists(self, session: Any, rel: str) -> bool:
        if isinstance(session.backend, LocalBackend):
            return session.backend.resolve(rel).exists()
        return await session.backend.exists(rel)

    async def _read_bytes(self, session: Any, rel: str) -> bytes:
        if isinstance(session.backend, LocalBackend):
            return session.backend.resolve(rel).read_bytes()
        return await session.backend.read_bytes(rel)

    async def _write_bytes(self, session: Any, rel: str, content: bytes) -> None:
        if isinstance(session.backend, LocalBackend):
            target = session.backend.resolve(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return
        await session.backend.write_bytes(rel, content)

    async def _remove_file(self, session: Any, rel: str) -> None:
        if isinstance(session.backend, LocalBackend):
            session.backend.resolve(rel).unlink(missing_ok=True)
            return
        await session.backend.remove_file(rel)

    def _safe_path(self, session: Any, path: str) -> tuple[str | None, str | None]:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(Path(session.workdir).resolve())
            except (OSError, ValueError):
                return None, "excluded"
        else:
            rel = candidate
        value = rel.as_posix()
        if (
            not value
            or value == "."
            or PurePosixPath(value).is_absolute()
            or ".." in PurePosixPath(value).parts
        ):
            return None, "excluded"
        if isinstance(session.backend, LocalBackend):
            try:
                session.backend.resolve(value).resolve().relative_to(
                    Path(session.workdir).resolve()
                )
            except (OSError, ValueError):
                return None, "excluded"
        return value, "excluded" if is_excluded(value) else None

    async def before_write(
        self,
        session: Any,
        turn_id: str,
        path: str,
        *,
        source: str = "tool",
        kind: str = "turn",
    ) -> tuple[dict[str, Any] | None, bool]:
        """Capture a path's before-state, returning (manifest, first-path-added)."""
        if not self.enabled:
            return None, False
        rel, excluded = self._safe_path(session, path)
        if rel is None:
            return None, False
        key = (session.id, turn_id)
        manifest = self._pending.setdefault(key, self._new_manifest(session, turn_id, kind=kind))
        current = next((row for row in manifest["files"] if row["path"] == rel), None)
        if current is not None:
            if not await self._unchanged_shell_row(session, turn_id, current):
                return manifest, False
            # A shell row without a before-state, for a file still as it was at
            # the shell baseline: the write tool's capture is the real one.
            manifest["files"].remove(current)
            self._existed.discard((session.id, turn_id, rel))
            await self._capture(session, turn_id, manifest, rel, excluded, source)
            return manifest, False
        added = await self._capture(session, turn_id, manifest, rel, excluded, source)
        return manifest, added and len(manifest["files"]) == 1

    async def _unchanged_shell_row(self, session: Any, turn_id: str, row: dict[str, Any]) -> bool:
        if row.get("source") != "shell" or row.get("beforeSha") is not None:
            return False
        baseline = self._turn_baseline.get((session.id, turn_id), {}).get(row["path"])
        if baseline is None:
            return False
        current = await self._current_bytes(session, row["path"])
        return current is not None and _sha(current) == baseline

    async def _capture(
        self,
        session: Any,
        turn_id: str,
        manifest: dict[str, Any],
        rel: str,
        excluded: str | None,
        source: str,
    ) -> bool:
        before_sha: str | None = None
        size = 0
        skipped: str | None = excluded
        exists = False
        if skipped is None:
            try:
                exists = await self._exists(session, rel)
                if exists:
                    data = await self._read_bytes(session, rel)
                    size = len(data)
                    if size > MAX_FILE_BYTES:
                        skipped = "too_large"
                    else:
                        before_sha = await self._store_blob(session.id, data)
            except (OSError, UnicodeError, AttributeError):
                skipped = "unreadable"
        if exists and before_sha is None:
            self._existed.add((session.id, turn_id, rel))
        entry = {
            "path": rel,
            "status": "modified" if exists else "created",
            "beforeSha": before_sha,
            "afterSha": None,
            "size": size,
            "source": source,
            "restorable": skipped is None,
            "skipped": skipped,
        }
        return merge_manifest(manifest, entry)

    async def note_after(self, session: Any, turn_id: str, path: str) -> None:
        if not self.enabled:
            return
        manifest = self._pending.get((session.id, turn_id))
        rel, _ = self._safe_path(session, path)
        if manifest is None or rel is None:
            return
        entry = next((row for row in manifest["files"] if row["path"] == rel), None)
        if entry is None:
            return
        try:
            if await self._exists(session, rel):
                data = await self._read_bytes(session, rel)
                entry["afterSha"] = _sha(data)
                existed = (
                    entry["beforeSha"] is not None or (session.id, turn_id, rel) in self._existed
                )
                entry["status"] = "modified" if existed else "created"
            else:
                entry["afterSha"] = None
                entry["status"] = "deleted"
        except (OSError, AttributeError):
            entry["restorable"] = False
            entry["skipped"] = "unreadable"

    async def begin_shell(self, session: Any) -> dict[str, str] | None:
        if not self.enabled or not self.settings.checkpoints.scanShellWrites:
            return None
        baseline = await self._git_status(session.workdir)
        self._shell_before[session.id] = baseline
        self._shell_hashes[session.id] = await self._hash_baseline(session, baseline or {})
        return baseline

    async def _hash_baseline(
        self, session: Any, baseline: dict[str, str]
    ) -> dict[str, tuple[str, int]]:
        """Hash and keep the bytes of a baseline's dirty/untracked files.

        Porcelain alone cannot tell whether a shell rewrote an already dirty
        file, and git has no before-state for it (nor for anything in a repo
        whose HEAD is unborn). Bounded by path count and file size.
        """
        seen = self._stat_cache.setdefault(session.id, {})
        if isinstance(session.backend, LocalBackend):
            # Up to MAX_BASELINE_BYTES of reads and blob writes: off the loop.
            return await asyncio.to_thread(
                self._hash_local_baseline, session.id, session.backend, baseline, seen
            )
        # A remote backend: no lstat or mtime to go by, so no cache either.
        hashes: dict[str, tuple[str, int]] = {}
        considered = 0
        budget = MAX_BASELINE_BYTES
        for rel in sorted(baseline):
            if is_excluded(rel):
                continue
            if considered >= MAX_BASELINE_PATHS:
                break
            considered += 1
            try:
                if not await self._exists(session, rel):
                    continue
                data = await self._read_bytes(session, rel)
                if len(data) > MAX_FILE_BYTES or len(data) > budget:
                    continue
                budget -= len(data)
                hashes[rel] = (await self._store_blob(session.id, data), len(data))
            except (OSError, AttributeError):
                continue
        return hashes

    def _hash_local_baseline(
        self,
        session_id: str,
        backend: LocalBackend,
        baseline: dict[str, str],
        seen: dict[str, tuple[int, int, str]],
    ) -> dict[str, tuple[str, int]]:
        """:meth:`_hash_baseline` for a local backend, run in a worker thread.

        A symlink is never followed: an untracked link to a key file must not
        be copied into the checkpoint store, nor restored through.
        """
        hashes: dict[str, tuple[str, int]] = {}
        considered = 0
        budget = MAX_BASELINE_BYTES
        for rel in sorted(baseline):
            parts = PurePosixPath(rel.replace("\\", "/")).parts
            if is_excluded(rel) or any(part in BASELINE_SKIPPED_PARTS for part in parts):
                continue
            if considered >= MAX_BASELINE_PATHS:
                break
            considered += 1
            try:
                target = backend.resolve(rel)
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    continue
                known = seen.get(rel)
                if known is not None and known[:2] == (info.st_mtime_ns, info.st_size):
                    hashes[rel] = (known[2], known[1])
                    continue
                if info.st_size > budget:
                    continue
                data = target.read_bytes()
                if len(data) > MAX_FILE_BYTES or len(data) > budget:
                    continue
                budget -= len(data)
                digest = _sha(data)
                blob = self._blob_path(session_id, digest)
                if not blob.exists():
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    blob.write_bytes(data)
                hashes[rel] = (digest, len(data))
                seen[rel] = (info.st_mtime_ns, info.st_size, digest)
            except (OSError, ValueError):
                continue
        return hashes

    async def _git_status(self, workdir: Path) -> dict[str, str] | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(workdir),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            return parse_porcelain(stdout) if process.returncode == 0 else None
        except (OSError, TimeoutError):
            log.debug("could not scan shell writes in %s", workdir, exc_info=True)
            try:
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workdir),
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                return parse_porcelain(completed.stdout) if completed.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                return None

    async def _git_head_bytes(self, workdir: Path, rel: str) -> bytes | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(workdir),
                "show",
                f"HEAD:{rel}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            return stdout if process.returncode == 0 else None
        except (OSError, TimeoutError):
            try:
                completed = subprocess.run(
                    ["git", "-C", str(workdir), "show", f"HEAD:{rel}"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                return completed.stdout if completed.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                return None

    async def end_shell(
        self, session: Any, turn_id: str, baseline: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        stored = self._shell_before.pop(session.id, None)
        hashes = self._shell_hashes.pop(session.id, {})
        before = baseline if baseline is not None else stored
        if before is None:
            return None
        after = await self._git_status(session.workdir)
        if after is None:
            return None
        key = (session.id, turn_id)
        seen = self._turn_baseline.setdefault(key, {})
        for rel, (digest, _) in hashes.items():
            seen.setdefault(rel, digest)
        manifest: dict[str, Any] | None = None
        # A dirty path can keep the same porcelain XY while a shell changes its
        # bytes again, so a hashed baseline path is compared by content, and
        # listed only when it changed. Unhashed ones fall back to porcelain.
        candidates = changed_paths(before, after) - hashes.keys()
        for rel, (digest, _) in hashes.items():
            current = await self._current_bytes(session, rel)
            if current is None or _sha(current) != digest:
                candidates.add(rel)
        for rel in sorted(candidates):
            if is_excluded(rel):
                continue
            manifest = self._pending.setdefault(key, self._new_manifest(session, turn_id))
            if any(row["path"] == rel for row in manifest["files"]):
                await self.note_after(session, turn_id, rel)
                continue
            was_clean = rel not in before
            exists = await self._exists(session, rel)
            if rel in hashes:
                # The blob was stored when the baseline was taken.
                before_sha, size = hashes[rel]
                restorable = True
            else:
                # ``git show HEAD:`` fails for untracked paths and an unborn
                # HEAD; a clean path then did not exist before the shell.
                head = await self._git_head_bytes(session.workdir, rel) if was_clean else None
                before_sha = await self._store_blob(session.id, head) if head is not None else None
                size = len(head or b"")
                restorable = was_clean
                if not was_clean and "D" not in before[rel]:
                    self._existed.add((session.id, turn_id, rel))
            status = (
                "deleted"
                if not exists
                else ("created" if was_clean and before_sha is None else "modified")
            )
            entry = {
                "path": rel,
                "status": status,
                "beforeSha": before_sha,
                "afterSha": None,
                "size": size,
                "source": "shell",
                "restorable": restorable,
                "skipped": None,
            }
            merge_manifest(manifest, entry)
            await self.note_after(session, turn_id, rel)
        return manifest

    async def finalize_turn(self, session: Any, turn_id: str) -> dict[str, Any] | None:
        manifest = self._pending.pop((session.id, turn_id), None)
        self._turn_baseline.pop((session.id, turn_id), None)
        # Blobs no row kept are pruned below, so hashes from this turn go too.
        self._stat_cache.pop(session.id, None)
        self._existed = {row for row in self._existed if row[:2] != (session.id, turn_id)}
        if not manifest or not manifest["files"]:
            # A shell baseline may have stored blobs that no row kept.
            with contextlib.suppress(OSError):
                self._delete_unreferenced_blobs(session.id)
            return None
        target = self._turn_path(session.id, manifest["id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        index = self._read_index(session.id)
        index.append(
            {
                "id": manifest["id"],
                "turnId": manifest["turnId"],
                "createdAt": manifest["createdAt"],
                "prompt": manifest["prompt"],
                "kind": manifest["kind"],
                "fileCount": len(manifest["files"]),
                "bytes": sum(
                    self._blob_path(session.id, digest).stat().st_size
                    for digest in {row.get("beforeSha") for row in manifest["files"]}
                    if digest and self._blob_path(session.id, digest).exists()
                ),
            }
        )
        self._write_index(session.id, index)
        self.prune(session.id)
        return manifest

    def _read_index(self, session_id: str) -> list[dict[str, Any]]:
        path = self._root(session_id) / "index.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write_index(self, session_id: str, value: list[dict[str, Any]]) -> None:
        path = self._root(session_id) / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def list(self, session_id: str) -> list[dict[str, Any]]:
        result = []
        for row in reversed(self._read_index(session_id)):
            try:
                result.append(
                    json.loads(self._turn_path(session_id, row["id"]).read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return result

    async def get_checkpoint(
        self, session_id: str, checkpoint_id: str | None
    ) -> dict[str, Any] | None:
        if checkpoint_id is None:
            return None
        try:
            return json.loads(
                self._turn_path(session_id, checkpoint_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None

    async def diff(
        self,
        session: Any,
        checkpoint_id: str,
        *,
        paths: builtins.list[str] | None = None,
        through: bool = False,
    ) -> dict[str, builtins.list[dict[str, Any]]]:
        selected = self._states(session.id, checkpoint_id, paths, through)
        result = []
        for rel, state in selected.items():
            current = await self._current_bytes(session, rel)
            before = self._load_blob(session.id, state.get("beforeSha"))
            last_after = state.get("lastAfterSha")
            changed = _sha(current) if current is not None else None
            changed_since = changed != last_after
            binary = _binary(current) or _binary(before)
            patch = "" if binary else _patch(rel, current, before)
            result.append(
                {
                    "path": rel,
                    "patch": patch,
                    "binary": binary,
                    "changedSince": changed_since,
                    "restorable": bool(state.get("restorable")),
                }
            )
        return {"files": result}

    def _states(
        self,
        session_id: str,
        checkpoint_id: str,
        paths: builtins.list[str] | None,
        through: bool,
    ) -> dict[str, dict[str, Any]]:
        manifests = list(reversed(self.list(session_id)))  # oldest first
        position = next((i for i, row in enumerate(manifests) if row["id"] == checkpoint_id), None)
        if position is None:
            raise KeyError(checkpoint_id)
        scope = manifests[position:] if through else manifests[position : position + 1]
        wanted: set[str] = set(paths or [])
        states: dict[str, dict[str, Any]] = {}
        last_after: dict[str, str | None] = {}
        for manifest in scope:
            for row in manifest["files"]:
                rel = row["path"]
                if wanted and rel not in wanted:
                    continue
                states.setdefault(rel, dict(row))
                last_after[rel] = row.get("afterSha")
        for rel, state in states.items():
            state["lastAfterSha"] = last_after[rel]
        return states

    async def restore(
        self,
        session: Any,
        checkpoint_id: str,
        *,
        paths: builtins.list[str] | None = None,
        through: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        states = self._states(session.id, checkpoint_id, paths, through)
        restored: builtins.list[str] = []
        skipped: builtins.list[dict[str, str]] = []
        ready: builtins.list[tuple[str, bytes | None]] = []
        for rel, state in states.items():
            if is_excluded(rel):
                skipped.append({"path": rel, "reason": "excluded"})
                continue
            if not state.get("restorable"):
                skipped.append({"path": rel, "reason": "not_restorable"})
                continue
            before_sha = state.get("beforeSha")
            before = self._load_blob(session.id, before_sha)
            if before_sha is not None and before is None:
                skipped.append({"path": rel, "reason": "missing_blob"})
                continue
            current = await self._current_bytes(session, rel)
            current_sha = _sha(current) if current is not None else None
            if current_sha != state.get("lastAfterSha") and not force:
                skipped.append({"path": rel, "reason": "changed_since"})
                continue
            ready.append((rel, before))
        restore_id: str | None = None
        if ready and not dry_run:
            restore_id = f"r-{uuid.uuid4().hex[:12]}"
            for rel, _ in ready:
                await self.before_write(session, restore_id, rel, source="tool", kind="restore")
            for rel, before in ready:
                if before is None:
                    await self._remove_file(session, rel)
                else:
                    await self._write_bytes(session, rel, before)
                await self.note_after(session, restore_id, rel)
                restored.append(rel)
            await self.finalize_turn(session, restore_id)
        elif dry_run:
            restored = [rel for rel, _ in ready]
        return {"restored": restored, "skipped": skipped, "checkpointId": restore_id}

    async def _current_bytes(self, session: Any, rel: str) -> bytes | None:
        try:
            return (
                await self._read_bytes(session, rel) if await self._exists(session, rel) else None
            )
        except OSError:
            return None

    def _load_blob(self, session_id: str, digest: str | None) -> bytes | None:
        if digest is None:
            return None
        try:
            return self._blob_path(session_id, digest).read_bytes()
        except OSError:
            return None

    def delete(self, session_id: str, checkpoint_id: str | None = None) -> None:
        root = self._root(session_id)
        if checkpoint_id is None:
            shutil.rmtree(root, ignore_errors=True)
            return
        self._turn_path(session_id, checkpoint_id).unlink(missing_ok=True)
        self._write_index(
            session_id,
            [row for row in self._read_index(session_id) if row.get("id") != checkpoint_id],
        )
        self._delete_unreferenced_blobs(session_id)

    def prune(self, session_id: str) -> None:
        index = self._read_index(session_id)
        limit = max(0, int(self.settings.checkpoints.maxTurns))
        byte_limit = max(0, int(self.settings.checkpoints.maxBytes))
        while index and (
            len(index) > limit or self._retained_bytes(session_id, index) > byte_limit
        ):
            removed = index.pop(0)
            self._turn_path(session_id, str(removed["id"])).unlink(missing_ok=True)
        self._write_index(session_id, index)
        self._delete_unreferenced_blobs(session_id)

    def _retained_bytes(self, session_id: str, index: builtins.list[dict[str, Any]]) -> int:
        digests: set[str] = set()
        for row in index:
            try:
                manifest = json.loads(
                    self._turn_path(session_id, str(row["id"])).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, KeyError):
                continue
            digests.update(
                digest for entry in manifest.get("files", []) if (digest := entry.get("beforeSha"))
            )
        return sum(
            path.stat().st_size
            for digest in digests
            if (path := self._blob_path(session_id, digest)).exists()
        )

    def _delete_unreferenced_blobs(self, session_id: str) -> None:
        referenced: set[str] = set()
        for manifest in self.list(session_id):
            for row in manifest["files"]:
                referenced.update(
                    value for value in (row.get("beforeSha"), row.get("afterSha")) if value
                )
        objects = self._root(session_id) / "objects"
        if objects.exists():
            for path in objects.glob("*/*"):
                if path.name not in referenced:
                    path.unlink(missing_ok=True)

    def cleanup_orphans(self, valid_session_ids: set[str]) -> None:
        root = self.paths.checkpoints_dir
        if not root.exists():
            return
        for path in root.iterdir():
            if path.is_dir() and path.name not in valid_session_ids:
                shutil.rmtree(path, ignore_errors=True)


def _binary(data: bytes | None) -> bool:
    if data is None:
        return False
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _patch(path: str, current: bytes | None, before: bytes | None) -> str:
    current_text = (current or b"").decode("utf-8").splitlines(keepends=True)
    before_text = (before or b"").decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(current_text, before_text, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


__all__ = [
    "CheckpointStore",
    "MAX_FILE_BYTES",
    "MAX_FILE_SIZE",
    "changed_paths",
    "is_excluded",
    "merge_manifest",
    "parse_porcelain",
]
