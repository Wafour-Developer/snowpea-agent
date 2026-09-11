"""Client for the snowpea.ai skill registry (M6 contract §1).

The hosted registry lands in v0.3.  v0.1 ships the interface plus a stub that
answers with an empty list, so :mod:`snowpea_core.skills.marketplace` can
already treat it as a fourth source without a feature flag.
"""

from __future__ import annotations

from typing import Any, Protocol

#: Where the hosted registry will live; not contacted in v0.1.
REGISTRY_URL = "https://snowpea.ai/api/skills"


class RegistryClient(Protocol):
    """What the aggregator needs from a skill registry."""

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Registry entries matching ``query``; each entry is a skill record."""
        ...

    async def resolve(self, identifier: str) -> str | None:
        """Install spec (path, git URL or ``<marketplace>/<plugin>``) for an id."""
        ...


class StubRegistryClient:
    """v0.1 placeholder: knows nothing, contacts nothing."""

    url = REGISTRY_URL

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []

    async def resolve(self, identifier: str) -> str | None:
        return None


#: The client the aggregator uses; v0.3 replaces it with an HTTP-backed one.
CLIENT: RegistryClient = StubRegistryClient()


def set_client(client: RegistryClient) -> RegistryClient:
    """Swap the module-level client (tests, and v0.3's real implementation)."""
    global CLIENT
    CLIENT = client
    return CLIENT


__all__ = ["CLIENT", "REGISTRY_URL", "RegistryClient", "StubRegistryClient", "set_client"]
