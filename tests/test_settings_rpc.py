"""M8: ``settings.get`` / ``settings.set`` / ``setup.catalog`` (additive, protocol 1.1.0)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon

from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


async def test_settings_get_global_defaults(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("settings.get", {"scope": "global"})
            settings = result["settings"]
            assert settings["agents"]["max_concurrent"] == 3
            assert settings["search"]["provider"] == "ddgs"
        finally:
            await client.stop()


async def test_settings_set_global_persists_and_reloads(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok(
                "settings.set",
                {"scope": "global", "patch": {"agents": {"max_concurrent": 7}}},
            )
            assert result["settings"]["agents"]["max_concurrent"] == 7
            # Untouched sibling fields survive the merge.
            assert result["settings"]["search"]["provider"] == "ddgs"

            # Persisted to disk, not just in memory.
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert on_disk["agents"]["max_concurrent"] == 7

            reread = await client.ok("settings.get", {"scope": "global"})
            assert reread["settings"]["agents"]["max_concurrent"] == 7
        finally:
            await client.stop()


async def test_settings_set_masks_secrets_in_response(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "search": {"credentials": {"brave_free": {"api_key": "sk-super-secret"}}}
                    },
                },
            )
            masked = result["settings"]["search"]["credentials"]["brave_free"]["api_key"]
            assert masked == "***"

            # The real value is what actually got persisted, not the mask.
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert (
                on_disk["search"]["credentials"]["brave_free"]["api_key"] == "sk-super-secret"
            )
        finally:
            await client.stop()


async def test_settings_get_masks_secrets(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {"media": {"mcp": {"api_key": "sk-media-secret"}}},
                },
            )
            reread = await client.ok("settings.get", {"scope": "global"})
            assert reread["settings"]["media"]["mcp"]["api_key"] == "***"
        finally:
            await client.stop()


async def test_settings_set_project_scope_writes_snowpea_dir(
    daemon: Daemon, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok(
                "settings.set",
                {
                    "scope": "project",
                    "workdir": str(workdir),
                    "patch": {"defaultMode": "accept"},
                },
            )
            assert result["settings"]["defaultMode"] == "accept"

            written = workdir / ".snowpea" / "settings.json"
            assert written.exists()
            on_disk = json.loads(written.read_text(encoding="utf-8"))
            assert on_disk["defaultMode"] == "accept"

            reread = await client.ok(
                "settings.get", {"scope": "project", "workdir": str(workdir)}
            )
            assert reread["settings"]["defaultMode"] == "accept"
        finally:
            await client.stop()


async def test_settings_get_project_scope_requires_workdir(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call("settings.get", {"scope": "project"})
            assert frame["error"]["data"]["code"] == "invalid_params"
        finally:
            await client.stop()


async def test_settings_set_invalid_patch_is_invalid_params(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call(
                "settings.set",
                {"scope": "global", "patch": {"agents": {"max_concurrent": "not-an-int"}}},
            )
            assert frame["error"]["data"]["code"] == "invalid_params"
        finally:
            await client.stop()


async def test_settings_set_project_invalid_patch_is_invalid_params(
    daemon: Daemon, tmp_path: Path
) -> None:
    workdir = tmp_path / "project2"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call(
                "settings.set",
                {
                    "scope": "project",
                    "workdir": str(workdir),
                    "patch": {"defaultMode": "not-a-real-mode"},
                },
            )
            assert frame["error"]["data"]["code"] == "invalid_params"
        finally:
            await client.stop()


async def test_setup_catalog_returns_vendors_and_search_with_ddgs_first(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("setup.catalog")
            assert len(result["vendors"]) == 11
            assert result["search"][0]["id"] == "ddgs"
            assert result["browser"]
            assert result["tools"]
            assert result["gateway"]
            for section in ("vendors", "search", "browser", "tools", "gateway"):
                for item in result[section]:
                    assert set(item) >= {
                        "id",
                        "label",
                        "tier",
                        "key",
                        "default",
                        "description",
                        "active",
                        "tags",
                    }
        finally:
            await client.stop()
