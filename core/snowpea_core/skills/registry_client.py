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

#: Network timeout for every registry call (download, publish, rate).
REGISTRY_TIMEOUT_SEC = 10.0

#: The federated search fans out to every hub with ``live=1``; a slow hub
#: costs its own results, not the whole call, so this stays short (§4b: each
#: hub gets its own 4s budget server-side).
SEARCH_TIMEOUT_SEC = 6.0

#: A downloaded skill zip larger than this is refused before it is written.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class RegistryError(RuntimeError):
    """A registry call that reached the server but was refused, or timed out."""


class RegistryNotFetchable(RegistryError):
    """The registry knows the id but cannot serve a zip for it (HTTP 501).

    A federated item whose hub carries no downloadable archive (e.g. a
    ClawHub feed entry) answers this way; the caller may still have its own
    fallback (a ``github:`` spec can be ``git clone``d directly).
    """


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


#: One hub's federated search failure (§4b's ``unavailable`` array).
HubFailure = dict[str, Any]


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
        results, _hub_failures = await self._search(query)
        return results

    async def search_with_sources(
        self, query: str
    ) -> tuple[list[dict[str, Any]], list[HubFailure]]:
        """The federated search (``sources=all&live=1``) plus per-hub failures.

        Never raises: a registry that cannot be reached at all answers
        ``([], [])`` — the caller (:mod:`snowpea_core.skills.marketplace`)
        treats that the same as an unreachable source, using its own outer
        try/except for the "whole registry is down" message.
        """
        return await self._search(query)

    async def _search(self, query: str) -> tuple[list[dict[str, Any]], list[HubFailure]]:
        url = f"{self.url}/skills?q={quote(query)}&sources=all&live=1"
        try:
            from snowpea_core.tools import http_util

            async with http_util.new_client(timeout=SEARCH_TIMEOUT_SEC) as client:
                response = await client.get(url)
                self._raise_for_error(response)
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - offline must not break aggregated search
            log.info("snowpea-registry unavailable: %s", exc)
            return [], []
        results = payload.get("results") if isinstance(payload, dict) else None
        hub_failures = payload.get("unavailable") if isinstance(payload, dict) else None
        return (
            [item for item in (results or []) if isinstance(item, dict)],
            [hub for hub in (hub_failures or []) if isinstance(hub, dict)],
        )

    async def resolve(self, identifier: str) -> str | None:
        """Any install spec resolves to its download URL on this registry."""
        if not identifier.strip():
            return None
        return f"{self.url}/skills/{self._encode_id(identifier)}/download"

    async def sources(self) -> list[dict[str, Any]]:
        """``GET /v1/sources`` — every hub's health (``id, label, enabled, ...``)."""
        payload = await self._get_json(f"{self.url}/sources")
        sources = payload.get("sources") if isinstance(payload, dict) else None
        return [item for item in (sources or []) if isinstance(item, dict)]

    @staticmethod
    def _encode_id(identifier: str) -> str:
        """``registry:<id>`` keeps the bare id (path-safe); anything else — a
        federated spec like ``clawhub:@cua/driver`` — is percent-encoded
        whole, ``/`` and ``@`` included, since the registry's own id for it
        *is* that whole string.
        """
        if identifier.startswith("registry:"):
            return quote(identifier.split(":", 1)[1])
        return quote(identifier, safe="")

    async def download(self, identifier: str, *, version: str | None = None) -> DownloadedSkill:
        """Fetch the zip for ``identifier`` — ``registry:<id>``, a bare local
        id, or any federated spec (``clawhub:...``, ``github:...``) the
        registry's generic download proxy can serve. Raises
        :class:`RegistryNotFetchable` on a 501 (the hub has no archive).
        """
        ident = identifier.split(":", 1)[1] if identifier.startswith("registry:") else identifier
        url = f"{self.url}/skills/{self._encode_id(identifier)}/download"
        if version:
            url += f"?v={quote(version)}"
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=self.timeout) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise RegistryError(f"no skill named {ident!r} on {self.url}")
            if response.status_code == 501:
                raise RegistryNotFetchable(
                    self._error_message(response) or f"{ident} is not fetchable from its hub"
                )
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
    def _error_message(response: Any) -> str | None:
        try:
            body = response.json()
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, dict) and detail.get("message"):
                return f"{detail.get('code', response.status_code)}: {detail['message']}"
        except Exception:  # noqa: BLE001 - a non-JSON error body still gets reported
            return None
        return None

    @classmethod
    def _raise_for_error(cls, response: Any) -> None:
        if response.status_code < 400:
            return
        raise RegistryError(cls._error_message(response) or f"HTTP {response.status_code}")


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
    "SEARCH_TIMEOUT_SEC",
    "DownloadedSkill",
    "HttpRegistryClient",
    "HubFailure",
    "RegistryClient",
    "RegistryError",
    "RegistryNotFetchable",
    "StubRegistryClient",
    "configure_client",
    "resolve_token",
    "resolve_url",
    "set_client",
]
