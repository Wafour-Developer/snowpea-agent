"""Where a report goes and how: the endpoint, the install id and the upload.

The endpoint is ``https://browser.snowpea.ai/api/feedback``;
``SNOWPEA_FEEDBACK_URL`` overrides it (https, or plain http to a loopback host
only, for tests and staging).  The body is gzip with ``Content-Type:
application/json`` and ``Content-Encoding: gzip``; no redirect is followed and
nothing is retried automatically (a 4xx is final; a 429/503 says when to try
again).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("snowpea.feedback")

FEEDBACK_URL = "https://browser.snowpea.ai/api/feedback"
ENV_URL = "SNOWPEA_FEEDBACK_URL"
INSTALL_ID_FILE = "feedback-install-id"
#: Whether the 개선하기 server takes ``client.app: "snowpea-core"`` (live on qai
#: since 2026-11-18).  False hides ``improvementFeedback`` from ``system.hello``
#: (clients hide the entry) unless ``SNOWPEA_FEEDBACK_URL`` points elsewhere.
SERVER_LIVE = True


def advertised(env: dict[str, str] | None = None) -> bool:
    """Whether ``system.hello`` lists ``improvementFeedback``."""
    env = os.environ if env is None else env
    return SERVER_LIVE or bool((env.get(ENV_URL) or "").strip())
#: No progress either way for this long ends the upload.
IDLE_SEC = 30
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class EndpointError(ValueError):
    """``SNOWPEA_FEEDBACK_URL`` is not https (or http on loopback)."""


def feedback_endpoint(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    url = (env.get(ENV_URL) or "").strip() or FEEDBACK_URL
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK:
        return url
    raise EndpointError(f"{ENV_URL} must be https (or http on loopback)")


def install_id(home: str | os.PathLike[str]) -> str:
    """A random UUID per core home, kept in ``$SNOWPEA_HOME/feedback-install-id`` (0600)."""
    path = Path(home) / INSTALL_ID_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
        if _UUID.match(value):
            return value.lower()
    except OSError:
        pass
    value = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{INSTALL_ID_FILE}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{value}\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("feedback: could not keep the install id: %s", exc.__class__.__name__)
    return value


@dataclass(eq=False)
class UploadError(Exception):
    """The upload did not end in a report id.

    ``kind``: ``rejected`` (a 4xx other than 429; never retried), ``busy``
    (429 / 503, ``retry_after`` seconds when the server said), ``unreachable``
    (no answer, a network error, a redirect, a 5xx or an answer without an id).
    """

    kind: str
    status: int
    detail: str = ""
    retry_after: int | None = None
    server_error: str | None = None
    server_message: str | None = None

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail or f'HTTP {self.status}'}"


def _retry_after(value: str | None) -> int | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


async def post_feedback(
    url: str, body: bytes, *, user_agent: str, idle_sec: float = IDLE_SEC
) -> dict[str, str]:
    """POST the gzip ``body``; ``{"id", "receivedAt"}`` or :class:`UploadError`."""
    import aiohttp

    total = idle_sec + (len(body) + 1024 * 1024 - 1) // (1024 * 1024) * 10
    timeout = aiohttp.ClientTimeout(total=total, sock_connect=idle_sec, sock_read=idle_sec)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Encoding": "gzip",
        "User-Agent": user_agent,
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
            async with client.post(url, data=body, headers=headers, allow_redirects=False) as resp:
                status = resp.status
                raw = await resp.content.read(64 * 1024)
                retry = _retry_after(resp.headers.get("Retry-After"))
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise UploadError("unreachable", 0, exc.__class__.__name__) from exc
    answer: dict[str, Any] = {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            answer = parsed
    except (UnicodeDecodeError, ValueError):
        pass
    server_error = answer.get("error") if isinstance(answer.get("error"), str) else None
    server_message = answer.get("message")[:500] if isinstance(answer.get("message"), str) else None
    detail = server_message or f"HTTP {status}"
    if status in (200, 201):
        if isinstance(answer.get("id"), str) and answer["id"]:
            received = answer.get("receivedAt")
            if not isinstance(received, str) or not received:
                from datetime import UTC, datetime

                received = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            return {"id": answer["id"], "receivedAt": received}
        raise UploadError("unreachable", status, "the server's answer had no report id")
    if status in (429, 503):
        raise UploadError("busy", status, detail, retry, server_error, server_message)
    if 400 <= status < 500:
        raise UploadError("rejected", status, detail, None, server_error, server_message)
    if 300 <= status < 400:
        raise UploadError("unreachable", status, f"HTTP {status} redirect not followed")
    raise UploadError("unreachable", status, detail, None, server_error, server_message)


__all__ = [
    "ENV_URL",
    "EndpointError",
    "FEEDBACK_URL",
    "INSTALL_ID_FILE",
    "UploadError",
    "feedback_endpoint",
    "install_id",
    "post_feedback",
]
