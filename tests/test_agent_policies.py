"""M15 §A (tool-use discipline) and §B5 (skill / MCP load timing).

The prompt half is asserted on the composed stable tier; the code half is
asserted on the guards themselves — a rule that only lives in prose is a rule
the model can ignore.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.prompts import compose
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.session.session import Session
from snowpea_core.skills import marketplace
from snowpea_core.skills.loader import SkillLoader
from snowpea_core.tools import file_state, fs, output_spill
from snowpea_core.tools.registry import ToolContext, register_builtin_tools

# ``asyncio_mode = "auto"`` runs the async tests here; no module-level mark is
# needed, and one would warn on every synchronous test in this file.

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"
FIXTURE_ECHO = Path(__file__).parent / "fixtures" / "mcp" / "echo_server.py"


# ---------------------------------------------------------------------------
# §A1 the working-discipline fragment
# ---------------------------------------------------------------------------


def test_working_discipline_is_in_the_stable_tier() -> None:
    tiers = compose.build_tiers()
    assert "Working discipline." in tiers.stable
    assert "never answer from memory" in tiers.stable.lower()
    assert "not a successful task" in tiers.stable
    assert "`explore` agent" in tiers.stable
    # Stable, not context or volatile: it changes with the mode, never per turn.
    assert "Working discipline." not in tiers.context
    assert "Working discipline." not in tiers.volatile


@pytest.mark.parametrize("vendor", ["openai-family", "small-local"])
def test_the_weaker_families_are_told_to_call_it_in_the_same_response(vendor: str) -> None:
    text = compose.build_tiers(vendor_class=vendor).stable
    assert "in the same response" in text


def test_the_anthropic_vendor_layer_still_adds_nothing() -> None:
    from snowpea_core.prompts.loader import load

    assert load("vendors/anthropic") == ""


# ---------------------------------------------------------------------------
# §A3 the read-before-write guard
# ---------------------------------------------------------------------------


class _Backend:
    """The two calls ``fs`` makes, against a real directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def read_file(self, path: str) -> str:
        target = self.root / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _ctx(root: Path, session_id: str, *, parent: str | None = None, guard: bool = True) -> Any:
    settings = Settings()
    settings.tools.readBeforeWrite = guard
    core = Core(settings=settings, paths=Paths.create(root / "home"), token="t")
    session = Session(id=session_id, workdir=root, parent_session_id=parent)
    return ToolContext(session=session, core=core, backend=_Backend(root))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


async def test_a_file_never_read_is_edited_with_a_note(tmp_path: Path) -> None:
    """Hermes warns and proceeds; snowpea used to refuse and cost a re-read round."""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ctx = _ctx(tmp_path, "s-1")

    result = await fs.patch(ctx, {"path": "a.txt", "old_string": "hello", "new_string": "bye"})

    assert result.ok is True, result.error
    assert "note: " in (result.output or "") and "has not been read" in (result.output or "")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bye\n"


async def test_a_full_read_unlocks_the_edit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ctx = _ctx(tmp_path, "s-1")

    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True
    result = await fs.patch(ctx, {"path": "a.txt", "old_string": "hello", "new_string": "bye"})

    assert result.ok is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bye\n"


async def test_a_partial_read_is_noted_on_the_write(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    ctx = _ctx(tmp_path, "s-1")

    windowed = await fs.read_file(ctx, {"path": "a.txt", "offset": 2, "limit": 2})
    assert windowed.output == "two\nthree\n"

    result = await fs.write_file(ctx, {"path": "a.txt", "content": "new\n"})
    assert result.ok is True
    assert "only read in part" in (result.output or "")

    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True
    again = await fs.write_file(ctx, {"path": "a.txt", "content": "newer\n"})
    assert again.ok is True and "note:" not in (again.output or "")


async def test_a_finished_siblings_write_is_noted_not_refused(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    first = _ctx(tmp_path, "s-child-1", parent="s-parent")
    second = _ctx(tmp_path, "s-child-2", parent="s-parent")

    assert (await fs.read_file(first, {"path": "a.txt"})).ok is True
    assert (await fs.read_file(second, {"path": "a.txt"})).ok is True
    assert (await fs.write_file(second, {"path": "a.txt", "content": "from two\n"})).ok is True

    # The sibling has finished: like Hermes, the edit goes through with a note
    # naming who wrote the file in between (a *running* sibling is refused —
    # see tests/test_delegation_policy.py).
    edited = await fs.patch(
        first, {"path": "a.txt", "old_string": "from two", "new_string": "from one"}
    )
    assert edited.ok is True, edited.error
    assert "s-child-2" in (edited.output or "")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "from one\n"

    assert (await fs.read_file(first, {"path": "a.txt"})).ok is True
    again = await fs.write_file(first, {"path": "a.txt", "content": "from one again\n"})
    assert again.ok is True and "note:" not in (again.output or "")


async def test_an_unrelated_session_is_not_a_sibling(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    mine = _ctx(tmp_path, "s-mine")
    theirs = _ctx(tmp_path, "s-theirs")

    assert (await fs.read_file(mine, {"path": "a.txt"})).ok is True
    assert (await fs.read_file(theirs, {"path": "a.txt"})).ok is True
    assert (await fs.write_file(theirs, {"path": "a.txt", "content": "theirs\n"})).ok is True

    # Different groups keep separate registries: no cross-project surprise.
    assert (await fs.write_file(mine, {"path": "a.txt", "content": "mine\n"})).ok is True


async def test_a_new_file_needs_no_prior_read(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "s-1")
    result = await fs.write_file(ctx, {"path": "fresh.txt", "content": "new\n"})
    assert result.ok is True
    assert (tmp_path / "fresh.txt").read_text(encoding="utf-8") == "new\n"


async def test_the_guard_can_be_switched_off(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ctx = _ctx(tmp_path, "s-1", guard=False)

    result = await fs.patch(ctx, {"path": "a.txt", "old_string": "hello", "new_string": "bye"})
    assert result.ok is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bye\n"


# ---------------------------------------------------------------------------
# §A4 the output spill
# ---------------------------------------------------------------------------


def test_short_output_is_left_alone(tmp_path: Path) -> None:
    text = "\n".join(f"line {n}" for n in range(10))
    spilled = output_spill.spill(text, head_lines=30, tail_lines=10, kind="shell", home=tmp_path)
    assert spilled.text == text
    assert spilled.trimmed is False


def test_long_output_keeps_a_head_a_tail_and_a_pointer(tmp_path: Path) -> None:
    text = "\n".join(f"line {n}" for n in range(500))
    spilled = output_spill.spill(text, head_lines=30, tail_lines=10, kind="shell", home=tmp_path)

    assert spilled.trimmed is True
    lines = spilled.text.splitlines()
    assert lines[0] == "line 0"
    assert lines[29] == "line 29"
    assert lines[-1] == "line 499"
    pointer = lines[30]
    assert pointer.startswith("[… 460 lines omitted — read_file(")
    assert "offset=31" in pointer and "limit=460" in pointer

    assert spilled.path is not None
    assert spilled.path.parent == tmp_path / "cache" / "tool-output"
    assert spilled.path.read_text(encoding="utf-8") == text
    # The pointer is a call that works: the omitted middle is really there.
    stored = spilled.path.read_text(encoding="utf-8").splitlines()
    assert stored[30:490] == [f"line {n}" for n in range(30, 490)]


def test_the_pointer_names_the_file_it_wrote(tmp_path: Path) -> None:
    text = "\n".join(str(n) for n in range(200))
    spilled = output_spill.spill(text, head_lines=20, tail_lines=5, kind="grep", home=tmp_path)
    assert spilled.path is not None
    assert str(spilled.path) in spilled.text
    assert spilled.path.name.startswith("grep-")


def test_max_result_lines_comes_from_settings() -> None:
    settings = Settings()
    core = type("C", (), {"settings": settings})()
    assert output_spill.max_result_lines(core) == 400
    settings.tools.maxResultLines = 50
    assert output_spill.max_result_lines(core) == 50
    settings.tools.maxResultLines = 1  # floored, never absurd
    assert output_spill.max_result_lines(core) == 20


# ---------------------------------------------------------------------------
# §B5a-c the loader
# ---------------------------------------------------------------------------


def _loader(home: Path, store: Any = None, sessions: Any = None) -> SkillLoader:
    core = type(
        "C",
        (),
        {
            "paths": Paths.create(home),
            "store": store,
            "sessions": sessions or type("S", (), {"list": staticmethod(lambda: [])})(),
            "commands": None,
            "hub": None,
        },
    )()
    from snowpea_core.commands.registry import CommandRegistry

    core.commands = CommandRegistry()  # type: ignore[attr-defined]
    return SkillLoader(core)  # type: ignore[arg-type]


def _skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: the {name} skill\n---\n\nDo {name}.\n",
        encoding="utf-8",
    )


def test_workdirs_include_the_projects_the_store_remembers(tmp_path: Path) -> None:
    live = tmp_path / "live"
    stored = tmp_path / "stored"
    gone = tmp_path / "gone"
    live.mkdir()
    stored.mkdir()

    store = type(
        "Store",
        (),
        {"session_workdirs": staticmethod(lambda limit=50: [str(stored), str(gone)])},
    )()
    sessions = type(
        "Sessions",
        (),
        {"list": staticmethod(lambda: [type("Row", (), {"workdir": str(live)})()])},
    )()
    loader = _loader(tmp_path / "home", store=store, sessions=sessions)

    found = loader.workdirs()
    assert live in found
    assert stored in found
    assert gone not in found  # a deleted checkout is not walked


def test_a_stored_project_skill_loads_at_boot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _skill(project / ".snowpea" / "skills" / "stored-one", "stored-one")
    store = type(
        "Store", (), {"session_workdirs": staticmethod(lambda limit=50: [str(project)])}
    )()
    loader = _loader(tmp_path / "home", store=store)

    loader.load_sync()

    assert "stored-one" in loader.skills
    assert any(command.name == "stored-one" for command in loader.core.commands.list())


def test_a_bare_skill_md_plugin_is_registered(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _skill(home / "plugins" / "flux", "flux")
    loader = _loader(home)

    loader.load_sync()

    assert "flux" in loader.skills
    assert loader.skills["flux"].kind == "skill"


async def test_reload_workdir_registers_one_project_incrementally(tmp_path: Path) -> None:
    loader = _loader(tmp_path / "home")
    loader.load_sync()
    assert "later" not in loader.skills

    project = tmp_path / "project"
    _skill(project / ".snowpea" / "skills" / "later", "later")
    await loader.reload_workdir(project)

    assert "later" in loader.skills
    assert any(command.name == "later" for command in loader.core.commands.list())


# ---------------------------------------------------------------------------
# §B5d installing a bare skill
# ---------------------------------------------------------------------------


async def test_a_bare_skill_installs_into_skills_not_plugins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source" / "tidy"
    _skill(source, "tidy")

    target = await marketplace.install(str(source), home / "plugins", home)

    assert target == home / "skills" / "tidy"
    assert (target / "SKILL.md").is_file()
    assert not (home / "plugins" / "tidy").exists()


async def test_a_real_plugin_bundle_still_lands_in_plugins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source" / "bundle"
    _skill(source / "skills" / "inner", "inner")
    (source / "plugin.json").write_text(json.dumps({"name": "bundle"}), encoding="utf-8")

    target = await marketplace.install(str(source), home / "plugins", home)

    assert target == home / "plugins" / "bundle"
    assert not (home / "skills" / "bundle").exists()


def test_is_bare_skill_says_no_to_a_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _skill(bundle, "outer")
    (bundle / "commands").mkdir()
    assert marketplace.is_bare_skill(bundle) is False


# ---------------------------------------------------------------------------
# §B5b/e the daemon and session.create
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def _commands(client: RpcClient) -> set[str]:
    result = await client.ok("command.list", {})
    return {str(row["name"]) for row in result["commands"]}


async def test_session_create_loads_that_projects_skills_first(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "brand-new"
    _skill(project / ".snowpea" / "skills" / "x", "x")

    client = await connect(http, daemon)
    assert "x" not in await _commands(client)

    await client.ok("session.create", {"workdir": str(project), "mode": "accept"})

    # No reload, no restart: the command is there for the very first prompt.
    assert "x" in await _commands(client)
    skills = await client.ok("skill.list", {})
    assert any(row["name"] == "x" for row in skills["skills"])

    await client.stop()


async def test_a_global_mcp_server_starts_at_boot(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "boot-echo": {"command": sys.executable, "args": [str(FIXTURE_ECHO)]}
                }
            }
        ),
        encoding="utf-8",
    )

    with fake_provider(FIXTURE):
        instance = await make_daemon(home)
        try:
            client = await connect(http, instance)
            # No session has been created, so only the boot-time sync can
            # explain the tool being here.
            tools = await client.ok("tool.list", {})
            names = {str(row["name"]) for row in tools["tools"]}
            assert any("boot-echo" in name for name in names), sorted(names)
            await client.stop()
        finally:
            from snowpea_core.tools import mcp_client

            await mcp_client.MANAGER.close_all()
            await instance.stop()


def test_register_builtin_tools_still_exposes_the_file_tools() -> None:
    core = type("C", (), {})()
    del core
    from snowpea_core.tools.registry import ToolRegistry

    registry = register_builtin_tools(ToolRegistry())
    read = registry.get("read_file")
    assert read is not None
    assert "offset" in read.input_schema["properties"]
    assert "limit" in read.input_schema["properties"]
