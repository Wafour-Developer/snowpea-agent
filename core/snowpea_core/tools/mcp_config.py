"""Reading and writing ``.mcp.json`` for the management API (M14 §2).

The file format is the Claude Code one, so a project's ``.mcp.json`` stays
shareable with other tools.  That is also why every write here is a
read-modify-write of the *whole* document: unknown top-level keys and the other
servers' entries — including keys snowpea does not understand — are preserved
verbatim, and the replacement is put in place with a temp file plus
``os.replace`` so a crash mid-write can never leave a truncated config behind.

Nothing in this module starts a server or touches the registry; that is
``mcp_client``'s job.  Nothing here logs or returns ``env``/``headers`` values.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from snowpea_core.tools.mcp_client import CONFIG_NAME

#: A server name is a key in ``mcpServers`` and half of a tool name, so it is
#: restricted to what both a filename-ish key and ``mcp__<server>__<tool>``
#: tolerate (M14 §3).
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: Characters that only mean anything to a shell.  ``command`` is exec'd
#: directly with an argv list, so one of these in it is always a mistake — and
#: usually an attempt to smuggle a second command in.
SHELL_META = ";|&$`\n\r><*?!"

#: Scopes whose file this module may rewrite.
WRITABLE_SCOPES = ("project", "global")


class McpConfigError(ValueError):
    """An entry or a name the management API must refuse (``mcp_invalid``)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def validate_name(name: str) -> str:
    """The server name, or raise :class:`McpConfigError` naming ``name``."""
    text = (name or "").strip()
    if not NAME_RE.match(text):
        raise McpConfigError(
            "name",
            f"'{text}' is not a valid server name; use 1-64 of [a-zA-Z0-9_-]",
        )
    return text


def validate_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Check one ``mcpServers`` entry; returns it unchanged when it holds."""
    command = entry.get("command")
    url = entry.get("url")
    if bool(command) == bool(url):
        raise McpConfigError("command", "give exactly one of command or url")
    if command is not None:
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError("command", "command must be a non-empty string")
        if any(char in command for char in SHELL_META):
            raise McpConfigError(
                "command",
                "command is executed directly, not through a shell; "
                "put the arguments in args instead of writing a shell string",
            )
    args = entry.get("args")
    if args is not None and (
        not isinstance(args, list) or not all(isinstance(item, str) for item in args)
    ):
        raise McpConfigError("args", "args must be a list of strings")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise McpConfigError("url", "url must be an http:// or https:// endpoint")
    for key in ("env", "headers"):
        value = entry.get(key)
        if value is not None and not isinstance(value, dict):
            raise McpConfigError(key, f"{key} must be an object of string values")
    return entry


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def config_path(scope: str, home: Path | str, workdir: Path | str | None) -> Path:
    """``<workdir>/.mcp.json`` for ``project``, ``$SNOWPEA_HOME/.mcp.json`` for ``global``."""
    if scope == "global":
        return Path(home).expanduser() / CONFIG_NAME
    if scope != "project":
        raise McpConfigError("scope", f"{scope} servers are read-only")
    if workdir is None:
        raise McpConfigError("workdir", "a project server needs a workdir")
    return Path(workdir).expanduser() / CONFIG_NAME


def read_document(path: Path) -> dict[str, Any]:
    """The whole file, or ``{}`` when it is missing or not a JSON object."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def read_servers(path: Path) -> dict[str, dict[str, Any]]:
    """The ``mcpServers`` table of ``path``, entries that are objects only."""
    servers = read_document(path).get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(name): entry for name, entry in servers.items() if isinstance(entry, dict)}


def write_document(path: Path, document: dict[str, Any]) -> Path:
    """Replace ``path`` atomically: temp file in the same directory, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return target


def save_entry(path: Path, name: str, entry: dict[str, Any]) -> Path:
    """Put ``entry`` under ``mcpServers[name]``, keeping everything else."""
    document = read_document(path)
    servers = document.get("mcpServers")
    document["mcpServers"] = dict(servers) if isinstance(servers, dict) else {}
    document["mcpServers"][name] = entry
    return write_document(path, document)


def delete_entry(path: Path, name: str) -> bool:
    """Drop ``mcpServers[name]``; ``False`` when it was not there."""
    document = read_document(path)
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    servers = dict(servers)
    servers.pop(name)
    document["mcpServers"] = servers
    write_document(path, document)
    return True


__all__ = [
    "NAME_RE",
    "SHELL_META",
    "WRITABLE_SCOPES",
    "McpConfigError",
    "config_path",
    "delete_entry",
    "read_document",
    "read_servers",
    "save_entry",
    "validate_entry",
    "validate_name",
    "write_document",
]
