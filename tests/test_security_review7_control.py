"""Security review 7, part A: the control-plane table is complete (L7-1), holds the
global MCP file and the external CLIs' logins (L7-2).  Paths are DATA; nothing runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import snowpea_core
from snowpea_core.config.paths import HOME_ENTRIES, Paths
from snowpea_core.security import control_plane as cp

USER = "/home/u"
HOME = f"{USER}/.snowpea"

#: ``home / "name"`` and friends, as the core spells them.
_BUILT = re.compile(
    r"(?:paths\.home|self\.home|core\.paths\.home|Path\(home\)|Path\(self\.home\)|\bhome)"
    r"\s*\)?\s*/\s*\"([^\"/{}]+)\"")
#: Names a ``home`` that is not snowpea's (an external CLI's own home, a
#: project's ``.snowpea`` folder) is given in the sources scanned below.
_NOT_SNOWPEA_HOME = {
    "chats", "projects", "node_modules", "budget.json", "escalations.json", "projects.json",
    "settings.json", "bin", "sessions",
}


def _classified(name: str) -> bool:
    return cp.entry_for((name,)) is not None or name in cp.WORKING


def test_every_declared_home_entry_is_classified(tmp_path: Path) -> None:
    assert [name for name in HOME_ENTRIES if not _classified(name)] == []
    paths = Paths(home=tmp_path)
    for name in dir(Paths):
        if name.startswith("_") or not isinstance(getattr(Paths, name), property):
            continue
        value = getattr(paths, name)
        if isinstance(value, Path) and value != tmp_path:
            top = value.relative_to(tmp_path).parts[0]
            assert _classified(top), name


def test_no_source_builds_a_home_file_that_is_not_declared() -> None:
    """A file a module starts keeping under the home cannot stay out of the table."""
    root = Path(snowpea_core.__file__).parent
    declared = set(HOME_ENTRIES) | {entry.parts[0] for entry in cp.CONTROL} | set(cp.WORKING)
    declared |= {"token", "token.tmp", "credentials.json", "daemon.json", "settings.json",
                 "state.db", "install.json", "logs", "update-check.json"}
    unknown: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if "vendor" in path.parts or "web_dist" in path.parts:
            continue
        for match in _BUILT.finditer(path.read_text(encoding="utf-8", errors="replace")):
            name = match.group(1)
            if name not in declared and name not in _NOT_SNOWPEA_HOME:
                unknown.setdefault(name, str(path.relative_to(root)))
    assert unknown == {}


@pytest.mark.parametrize(("rel", "level"), [
    (".mcp.json", "write"),
    ("state.db", "write"), ("state.db-wal", "write"),
    ("jarvis/delegation.db", "write"), ("jarvis/jobs.json", "write"),
    ("trust/projects.json", "write"), ("trust/migrated.json", "write"),
    ("daemon.lock", "write"), ("setup-state.json", "write"),
    (".codex/auth.json", "all"), (".claude/.credentials.json", "all"),
    (".codex/config.toml", "ask"), (".claude/settings.json", "ask"),
    ("lsp/node_modules/x/index.js", "ask"),
])
def test_the_table_holds(rel: str, level: str) -> None:
    entry = cp.entry_for(tuple(rel.split("/")))
    assert entry is not None and entry.level == level, rel


def test_working_state_is_not_in_the_table() -> None:
    for rel in ("memory.md", "extension-data/x/data.json", "logs/daemon.log", "plans/p.md"):
        assert cp.entry_for(tuple(rel.split("/"))) is None, rel
        assert rel.split("/")[0] in cp.WORKING


@pytest.mark.parametrize("path", [
    f"{USER}/.codex/auth.json", f"{USER}/.claude/.credentials.json", f"{USER}/.claude.json",
    f"{USER}/.gemini/oauth_creds.json", f"{USER}/.config/cursor/auth.json",
    f"{USER}/.config/anthropic/credentials.json",
])
def test_an_external_cli_login_is_a_secret(path: str) -> None:
    entry = cp.external_secret(path, USER)
    assert entry is not None and entry.level == "all"


def test_the_fs_methods_answer_from_the_same_table() -> None:
    hit = cp.control_plane_effect(f"{USER}/.codex/auth.json", "read", home=HOME, user_home=USER)
    assert hit is not None and hit.verdict == "blocked" and hit.shown == "~/.codex/auth.json"
    hit = cp.control_plane_effect(f"{HOME}/.mcp.json", "write", home=HOME, user_home=USER)
    assert hit is not None and hit.verdict == "blocked"
    assert cp.control_plane_effect(f"{HOME}/.mcp.json", "read", home=HOME,
                                   user_home=USER) is None
    assert cp.control_plane_effect(f"{USER}/.codex/config.toml", "read", home=HOME,
                                   user_home=USER) is None


@pytest.mark.parametrize("path", [
    f"{USER}/.codex/config.toml", f"{USER}/.claude/settings.json", f"{USER}/proj/auth.json",
    f"{USER}/.config/anthropic-notes.txt",
])
def test_other_files_of_an_external_cli_are_not(path: str) -> None:
    assert cp.external_secret(path, USER) is None
