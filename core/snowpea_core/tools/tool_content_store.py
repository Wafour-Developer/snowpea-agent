"""Host tool results with images, kept on disk for ``session.toolContent``.

A ``tool.result`` event carries an image as a ``contentRef``; the bytes used
to live only in :class:`~snowpea_core.tools.host_tools.HostTools`' in-memory
cache of the last 200 results. Reopening an older thread, or any thread after
the daemon restarted, then found nothing behind the reference and the browser
showed "Screenshot no longer available".

This store keeps each result that has an image under
``<home>/tool-content/<sessionId>/<callId>.json`` (the text blocks inline, an
image as the name of a file next to it). Images are re-encoded as a JPEG no
longer than :data:`MAX_EDGE` on its long side when that is smaller than the
original; the stored block says which media type it ended up as. The whole
directory is held under :data:`DEFAULT_LIMIT_BYTES`, oldest result first, and a
deleted session takes its folder with it.

Sensitive results never reach here: their content is dropped before
``keep_content`` is called, exactly as for the event.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Ceiling for the whole store; the oldest results go first.
DEFAULT_LIMIT_BYTES = 500 * 1024 * 1024
#: Longest edge a stored image keeps.
MAX_EDGE = 1280
JPEG_QUALITY = 75

#: Session and call ids become path parts, so only these characters may.
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def _safe(part: str) -> bool:
    return bool(_SAFE.match(part)) and ".." not in part


def _file_part(call_id: str) -> str:
    # ``:`` is legal in a call id but not in a Windows file name.
    return call_id.replace(":", "_")


def shrink(data: bytes, media_type: str) -> tuple[bytes, str]:
    """``data`` as a JPEG no longer than :data:`MAX_EDGE`, when that is smaller.

    Returns the original when Pillow is missing, the image will not decode, or
    the JPEG would not be any smaller.
    """
    try:  # Pillow is optional (the ``images`` extra).
        from PIL import Image  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return data, media_type
    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                return data, media_type
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            if image.mode in ("RGBA", "LA", "P"):
                # A transparent screenshot area turns white, not black.
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
            else:
                flat = image.convert("RGB")
            buffer = io.BytesIO()
            flat.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:  # noqa: BLE001 - a bad image is kept as it came
        log.debug("tool content image kept unshrunk: %s", exc)
        return data, media_type
    out = buffer.getvalue()
    if len(out) >= len(data):
        return data, media_type
    return out, "image/jpeg"


class ToolContentStore:
    """The on-disk half of ``session.toolContent``."""

    def __init__(self, root: Path, limit_bytes: int = DEFAULT_LIMIT_BYTES) -> None:
        self.root = root
        self.limit_bytes = limit_bytes
        self._lock = threading.Lock()
        #: Bytes on disk, counted once on the first save and kept up to date.
        self._total: int | None = None
        #: Sessions deleted while this daemon ran: a write still in flight for
        #: one must not bring its folder back.
        self._purged: set[str] = set()

    def _dir(self, session_id: str) -> Path | None:
        return self.root / session_id if _safe(session_id) else None

    def save(self, session_id: str, call_id: str, blocks: list[dict[str, Any]]) -> bool:
        """Write one result; False when it has no image or cannot be kept."""
        folder = self._dir(session_id)
        if folder is None or not _safe(call_id):
            return False
        if not any(block.get("type") == "image" and block.get("data") for block in blocks):
            return False
        stem = _file_part(call_id)
        manifest: list[dict[str, Any]] = []
        images: list[tuple[str, bytes]] = []
        for index, block in enumerate(blocks):
            kind = block.get("type")
            if kind == "text":
                manifest.append({"type": "text", "text": str(block.get("text") or "")})
            elif kind == "image" and block.get("data"):
                try:
                    raw = base64.b64decode(str(block["data"]), validate=False)
                except (binascii.Error, ValueError):
                    continue
                data, media_type = shrink(raw, str(block.get("mediaType") or "image/png"))
                name = f"{stem}-{index}.{_EXTENSIONS.get(media_type, 'bin')}"
                images.append((name, data))
                manifest.append({"type": "image", "mediaType": media_type, "file": name})
        if not images:
            return False
        try:
            with self._lock:
                if session_id in self._purged:
                    return False
                folder.mkdir(parents=True, exist_ok=True)
                written = 0
                for name, data in images:
                    (folder / name).write_bytes(data)
                    written += len(data)
                body = json.dumps({"callId": call_id, "content": manifest}).encode()
                temp = folder / f".{stem}.json.tmp"
                temp.write_bytes(body)
                os.replace(temp, folder / f"{stem}.json")
                written += len(body)
                self._grow(written)
        except OSError:
            log.warning("could not keep tool content for %s/%s", session_id, call_id, exc_info=True)
            return False
        return True

    def load(self, session_id: str, call_id: str) -> list[dict[str, Any]] | None:
        """The blocks of a kept result, images as base64, or None."""
        folder = self._dir(session_id)
        if folder is None or not _safe(call_id):
            return None
        try:
            record = json.loads((folder / f"{_file_part(call_id)}.json").read_text())
            blocks: list[dict[str, Any]] = []
            for block in record.get("content") or []:
                if block.get("type") == "image":
                    name = str(block.get("file") or "")
                    if not name or "/" in name or "\\" in name or name.startswith("."):
                        continue
                    data = (folder / name).read_bytes()
                    blocks.append(
                        {
                            "type": "image",
                            "mediaType": block.get("mediaType") or "image/png",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                elif block.get("type") == "text":
                    blocks.append({"type": "text", "text": str(block.get("text") or "")})
        except (OSError, ValueError, AttributeError):
            return None
        return blocks or None

    def purge(self, session_id: str) -> None:
        """Drop everything a session kept (the session was deleted)."""
        folder = self._dir(session_id)
        if folder is None:
            return
        with self._lock:
            self._purged.add(session_id)
            if not folder.is_dir():
                return
            freed = _size(folder)
            shutil.rmtree(folder, ignore_errors=True)
            if self._total is not None:
                self._total = max(0, self._total - freed)

    # -- the size ceiling ---------------------------------------------------
    def _grow(self, added: int) -> None:
        """Account for ``added`` bytes and evict the oldest results past the limit.

        Called with the lock held.
        """
        if self._total is None:
            self._total = _size(self.root)
        else:
            self._total += added
        if self._total <= self.limit_bytes:
            return
        results = sorted(self.root.glob("*/*.json"), key=_mtime)
        for record in results:
            if self._total <= self.limit_bytes:
                break
            self._total -= _drop(record)
        for folder in self.root.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _drop(record: Path) -> int:
    """Delete one result (its manifest and the images it names); returns the bytes freed."""
    names: list[str] = []
    try:
        for block in json.loads(record.read_text()).get("content") or []:
            name = str(block.get("file") or "")
            if name and "/" not in name and "\\" not in name and not name.startswith("."):
                names.append(name)
    except (OSError, ValueError, AttributeError):
        pass
    freed = 0
    for path in [record, *(record.parent / name for name in names)]:
        try:
            freed += path.stat().st_size
            path.unlink()
        except OSError:
            continue
    return freed


__all__ = ["DEFAULT_LIMIT_BYTES", "MAX_EDGE", "ToolContentStore", "shrink"]
