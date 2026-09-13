"""Client for the hosted snowpea skill registry (M6-M7 contract §1, v0.3).

Base URL resolution order, highest priority first:

1. an explicit argument (``--registry`` on the CLI),
2. the ``SNOWPEA_REGISTRY_URL`` environment variable,
3. ``settings.json``'s ``skills.registry.url``,
4. :data:`REGISTRY_URL`, the public default.

Tokens (for publish/rate) resolve the same way from ``--token``,
``SNOWPEA_REGISTRY_TOKEN`` and ``skills.registry.token``.

Every network call goes through :class:`HttpRegistryClient`, which never lets
a network failure escape ``search`` — a source that cannot be reached
contributes nothing to :mod:`snowpea_core.skills.marketplace`'s aggregated
search instead of failing it.  ``publish`` and ``rate`` are used directly by
the CLI, so they *do* raise: the user is looking right at the terminal.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger("snowpea.skills.registry")

#: The public registry; not contacted unless something asks it to be.
REGISTRY_URL = "https://registry.snowpea.ai/v1"

#: Network timeout for every registry call (search, download, publish, rate).
REGISTRY_TIMEOUT_SEC = 10.0

#: A downloaded skill zip larger than this is refused before it is written.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class RegistryError(RuntimeError):
    """A registry call that reached the server but was refused, or timed out."""


def resolve_url(explicit: str | None = None, settings: Any = None) -> str:
    """``--registry`` > ``SNOWPEA_REGISTRY_URL`` > ``settings.skills.registry.url`` > default."""
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("SNOWPEA_REGISTRY_URL")
    if env:
        return env.rstrip("/")
    configured = _settings_str(settings, "url")
    if configured:
        return configured.rstrip("/")
    return REGISTRY_URL


def resolve_token(explicit: str | None = None, settings: Any = None) -> str | None:
    """``--token`` > ``SNOWPEA_REGISTRY_TOKEN`` > ``settings.skills.registry.token``."""
    if explicit:
        return explicit
    env = os.environ.get("SNOWPEA_REGISTRY_TOKEN")
    if env:
        return env
    return _settings_str(settings, "token")


def _settings_str(settings: Any, field: str) -> str | None:
    if settings is None:
        return None
    registry = getattr(getattr(settings, "skills", None), "registry", None)
    value = getattr(registry, field, None)
    return value if isinstance(value, str) and value else None


class RegistryClient(Protocol):
    """What the aggregator needs from a skill registry."""

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Registry entries matching ``query``; each entry is a skill record."""
        ...

    async def resolve(self, identifier: str) -> str | None:
        """Install spec (path, git URL or ``<marketplace>/<plugin>``) for an id."""
        ...


class StubRegistryClient:
    """Contacts nothing; used in tests and as a safety net if wiring is skipped."""

    url = REGISTRY_URL

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []

    async def resolve(self, identifier: str) -> str | None:
        return None


@dataclass
class DownloadedSkill:
    """A downloaded skill package, still in memory."""

    content: bytes
    version: str | None
    filename: str | None


class HttpRegistryClient:
    """Talks to a real snowpea-registry instance over HTTP.

    ``url`` is fixed at construction (the aggregator builds one client and
    keeps it); per-call ``--registry``/``--token`` overrides go through the
    module-level :func:`search`/:func:`publish`/:func:`rate` helpers instead,
    which build a throwaway client for that one call.
    """

    def __init__(self, url: str = REGISTRY_URL, *, timeout: float = REGISTRY_TIMEOUT_SEC) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: str) -> list[dict[str, Any]]:
        try:
            payload = await self._get_json(f"{self.url}/skills?q={quote(query)}")
        except Exception as exc:  # noqa: BLE001 - offline must not break aggregated search
            log.info("snowpea-registry unavailable: %s", exc)
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        return [item for item in (results or []) if isinstance(item, dict)]

    async def resolve(self, identifier: str) -> str | None:
        """``registry:<id>`` and a bare id both resolve to the download URL."""
        ident = identifier.split(":", 1)[1] if identifier.startswith("registry:") else identifier
        if not ident.strip():
            return None
        return f"{self.url}/skills/{quote(ident)}/download"

    async def download(self, identifier: str, *, version: str | None = None) -> DownloadedSkill:
        """Fetch the zip for ``identifier`` (bare id or ``registry:<id>``)."""
        ident = identifier.split(":", 1)[1] if identifier.startswith("registry:") else identifier
        url = f"{self.url}/skills/{quote(ident)}/download"
        if version:
            url += f"?v={quote(version)}"
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=self.timeout) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise RegistryError(f"no skill named {ident!r} on {self.url}")
            self._raise_for_error(response)
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise RegistryError(
                    f"{ident}: download is {content_length} bytes, over the "
                    f"{MAX_DOWNLOAD_BYTES} byte cap"
                )
            content = response.content
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise RegistryError(
                    f"{ident}: download is {len(content)} bytes, over the "
                    f"{MAX_DOWNLOAD_BYTES} byte cap"
                )
            return DownloadedSkill(
                content=content,
                version=response.headers.get("x-snowpea-version"),
                filename=_filename_from_disposition(response.headers.get("content-disposition")),
            )

    async def publish(
        self, zip_bytes: bytes, *, filename: str = "skill.zip", token: str | None = None
    ) -> dict[str, Any]:
        """``POST /v1/skills`` with a zip; returns the ``{ok, created, skill}`` body."""
        if not token:
            raise RegistryError(
                "no registry token: pass --token, set SNOWPEA_REGISTRY_TOKEN, or run "
                "`snowpea setup tools` to save one"
            )
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.url}/skills",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (filename, zip_bytes, "application/zip")},
            )
            self._raise_for_error(response)
            return response.json()

    async def rate(
        self, identifier: str, stars: int, *, comment: str | None = None, token: str | None = None
    ) -> dict[str, Any]:
        """``POST /v1/skills/{id}/rating``; returns ``{ok, rating, ratingCount}``."""
        ident = identifier.split(":", 1)[1] if identifier.startswith("registry:") else identifier
        body: dict[str, Any] = {"stars": stars}
        if comment:
            body["comment"] = comment
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.url}/skills/{quote(ident)}/rating", headers=headers, json=body
            )
            self._raise_for_error(response)
            return response.json()

    async def _get_json(self, url: str) -> Any:
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=self.timeout) as client:
            response = await client.get(url)
            self._raise_for_error(response)
            return response.json()

    @staticmethod
    def _raise_for_error(response: Any) -> None:
        if response.status_code < 400:
            return
        message = f"HTTP {response.status_code}"
        try:
            body = response.json()
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, dict) and detail.get("message"):
                message = f"{detail.get('code', response.status_code)}: {detail['message']}"
        except Exception:  # noqa: BLE001 - a non-JSON error body still gets reported
            pass
        raise RegistryError(message)


def _filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


#: The client the aggregator uses; ``skills.registry.url`` swaps its target.
CLIENT: RegistryClient = HttpRegistryClient(REGISTRY_URL)


def set_client(client: RegistryClient) -> RegistryClient:
    """Swap the module-level client (tests, and a settings-driven base URL)."""
    global CLIENT
    CLIENT = client
    return CLIENT


def configure_client(settings: Any = None) -> RegistryClient:
    """Rebuild :data:`CLIENT` from ``settings``/env, and return it."""
    return set_client(HttpRegistryClient(resolve_url(settings=settings)))


__all__ = [
    "CLIENT",
    "MAX_DOWNLOAD_BYTES",
    "REGISTRY_TIMEOUT_SEC",
    "REGISTRY_URL",
    "DownloadedSkill",
    "HttpRegistryClient",
    "RegistryClient",
    "RegistryError",
    "StubRegistryClient",
    "configure_client",
    "resolve_token",
    "resolve_url",
    "set_client",
]
