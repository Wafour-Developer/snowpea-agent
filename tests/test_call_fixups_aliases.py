"""Tool names and arguments other agents use reach snowpea's tools."""

from snowpea_core.agent import call_fixups
from snowpea_core.tools.shell import DEFAULT_TIMEOUT, MAX_TIMEOUT, _fit_sleeps

KNOWN = ["save_memory", "search_memory", "plan_update_step", "shell"]


def test_memory_names_from_other_agents_resolve() -> None:
    for name in ("update_memory", "memory_write", "remember", "memory", "add_memory"):
        assert call_fixups.resolve_name(name, KNOWN) == "save_memory", name
    for name in ("memory_search", "recall", "query_memory"):
        assert call_fixups.resolve_name(name, KNOWN) == "search_memory", name
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    fixed = call_fixups.coerce_arguments({"content": "the repo is llm_opt"}, schema)
    assert fixed.get("text") == "the repo is llm_opt"


def test_a_long_sleep_gets_a_timeout_that_covers_it() -> None:
    assert _fit_sleeps("sleep 240; ssh hon1 'tail log'", DEFAULT_TIMEOUT) == 240 + DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 5 && ls", DEFAULT_TIMEOUT) == DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 3m", DEFAULT_TIMEOUT) == 180 + DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 99999", DEFAULT_TIMEOUT) == MAX_TIMEOUT
    assert _fit_sleeps("echo nosleep", DEFAULT_TIMEOUT) == DEFAULT_TIMEOUT


def test_old_memory_names_in_definitions_and_skills_mean_the_new_tools() -> None:
    from snowpea_core.agent.definition import AgentDefinition
    from snowpea_core.skills.skill_md import parse_skill_md
    from snowpea_core.tools.renames import canonical_names

    assert canonical_names(["read_file", "memory_write", "save_memory", "memory_search"]) == [
        "read_file",
        "save_memory",
        "search_memory",
    ]
    defn = AgentDefinition(name="x", description="", prompt="", tools=["memory_write", "grep"])
    assert defn.tool_list() == ["save_memory", "grep"]
    doc = parse_skill_md(
        "---\nname: s\nallowed-tools: [memory_search, glob]\n---\nbody", default_name="s"
    )
    assert doc.allowed_tools == ["search_memory", "glob"]


def test_an_allowlist_rule_saved_under_the_old_name_still_covers_the_tool(tmp_path) -> None:
    from snowpea_core.permissions.allowlist import Allowlist, pattern_for_tool, tool_target

    allowlist = Allowlist()
    allowlist.add(
        pattern_for_tool("memory_write"), "project", tool_target("memory_write"), workdir=tmp_path
    )
    assert allowlist.matches("save_memory", {}, workdir=tmp_path)
    assert not allowlist.matches("search_memory", {}, workdir=tmp_path)
