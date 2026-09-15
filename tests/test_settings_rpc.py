"""M8: ``settings.get`` / ``settings.set`` / ``setup.catalog`` (additive, protocol 1.1.0)."""

from __future__ import annotations

import json
import os
import stat
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


async def test_settings_set_rejects_masked_secret_global(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            # Store a real secret first.
            await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "search": {"credentials": {"brave_free": {"api_key": "sk-real-secret"}}}
                    },
                },
            )

            # A naive read-modify-write that echoes the masked "***" back,
            # bundled with an unrelated change, must be rejected wholesale.
            frame = await client.call(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "defaultMode": "accept",
                        "search": {"credentials": {"brave_free": {"api_key": "***"}}},
                    },
                },
            )
            assert frame["error"]["data"]["code"] == "invalid_params"
            assert "api_key" in frame["error"]["message"]

            # Neither the masked write nor the unrelated sibling change landed.
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert on_disk["search"]["credentials"]["brave_free"]["api_key"] == "sk-real-secret"
            assert on_disk.get("defaultMode") != "accept"

            # A real value can still be stored afterwards.
            result = await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "search": {"credentials": {"brave_free": {"api_key": "sk-new-real"}}}
                    },
                },
            )
            assert result["settings"]["search"]["credentials"]["brave_free"]["api_key"] == "***"
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert on_disk["search"]["credentials"]["brave_free"]["api_key"] == "sk-new-real"
        finally:
            await client.stop()


async def test_settings_set_rejects_masked_secret_project(
    daemon: Daemon, tmp_path: Path
) -> None:
    workdir = tmp_path / "project-masked"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call(
                "settings.set",
                {
                    "scope": "project",
                    "workdir": str(workdir),
                    "patch": {"media": {"mcp": {"api_key": "***"}}},
                },
            )
            assert frame["error"]["data"]["code"] == "invalid_params"

            written = workdir / ".snowpea" / "settings.json"
            assert not written.exists()
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
            assert result["stt"]
            assert result["tts"]
            for section in ("vendors", "search", "browser", "tools", "gateway", "stt", "tts"):
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


async def test_setup_catalog_audio_rows_are_two_states_not_three(daemon: Daemon) -> None:
    """``off`` is always choosable; ``auto`` does not exist any more.

    Voice is unset (off) or pinned to one engine. The row a surface leads with
    is the recommended engine, which is also what ``default`` means on a voice
    catalog now — the row to pre-select, not a provider that would run.
    """
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("setup.catalog")
            stt = {item["id"]: item["active"] for item in result["stt"]}
            tts = {item["id"]: item["active"] for item in result["tts"]}
            assert stt["off"] is True and tts["off"] is True
            assert "auto" not in stt and "auto" not in tts

            stt_default = next(item for item in result["stt"] if item["default"])
            tts_default = next(item for item in result["tts"] if item["default"])
            assert stt_default["recommended"] and tts_default["recommended"]
            assert stt_default["installable"] and tts_default["installable"]
        finally:
            await client.stop()


async def test_setup_catalog_audio_detection_matches_the_cli_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend on ``PATH`` shows ``active: true``, the way ``snowpea setup`` sees it.

    This reuses ``audio.capabilities``'s own detection (CORE-setup-catalog-audio),
    so pinning ``PATH`` to a directory holding only a fake ``piper`` script is
    enough to prove the RPC does not re-derive its own PATH check.
    """
    import stat as stat_mod

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "piper"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat_mod.S_IXUSR | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    home = tmp_path / "home"
    instance = await make_daemon(home)
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, instance)
            try:
                result = await client.ok("setup.catalog")
                tts = {item["id"]: item["active"] for item in result["tts"]}
                assert tts["piper"] is True
                assert tts["edge-tts"] is False
            finally:
                await client.stop()
    finally:
        await instance.stop()


async def test_settings_get_never_returns_a_token_in_clear(daemon: Daemon) -> None:
    """CORE-fixes-v017 R1: every token-shaped credential is masked on the wire.

    ``oauth_token`` arrived with the web-login work and was in neither masking
    set, so the TUI settings view, the IDE and any debug dump received a live
    OAuth access token in plaintext.
    """
    secrets = {
        "api_key": "sk-secret-api-key",
        "oauth_token": "oauth-secret-token",
        "refresh_token": "refresh-secret-token",
        "access_token": "access-secret-token",
        "id_token": "id-secret-token",
        "token": "bare-secret-token",
    }
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            written = await client.ok(
                "settings.set",
                {"scope": "global", "patch": {"providers": {"openai": {**secrets}}}},
            )
            read = await client.ok("settings.get", {"scope": "global"})
            for payload in (written["settings"], read["settings"]):
                masked = payload["providers"]["openai"]
                for key in secrets:
                    assert masked[key] == "***", f"{key} leaked"
            # Masking is presentation only: the file still holds the real values.
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert on_disk["providers"]["openai"]["oauth_token"] == secrets["oauth_token"]
        finally:
            await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
async def test_settings_json_is_written_owner_only(daemon: Daemon) -> None:
    """CORE-fixes-v017 R2: settings.json holds api keys and OAuth tokens."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await client.ok(
                "settings.set",
                {"scope": "global", "patch": {"providers": {"openai": {"api_key": "sk-x"}}}},
            )
        finally:
            await client.stop()
    mode = stat.S_IMODE(daemon.paths.settings_json.stat().st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
async def test_saving_tightens_an_already_world_readable_settings_file(tmp_path: Path) -> None:
    """An install that predates the fix is tightened on the next write."""
    from snowpea_core.config.paths import Paths
    from snowpea_core.config.settings import Settings

    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text("{}\n", encoding="utf-8")
    os.chmod(paths.settings_json, 0o644)
    Settings().save(paths)
    assert stat.S_IMODE(paths.settings_json.stat().st_mode) == 0o600


async def test_null_in_a_patch_deletes_the_key(daemon: Daemon) -> None:
    """A deep merge cannot express a removal, so ``null`` means "delete".

    Without it there was no way to delete a model profile, an agent assignment
    or a team over RPC at all — the key survived every patch
    (CORE-model-assignment).
    """
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "models": {
                            "default": "fast",
                            "profiles": {
                                "fast": {"provider": "openai", "model": "gpt-fast"},
                                "deep": {"provider": "anthropic", "model": "claude-deep"},
                            },
                        },
                        "agents": {"models": {"executor": "fast", "architect": "deep"}},
                    },
                },
            )

            cleared = await client.ok(
                "settings.set",
                {"scope": "global", "patch": {"agents": {"models": {"executor": None}}}},
            )
            assert cleared["settings"]["agents"]["models"] == {"architect": "deep"}

            # Deleting a profile that nothing references works; the sibling stays.
            # ``default`` has to move off it first — see the next test.
            dropped = await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {"models": {"default": "deep", "profiles": {"fast": None}}},
                },
            )
            assert set(dropped["settings"]["models"]["profiles"]) == {"deep"}

            # It is persisted, not just echoed.
            on_disk = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
            assert set(on_disk["models"]["profiles"]) == {"deep"}
        finally:
            await client.stop()


async def test_deleting_a_profile_that_is_still_referenced_is_refused(daemon: Daemon) -> None:
    """The validator still runs after the delete, so the document stays consistent."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await client.ok(
                "settings.set",
                {
                    "scope": "global",
                    "patch": {
                        "models": {
                            "default": "fast",
                            "profiles": {"fast": {"provider": "openai", "model": "gpt-fast"}},
                        }
                    },
                },
            )
            frame = await client.call(
                "settings.set",
                {"scope": "global", "patch": {"models": {"profiles": {"fast": None}}}},
            )
            assert frame.get("error") is not None
            assert "unknown profile" in str(frame["error"])
        finally:
            await client.stop()
