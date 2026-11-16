"""Security review 7, part B — H-2: settings a caller may write, and where.

* snowpea's own home is never a project: the parent of ``$SNOWPEA_HOME`` as a
  ``workdir`` used to make ``settings.set {scope: "project"}`` write the global
  ``settings.json`` (one helper, every RPC that takes a project folder);
* every settings leaf is classified (``server/settings_leaves.py``): a new key
  that names a program, an address or a secret cannot be added unclassified,
  and the gate answers every command-like leaf as its row says, for every
  connection kind and both scopes;
* a remote web login's project-scope patch is judged against an allow table;
* a settings file that does not validate never stops the daemon from starting.

The command strings below ("prog", "sh") are DATA: nothing here starts a process.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config import project as project_config
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import NOT_A_PROJECT, ProjectSettings, home_problem
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, project_gate, settings_gate, settings_handlers
from snowpea_core.server import settings_leaves as leaves
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import DETAIL_REASONS, RpcError
from snowpea_core.server.protocol import SettingsGetParams, SettingsSetParams

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


#: Every connection kind the gates tell apart: name -> (connection, actor).
CALLERS: dict[str, tuple[Any, str | None]] = {
    "person": (_conn("ide"), None),
    "remote_login": (_conn("web", remote=True), None),
    "connector": (_conn("connector"), None),
    "legacy": (_conn("browser", version="0.2.0", client_id="b-legacy"), None),
    "browser": (_conn("browser", client_id="b-1"), None),
    "browser+actor": (_conn("browser", client_id="b-1"), "person"),
    "closed": (_conn("ide", closed=True), None),
    "none": (None, None),
}


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "dot" / ".snowpea"), token="t")


# -- H-2: snowpea's own home is not a project ------------------------------------------------


def test_what_counts_as_snowpeas_own_home(tmp_path: Path) -> None:
    home = tmp_path / "user" / ".snowpea"
    (home / "skills").mkdir(parents=True)
    project = tmp_path / "work" / "app"
    project.mkdir(parents=True)
    link = tmp_path / "link-to-home"
    link.symlink_to(home, target_is_directory=True)
    linked = tmp_path / "linked-project"
    linked.mkdir()
    (linked / ".snowpea").symlink_to(home, target_is_directory=True)
    rows = [
        (home, "home"),
        (home / "skills", "home"),  # a folder inside it
        (home / "not-there-yet" / "x", "home"),
        (link, "home"),  # by another name
        (home.parent, "parent"),  # its ``.snowpea`` IS the home (the user's home dir)
        (linked, "parent"),  # ... also through a link
        (project, None),
        (tmp_path, None),
        (tmp_path / "work", None),
    ]
    for folder, expected in rows:
        assert home_problem(folder, home) == expected, folder


@pytest.mark.parametrize("where", ["parent", "home", "inside"])
@pytest.mark.parametrize("caller", sorted(CALLERS))
async def test_settings_set_never_writes_the_global_file_as_a_project(
    core: Core, where: str, caller: str
) -> None:
    home = Path(core.paths.home)
    workdir = {"parent": home.parent, "home": home, "inside": home / "sessions"}[where]
    core.settings.save(core.paths)
    before = core.paths.settings_json.read_bytes()
    conn, actor = CALLERS[caller]
    params = SettingsSetParams(scope="project", workdir=str(workdir),
                               patch={"media": {"mcp": {"command": "prog"}}}, actor=actor)
    with pytest.raises(RpcError) as caught:
        await settings_handlers.settings_set_handler(conn, params, core)
    # For EVERY caller, a local person included: it is a bug whoever asks.
    assert caught.value.code == errors.INVALID_PARAMS
    assert caught.value.message == NOT_A_PROJECT
    assert caught.value.data == {"reason": "snowpea_home", "field": "workdir",
                                 "what": "parent" if where == "parent" else "home"}
    assert core.paths.settings_json.read_bytes() == before
    assert Settings.load(core.paths).media.mcp.command is None


def test_snowpea_home_is_an_enumerated_reason() -> None:
    assert DETAIL_REASONS["snowpea_home"][0] == errors.INVALID_PARAMS


def test_the_project_file_of_the_home_is_never_read_or_written(core: Core) -> None:
    """The lowest layer: whichever caller forgets the check still cannot."""
    home = Path(core.paths.home)
    core.paths.settings_json.write_text(
        json.dumps({"defaultMode": "auto", "allowlist": [{"pattern": "^ls$"}]}), "utf-8")
    for folder in (home.parent, home):
        loaded = ProjectSettings.load(folder)
        assert loaded.defaultMode is None and loaded.allowlist == []
        with pytest.raises(ValueError, match="not a project"):
            ProjectSettings(defaultMode="auto").save(folder)
    assert json.loads(core.paths.settings_json.read_text("utf-8"))["defaultMode"] == "auto"
    assert core.sessions.default_mode(home.parent) == "accept"


async def test_reading_the_project_scope_of_the_home_answers_no_project(core: Core) -> None:
    core.paths.settings_json.write_text(json.dumps({"defaultMode": "auto"}), "utf-8")
    result = await settings_handlers.settings_get_handler(
        CALLERS["person"][0],
        SettingsGetParams(scope="project", workdir=str(Path(core.paths.home).parent)), core)
    assert result.settings.get("defaultMode") is None


#: Every RPC that takes a project folder, and how much of the home it refuses:
#: ``dot`` (it reads or writes ``<workdir>/.snowpea`` as the PROJECT scope): the
#: home, inside it, and the folder whose ``.snowpea`` is the home; ``work`` (it
#: only works in the folder): the home and inside it.
PROJECT_FOLDER_RPCS: dict[str, str] = {
    "settings.set": "dot", "permission.allowlist.add": "dot", "project.trust.set": "dot",
    "skill.create": "dot", "skill.write": "dot", "skill.learn": "dot",
    "team.guide.set": "dot", "team.guide.delete": "dot",
    "session.create": "work", "job.schedule": "work", "external.create": "work",
    "mcp.add": "work", "mcp.update": "work", "mcp.remove": "work",
}


@pytest.mark.parametrize(("method", "how"), sorted(PROJECT_FOLDER_RPCS.items()))
async def test_every_rpc_with_a_project_folder_refuses_the_home(
    core: Core, tmp_path: Path, method: str, how: str
) -> None:
    from snowpea_core.server import app_server as app
    from snowpea_core.server import (
        external_handlers,
        job_handlers,
        mcp_handlers,
        session_handlers,
        skill_handlers,
        team_guide_handlers,
        trust_handlers,
    )
    from snowpea_core.server import protocol as p

    person = CALLERS["person"][0]
    home = Path(core.paths.home)
    (home / "sessions").mkdir()

    def call(folder: Path) -> Any:
        work = str(folder)
        if method == "settings.set":
            return settings_handlers.settings_set_handler(
                person, SettingsSetParams(scope="project", workdir=work, patch={}), core)
        if method == "permission.allowlist.add":
            return app.allowlist_add_handler(
                person, p.AllowlistAddParams(pattern="^ls$", scope="project", workdir=work), core)
        if method == "project.trust.set":
            return trust_handlers.project_trust_set_handler(
                person, p.ProjectTrustSetParams(workdir=work, scopes=["settings"], digests={}),
                core)
        if method == "skill.create":
            return skill_handlers.skill_create_handler(
                person, p.SkillCreateParams(name="x", workdir=work, scope="project",
                                            content="---\nname: x\ndescription: d\n---\nbody"),
                core)
        if method == "skill.write":
            return skill_handlers.skill_write_handler(
                person, p.SkillWriteParams(name="x", workdir=work, scope="project",
                                           content="---\nname: x\ndescription: d\n---\nbody"),
                core)
        if method == "team.guide.set":
            return team_guide_handlers.team_guide_set_handler(
                person, p.TeamGuideSetParams(team="t", scope="project", workdir=work,
                                             persona="p", routing=[]), core)
        if method == "team.guide.delete":
            return team_guide_handlers.team_guide_delete_handler(
                person, p.TeamGuideDeleteParams(team="t", scope="project", workdir=work), core)
        if method == "session.create":
            return session_handlers.session_create_handler(
                person, p.SessionCreateParams(workdir=work), core)
        if method == "job.schedule":
            return job_handlers.job_schedule_handler(
                person, p.JobScheduleParams(spec="in 10m", task="t", workdir=work), core)
        if method == "external.create":
            return external_handlers.create_handler(
                person, p.ExternalCreateParams(cli="claude", project=work), core)
        if method == "mcp.add":
            return mcp_handlers.mcp_add_handler(
                person, p.McpAddParams(name="x", url="https://mcp.example.com", scope="project",
                                       workdir=work, test=False), core)
        if method == "mcp.update":
            return mcp_handlers.mcp_update_handler(
                person, p.McpUpdateParams(name="x", scope="project", workdir=work,
                                          patch=p.McpEntryFields(disabled=True)), core)
        if method == "mcp.remove":
            return mcp_handlers.mcp_remove_handler(
                person, p.McpRemoveParams(name="x", scope="project", workdir=work), core)
        raise AssertionError(method)

    refused = [home, home / "sessions"] + ([home.parent] if how == "dot" else [])
    if method == "skill.learn":
        # It takes a session, not a folder: the session's folder is judged.
        from snowpea_core.agent.definition import DefinitionError
        from snowpea_core.commands.skill_cmd import learn_skill

        for folder in refused:
            session = SimpleNamespace(workdir=folder, id="s", history=[])
            with pytest.raises(DefinitionError, match="not a project"):
                await learn_skill(core, session, "x")  # type: ignore[arg-type]
        return
    for folder in refused:
        with pytest.raises(RpcError) as caught:
            await call(folder)
        assert caught.value.code == errors.INVALID_PARAMS, (method, folder)
        assert caught.value.data["reason"] == "snowpea_home", (method, folder)
    # Nothing project-scoped was written into the home.
    assert not (home / ".snowpea").exists()
    assert not (home / "skills" / "x").exists() and not (home / ".mcp.json").exists()


def test_the_folder_that_holds_the_home_is_still_a_place_to_work(core: Core) -> None:
    """``cd ~ && snowpea``: a session there is ordinary; only its PROJECT scope is not."""
    parent = Path(core.paths.home).parent
    assert project_gate.project_problem(core, parent, dot=False) is None
    assert project_gate.project_problem(core, parent) == "parent"
    project_gate.require_project(core, parent, dot=False)


# -- H-2: every settings leaf is classified ---------------------------------------------------


def test_every_settings_leaf_is_classified() -> None:
    """A new key fails here until it is put in ``settings_leaves`` — as plain,
    or as something the daemon runs or calls, with who may write it."""
    schema = leaves.schema_leaves()
    unclassified = sorted(leaf for leaf in schema if not leaves.covered(leaf))
    assert not unclassified, (
        "classify these settings keys in server/settings_leaves.py (PLAIN, or COMMAND_LIKE "
        f"when the daemon runs or calls what they name): {unclassified}"
    )
    plain = leaves.plain_leaves()
    gone = sorted(leaf for leaf in plain if leaf not in schema)
    assert not gone, f"PLAIN lists keys the schema no longer has: {gone}"
    both = sorted(plain & set(leaves.COMMAND_LIKE))
    assert not both, f"listed twice: {both}"


def test_a_plain_leaf_that_looks_like_a_command_was_reviewed() -> None:
    suspicious = sorted(
        leaf for leaf in leaves.plain_leaves()
        if leaves.looks_like_a_command(leaf) and leaf not in leaves.PLAIN_BY_REVIEW
    )
    assert not suspicious, (
        "these look like a program, an address or a secret: move them to COMMAND_LIKE, or "
        f"say in PLAIN_BY_REVIEW why they are not: {suspicious}"
    )
    stale = sorted(set(leaves.PLAIN_BY_REVIEW) - leaves.plain_leaves())
    assert not stale, stale


def test_a_command_like_leaf_cannot_be_added_unclassified() -> None:
    """The walk itself: a model with a new ``command`` key is caught."""
    from pydantic import BaseModel, Field

    class NewTool(BaseModel):
        command: str | None = None
        retries: int = 1

    class Grown(Settings):
        newtool: NewTool = Field(default_factory=NewTool)

    grown = leaves.schema_leaves(Grown)
    assert {"newtool.command", "newtool.retries"} <= grown
    assert not leaves.covered("newtool.command") and not leaves.covered("newtool.retries")
    assert leaves.looks_like_a_command("newtool.command")
    assert not leaves.looks_like_a_command("newtool.retries")


def _patch_for(leaf: str) -> dict[str, Any]:
    """A patch that sets ``leaf`` (``*`` -> a key, a value that fits the leaf)."""
    parts = [("x" if part == "*" else part) for part in leaf.split(".")]
    last = parts[-1]
    value: Any = "prog"
    if last in ("args", "passEnv", "allowedOrigins", "allowVars"):
        value = ["prog"]
    elif last in ("env", "headers", "oauth"):
        value = {"A": "b"}
    elif last in ("enabled", "trustProxy"):
        value = True
    elif last in ("port", "sessionHours"):
        value = 9
    elif last == "url" or last == "base_url":
        value = "https://example.com/mcp"
    elif last == "maxMode":
        value = "plan"
    node: Any = value
    for part in reversed(parts):
        node = {part: node}
    return node


def _gate(kind: str, patch: dict[str, Any], *, project: bool) -> str:
    conn, actor = CALLERS[kind]
    core = SimpleNamespace(settings=Settings(), paths=SimpleNamespace(home=Path("/srv/sp-home")))
    try:
        settings_handlers.guard_patch(conn, patch, project=project, actor=actor, core=core)
    except RpcError as exc:
        assert exc.code == errors.JARVIS_PERSON_ONLY, (kind, patch, exc.code)
        return "refused"
    return "ok"


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize("kind", sorted(set(CALLERS) - {"person"}))
@pytest.mark.parametrize("leaf", sorted(leaves.COMMAND_LIKE))
def test_who_may_write_a_command_like_leaf(leaf: str, kind: str, scope: str) -> None:
    """The gate against the inventory: one verdict per leaf, kind and scope."""
    _what, also = leaves.COMMAND_LIKE[leaf]
    want = "ok" if scope == "global" and kind in also else "refused"
    assert _gate(kind, _patch_for(leaf), project=scope == "project") == want, (leaf, kind, scope)


@pytest.mark.parametrize("leaf", sorted(leaves.COMMAND_LIKE))
def test_a_local_person_writes_every_command_like_leaf(leaf: str) -> None:
    for project in (False, True):
        conn, actor = CALLERS["person"]
        core = SimpleNamespace(settings=Settings(), paths=SimpleNamespace(home=Path("/srv/h")))
        try:
            settings_handlers.guard_patch(conn, _patch_for(leaf), project=project, actor=actor,
                                          core=core)
        except RpcError as exc:  # a person's VALUE checks (confirmedPath), never the gate
            assert exc.code == errors.INVALID_PARAMS, leaf


def test_media_is_a_local_persons_in_both_scopes() -> None:
    assert ("media",) in settings_gate.PERSON_LOCAL_ONLY
    for kind in sorted(set(CALLERS) - {"person"}):
        for project in (False, True):
            for patch in ({"media": {"mcp": {"command": "prog", "args": ["-c", "x"]}}},
                          {"media": {"mcp": {"url": "https://example.com"}}},
                          {"media": None}):
                assert _gate(kind, patch, project=project) == "refused", (kind, project, patch)


#: What a remote web login may write in the project scope, key by key.
PROJECT_PATCHES: list[tuple[dict[str, Any], bool]] = [
    ({"agents": {"teams": {"a": ["executor"]}}}, True),
    ({"agents": {"activeTeam": "a"}}, True),
    ({"models": {"default": "p"}}, True),
    ({"models": None}, True),
    ({"defaultMode": "accept"}, True),
    ({"defaultMode": "plan"}, True),
    ({"allowlist": [{"pattern": "^ls$"}]}, False),
    ({"jarvis": {"expose": False}}, False),
    ({"security": {"blockedCommands": {"custom": ["x"]}}}, False),
    ({"media": {"mcp": {"command": "prog"}}}, False),
    ({"mcp": {"servers": {"x": {"url": "https://example.com"}}}}, False),
    ({"providers": {"openai": {"api_key": "k"}}}, False),
    ({"hooks": {"x": 1}}, False),
    ({"anythingElse": {"x": 1}}, False),  # a key the project schema does not know
    ({"audio": {"tts": {"voice": "v"}}}, False),
]


@pytest.mark.parametrize(("patch", "allowed"), PROJECT_PATCHES)
def test_a_remote_logins_project_patch_meets_an_allow_table(
    patch: dict[str, Any], allowed: bool
) -> None:
    assert (_gate("remote_login", patch, project=True) == "ok") is allowed, patch
    for kind in ("connector", "legacy", "browser", "browser+actor", "closed", "none"):
        assert _gate(kind, patch, project=True) == "refused", (kind, patch)


# -- a settings file that does not validate never stops the daemon ---------------------------


@pytest.mark.parametrize(
    ("document", "kept", "defaulted"),
    [
        # A project document written over the global file (what H-2 left behind).
        ({"defaultMode": "auto", "allowlist": [{"nope": 1}], "agent": {"busy": "queue"}},
         {"agent.busy": "queue"}, ["allowlist"]),
        ({"web": {"remote": {"port": "not-a-port"}}, "approvals": {"timeoutSec": 77}},
         {"approvals.timeoutSec": 77}, ["web"]),
        ({"scheduler": {"tickSec": "soon"}, "agent": {"busy": "queue"}},
         {"agent.busy": "queue"}, ["scheduler"]),
        ({"team": "x", "ralph": "x"}, {}, ["team", "ralph"]),
    ],
)
def test_an_invalid_settings_file_loads_degraded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, document: dict[str, Any],
    kept: dict[str, Any], defaulted: list[str]
) -> None:
    paths = Paths.create(tmp_path / "home")
    text = json.dumps(document)
    paths.settings_json.write_text(text, "utf-8")
    with caplog.at_level("ERROR"):
        loaded = Settings.load(paths)  # it used to raise: the daemon did not start
    dumped = loaded.model_dump(mode="json")
    defaults = Settings().model_dump(mode="json")
    for dotted, value in kept.items():
        node: Any = dumped
        for part in dotted.split("."):
            node = node[part]
        assert node == value, dotted
    for section in defaulted:
        assert dumped[section] == defaults[section], section
        assert section in caplog.text
    # The file as it was is kept beside it, so nothing is lost by the next save.
    copy = paths.settings_json.with_name("settings.json.invalid")
    assert copy.read_text("utf-8") == text
    assert paths.settings_json.read_text("utf-8") == text


def test_a_valid_settings_file_loads_as_before(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    Settings.model_validate({"agent": {"busy": "queue"}}).save(paths)
    assert Settings.load(paths).agent.busy == "queue"
    assert not paths.settings_json.with_name("settings.json.invalid").exists()


def test_the_home_registry_is_what_core_fills(tmp_path: Path) -> None:
    home = tmp_path / "another" / ".snowpea"
    assert home_problem(home.parent) is None  # nobody's home yet
    Core(settings=Settings(), paths=Paths.create(home), token="t")
    assert home_problem(home.parent) == "parent" and home_problem(home) == "home"
    assert Path(home).resolve() in project_config._HOMES
