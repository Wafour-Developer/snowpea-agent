"""Shared-secret authentication for the local daemon.

The token lives at ``$SNOWPEA_HOME/token`` with mode 0600; every client reads
it from disk (or from ``daemon.json``) and presents it in ``system.hello``.
"""

from __future__ import annotations

import hmac
import os
import secrets

from snowpea_core.config.paths import Paths

TOKEN_BYTES = 32
TOKEN_MODE = 0o600


def generate_token() -> str:
    """A fresh URL-safe random token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def read_token(paths: Paths) -> str | None:
    """Return the stored token, or ``None`` when there is no readable one."""
    try:
        token = paths.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def write_token(paths: Paths, token: str) -> None:
    """Write the token with 0600 permissions, replacing any previous one."""
    paths.ensure()
    path = paths.token_file
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_MODE)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, TOKEN_MODE)
    os.replace(tmp, path)
    os.chmod(path, TOKEN_MODE)


def ensure_token(paths: Paths, *, rotate: bool = False) -> str:
    """Read the existing token or create one; ``rotate`` forces a new token."""
    if not rotate:
        existing = read_token(paths)
        if existing:
            return existing
    token = generate_token()
    write_token(paths, token)
    return token


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time token comparison."""
    if not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


__all__ = [
    "TOKEN_BYTES",
    "TOKEN_MODE",
    "ensure_token",
    "generate_token",
    "read_token",
    "token_matches",
    "write_token",
]
