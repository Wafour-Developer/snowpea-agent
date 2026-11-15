"""Codex-format plugins and what Codex has installed (protocol 1.52.0).

Every fixture is built under ``tmp_path`` with ``CODEX_HOME`` pointing at it;
nothing here reads the real ``~/.codex`` or ``~/.claude``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.commands.agent_cmd import definitions_for
from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import SkillReadParams, SkillRemoveParams
from snowpea_core.server.skill_handlers import skill_read_handler, skill_remove_handler
from snowpea_core.skills import marketplace
from snowpea_core.skills.loader import CODEX_APPS_NOTE, SkillLoader


def write_skill(directory: Path, name: str, description: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description or name}\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )
    return path


def write_command(directory: Path, name: str, description: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\ndescription: {description or name}\n---\n\nRun {name} with $ARGUMENTS.\n",
        encoding="utf-8",
    )
    return path


def codex_bundle(
    root: Path,
    name: str,
    *,
    version: str = "0.1.0",
    skills: tuple[str, ...] = (),
    commands: tuple[str, ...] = (),
    title: str = "",
    apps: bool = False,
    mcp: bool = False,
    hooks: bool = False,
) -> Path:
    """A Codex-format plugin: ``.codex-plugin/plugin.json`` plus content folders."""
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": f"{name} plugin",
        "author": {"name": "Tester"},
        "skills": "./skills/",
        "interface": {"displayName": title or name.title(), "category": "Coding"},
    }
    if apps:
        manifest["apps"] = "./.app.json"
        (root / ".app.json").parent.mkdir(parents=True, exist_ok=True)
        (root / ".app.json").write_text(json.dumps({"apps": {}}), encoding="utf-8")
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for skill in skills:
        write_skill(root / "skills" / skill, skill, f"{skill} from {name}")
    for command in commands:
        write_command(root / "commands", command, f"{command} from {name}")
    if mcp:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {f"{name}-mcp": {"command": "${PLUGIN_ROOT}/bin/serve"}}}),
            encoding="utf-8",
        )
    if hooks:
        (root / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PostToolUse": [
                            {"matcher": "shell", "hooks": [{"type": "command", "command": "true"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
    return root


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A throwaway snowpea home, a project, a Codex home, and no built-ins."""
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "project"
    workdir.mkdir()
    codex = tmp_path / "codex"
    codex.mkdir()
    builtins = tmp_path / "builtins"
    builtins.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setattr("snowpea_core.skills.loader.builtin_root", lambda: builtins)
    core = SimpleNamespace(
        paths=Paths(home=home),
        commands=CommandRegistry(),
        sessions=SimpleNamespace(list=lambda: [SimpleNamespace(workdir=str(workdir))]),
        hub=None,
        settings=Settings(),
    )
    return SimpleNamespace(core=core, home=home, workdir=workdir, codex=codex)


def load(env: SimpleNamespace) -> SkillLoader:
    loader = SkillLoader(env.core)
    loader.load_sync()
    env.core.skills = loader
    return loader


def cache_dir(env: SimpleNamespace, market: str, plugin: str, version: str) -> Path:
    return env.codex / "plugins" / "cache" / market / plugin / version


# ---------------------------------------------------------------------------
# a Codex-format bundle in snowpea's own plugins/
# ---------------------------------------------------------------------------


def test_codex_bundle_in_plugins_dir_loads_skills_commands_mcp_and_hooks(
    env: SimpleNamespace,
) -> None:
    root = codex_bundle(
        env.home / "plugins" / "cf",
        "cloudflare",
        skills=("workers",),
        commands=("build-agent",),
        title="Cloudflare",
        mcp=True,
        hooks=True,
    )
    loader = load(env)

    skill = loader.skills["workers"]
    assert (skill.source, skill.plugin) == ("plugin:cloudflare", "cloudflare")
    assert skill.format == "codex"
    command = loader.skills["build-agent"]
    assert (command.kind, command.format) == ("command", "codex")
    assert env.core.commands.get("build-agent").info().format == "codex"
    assert loader.mcp_servers["cloudflare-mcp"]["command"] == f"{root}/bin/serve"
    assert loader.hooks.count() == 1

    rows = {(row.name, row.kind): row for row in loader.list()}
    plugin = rows[("cloudflare", "plugin")]
    assert (plugin.format, plugin.title, plugin.note) == ("codex", "Cloudflare", "")
    assert rows[("workers", "skill")].format == "codex"


def test_claude_and_snowpea_bundles_keep_their_format(env: SimpleNamespace) -> None:
    claude = env.home / "plugins" / "kit"
    (claude / ".claude-plugin").mkdir(parents=True)
    (claude / ".claude-plugin" / "plugin.json").write_text('{"name": "kit"}', encoding="utf-8")
    write_skill(claude / "skills" / "kit-skill", "kit-skill")
    write_skill(env.home / "skills" / "mine", "mine")
    loader = load(env)
    assert loader.skills["kit-skill"].format == "claude"
    assert loader.skills["mine"].format == "snowpea"
    assert next(p for p in loader.plugins if p.name == "kit").format == "claude"


# ---------------------------------------------------------------------------
# what Codex has installed
# ---------------------------------------------------------------------------


def test_codex_cache_reads_the_newest_version_and_skips_disabled_plugins(
    env: SimpleNamespace,
) -> None:
    codex_bundle(cache_dir(env, "market", "alpha", "0.9.0"), "alpha", skills=("old-alpha",))
    codex_bundle(
        cache_dir(env, "market", "alpha", "0.10.0"),
        "alpha",
        version="0.10.0",
        skills=("new-alpha",),
        commands=("alpha-cmd",),
        mcp=True,
        hooks=True,
    )
    marker = cache_dir(env, "market", "alpha", "x").parent / ".codex-remote-plugin-install.json"
    marker.write_text('{"schema_version": 1}', encoding="utf-8")
    codex_bundle(cache_dir(env, "market", "beta", "1.0.0"), "beta", skills=("beta-skill",))
    (env.codex / "config.toml").write_text(
        '[plugins."beta@market"]\nenabled = false\n\n[plugins."alpha@market"]\nenabled = true\n',
        encoding="utf-8",
    )
    loader = load(env)

    alpha = next(p for p in loader.plugins if p.name == "alpha")
    assert (alpha.source, alpha.version, alpha.format) == ("codex-plugin", "0.10.0", "codex")
    assert alpha.root.name == "0.10.0"
    assert loader.skills["new-alpha"].source == "codex-plugin"
    assert loader.skills["alpha-cmd"].kind == "command"
    assert "old-alpha" not in loader.skills
    assert not any(p.name == "beta" for p in loader.plugins)
    assert "beta-skill" not in loader.skills
    # An imported plugin's MCP servers and hooks are never started.
    assert loader.mcp_servers == {}
    assert loader.hooks.count() == 0
    assert loader.codex_plugin_names == {"alpha"}
    groups = dict(loader.index_groups())
    assert "[codex-plugin:alpha]" in groups


def test_a_codex_plugin_with_apps_is_marked_not_failed(env: SimpleNamespace) -> None:
    root = cache_dir(env, "openai-curated", "slack", "0.1.8")
    codex_bundle(root, "slack", skills=("slack-post",), apps=True)
    loader = load(env)
    slack = next(p for p in loader.plugins if p.name == "slack")
    assert slack.note == CODEX_APPS_NOTE
    assert loader.skills["slack-post"].source == "codex-plugin"
    row = next(row for row in loader.list() if row.name == "slack" and row.kind == "plugin")
    assert row.note == "needs Codex apps"


def test_codex_home_skills_prompts_and_agent_roles(env: SimpleNamespace) -> None:
    write_skill(env.codex / "skills" / "ultraqa", "ultraqa")
    write_skill(env.codex / "skills" / ".system" / "imagegen", "imagegen")
    write_command(env.codex / "prompts", "analyst", "Pre-planning consultant")
    (env.codex / "agents").mkdir()
    (env.codex / "agents" / "sleuth.toml").write_text(
        'name = "sleuth"\n'
        'description = "Finds things"\n'
        'model = "gpt-5.5"\n'
        'model_reasoning_effort = "high"\n'
        'sandbox_mode = "read-only"\n'
        'nickname_candidates = ["Holmes"]\n'
        'developer_instructions = """\nYou find things.\n"""\n',
        encoding="utf-8",
    )
    (env.codex / "agents" / "quick.toml").write_text(
        'name = "quick"\ndescription = "Fast"\nmodel_reasoning_effort = "minimal"\n'
        'developer_instructions = "Be quick."\n',
        encoding="utf-8",
    )
    (env.codex / "agents" / "broken.toml").write_text("name = [", encoding="utf-8")
    # Never imported: they run commands nobody approved for snowpea.
    (env.codex / "hooks.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}),
        encoding="utf-8",
    )
    (env.codex / "config.toml").write_text(
        '[mcp_servers.secret]\ncommand = "serve"\n', encoding="utf-8"
    )
    loader = load(env)

    assert loader.skills["ultraqa"].source == "codex-global"
    assert loader.skills["ultraqa"].format == "codex"
    assert "imagegen" not in loader.skills
    analyst = loader.skills["analyst"]
    assert (analyst.kind, analyst.source) == ("command", "codex-global")
    assert env.core.commands.get("analyst").info().source == "codex-global"
    assert loader.hooks.count() == 0
    assert loader.mcp_servers == {}

    agents = {agent.name: agent for agent in loader.agents}
    assert set(agents) == {"sleuth", "quick"}
    assert agents["sleuth"].format == "codex"
    assert agents["sleuth"].frontmatter["permission"] == "plan"

    defns = {defn.name: defn for defn in definitions_for(env.core, env.workdir)}
    sleuth = defns["sleuth"]
    assert sleuth.source == "codex-global"
    assert (sleuth.model, sleuth.thinking, sleuth.permission) == ("gpt-5.5", "on", "plan")
    assert sleuth.prompt.strip() == "You find things."
    assert defns["quick"].thinking == "off"
    assert defns["quick"].permission == "inherit"


def test_a_codex_role_never_takes_a_builtin_role_name(env: SimpleNamespace) -> None:
    (env.codex / "agents").mkdir()
    (env.codex / "agents" / "explore.toml").write_text(
        'name = "explore"\ndescription = "Codex explore"\ndeveloper_instructions = "x"\n',
        encoding="utf-8",
    )
    load(env)
    defns = {defn.name: defn for defn in definitions_for(env.core, env.workdir)}
    assert defns["explore"].source == "builtin"


def test_project_agents_skills_are_codex_project(env: SimpleNamespace) -> None:
    write_skill(env.workdir / ".agents" / "skills" / "lint-rules", "lint-rules")
    loader = load(env)
    skill = loader.skills["lint-rules"]
    assert (skill.source, skill.format) == ("codex-project", "codex")


# ---------------------------------------------------------------------------
# precedence and the switch
# ---------------------------------------------------------------------------


def claude_installed(env: SimpleNamespace, bundle: Path, key: str) -> None:
    """Register ``bundle`` in a Claude tree under the snowpea home."""
    plugins = env.home / ".claude" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"plugins": {key: [{"scope": "user", "installPath": str(bundle)}]}}),
        encoding="utf-8",
    )


def test_collisions_snowpea_beats_claude_beats_codex(env: SimpleNamespace, tmp_path: Path) -> None:
    # A skill name all three ship, inside plugins of three different names.
    snow = env.home / "plugins" / "snow"
    snow.mkdir(parents=True)
    (snow / "plugin.json").write_text('{"name": "snow"}', encoding="utf-8")
    write_skill(snow / "skills" / "shared", "shared", "from snowpea")
    claude = tmp_path / "claude-cache" / "clp"
    (claude / ".claude-plugin").mkdir(parents=True)
    (claude / ".claude-plugin" / "plugin.json").write_text('{"name": "clp"}', encoding="utf-8")
    write_skill(claude / "skills" / "shared", "shared", "from claude")
    write_skill(claude / "skills" / "both", "both", "from claude")
    write_command(claude / "commands", "cmd-both", "from claude")
    claude_installed(env, claude, "clp@market")
    codex_bundle(cache_dir(env, "m", "cdx", "1.0.0"), "cdx", skills=("shared", "both", "solo"))
    codex_bundle(cache_dir(env, "m", "clp", "1.0.0"), "clp", skills=("never",))
    write_command(env.codex / "prompts", "cmd-both", "from codex")
    write_skill(env.home / "skills" / "global-one", "global-one", "from snowpea")
    write_skill(env.codex / "skills" / "global-one", "global-one", "from codex")
    (env.codex / "agents").mkdir()
    (env.codex / "agents" / "helper.toml").write_text(
        'name = "helper"\ndescription = "codex"\n', encoding="utf-8"
    )
    (env.home / "agents").mkdir()
    (env.home / "agents" / "helper.md").write_text(
        "---\nname: helper\ndescription: snowpea\n---\nmine\n", encoding="utf-8"
    )
    loader = load(env)

    assert loader.skills["shared"].description == "from snowpea"
    assert loader.skills["both"].source == "claude-plugin"
    assert loader.skills["cmd-both"].source == "claude-plugin"
    assert loader.skills["solo"].source == "codex-plugin"
    assert loader.skills["global-one"].source == "global"
    # A Codex plugin whose name a Claude Code plugin already has is skipped.
    assert [p.source for p in loader.plugins if p.name == "clp"] == ["claude-plugin"]
    assert "never" not in loader.skills
    helpers = [agent for agent in loader.agents if agent.name == "helper"]
    assert [agent.source for agent in helpers] == ["global"]


def test_import_switch_off_reads_nothing_from_codex(env: SimpleNamespace) -> None:
    codex_bundle(cache_dir(env, "m", "alpha", "1.0.0"), "alpha", skills=("alpha-skill",))
    write_skill(env.codex / "skills" / "ultraqa", "ultraqa")
    write_command(env.codex / "prompts", "analyst")
    write_skill(env.workdir / ".agents" / "skills" / "lint-rules", "lint-rules")
    env.core.settings.skills.importCodex = False
    loader = load(env)
    assert not any(p.source == "codex-plugin" for p in loader.plugins)
    assert not any(s.source.startswith("codex") for s in loader.skills.values())
    assert loader.agents == []


def test_codex_home_falls_back_to_dot_codex(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert SkillLoader(env.core).codex_root == tmp_path / "user" / ".codex"


# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------


async def test_codex_items_are_read_only(env: SimpleNamespace) -> None:
    path = write_skill(env.codex / "skills" / "ultraqa", "ultraqa")
    write_skill(env.workdir / ".agents" / "skills" / "lint-rules", "lint-rules")
    load(env)

    read = await skill_read_handler(None, SkillReadParams(name="ultraqa"), env.core)  # type: ignore[arg-type]
    assert (read.readOnly, read.format, read.scope) == (True, "codex", "global")
    project = await skill_read_handler(
        None,  # type: ignore[arg-type]
        SkillReadParams(name="lint-rules", workdir=str(env.workdir)),
        env.core,
    )
    assert (project.readOnly, project.format, project.scope) == (True, "codex", "project")

    with pytest.raises(RpcError) as caught:
        await skill_remove_handler(None, SkillRemoveParams(name="ultraqa"), env.core)  # type: ignore[arg-type]
    assert caught.value.code == "not_found"
    assert path.is_file()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


async def test_install_a_local_codex_folder_lands_in_plugins(
    env: SimpleNamespace, tmp_path: Path
) -> None:
    source = codex_bundle(tmp_path / "src" / "whatever", "linear", skills=("triage",))
    loader = load(env)
    target = await loader.install(str(source))
    assert target == env.home / "plugins" / "linear"
    assert (target / ".codex-plugin" / "plugin.json").is_file()
    assert loader.skills["triage"].source == "plugin:linear"
    assert loader.skills["triage"].format == "codex"


def codex_marketplace_repo(root: Path) -> Path:
    codex_bundle(root / "plugins" / "demo", "demo", skills=("demo-skill",))
    catalog = root / ".agents" / "plugins" / "marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "name": "probe",
                "interface": {"displayName": "Probe"},
                "plugins": [
                    {
                        "name": "demo",
                        "source": {"source": "local", "path": "./plugins/demo"},
                        "policy": {"installation": "AVAILABLE"},
                        "category": "Productivity",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return catalog


async def test_install_from_a_registered_codex_catalog(
    env: SimpleNamespace, tmp_path: Path
) -> None:
    repo = tmp_path / "market"
    catalog = codex_marketplace_repo(repo)
    marketplace.save_marketplaces(
        env.home, [{"name": "probe", "url": str(catalog), "repo": str(repo)}]
    )
    found = await marketplace.search_claude_marketplaces("demo", env.home)
    assert [(hit.name, hit.source, hit.install_spec) for hit in found.hits] == [
        ("demo", "codex-marketplace", "probe/demo")
    ]
    loader = load(env)
    target = await loader.install("probe/demo")
    assert target == env.home / "plugins" / "demo"
    assert not (target / "plugins").exists()
    assert loader.skills["demo-skill"].format == "codex"


async def test_install_github_spec_reads_the_codex_catalog(
    env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "remote"
    catalog = codex_marketplace_repo(repo)
    asked: list[str] = []

    class Fetcher(marketplace.HttpFetcher):
        async def get_json(self, url: str) -> Any:
            asked.append(url)
            if url.endswith("/main/.agents/plugins/marketplace.json"):
                return json.loads(catalog.read_text(encoding="utf-8"))
            raise OSError("404")

    async def fake_clone(url: str, target: Path) -> None:
        assert url == "https://github.com/openai/plugins.git"
        import shutil

        shutil.copytree(repo, target)

    monkeypatch.setattr(marketplace, "FETCHER", Fetcher())
    monkeypatch.setattr(marketplace, "_clone", fake_clone)
    loader = load(env)
    target = await loader.install("github:openai/plugins@demo")
    assert target == env.home / "plugins" / "demo"
    assert (target / ".codex-plugin" / "plugin.json").is_file()
    assert loader.skills["demo-skill"].source == "plugin:demo"
    assert any(url.endswith(".agents/plugins/marketplace.json") for url in asked)
