"""m27, M3: a project's ``.mcp.json`` stdio command is judged by the security lists.

A trusted project's server started without the check hooks get.  Now a
project stdio server whose command line the lists block is never started: its
``mcp.list`` row says why.  A global server is the person's own entry and is
not judged.

Every blocked command below is DATA: the server start is replaced by a
recorder for those, so nothing but the harmless echo fixture is ever spawned.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import mcp_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import McpListParams
from snowpea_core.tools import mcp_client
from snowpea_core.tools.mcp_client import McpError, McpServer, McpServerConfig
from snowpea_core.tools.registry import register_builtin_tools

FIXTURE_ECHO = Path(__file__).parent / "fixtures" / "mcp" / "echo_server.py"
#: Hits the essential command list (a disk format).  Never run: see ``started``.
BLOCKED = {"command": "mkfs.ext4", "args": ["/dev/sdb1"]}


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools)
    mcp_client.MANAGER.bind(built)
    return built


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest_asyncio.fixture(autouse=True)
async def _stop_servers() -> AsyncIterator[None]:
    mcp_client.MANAGER.listeners = []
    yield
    await mcp_client.MANAGER.close_all()
    mcp_client.MANAGER.listeners = []


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces the spawn: records the server and reports it ready."""
    seen: list[str] = []

    async def supervise(self: McpServer) -> None:
        seen.append(self.config.name)
        assert self._ready is not None
        self._ready.set_result(None)
        await self._stop.wait()

    monkeypatch.setattr(McpServer, "_supervise", supervise)
    return seen


def _mcp_json(project: Path, servers: dict[str, Any]) -> None:
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


async def test_a_blocked_project_server_is_not_started(core: Core, project: Path,
                                                       started: list[str]) -> None:
    _mcp_json(project, {"wiper": BLOCKED})
    configs = mcp_client.discover(core.paths.home, project)
    assert configs["wiper"].project == str(project)
    assert await mcp_client.register_config(core, configs["wiper"]) == []
    assert started == []
    server = mcp_client.MANAGER.get("wiper")
    assert server is not None and server.state == "error"
    assert "blocked by the security list" in (server.error or "")
    assert "global server" in (server.error or "")
    listed = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(project)), core)
    row = next(s for s in listed.servers if s.name == "wiper")
    assert row.state == "error" and "security list" in (row.error or "")
    # A restart does not get round it.
    with pytest.raises(McpError):
        await server.start()
    assert started == []


async def test_a_global_server_is_not_judged_this_way(core: Core, started: list[str]) -> None:
    config = McpServerConfig.parse("mine", dict(BLOCKED), "global")
    assert config is not None
    assert mcp_client.project_command_refusal(config, core.paths.home) is None
    await mcp_client.MANAGER.register(config).start()
    assert started == ["mine"]


async def test_an_ordinary_project_server_starts(core: Core, project: Path) -> None:
    _mcp_json(project, {"echo": {"command": sys.executable, "args": [str(FIXTURE_ECHO)]}})
    config = mcp_client.discover(core.paths.home, project)["echo"]
    assert mcp_client.project_command_refusal(config, core.paths.home) is None
    names = await mcp_client.register_config(core, config)
    assert names
    server = mcp_client.MANAGER.get("echo")
    assert server is not None and server.state == "ready"


def test_the_lists_switched_off_do_not_judge(core: Core, project: Path) -> None:
    _mcp_json(project, {"wiper": BLOCKED})
    config = mcp_client.discover(core.paths.home, project)["wiper"]
    assert mcp_client.project_command_refusal(config, core.paths.home) is not None
    core.settings.security.enabled = False
    assert mcp_client.project_command_refusal(config, core.paths.home) is None


# -- m27 fix, M-3: env and cwd are part of what is judged ----------------------------------

#: A server entry whose own line is harmless; what it sets in env is not.  DATA only.
ENV_SWAPS = [
    {"PATH": "/tmp/m27-not-a-dir:/usr/bin"},
    {"NODE_OPTIONS": "--require /tmp/m27-not-a-file.js"},
    {"LD_PRELOAD": "/tmp/m27-not-a-file.so"},
]


@pytest.mark.parametrize("env", ENV_SWAPS)
async def test_an_env_a_program_reads_as_code_is_judged(core: Core, project: Path,
                                                        started: list[str],
                                                        env: dict[str, str]) -> None:
    _mcp_json(project, {"s": {"command": "node", "args": ["server.js"], "env": env}})
    config = mcp_client.discover(core.paths.home, project)["s"]
    refusal = mcp_client.project_command_refusal(config, core.paths.home)
    assert refusal is not None and "not started" in refusal
    assert next(iter(env)) in refusal
    assert await mcp_client.register_config(core, config) == []
    assert started == []
    # The same entry without the env starts (unknownCode "auto", the default).
    _mcp_json(project, {"s": {"command": "node", "args": ["server.js"]}})
    plain = mcp_client.discover(core.paths.home, project)["s"]
    assert mcp_client.project_command_refusal(plain, core.paths.home) is None


def test_an_ordinary_env_is_fine(core: Core, project: Path) -> None:
    _mcp_json(project, {"s": {"command": "uvx", "args": ["mcp-server-fetch"],
                              "env": {"API_KEY": "abc", "NODE_ENV": "production"}}})
    config = mcp_client.discover(core.paths.home, project)["s"]
    assert mcp_client.project_command_refusal(config, core.paths.home) is None


@pytest.mark.parametrize("where", ["home", "parent", "absolute-elsewhere"])
async def test_a_cwd_outside_the_project_is_refused(core: Core, project: Path, tmp_path: Path,
                                                    started: list[str], where: str) -> None:
    cwd = {"home": str(core.paths.home), "parent": "..",
           "absolute-elsewhere": str(tmp_path)}[where]
    _mcp_json(project, {"s": {"command": "uvx", "args": ["mcp-server-fetch"], "cwd": cwd}})
    config = mcp_client.discover(core.paths.home, project)["s"]
    refusal = mcp_client.project_command_refusal(config, core.paths.home)
    assert refusal is not None and "outside the project folder" in refusal
    assert await mcp_client.register_config(core, config) == []
    assert started == []


async def test_a_cwd_inside_the_project_starts_there(core: Core, project: Path,
                                                     started: list[str]) -> None:
    (project / "srv").mkdir()
    _mcp_json(project, {"s": {"command": "uvx", "args": ["mcp-server-fetch"], "cwd": "srv"}})
    config = mcp_client.discover(core.paths.home, project)["s"]
    assert mcp_client.project_command_refusal(config, core.paths.home) is None
    assert mcp_client.project_cwd(config) == str(project / "srv")
    await mcp_client.MANAGER.register(config).start()
    assert started == ["s"]


def test_the_control_folder_as_cwd_is_refused_even_inside(core: Core, tmp_path: Path) -> None:
    """A project whose folder holds snowpea's home: the check itself refuses."""
    root = core.paths.home.parent
    _mcp_json(root, {"s": {"command": "node", "args": ["server.js"],
                           "cwd": core.paths.home.name}})
    config = mcp_client.discover(core.paths.home, root)["s"]
    refusal = mcp_client.project_command_refusal(config, core.paths.home)
    assert refusal is not None and "not started" in refusal


def test_unknown_code_ask_refuses_an_opaque_line(core: Core, project: Path) -> None:
    _mcp_json(project, {"s": {"command": "node", "args": ["server.js"]}})
    config = mcp_client.discover(core.paths.home, project)["s"]
    assert mcp_client.project_command_refusal(config, core.paths.home) is None
    core.settings.security.unknownCode = "ask"
    refusal = mcp_client.project_command_refusal(config, core.paths.home)
    assert refusal is not None and "needs a person's answer" in refusal


# -- m27 fix, M-4: mcp.reload reads a project's servers only as trusted ----------------------


@pytest.mark.real_project_trust
async def test_reload_starts_no_server_of_an_untrusted_or_changed_project(
        core: Core, project: Path, started: list[str]) -> None:
    from snowpea_core.server.protocol import McpReloadParams
    from snowpea_core.trust import projects as trust

    trust.set_default_home(core.paths.home)
    _mcp_json(project, {"p": {"command": "uvx", "args": ["mcp-server-fetch"]}})
    reload = McpReloadParams(workdir=str(project))
    await mcp_handlers.mcp_reload_handler(None, reload, core)
    assert started == []
    _listed_untrusted(await _rows(core, project), "p")
    trust.store_for(core.paths.home).set(project, ["mcp"], by="ide")
    await mcp_handlers.mcp_reload_handler(None, reload, core)
    assert started == ["p"]
    assert (await _rows(core, project))["p"].state == "ready"
    # Changed since it was trusted: none of it starts, and what ran stops.
    _mcp_json(project, {"p": {"command": "uvx", "args": ["mcp-server-fetch"]},
                        "q": {"command": "uvx", "args": ["mcp-server-time"]}})
    await mcp_handlers.mcp_reload_handler(None, reload, core)
    assert started == ["p"] and mcp_client.MANAGER.get("p") is None
    rows = await _rows(core, project)
    _listed_untrusted(rows, "p")
    _listed_untrusted(rows, "q")


async def _rows(core: Core, project: Path) -> dict[str, Any]:
    listed = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(project)), core)
    return {s.name: s for s in listed.servers}


def _listed_untrusted(rows: dict[str, Any], name: str) -> None:
    row = rows[name]
    assert row.scope == "project" and row.state == "stopped"
    assert row.error == mcp_handlers.UNTRUSTED_TEXT


@pytest.mark.real_project_trust
async def test_an_untrusted_project_entry_is_listed_edited_and_removed_never_started(
        core: Core, project: Path, started: list[str]) -> None:
    """The browser's Settings › MCP saves project servers, then says the folder
    must be trusted: the person can still see, fix and delete what was saved."""
    from snowpea_core.server.protocol import McpEntryFields, McpRemoveParams, McpUpdateParams
    from snowpea_core.trust import projects as trust

    trust.set_default_home(core.paths.home)
    _mcp_json(project, {"p": {"command": "uvx", "args": ["mcp-server-fetch"]}})
    _listed_untrusted(await _rows(core, project), "p")
    await mcp_handlers.mcp_update_handler(None, McpUpdateParams(
        name="p", scope="project", workdir=str(project),
        patch=McpEntryFields(args=["mcp-server-time"])), core)
    saved = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["p"]["args"] == ["mcp-server-time"]
    rows = await _rows(core, project)
    _listed_untrusted(rows, "p")
    assert rows["p"].args == ["mcp-server-time"]
    assert started == [] and mcp_client.MANAGER.get("p") is None
    await mcp_handlers.mcp_remove_handler(
        None, McpRemoveParams(name="p", scope="project", workdir=str(project)), core)
    saved = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "p" not in saved["mcpServers"]
    assert "p" not in await _rows(core, project)
    assert started == []
