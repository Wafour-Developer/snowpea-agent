"""List SSH identity files under the account's ``~/.ssh`` (read-only, no private key bytes)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STANDARD_KEY_NAMES = frozenset(
    {
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
        "id_ed25519_sk",
        "id_ecdsa_sk",
    }
)


@dataclass(frozen=True)
class SshKeyCandidate:
    path: str
    name: str
    key_type: str | None
    comment: str | None
    mode_ok: bool


def _mode_ok(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return (mode & 0o077) == 0 and stat.S_ISREG(path.stat().st_mode)


def _pub_meta(pub_path: Path) -> tuple[str | None, str | None]:
    try:
        first = pub_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None, None
    parts = first.split()
    if len(parts) < 2:
        return None, None
    comment = " ".join(parts[2:])[:100] if len(parts) > 2 else ""
    return parts[0], comment or None


def _is_private_key_candidate(path: Path) -> bool:
    name = path.name
    if name.endswith(".pub"):
        return False
    if name in STANDARD_KEY_NAMES:
        return path.is_file() or path.exists()
    pub = path.with_name(name + ".pub")
    return path.is_file() and pub.is_file()


def list_ssh_keys(ssh_dir: Path) -> list[dict[str, Any]]:
    """Return candidate private keys; private file contents are never read."""
    if not ssh_dir.is_dir():
        return []
    found: dict[str, SshKeyCandidate] = {}
    try:
        names = sorted(os.listdir(ssh_dir))
    except OSError:
        return []
    for name in names:
        if name.startswith("."):
            continue
        path = ssh_dir / name
        try:
            if not path.is_file() and name not in STANDARD_KEY_NAMES:
                continue
            if name not in STANDARD_KEY_NAMES:
                if not _is_private_key_candidate(path):
                    continue
            elif not path.is_file():
                continue
            pub = path.with_name(name + ".pub")
            key_type, comment = _pub_meta(pub) if pub.is_file() else (None, None)
            mode_ok = _mode_ok(path) if path.is_file() else False
            text = str(path.resolve())
            found[text] = SshKeyCandidate(
                path=text,
                name=name,
                key_type=key_type,
                comment=comment,
                mode_ok=mode_ok,
            )
        except OSError:
            continue
    return [
        {
            "path": row.path,
            "name": row.name,
            "type": row.key_type,
            "comment": row.comment,
            "modeOk": row.mode_ok,
        }
        for row in sorted(found.values(), key=lambda item: item.name)
    ]


__all__ = ["STANDARD_KEY_NAMES", "list_ssh_keys"]