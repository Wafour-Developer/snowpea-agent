"""Gateway credentials (``$SNOWPEA_HOME/credentials.json``, mode 0600).

A ``credentialsRef`` is a *name*, never a secret: either a key in this file or
the name of an environment variable.  That is what travels over the RPC wire,
what gets written into ``state.db`` beside a binding, and what appears in logs.
The value itself is read here and nowhere else, and is never logged.

The file is a flat JSON object.  A value may be a string (one token) or an
object when a platform needs more than one, e.g.::

    {"tg_main": "123:ABC",
     "slack_work": {"token": "xoxb-...", "app_token": "xapp-..."}}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import Paths

log = logging.getLogger("snowpea.credentials")

#: Owner read/write only; anything wider is tightened on write.
FILE_MODE = 0o600


class CredentialError(RuntimeError):
    """A ``credentialsRef`` does not resolve to anything usable."""


class CredentialStore:
    """Reads and writes ``credentials.json``; resolves refs to secrets."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    @property
    def path(self) -> Path:
        return self.paths.credentials_json

    # -- file ----------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """Whole file, or ``{}`` when it is missing or unreadable."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s; treating it as empty", self.path)
            return {}
        return raw if isinstance(raw, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        """Write the file atomically and leave it at 0600."""
        self.paths.ensure()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, FILE_MODE)
        tmp.replace(self.path)
        os.chmod(self.path, FILE_MODE)

    def set(self, ref: str, value: Any) -> None:
        """Store one credential under ``ref``."""
        data = self.load()
        data[ref] = value
        self.save(data)

    def names(self) -> list[str]:
        """Stored refs; the values stay here."""
        return sorted(self.load())

    # -- resolution ----------------------------------------------------
    def resolve(self, ref: str) -> Any:
        """``credentials.json`` key first, then an environment variable.

        Raises :class:`CredentialError` naming only the ref, so the message is
        safe to send back over RPC and into the log.
        """
        if not ref:
            raise CredentialError("a credentialsRef is required")
        data = self.load()
        if ref in data:
            return data[ref]
        env = os.environ.get(ref)
        if env:
            return env
        raise CredentialError(
            f"credentialsRef {ref!r} is neither a key in credentials.json nor a set env var"
        )

    def resolve_tokens(self, ref: str) -> dict[str, Any]:
        """Resolve to a ``{"token": ..., ...}`` mapping whatever the shape."""
        value = self.resolve(ref)
        if isinstance(value, str):
            return {"token": value}
        if isinstance(value, dict):
            token = value.get("token") or value.get("bot_token") or value.get("api_key")
            if not token:
                raise CredentialError(f"credentialsRef {ref!r} has no 'token' field")
            return {**value, "token": str(token)}
        raise CredentialError(f"credentialsRef {ref!r} is not a string or an object")


__all__ = ["FILE_MODE", "CredentialError", "CredentialStore"]
