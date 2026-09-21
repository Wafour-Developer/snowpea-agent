"""The agent-facing skill tools: ``skill_search`` / ``list`` / ``install`` / ``remove``.

No daemon here — the tools only need a loader, a command registry and a
``$SNOWPEA_HOME``, so the tests drive them directly and stay fast.  Search runs
against the same pinned fixtures ``test_plugin_load`` uses, with the
marketplace ``FETCHER`` and the registry client swapped for fixture readers.
Install runs against a local fixture plugin, and the assertion that matters is
that its ``/`` command is registered by the time the tool returns — that is what
"available without a restart" means in practice.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config.paths import Paths
from snowpea_core.skills import marketplace, registry_client
from snowpea_core.skills.loader import SkillLoader
from snowpea_core.tools import skills_tools
from snowpea_core.tools.registry import ToolContext, effective_permission

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures"
MARKETPLACE_DIR = FIXTURES / "marketplace"

TOOLS = {tool.name: tool for tool in skills_tools.TOOLS}


class FixtureFetcher(marketplace.HttpFetcher):
    """Reads the pinned marketplace json instead of the network."""

    async def get_json(self, url: str) -> Any:
        return json.loads(Path(url).read_text(encoding="utf-8"))


class FixtureRegistry:
    """The hosted registry, offline: two hits, one of them rated."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or json.loads(
            (MARKETPLACE_DIR / "registry.json").read_text(encoding="utf-8")
        )

    async def search_with_sources(
        self, query: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        needle = query.lower()
        hits = [
            item
            for item in self._payload.get("results", [])
            if needle in str(item.get("name", "")).lower()
            or needle in str(item.get("description", "")).lower()
        ]
        return hits, list(self._payload.get("unavailable", []))

    async def resolve(self, identifier: str) -> str | None:
        return None


def write_plugin(root: Path, name: str) -> Path:
    """A minimal plugin: a manifest and one user-invocable skill."""
    directory = root / name
    (directory / "skills" / "greet").mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.2.3", "description": "a fixture plugin"}),
        encoding="utf-8",
    )
    (directory / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Say hello to $ARGUMENTS.\n---\n\nSay hello.\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    """A tool context over a real loader rooted in a throwaway SNOWPEA_HOME."""
    monkeypatch.setattr(marketplace, "FETCHER", FixtureFetcher())
    monkeypatch.setattr(registry_client, "CLIENT", FixtureRegistry())

    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "project"
    workdir.mkdir()
    # No builtin skills and no open sessions: the loader then sees only what a
    # test installs, so the before/after diff is exactly the plugin under test.
    core = SimpleNamespace(
        paths=Paths(home=home),
        commands=CommandRegistry(),
        sessions=SimpleNamespace(list=lambda: []),
        hub=None,
    )
    loader = SkillLoader(core)  # type: ignore[arg-type]
    monkeypatch.setattr("snowpea_core.skills.loader.builtin_root", lambda: tmp_path / "absent")
    loader.load_sync()
    core.skills = loader
    session = SimpleNamespace(id="s-1", workdir=str(workdir))
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


def use_fixture_marketplace(ctx: ToolContext) -> None:
    marketplace.save_marketplaces(
        ctx.core.paths.home,
        [{"name": "fixture-market", "url": str(MARKETPLACE_DIR / "claude.json")}],
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_merges_both_sources_with_install_specs(ctx: ToolContext) -> None:
    use_fixture_marketplace(ctx)

    result = await skills_tools.skill_search(ctx, {"query": "pdf"})

    assert result.ok
    assert result.meta == {"query": "pdf", "count": 3, "unavailable": []}
    assert "pdf-toolkit" in result.output
    # The marketplace entry carries its own source url, so that is the spec.
    assert "install spec: https://github.com/snowpea-fixtures/pdf-toolkit" in result.output
    assert "install spec: clawhub:@fixtures/pdf-extract" in result.output
    assert "[ClawHub]" in result.output
    # The one thing the model must not do is answer with a CLI command.
    assert "snowpea skill install" not in result.output
    # Only pdf hits; the xlsx plugin in the same marketplace does not match.
    assert "spreadsheet-toolkit" not in result.output


async def test_search_can_keep_one_source(ctx: ToolContext) -> None:
    use_fixture_marketplace(ctx)

    result = await skills_tools.skill_search(
        ctx, {"query": "pdf", "sources": ["claude-marketplace"]}
    )

    assert result.ok and result.meta["count"] == 1
    assert "pdf-toolkit" in result.output and "pdf-extract" not in result.output


async def test_search_shows_rating_and_downloads_when_published(ctx: ToolContext) -> None:
    marketplace.save_marketplaces(ctx.core.paths.home, [])
    payload = {
        "results": [
            {
                "id": "registry:pdf-pro",
                "name": "pdf-pro",
                "description": "Rated pdf tooling.",
                "installSpec": "registry:pdf-pro",
                "rating": 4.5,
                "downloads": 1200,
            }
        ]
    }
    registry_client.CLIENT = FixtureRegistry(payload)  # type: ignore[assignment]

    result = await skills_tools.skill_search(ctx, {"query": "pdf"})

    assert "4.5 stars, 1200 downloads" in result.output


async def test_search_says_offline_rather_than_empty(ctx: ToolContext) -> None:
    class Dead(marketplace.HttpFetcher):
        async def get_json(self, url: str) -> Any:
            raise OSError("network is unreachable")

    marketplace.FETCHER = Dead()
    registry_client.CLIENT = FixtureRegistry({"results": []})  # type: ignore[assignment]
    use_fixture_marketplace(ctx)

    result = await skills_tools.skill_search(ctx, {"query": "pdf"})

    assert result.ok and result.meta["count"] == 0
    assert "network is unreachable" in result.output
    assert "offline, not empty" in result.output


async def test_search_needs_a_query(ctx: ToolContext) -> None:
    result = await skills_tools.skill_search(ctx, {})
    assert not result.ok and "query is required" in (result.error or "")


# ---------------------------------------------------------------------------
# install, list, remove
# ---------------------------------------------------------------------------


async def test_install_from_a_local_path_makes_the_command_live(
    ctx: ToolContext, tmp_path: Path
) -> None:
    source = write_plugin(tmp_path / "src", "fixture-plugin")
    assert ctx.core.commands.get("greet") is None

    result = await skills_tools.skill_install(ctx, {"spec": str(source)})

    assert result.ok, result.error
    assert "installed fixture-plugin" in result.output
    assert "new commands: /greet" in result.output
    assert "no restart" in result.output
    # The reload is the point: the command is callable without a daemon restart.
    assert ctx.core.commands.get("greet") is not None
    assert (ctx.core.paths.home / "plugins" / "fixture-plugin" / "plugin.json").is_file()


async def test_install_resolves_a_path_relative_to_the_session_workdir(
    ctx: ToolContext,
) -> None:
    workdir = Path(ctx.session.workdir)
    write_plugin(workdir, "relative-plugin")

    result = await skills_tools.skill_install(ctx, {"spec": "./relative-plugin"})

    assert result.ok, result.error
    assert (ctx.core.paths.home / "plugins" / "relative-plugin").is_dir()


async def test_install_reports_an_unresolvable_spec_instead_of_raising(
    ctx: ToolContext,
) -> None:
    result = await skills_tools.skill_install(ctx, {"spec": "no-such-thing"})
    assert not result.ok and "could not resolve" in (result.error or "")


async def test_list_names_the_plugin_its_version_and_its_path(
    ctx: ToolContext, tmp_path: Path
) -> None:
    source = write_plugin(tmp_path / "src", "fixture-plugin")
    await skills_tools.skill_install(ctx, {"spec": str(source)})

    result = await skills_tools.skill_list(ctx, {})

    assert result.ok and result.meta == {"plugins": 1}
    assert "fixture-plugin 1.2.3 — a fixture plugin" in result.output
    assert str(ctx.core.paths.home / "plugins" / "fixture-plugin") in result.output
    assert "greet [plugin:fixture-plugin]" in result.output


async def test_list_on_an_empty_home(ctx: ToolContext) -> None:
    result = await skills_tools.skill_list(ctx, {})
    assert result.ok and "No plugins are installed." in result.output


async def test_remove_deletes_the_plugin_and_the_command(ctx: ToolContext, tmp_path: Path) -> None:
    source = write_plugin(tmp_path / "src", "fixture-plugin")
    await skills_tools.skill_install(ctx, {"spec": str(source)})

    result = await skills_tools.skill_remove(ctx, {"name": "fixture-plugin"})

    assert result.ok, result.error
    assert "commands that are gone: /greet" in result.output
    assert ctx.core.commands.get("greet") is None
    assert not (ctx.core.paths.home / "plugins" / "fixture-plugin").exists()


async def test_remove_says_what_is_installed_when_the_name_is_wrong(
    ctx: ToolContext, tmp_path: Path
) -> None:
    source = write_plugin(tmp_path / "src", "fixture-plugin")
    await skills_tools.skill_install(ctx, {"spec": str(source)})

    result = await skills_tools.skill_remove(ctx, {"name": "typo"})

    assert not result.ok
    assert "no installed plugin named 'typo'" in (result.error or "")
    assert "fixture-plugin" in (result.error or "")


# ---------------------------------------------------------------------------
# permissions and registration
# ---------------------------------------------------------------------------


async def test_permission_tags_gate_install_and_remove_but_not_search() -> None:
    assert TOOLS["skill_search"].permission == "read"
    assert TOOLS["skill_list"].permission == "read"
    # exec, not network: an install unpacks third-party code into SNOWPEA_HOME
    # and runs it in this session, and plan mode must not be able to do that.
    assert TOOLS["skill_install"].permission == "exec"
    assert TOOLS["skill_remove"].permission == "exec"
    for tool in skills_tools.TOOLS:
        assert effective_permission(tool, {}, None, None) == tool.permission


async def test_accept_mode_asks_before_installing() -> None:
    from snowpea_core.permissions.policy import PermissionPolicy

    policy = PermissionPolicy()
    assert policy.decide("accept", TOOLS["skill_install"].permission) == "ask"
    assert policy.decide("accept", TOOLS["skill_remove"].permission) == "ask"
    assert policy.decide("accept", TOOLS["skill_search"].permission) == "allow"
    # Plan mode asks before an ``exec`` call rather than refusing it (621bb0b);
    # an install still cannot happen without the user saying so.
    assert policy.decide("plan", TOOLS["skill_install"].permission) == "ask"


async def test_the_four_tools_are_in_the_builtin_catalog() -> None:
    from snowpea_core.tools.registry import ToolRegistry, register_builtin_tools

    registry = register_builtin_tools(ToolRegistry())
    for name in ("skill_search", "skill_list", "skill_install", "skill_remove"):
        tool = registry.get(name)
        assert tool is not None and tool.category == "skills"
        assert tool.description and tool.input_schema["type"] == "object"
    # The search description has to send the model here instead of to the CLI.
    assert "snowpea CLI" in registry.get("skill_search").description  # type: ignore[union-attr]


async def test_skill_install_notes_when_already_available_in_claude_plugin_cache(
    ctx: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_bundle = tmp_path / "claude_cache" / "frontend-design"
    cache_bundle.mkdir(parents=True, exist_ok=True)
    (cache_bundle / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (cache_bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "frontend-design", "version": "1.0.0"}),
        encoding="utf-8",
    )
    installed_file = ctx.core.paths.home / ".claude" / "plugins" / "installed_plugins.json"
    installed_file.parent.mkdir(parents=True, exist_ok=True)
    installed_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "frontend-design@claude-code-plugins": [
                        {
                            "scope": "user",
                            "installPath": str(cache_bundle),
                            "version": "1.0.0",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    loader = ctx.core.skills
    await loader.reload()
    assert "frontend-design" in loader.claude_plugin_names

    async def fake_install(spec: str) -> Path:
        target = ctx.core.paths.home / "plugins" / "frontend-design"
        target.mkdir(parents=True, exist_ok=True)
        (target / "plugin.json").write_text(
            json.dumps({"name": "frontend-design", "version": "1.0.1"}),
            encoding="utf-8",
        )
        (target / "skills" / "design").mkdir(parents=True, exist_ok=True)
        (target / "skills" / "design" / "SKILL.md").write_text(
            "---\nname: design\ndescription: Design UI\n---\n\nBody.\n",
            encoding="utf-8",
        )
        await loader.reload()
        return target

    monkeypatch.setattr(loader, "install", fake_install)

    result = await skills_tools.skill_install(
        ctx, {"spec": "github:claude-code-plugins/frontend-design@frontend-design"}
    )
    assert result.ok, result.error
    assert (
        "already available from Claude Code's plugin cache; installing a snowpea copy anyway"
        in result.output
    )
    assert "installed frontend-design" in result.output


async def test_a_plugin_skill_never_replaces_a_core_command(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """A Claude Code plugin ships ``ralph`` and ``notes``; snowpea ships ``/ralph``.

    Before the guard the skill silently took over ``/ralph`` — the prompt went to
    the model as "Follow these instructions for /ralph…" and the loop never ran —
    and the next rescan unregistered the name, deleting the core command.
    """
    from snowpea_core.commands.registry import register_builtin_commands

    registry = ctx.core.commands
    register_builtin_commands(registry)
    core_ralph = registry.get("ralph")
    assert core_ralph is not None and "ralph" in registry.protected

    bundle = tmp_path / "claude_cache" / "omc"
    (bundle / ".claude-plugin").mkdir(parents=True)
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "omc", "version": "1.0.0"}), encoding="utf-8"
    )
    for name in ("ralph", "notes"):
        (bundle / "skills" / name).mkdir(parents=True)
        (bundle / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: plugin {name}\n---\n\nBody.\n", encoding="utf-8"
        )
    installed = ctx.core.paths.home / ".claude" / "plugins" / "installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {"omc@market": [{"scope": "user", "installPath": str(bundle)}]},
            }
        ),
        encoding="utf-8",
    )

    loader = ctx.core.skills
    await loader.reload()
    # The core command is untouched; the plugin's skill is reachable qualified.
    assert registry.get("ralph") is core_ralph
    assert registry.get("omc:ralph") is not None
    # A name nothing in the core owns is registered plainly, as before.
    assert registry.get("notes") is not None

    # A second scan must not delete the core command either.
    await loader.reload()
    assert registry.get("ralph") is core_ralph
    assert registry.get("omc:ralph") is not None
