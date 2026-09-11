"""CORE-settings-reload — ``settings.json`` edits reach a running daemon.

``Daemon.start`` reads the file once, so before this every collaborator that
captured the resulting ``Settings`` (the provider registry above all) kept the
values the daemon booted with.  Running ``snowpea setup --model X`` against a
live daemon therefore wrote the new model to disk and then went on sending the
old one, which a vLLM answers with ``HTTP 404: the model does not exist``.

The tests here drive a real in-process daemon and edit ``settings.json`` from
the outside, the way the setup wizard's own process does.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon

from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio

#: Nothing in these tests talks to a model; the port is closed on purpose.
BASE_URL = "http://127.0.0.1:9/v1"

OLD_MODEL = "wrong-model"
NEW_MODEL = "gemma-4-31B"


def _settings_doc(model: str) -> dict:
    return {
        "providers": {
            "default": "local",
            "local": {"base_url": BASE_URL, "model": model},
        }
    }


def _write_settings(path: Path, doc: dict) -> None:
    """Replace ``settings.json`` from outside the daemon, atomically."""
    tmp = path.with_suffix(".json.outside")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home", _settings_doc(OLD_MODEL))
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


# ---------------------------------------------------------------------------
# the reported bug
# ---------------------------------------------------------------------------


async def test_session_create_adopts_an_outside_edit(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``snowpea setup --model X`` then a new session: the new model is used."""
    core = daemon.core
    assert core is not None
    assert core.providers.get("local").model == OLD_MODEL

    _write_settings(core.paths.settings_json, _settings_doc(NEW_MODEL))

    client = await connect(http, daemon)
    try:
        await client.ok("session.create", {"workdir": str(workdir)})
    finally:
        await client.stop()

    assert core.settings.providers["local"]["model"] == NEW_MODEL
    assert core.providers.settings is core.settings
    assert core.providers.get("local").model == NEW_MODEL


async def test_session_prompt_adopts_an_outside_edit(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """A session opened *before* the edit also gets the new model."""
    core = daemon.core
    assert core is not None
    client = await connect(http, daemon)
    try:
        created = await client.ok("session.create", {"workdir": str(workdir)})
        assert core.providers.get("local").model == OLD_MODEL

        _write_settings(core.paths.settings_json, _settings_doc(NEW_MODEL))

        # "/help" is a builtin, so the turn finishes without touching a model.
        await client.ok(
            "session.prompt", {"sessionId": created["sessionId"], "text": "/help"}
        )
    finally:
        await client.stop()

    assert core.providers.get("local").model == NEW_MODEL


async def test_an_unchanged_file_is_not_reloaded(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """The stat check means an untouched file costs no re-read and no event."""
    core = daemon.core
    assert core is not None
    before = core.settings
    client = await connect(http, daemon)
    try:
        await client.ok("session.create", {"workdir": str(workdir)})
        assert core.settings is before
        assert client.of_method("settings.changed") == []
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# system.reloadSettings
# ---------------------------------------------------------------------------


async def test_reload_settings_rpc_reports_changed_keys(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    core = daemon.core
    assert core is not None
    doc = _settings_doc(NEW_MODEL)
    doc["agents"] = {"max_concurrent": 9}
    _write_settings(core.paths.settings_json, doc)

    client = await connect(http, daemon)
    try:
        result = await client.ok("system.reloadSettings", {})
        assert result["reloaded"] is True
        assert result["changedKeys"] == ["agents", "providers"]

        # Every holder of the old document sees the new one.
        assert core.settings.agents.max_concurrent == 9
        assert core.sessions.settings is core.settings
        assert core.approvals.settings is core.settings
        assert core.allowlist.settings is core.settings
        assert core.scheduler.settings is core.settings.scheduler

        # Calling again with nothing changed is a no-op.
        again = await client.ok("system.reloadSettings", {})
        assert again == {"reloaded": False, "changedKeys": []}
    finally:
        await client.stop()


async def test_reload_settings_notifies_every_client(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    core = daemon.core
    assert core is not None
    watcher = await connect(http, daemon)
    caller = await connect(http, daemon)
    try:
        _write_settings(core.paths.settings_json, _settings_doc(NEW_MODEL))
        await caller.ok("system.reloadSettings", {})
        params = await watcher.wait_notification("settings.changed")
        assert params == {"scope": "global", "keys": ["providers"]}
    finally:
        await caller.stop()
        await watcher.stop()


# ---------------------------------------------------------------------------
# settings.set
# ---------------------------------------------------------------------------


async def test_settings_set_rebinds_the_daemon(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    """``settings.set`` must leave file, ``settings.get`` and the registry equal."""
    core = daemon.core
    assert core is not None
    client = await connect(http, daemon)
    try:
        await client.ok(
            "settings.set",
            {"scope": "global", "patch": {"providers": {"local": {"model": NEW_MODEL}}}},
        )
        echoed = await client.ok("settings.get", {"scope": "global"})
        assert echoed["settings"]["providers"]["local"]["model"] == NEW_MODEL

        on_disk = json.loads(core.paths.settings_json.read_text(encoding="utf-8"))
        assert on_disk["providers"]["local"]["model"] == NEW_MODEL

        assert core.providers.get("local").model == NEW_MODEL
        assert core.providers.settings is core.settings
        # The write was the daemon's own, so the next prompt must not re-read it.
        assert core.settings_file_changed() is False

        changed = await client.wait_notification("settings.changed")
        assert changed["keys"] == ["providers"]
    finally:
        await client.stop()


async def test_settings_set_then_prompt_needs_no_restart(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    client = await connect(http, daemon)
    try:
        created = await client.ok("session.create", {"workdir": str(workdir)})
        await client.ok(
            "settings.set",
            {"scope": "global", "patch": {"providers": {"local": {"model": NEW_MODEL}}}},
        )
        await client.ok(
            "session.prompt", {"sessionId": created["sessionId"], "text": "/help"}
        )
        assert core.providers.model_for("local") == NEW_MODEL
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# project settings
# ---------------------------------------------------------------------------


async def test_project_settings_are_read_fresh_per_session(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``<workdir>/.snowpea/settings.json`` is loaded at session.create, not cached."""
    core = daemon.core
    assert core is not None
    project = workdir / ".snowpea"
    project.mkdir()
    settings_file = project / "settings.json"

    client = await connect(http, daemon)
    try:
        settings_file.write_text(json.dumps({"defaultMode": "plan"}), encoding="utf-8")
        first = await client.ok("session.create", {"workdir": str(workdir)})
        assert core.sessions.get(first["sessionId"]).mode == "plan"

        settings_file.write_text(json.dumps({"defaultMode": "auto"}), encoding="utf-8")
        second = await client.ok("session.create", {"workdir": str(workdir)})
        assert core.sessions.get(second["sessionId"]).mode == "auto"
    finally:
        await client.stop()


async def test_a_registry_save_is_not_seen_as_an_outside_edit(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    """``/model`` persists through the registry; that write is the daemon's own."""
    core = daemon.core
    assert core is not None
    core.providers.configure("local", {"model": NEW_MODEL})
    assert core.providers.save() is True
    assert core.settings_file_changed() is False


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _snowpea_bin() -> Path | None:
    """The ``snowpea`` console script beside this interpreter, or on PATH."""
    candidate = Path(sys.executable).parent / "snowpea"
    if candidate.exists():
        return candidate
    found = shutil.which("snowpea")
    return Path(found) if found else None


async def test_setup_tells_a_running_daemon_to_reload(
    daemon: Daemon, workdir: Path
) -> None:
    """``snowpea setup --model X`` with a daemon up prints the reload line."""
    binary = _snowpea_bin()
    if binary is None:  # pragma: no cover - a non-editable install
        pytest.skip("the snowpea console script is not on this interpreter")
    core = daemon.core
    assert core is not None

    env = dict(os.environ)
    env["SNOWPEA_HOME"] = str(core.paths.home)
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "setup",
        "--vendor",
        "local",
        "--base-url",
        BASE_URL,
        "--model",
        NEW_MODEL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(workdir),
    )
    raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=90)
    out = raw_out.decode()
    assert process.returncode == 0, raw_err.decode()
    assert "daemon: settings reloaded" in out, out
    assert core.providers.get("local").model == NEW_MODEL
