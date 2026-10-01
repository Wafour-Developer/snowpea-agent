"""CORE-round-cost §2: a delegated child starts on a diet.

A child session opens with an empty history and a brief that already says what
it is for, yet it was being handed the parent's whole prompt: the memory recall
block, the full skills index and every nested ``AGENTS.md`` in the tree.  None
of that is context it asked for, and a child pays for it on every round of its
own loop.

``agents.childContext = "lean"`` (the default) drops those three and narrows a
read-only child's eager tool set.  What it keeps is what makes it a child: its
role, its definition's prompt, and its round budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent.subagent import BUDGET_LINE
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.prompts import compose
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import deferred
from snowpea_core.tools.registry import register_builtin_tools

MEMORY_BLOCK = (
    "Things you remember about this user from earlier sessions.\n"
    '<memory id="m1" tags="style">The user prefers short replies.</memory>'
)

SKILLS = (("[project]", (("deploy", "How this project ships."),)),)


class FakeSkills:
    def index_groups(self) -> object:
        return SKILLS


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (root / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    return root


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools.bind(built.settings))
    built.skills = FakeSkills()  # type: ignore[assignment]
    return built


def child_session(workdir: Path, **kwargs: object) -> Session:
    return Session(id="s-child", workdir=workdir, is_subagent=True, **kwargs)  # type: ignore[arg-type]


def prompt_for(core: Core, session: Session) -> str:
    agent_mod.invalidate_environment()
    agent_mod.invalidate_tools()
    return agent_mod.build_system_prompt(
        session, core.tools.specs(session), MEMORY_BLOCK, core=core
    )


# ---------------------------------------------------------------------------
# what the diet drops
# ---------------------------------------------------------------------------


def test_a_lean_child_has_no_memory_block_and_no_skills_index(
    core: Core, workdir: Path
) -> None:
    text = prompt_for(core, child_session(workdir))
    assert "The user prefers short replies" not in text
    assert "deploy: How this project ships." not in text


def test_a_lean_child_gets_the_root_context_file_only(core: Core, workdir: Path) -> None:
    text = prompt_for(core, child_session(workdir))
    assert "root rules" in text
    assert "src rules" not in text


def test_a_parent_session_keeps_everything(core: Core, workdir: Path) -> None:
    parent = Session(id="s-parent", workdir=workdir)
    text = prompt_for(core, parent)
    assert "The user prefers short replies" in text
    assert "deploy: How this project ships." in text
    assert "src rules" in text


def test_child_context_full_restores_the_parent_s_prompt(
    core: Core, workdir: Path
) -> None:
    core.settings.agents.childContext = "full"
    text = prompt_for(core, child_session(workdir))
    assert "The user prefers short replies" in text
    assert "deploy: How this project ships." in text
    assert "src rules" in text


# ---------------------------------------------------------------------------
# what the diet keeps
# ---------------------------------------------------------------------------


def test_a_lean_child_keeps_its_role_and_its_definition_prompt(
    core: Core, workdir: Path
) -> None:
    session = child_session(workdir)
    session.prompt_role = "executor"
    session.system_prompt = "You explore code and answer questions about it."
    text = prompt_for(core, session)
    assert "You explore code and answer questions about it." in text
    # The subagent preamble — the report contract — is not part of the diet.
    assert compose.load("roles/_preamble") in text


def test_the_budget_line_is_a_brief_not_a_prompt() -> None:
    """It travels with the task, so no prompt diet can take it away."""
    assert "{n}" in BUDGET_LINE
    assert BUDGET_LINE.format(n=40).strip()


def test_a_lean_child_can_still_reach_a_skill_by_name(core: Core, workdir: Path) -> None:
    assert "skill_view" in {spec.name for spec in core.tools.specs(child_session(workdir))}


# ---------------------------------------------------------------------------
# the read-only child's tool list
# ---------------------------------------------------------------------------


def test_a_read_only_child_starts_with_its_read_tools_and_tool_search(
    core: Core, workdir: Path
) -> None:
    session = child_session(workdir)
    session.allowed_tools = {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "lsp_symbols",
        "lsp_references",
        "git_status",
        "git_diff",
        "web_search",
    }
    sent = {spec.name for spec in core.tools.specs(session)}
    # view_image joins: reading tools survive any narrowing (deferred.ALWAYS_ALLOWED).
    assert sent == {"read_file", "view_image", "glob", "grep", "tool_search"}

    hidden = {spec.name for spec in core.tools.deferred_specs(session)}
    assert "lsp_symbols" in hidden
    assert "git_diff" in hidden
    # Nothing outside the definition's list leaks back in.
    assert not hidden & {"write_file", "shell"}


def test_a_child_that_may_write_is_not_read_only(core: Core, workdir: Path) -> None:
    session = child_session(workdir)
    session.allowed_tools = {"read_file", "write_file", "shell", "grep"}
    assert deferred.is_readonly_child(session) is False
    sent = {spec.name for spec in core.tools.specs(session)}
    assert sent == {
        "read_file", "view_image", "glob", "grep", "write_file", "shell", "tool_search"
    }


# ---------------------------------------------------------------------------
# a child's definition tools and inherited host tools are sent in full
# ---------------------------------------------------------------------------


@pytest.fixture
def browser_host(core: Core):  # type: ignore[no-untyped-def]
    """A browser connection that registered ``repl`` and ``browser_navigate``."""
    from types import SimpleNamespace

    from snowpea_core.tools.host_tools import HOST_TOOLS

    conn = SimpleNamespace(
        surface_id="surf-child-eager",
        client_id="snowpea-browser-child",
        client_kind="browser",
        closed=False,
        on_close=[],
    )
    HOST_TOOLS.register(
        core,
        conn,
        [
            {"name": "repl", "permission": "read"},
            {"name": "browser_navigate", "permission": "read"},
        ],
    )
    try:
        yield conn
    finally:
        HOST_TOOLS.drop_connection(conn)


def _child_of_browser(core: Core, workdir: Path, defn: object, tools: object = None) -> Session:
    from snowpea_core.agent.subagent import get_manager, inherit_host

    parent = Session(id="s-browser-parent", workdir=workdir)
    parent.host_tools_from = "snowpea-browser-child"
    child = Session(id=f"s-child-{id(defn)}", workdir=workdir)
    inherit_host(child, parent)
    get_manager(core)._apply_definition(child, defn, tools)  # type: ignore[arg-type]
    return child


def test_a_child_gets_inherited_host_tools_eagerly_even_when_its_list_omits_them(
    core: Core, workdir: Path, browser_host: object
) -> None:
    from snowpea_core.agent.definition import AgentDefinition

    defn = AgentDefinition(
        name="coder", description="d", tools=["read_file", "write_file", "shell"]
    )
    child = _child_of_browser(core, workdir, defn)
    sent = {spec.name for spec in core.tools.specs(child)}
    assert {"repl", "browser_navigate"} <= sent
    assert not {"repl", "browser_navigate"} & {
        spec.name for spec in core.tools.deferred_specs(child)
    }
    # And the loop lets the call through.
    from snowpea_core.agent.loop import skill_refuses

    assert not skill_refuses(child, "repl")
    assert skill_refuses(child, "patch")


def test_a_read_only_project_definition_sends_its_whole_list(
    core: Core, workdir: Path, browser_host: object
) -> None:
    from snowpea_core.agent.definition import AgentDefinition

    tools = ["read_file", "grep", "web_search", "git_diff"]
    project = AgentDefinition(name="researcher", description="d", tools=tools)
    child = _child_of_browser(core, workdir, project)
    sent = {spec.name for spec in core.tools.specs(child)}
    assert {"web_search", "git_diff", "repl"} <= sent
    assert not core.tools.deferred_specs(child)

    # The built-in read-only roles keep their sparse eager set.
    builtin = AgentDefinition(name="explore", description="d", tools=tools, source="builtin")
    child = _child_of_browser(core, workdir, builtin)
    sent = {spec.name for spec in core.tools.specs(child)}
    assert "repl" in sent
    assert {"web_search", "git_diff"} <= {
        spec.name for spec in core.tools.deferred_specs(child)
    }


def test_the_parent_session_is_unchanged(
    core: Core, workdir: Path, browser_host: object
) -> None:
    parent = Session(id="s-browser-parent-only", workdir=workdir)
    parent.host_tools_from = "snowpea-browser-child"
    parent.allowed_tools = {"read_file", "shell"}
    # A narrowed human session gets no host tools added by narrowing.
    assert "repl" not in {spec.name for spec in core.tools.specs(parent)}
    assert deferred.narrowed(parent.allowed_tools, parent) == (
        {"read_file", "shell"} | deferred.ALWAYS_ALLOWED
    )
