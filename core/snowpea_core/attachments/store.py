"""Where inline attachments are kept on disk.

Everything a client pastes into a prompt lands under
``$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>``.  The content hash is
the filename, so pasting the same screenshot into ten prompts costs one file,
and a resumed session can still find the bytes its history refers to.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path

from snowpea_core.attachments.model import Attachment, AttachmentError, safe_name
from snowpea_core.config.paths import Paths

log = logging.getLogger("snowpea.attachments.store")

#: Directory name under ``SNOWPEA_HOME``.
DIRNAME = "attachments"


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

    def total_bytes(self) -> int:
        """How much disk the whole store is using."""
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


__all__ = ["DIRNAME", "AttachmentStore"]
