"""Tool-call repairs and the refusals that replaced silent failures.

Each case here is a failure seen in real sessions against an open-weight
model: ``terminal``/``patch=path`` as tool names, ``ask_user`` options sent as
a JSON string, a cut-off ``write_file`` that read as "path is required", and a
delegated child refused ``read_file`` because the lead listed only ``shell``.
"""

from __future__ import annotations

from types import SimpleNamespace

from snowpea_core.agent import call_fixups
from snowpea_core.agent.loop import skill_refuses
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.normalize import UNPARSED_ARGUMENTS, parse_arguments
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolRegistry, register_builtin_tools

REGISTRY = register_builtin_tools(ToolRegistry())
NAMES = REGISTRY.names()


def test_other_agents_tool_names_resolve() -> None:
    assert call_fixups.resolve_name("terminal", NAMES) == "shell"
    assert call_fixups.resolve_name("Bash", NAMES) == "shell"
    assert call_fixups.resolve_name("Read", NAMES) == "read_file"
    assert call_fixups.resolve_name("str_replace_editor", NAMES) == "patch"
    assert call_fixups.resolve_name("functions.shell", NAMES) == "shell"


def test_a_mangled_name_resolves_to_its_leading_tool() -> None:
    assert call_fixups.resolve_name("patch=path", NAMES) == "patch"
    assert call_fixups.resolve_name("shell<|call|>", NAMES) == "shell"


def test_a_merely_similar_name_is_not_run_but_suggested() -> None:
    assert call_fixups.resolve_name("read_fiel", NAMES) is None
    assert "read_file" in call_fixups.suggestions("read_fiel", NAMES)


def test_stringly_typed_ask_user_arguments_are_decoded() -> None:
    schema = REGISTRY.get("ask_user").input_schema
    args = {
        "question": "인증 방식?",
        "header": "인증",
        "options": '[{"label": "세션", "description": "쿠키"}]',
        "multi": "false",
    }
    fixed = call_fixups.coerce_arguments(args, schema)
    assert fixed["options"] == [{"label": "세션", "description": "쿠키"}]
    assert fixed["multi"] is False
    assert fixed["question"] == "인증 방식?"


def test_a_string_field_is_never_decoded() -> None:
    schema = REGISTRY.get("write_file").input_schema
    fixed = call_fixups.coerce_arguments({"path": "a.json", "content": "[1, 2]"}, schema)
    assert fixed["content"] == "[1, 2]"


def test_claude_code_argument_names_map_to_snowpeas() -> None:
    schema = REGISTRY.get("patch").input_schema
    fixed = call_fixups.coerce_arguments(
        {"file_path": "a.py", "old_str": "x", "new_str": "y"}, schema
    )
    assert fixed == {"path": "a.py", "old_string": "x", "new_string": "y"}


def test_cut_off_arguments_are_reported_not_emptied() -> None:
    raw = '{"path": "deck/index.html", "content": "<html><body>' + "x" * 500
    args = parse_arguments(raw)
    assert UNPARSED_ARGUMENTS in args
    message = call_fixups.unparsed_message("write_file", args)
    assert message is not None
    assert "not valid JSON" in message and "smaller parts" in message
    assert call_fixups.unparsed_message("write_file", {"path": "a"}) is None


def test_a_narrowed_child_can_still_read_and_search() -> None:
    session = Session(id="s-child", workdir=".")
    session.allowed_tools = {"shell"}
    for name in ("read_file", "glob", "grep", "view_image", "tool_search", "shell"):
        assert not skill_refuses(session, name), name
    assert skill_refuses(session, "write_file")


def test_skill_allowed_tools_preapprove_instead_of_restricting() -> None:
    policy = PermissionPolicy()
    shell = REGISTRY.get("shell")
    session = SimpleNamespace(skill_approved_tools=None, workdir=".")
    assert policy.decide("accept", "exec", shell, {"command": "make"}, session) == "ask"
    session.skill_approved_tools = {"shell"}
    assert policy.decide("accept", "exec", shell, {"command": "make"}, session) == "allow"
