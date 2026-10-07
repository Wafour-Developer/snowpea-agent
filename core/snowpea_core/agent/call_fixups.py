"""Repairs to a model's tool calls before they run.

Open-weight models trained on other agents' transcripts call ``terminal`` or
``Bash`` for ``shell``, emit ``patch=path`` as a tool name, and send nested
arrays as JSON *strings* (``"options": "[{...}]"``, ``"multiSelect": "false"``).
Each of those used to be a refusal the model then had to reason its way out
of.  What can be repaired without guessing is repaired here: a known alias or
a mangled name resolves to the real tool, and a string whose schema type is
array/object/boolean/number is decoded.  Nothing here invents a value.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable
from typing import Any

from snowpea_core.providers.normalize import UNPARSED_ARGUMENTS

#: Names other agents use for snowpea's tools (Claude Code, Codex, Hermes,
#: OpenHands).  Keys are lower-cased; the lookup is case-insensitive.
ALIASES: dict[str, str] = {
    "bash": "shell",
    "terminal": "shell",
    "run_shell_command": "shell",
    "execute_command": "shell",
    "exec_command": "shell",
    "run_command": "shell",
    "shell_command": "shell",
    "read": "read_file",
    "cat": "read_file",
    "open_file": "read_file",
    "view": "read_file",
    "write": "write_file",
    "create_file": "write_file",
    "edit": "patch",
    "edit_file": "patch",
    "str_replace": "patch",
    "str_replace_editor": "patch",
    "str_replace_based_edit_tool": "patch",
    "replace": "patch",
    "apply_patch": "patch",
    "search_files": "grep",
    "find_files": "glob",
    "ls": "list_dir",
    "list_files": "list_dir",
    "list_directory": "list_dir",
    "python": "execute_code",
    "run_python": "execute_code",
    "task": "delegate_task",
    "agent": "delegate_task",
    "askuserquestion": "ask_user",
    "ask_user_question": "ask_user",
    "toolsearch": "tool_search",
    "websearch": "web_search",
    "webfetch": "web_extract",
    "web_fetch": "web_extract",
    "fetch": "web_extract",
    # Memory, as other agents name it (Hermes' "memory", Cursor's
    # "update_memory", mem0's "add_memory"), and snowpea's own names before the
    # rename (tools/renames.py): an unknown name used to be refused with a
    # suggestion of plan_update_step.
    "memory_write": "save_memory",
    "update_memory": "save_memory",
    "add_memory": "save_memory",
    "create_memory": "save_memory",
    "write_memory": "save_memory",
    "memory_add": "save_memory",
    "memory_save": "save_memory",
    "store_memory": "save_memory",
    "remember": "save_memory",
    "memory": "save_memory",
    "memory_search": "search_memory",
    "recall": "search_memory",
    "memory_recall": "search_memory",
    "read_memory": "search_memory",
    "query_memory": "search_memory",
    "find_memory": "search_memory",
}


#: Argument names other agents use, by the name snowpea's schemas use.  Only
#: applied when the schema has the target and lacks the alias.
ARG_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "filename", "file", "absolute_path"),
    "command": ("cmd",),
    "old_string": ("old_str", "oldString"),
    "new_string": ("new_str", "newString"),
    "replace_all": ("replaceAll",),
    "content": ("contents", "file_text"),
    "text": ("content", "memory", "note", "fact"),
}


def resolve_name(name: str, known: Iterable[str]) -> str | None:
    """The registered tool ``name`` means, or ``None`` when it is unclear.

    Exact names win; then a namespace prefix (``functions.shell``) or a
    trailing fragment (``patch=path``, ``shell<|call|>``) is stripped; then the
    alias table.  A name that only *resembles* a tool is not resolved; that
    is left to :func:`suggestions`, because running the wrong tool is worse
    than asking again.
    """
    names = set(known)
    if name in names:
        return name
    candidates = [name]
    bare = name.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
    candidates.append(bare)
    head = re.match(r"[A-Za-z_][A-Za-z0-9_]*", bare)
    if head:
        candidates.append(head.group(0))
    for candidate in candidates:
        if candidate in names:
            return candidate
        lowered = candidate.lower()
        if lowered in names:
            return lowered
        alias = ALIASES.get(lowered)
        if alias in names:
            return alias
    return None


def suggestions(name: str, known: Iterable[str], limit: int = 3) -> list[str]:
    """Registered names close to ``name``, for the unknown-tool refusal."""
    return difflib.get_close_matches(name.lower(), sorted(known), n=limit, cutoff=0.5)


def _decode(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def coerce(value: Any, schema: dict[str, Any] | None) -> Any:
    """``value`` decoded to the JSON type ``schema`` declares, where that is unambiguous."""
    if not isinstance(schema, dict):
        return value
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = set(kind)
    else:
        kinds = {kind} if kind else set()
    if isinstance(value, str) and "string" not in kinds:
        text = value.strip()
        if kinds & {"array", "object"} and text[:1] in "[{":
            value = _decode(text)
        elif "boolean" in kinds and text.lower() in ("true", "false"):
            value = text.lower() == "true"
        elif "integer" in kinds and re.fullmatch(r"-?\d+", text):
            value = int(text)
        elif "number" in kinds and re.fullmatch(r"-?\d+(\.\d+)?", text):
            value = float(text)
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            return {key: coerce(item, props.get(key)) for key, item in value.items()}
        return value
    if isinstance(value, list):
        return [coerce(item, schema.get("items")) for item in value]
    return value


def coerce_arguments(
    arguments: dict[str, Any], input_schema: dict[str, Any] | None
) -> dict[str, Any]:
    """Arguments renamed and typed against a tool's ``input_schema``."""
    if UNPARSED_ARGUMENTS in arguments or not isinstance(input_schema, dict):
        return arguments
    props = input_schema.get("properties")
    if isinstance(props, dict):
        arguments = dict(arguments)
        for target, aliases in ARG_ALIASES.items():
            if target not in props or target in arguments:
                continue
            for alias in aliases:
                if alias in arguments and alias not in props:
                    arguments[target] = arguments.pop(alias)
                    break
    coerced = coerce(arguments, {"type": "object", **input_schema})
    return coerced if isinstance(coerced, dict) else arguments


def unparsed_message(name: str, arguments: dict[str, Any]) -> str | None:
    """The refusal for arguments that were not valid JSON, or ``None``."""
    info = arguments.get(UNPARSED_ARGUMENTS)
    if not isinstance(info, dict):
        return None
    length = info.get("length", 0)
    tail = str(info.get("tail", "")).replace("\n", "\\n")
    return (
        f"the arguments for {name} were not valid JSON ({length} characters, ending "
        f"with …{tail[-40:]!r}), so nothing ran. If this was a large write, the "
        "output was most likely cut off by the length limit: write the file in "
        "smaller parts (write_file the first part, then add the rest with patch "
        "or a shell heredoc appending to it), rather than repeating the same call."
    )


__all__ = [
    "ALIASES",
    "ARG_ALIASES",
    "coerce",
    "coerce_arguments",
    "resolve_name",
    "suggestions",
    "unparsed_message",
]
