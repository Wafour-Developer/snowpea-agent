"""The issue-tracker client, a module of the extension's own.

plugin.py imports it with ``from . import tracker``; a reload imports it again
from disk, and no other extension can see or shadow it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aiohttp


class TrackerError(Exception):
    """A failure worth showing to the model as it is."""


class Tracker:
    """A small HTTP client for ``GET <baseUrl>/api/tickets/<id>``."""

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # One connection pool for the extension's lifetime, closed by on_dispose.
        self.http = aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        )

    def link(self, ticket: str) -> str:
        return f"{self.base_url}/browse/{ticket}"

    async def get(self, ticket: str) -> dict[str, Any]:
        try:
            async with self.http.get(f"{self.base_url}/api/tickets/{ticket}") as response:
                if response.status == 404:
                    raise TrackerError(f"{ticket} does not exist")
                if response.status >= 400:
                    raise TrackerError(f"the tracker answered HTTP {response.status}")
                data: dict[str, Any] = await response.json()
                return data
        except aiohttp.ClientError as exc:
            raise TrackerError(f"cannot reach the tracker: {exc}") from exc

    async def close(self) -> None:
        await self.http.close()


def ticket_in_branch(workdir: Path, project: str) -> str:
    """``SP-12`` when the checked-out branch is named like ``SP-12-fix-login``."""
    for folder in (workdir, *workdir.parents):
        head = folder / ".git" / "HEAD"
        if head.is_file():
            branch = head.read_text(encoding="utf-8").strip().removeprefix("ref: refs/heads/")
            match = re.search(rf"\b{re.escape(project)}-\d+\b", branch)
            return match.group(0) if match else ""
    return ""
