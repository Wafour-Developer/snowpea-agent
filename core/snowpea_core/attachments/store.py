"""Where inline attachments are kept on disk.

Everything a client pastes into a prompt lands under
``$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>``.  The content hash is
the filename, so pasting the same screenshot into ten prompts costs one file,
and a resumed session can still find the bytes its history refers to.

That copy may be downscaled for the model.  The bytes the user actually
attached are kept too, under ``<session>/files/<original name>`` with an
``index.json`` beside them (1.23.0): the agent is told the path and may use
the file as it is — upload it, convert it — without a copy step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from snowpea_core.attachments.model import Attachment, AttachmentError, safe_name
from snowpea_core.config.paths import Paths

log = logging.getLogger("snowpea.attachments.store")

#: Directory name under ``SNOWPEA_HOME``.
DIRNAME = "attachments"
#: Where a session's original files are kept, and their index.
FILES_DIRNAME = "files"
INDEX_NAME = "index.json"
#: Most original bytes one session may keep (1.23.0).
SESSION_MAX_BYTES = 500 * 1024 * 1024


class AttachmentStore:
    """Content-addressed storage for one snowpea home."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    @classmethod
    def from_paths(cls, paths: Paths) -> AttachmentStore:
        """The store for a resolved :class:`~snowpea_core.config.paths.Paths`."""
        return cls(paths.home / DIRNAME)

    def session_dir(self, session_id: str) -> Path:
        """The directory holding one session's attachments."""
        return self.root / safe_name(session_id or "shared")

    def path_for(self, session_id: str, attachment: Attachment) -> Path:
        return self.session_dir(session_id) / f"{attachment.sha256}.{attachment.extension}"

    def save(self, session_id: str, attachment: Attachment) -> Attachment:
        """Persist ``attachment`` and return a path-backed copy of it.

        An attachment that already lives on disk is returned unchanged: the
        user's own file is never copied into the store.  Writing is a no-op
        when a file with the same content hash is already there.
        """
        if attachment.path is not None:
            return attachment
        target = self.path_for(session_id, attachment)
        if target.exists() and target.stat().st_size == attachment.size:
            return replace(attachment, path=target, data=None)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(attachment.read_bytes())
            tmp.replace(target)
        except OSError as exc:
            raise AttachmentError("internal", f"attachment: could not store: {exc}") from exc
        return replace(attachment, path=target, data=None)

    def save_all(self, session_id: str, attachments: Iterable[Attachment]) -> list[Attachment]:
        """Persist several attachments, keeping their order."""
        return [self.save(session_id, item) for item in attachments]

    def iter_files(self, session_id: str) -> Iterator[Path]:
        """Every stored file for a session, oldest name first."""
        directory = self.session_dir(session_id)
        if not directory.is_dir():
            return iter(())
        return iter(sorted(p for p in directory.iterdir() if p.is_file()))

    def purge(self, session_id: str) -> int:
        """Delete a session's attachments; returns how many files went."""
        directory = self.session_dir(session_id)
        if not directory.is_dir():
            return 0
        count = sum(1 for path in directory.iterdir() if path.is_file())
        shutil.rmtree(directory, ignore_errors=True)
        return count

    # -- originals (1.23.0) ---------------------------------------------
    def files_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / FILES_DIRNAME

    def listing(self, session_id: str) -> list[dict[str, Any]]:
        """``[{name, path, mime, size, sha256, messageId}]``, oldest first."""
        index = self.files_dir(session_id) / INDEX_NAME
        try:
            rows = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _write_listing(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        index = self.files_dir(session_id) / INDEX_NAME
        tmp = index.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(index)

    def keep_original(
        self, session_id: str, name: str, data: bytes | None, mime: str, source: Path | None = None
    ) -> Path:
        """Record what the user attached, as they attached it; returns its path.

        Inline bytes are written under their sanitized original name (``a (2).png``
        when the name is taken by other bytes); a file the user pointed at is
        recorded where it is, never copied.  ``attachment_too_large`` past
        :data:`SESSION_MAX_BYTES` for the session.
        """
        rows = self.listing(session_id)
        if source is not None:
            resolved = source.expanduser().resolve()
            size = resolved.stat().st_size if resolved.exists() else 0
            digest = ""
            target = resolved
        else:
            payload = data or b""
            digest = hashlib.sha256(payload).hexdigest()
            size = len(payload)
            for row in rows:
                if row.get("sha256") == digest and Path(str(row.get("path"))).exists():
                    return Path(str(row["path"]))
            used = sum(int(row.get("size") or 0) for row in rows if row.get("sha256"))
            if used + size > SESSION_MAX_BYTES:
                raise AttachmentError(
                    "attachment_too_large",
                    f"attachment {safe_name(name)!r} would take this session's files past "
                    f"{SESSION_MAX_BYTES // (1024 * 1024)} MB; start a new session or "
                    "remove some first",
                )
            target = self._free_name(session_id, safe_name(name))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_bytes(payload)
                tmp.replace(target)
            except OSError as exc:
                raise AttachmentError("internal", f"attachment: could not store: {exc}") from exc
        if not any(row.get("path") == str(target) for row in rows):
            rows.append(
                {
                    "name": safe_name(name),
                    "path": str(target),
                    "mime": mime,
                    "size": size,
                    "sha256": digest,
                    "messageId": None,
                }
            )
            try:
                self.files_dir(session_id).mkdir(parents=True, exist_ok=True)
                self._write_listing(session_id, rows)
            except OSError as exc:
                raise AttachmentError("internal", f"attachment: could not index: {exc}") from exc
        return target

    def assign_message(self, session_id: str, message_id: str) -> None:
        """Stamp the files a prompt just brought with the turn it started."""
        rows = self.listing(session_id)
        if not any(row.get("messageId") is None for row in rows):
            return
        for row in rows:
            if row.get("messageId") is None:
                row["messageId"] = message_id
        try:
            self._write_listing(session_id, rows)
        except OSError:  # pragma: no cover - the files are there either way
            log.warning("could not stamp attachments of %s", session_id, exc_info=True)

    def _free_name(self, session_id: str, name: str) -> Path:
        directory = self.files_dir(session_id)
        candidate = directory / name
        stem, suffix = Path(name).stem, Path(name).suffix
        count = 2
        while candidate.exists() or candidate.name == INDEX_NAME:
            candidate = directory / f"{stem} ({count}){suffix}"
            count += 1
        return candidate

    def total_bytes(self) -> int:
        """How much disk the whole store is using."""
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


__all__ = ["DIRNAME", "SESSION_MAX_BYTES", "AttachmentStore"]
