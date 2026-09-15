"""CORE-context-files: the project's own instruction files reaching the model.

``/init`` and ``/deepinit`` write ``AGENTS.md`` files, and almost none of them
were used.  Only ``<workdir>/AGENTS.md`` was ever read, the 4000-character clip
silently dropped the useful half of a real one, and the 30-second environment
cache meant the turn right after ``/init`` still ran without the file that had
just been written.

Discovery now follows Hermes' ``build_context_files_prompt`` (MIT, see
``docs/design/deviations/CORE-context-files.md``): one type wins, the
``AGENTS.md`` chain runs from the git root down, and the cap comes from the
model's own context window.  On top of that, the nested files ``/deepinit``
writes are loaded up front while the budget allows, and the rest are attached
to the first tool result that touches their directory.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.agent import agent
from snowpea_core.agent import context_files as ctx_files
from snowpea_core.commands.registry import CONTEXT_WRITING_COMMANDS
from snowpea_core.prompts import environment
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolResult

# ``asyncio_mode = "auto"`` in pyproject.toml runs the async tests here; a
# module-level asyncio mark would warn on every synchronous one.

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "prompts.json"
TIMEOUT = 15.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A project that is a git repository, since the chain stops at its root."""
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "test").mkdir()
    return root


async def open_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


def make_session(workdir: Path, **kwargs: object) -> Session:
    return Session(id="s-test", workdir=workdir, **kwargs)  # type: ignore[arg-type]


def labels(project: environment.ProjectContext) -> list[str]:
    return [item.name for item in project.files]


def strip_markers(text: str) -> str:
    """The kept file content, without the ``[...truncated …]`` explanation."""
    text = re.sub(r"\n\n\[\.\.\.truncated.*?\]\n\n", "", text, flags=re.S)
    return re.sub(r"\n?…\[truncated:.*?\]", "", text, flags=re.S)


# ---------------------------------------------------------------------------
# (1) discovery: one type wins, in order
# ---------------------------------------------------------------------------


def test_only_the_first_matching_type_is_loaded(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("agents", encoding="utf-8")
    (workdir / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (workdir / ".cursorrules").write_text("cursor", encoding="utf-8")
    assert labels(environment.build_project_context(workdir)) == ["AGENTS.md"]

    (workdir / ".snowpea").mkdir()
    (workdir / ".snowpea" / "instructions.md").write_text("snowpea", encoding="utf-8")
    project = environment.build_project_context(workdir)
    assert labels(project) == [".snowpea/instructions.md"]
    assert "snowpea" in project.files[0].text


def test_claude_md_wins_when_there_is_no_agents_file(workdir: Path) -> None:
    (workdir / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
    (workdir / ".cursorrules").write_text("cursor rules", encoding="utf-8")
    assert labels(environment.build_project_context(workdir)) == ["CLAUDE.md"]


def test_cursor_rules_are_the_last_resort(workdir: Path) -> None:
    (workdir / ".cursorrules").write_text("cursor rules", encoding="utf-8")
    rules = workdir / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "b.mdc").write_text("b rule", encoding="utf-8")
    (rules / "a.mdc").write_text("a rule", encoding="utf-8")
    assert labels(environment.build_project_context(workdir)) == [
        ".cursorrules",
        ".cursor/rules/a.mdc",
        ".cursor/rules/b.mdc",
    ]


def test_nothing_found_is_an_empty_block(workdir: Path) -> None:
    project = environment.build_project_context(workdir)
    assert not project
    assert environment.context_files_block(project) == ""


# ---------------------------------------------------------------------------
# (2) the AGENTS.md chain
# ---------------------------------------------------------------------------


def test_the_chain_runs_from_the_git_root_down(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    inner = workdir / "src" / "pkg"
    inner.mkdir()
    (inner / "AGENTS.md").write_text("pkg rules", encoding="utf-8")

    project = environment.build_project_context(inner, include_nested=False)
    # Root first, the session's own directory last: deeper is more specific.
    assert labels(project) == ["../../AGENTS.md", "../AGENTS.md", "AGENTS.md"]
    assert "root rules" in project.files[0].text
    assert "pkg rules" in project.files[-1].text


def test_the_override_file_shadows_the_committed_one(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("committed", encoding="utf-8")
    (workdir / "AGENTS.override.md").write_text("personal", encoding="utf-8")
    project = environment.build_project_context(workdir)
    assert labels(project) == ["AGENTS.override.md"]
    assert "personal" in project.files[0].text
    assert "committed" not in project.files[0].text


def test_identical_content_along_the_chain_is_loaded_once(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("the same rules", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("the same rules", encoding="utf-8")
    project = environment.build_project_context(workdir / "src", include_nested=False)
    assert labels(project) == ["../AGENTS.md"]


def test_without_a_git_root_the_chain_is_the_workdir_alone(tmp_path: Path) -> None:
    """A file planted in /tmp or $HOME must never gain prompt authority."""
    (tmp_path / "AGENTS.md").write_text("planted", encoding="utf-8")
    inner = tmp_path / "scratch"
    inner.mkdir()
    (inner / "AGENTS.md").write_text("mine", encoding="utf-8")

    assert environment.find_git_root(inner) is None
    assert environment.agents_directory_chain(inner) == [inner.resolve()]
    project = environment.build_project_context(inner)
    assert labels(project) == ["AGENTS.md"]
    assert "planted" not in project.files[0].text


def test_the_chain_stops_at_the_git_root(workdir: Path) -> None:
    (workdir.parent / "AGENTS.md").write_text("above the repo", encoding="utf-8")
    (workdir / "AGENTS.md").write_text("in the repo", encoding="utf-8")
    project = environment.build_project_context(workdir)
    assert labels(project) == ["AGENTS.md"]
    assert "above the repo" not in project.files[0].text


# ---------------------------------------------------------------------------
# (3) caps and truncation
# ---------------------------------------------------------------------------


def test_the_cap_scales_with_the_context_window() -> None:
    floor = environment.CONTEXT_FILE_MAX_CHARS
    assert environment.context_file_max_chars(None) == floor
    assert environment.context_file_max_chars(0) == floor
    # A small local model (<= 32k) drops to the 8 000 floor; > 32k stays at 20 000.
    assert environment.context_file_max_chars(8_000) == environment.CONTEXT_FILE_LOW_WINDOW_CHARS
    assert environment.context_file_max_chars(32_000) == environment.CONTEXT_FILE_LOW_WINDOW_CHARS
    assert environment.context_file_max_chars(40_000) == floor
    assert environment.context_file_max_chars(200_000) == int(200_000 * 4 * 0.06)
    assert (
        environment.context_file_max_chars(10_000_000)
        == environment.CONTEXT_FILE_CEILING_CHARS
    )
    # An explicit override wins over everything.
    assert environment.context_file_max_chars(200_000, override=1_234) == 1_234


def test_the_total_cap_scales_with_the_context_window() -> None:
    """CORE-round-cost §3: the whole block has its own, tighter ceiling."""
    assert environment.context_files_max_chars(None) == environment.CONTEXT_FILES_MIN_CHARS
    assert environment.context_files_max_chars(8_000) == environment.CONTEXT_FILES_MIN_CHARS
    assert environment.context_files_max_chars(200_000) == int(200_000 * 4 * 0.10)
    assert (
        environment.context_files_max_chars(10_000_000)
        == environment.CONTEXT_FILES_MAX_CHARS
    )
    assert environment.context_files_max_chars(200_000, override=999) == 999


def test_the_total_cap_truncates_the_deepest_file_first(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("R" * 400, encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("S" * 400, encoding="utf-8")
    project = environment.build_project_context(
        workdir / "src", max_chars=1_000, total_max_chars=500, include_nested=False
    )
    root, deepest = project.files
    # The root file is the project's contract, so it survives whole.
    assert root.text == "R" * 400
    assert root.truncated is False
    assert deepest.truncated is True
    assert deepest.text.startswith("S" * 100)
    assert "…[truncated: 300 more chars; read AGENTS.md for the rest]" in deepest.text

    block = environment.context_files_block(project)
    assert "agent.contextFilesMaxChars" in block


def test_truncation_keeps_the_head_and_the_tail(workdir: Path) -> None:
    body = "H" * 500 + "M" * 500 + "T" * 500
    (workdir / "AGENTS.md").write_text(body, encoding="utf-8")
    project = environment.build_project_context(workdir, max_chars=1_000)
    text = project.files[0].text
    assert project.files[0].truncated is True
    # 70% head of 1000 characters, then 20% tail: the head runs past the Hs.
    assert text.startswith("H" * 500 + "M" * 200)
    assert text.endswith("T" * 200)
    assert "[...truncated AGENTS.md: kept 700+200 of 1500 characters." in text
    assert "read_file" in text
    # The warning is surfaced in the block, in words, not only as a marker.
    block = environment.context_files_block(project)
    assert "was truncated" in block
    assert "agent.contextFileMaxChars" in block


def test_the_merged_chain_is_capped_once_more(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("A" * 600, encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("B" * 600, encoding="utf-8")
    project = environment.build_project_context(
        workdir / "src", max_chars=1_000, total_max_chars=1_000, include_nested=False
    )
    assert [item.name for item in project.files] == ["../AGENTS.md", "AGENTS.md"]
    assert project.files[-1].truncated is True
    # The budget bounds the file content that is kept; the marker explaining
    # the cut is the slack the two ratios leave (see truncate_context_content).
    kept = sum(len(strip_markers(item.text)) for item in project.files)
    assert kept <= 1_000


async def test_a_settings_override_reaches_the_loader(daemon: Daemon) -> None:
    assert daemon.core is not None
    assert agent.context_file_settings(daemon.core) == (None, False)
    daemon.core.settings.agent.contextFileMaxChars = 123
    daemon.core.settings.agent.ignoreContextFiles = True
    assert agent.context_file_settings(daemon.core) == (123, True)


async def test_ignore_context_files_drops_the_block(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    (workdir / "AGENTS.md").write_text("Always use tabs.", encoding="utf-8")
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await open_session(client, workdir)
    session = daemon.core.sessions.get(session_id)
    assert session is not None

    daemon.core.settings.agent.ignoreContextFiles = True
    agent.invalidate_environment(session)
    assert "Always use tabs." not in agent.build_system_prompt(session, [], core=daemon.core)
    await client.stop()


# ---------------------------------------------------------------------------
# (4) nested files, loaded up front
# ---------------------------------------------------------------------------


def test_nested_files_are_found_below_the_workdir(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("root", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src", encoding="utf-8")
    (workdir / "test" / "AGENTS.md").write_text("test", encoding="utf-8")
    for skipped in ("node_modules", ".git", "dist", ".hidden"):
        buried = workdir / skipped / "pkg"
        buried.mkdir(parents=True)
        (buried / "AGENTS.md").write_text("noise", encoding="utf-8")

    assert environment.find_nested_context_files(workdir) == [
        "src/AGENTS.md",
        "test/AGENTS.md",
    ]


def test_nested_discovery_stops_at_the_depth_limit(workdir: Path) -> None:
    deep = workdir / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    for directory in (workdir / "a", workdir / "a" / "b", workdir / "a" / "b" / "c", deep):
        (directory / "AGENTS.md").write_text(directory.name, encoding="utf-8")

    assert environment.find_nested_context_files(workdir, max_depth=2) == ["a/AGENTS.md"]
    # The default reaches four levels below the workdir, and no further.
    assert environment.NESTED_CONTEXT_MAX_DEPTH == 4
    assert environment.find_nested_context_files(workdir) == [
        "a/AGENTS.md",
        "a/b/AGENTS.md",
        "a/b/c/AGENTS.md",
    ]


def test_the_whole_deepinit_hierarchy_is_loaded_up_front(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (workdir / "test" / "AGENTS.md").write_text("test rules", encoding="utf-8")

    project = environment.build_project_context(workdir)
    assert labels(project) == ["AGENTS.md", "src/AGENTS.md", "test/AGENTS.md"]
    assert project.nested_loaded == ("src/AGENTS.md", "test/AGENTS.md")
    assert project.nested_skipped == ()

    block = environment.context_files_block(project)
    for text in ("root rules", "src rules", "test rules"):
        assert text in block
    assert "Nested instructions not loaded" not in block


def test_what_does_not_fit_is_listed_instead(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("R" * 400, encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("S" * 400, encoding="utf-8")
    (workdir / "test" / "AGENTS.md").write_text("T" * 400, encoding="utf-8")

    project = environment.build_project_context(
        workdir, max_chars=500, total_max_chars=500
    )
    assert labels(project) == ["AGENTS.md"]
    assert project.nested_loaded == ()
    assert project.nested_skipped == ("src/AGENTS.md", "test/AGENTS.md")

    block = environment.context_files_block(project)
    assert (
        "Nested instructions not loaded (read_file when you work there): "
        "src/AGENTS.md, test/AGENTS.md" in block
    )


async def test_a_brand_new_session_sees_the_whole_hierarchy(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """The user's requirement: a fresh thread in the project already has them."""
    (workdir / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (workdir / "test" / "AGENTS.md").write_text("test rules", encoding="utf-8")

    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await open_session(client, workdir)
    session = daemon.core.sessions.get(session_id)
    assert session is not None

    prompt = agent.build_system_prompt(session, [], core=daemon.core)
    assert "# Project Context" in prompt
    for text in ("root rules", "src rules", "test rules"):
        assert text in prompt
    assert session.loaded_context_files == {
        "AGENTS.md",
        "src/AGENTS.md",
        "test/AGENTS.md",
    }

    # A resumed session sees the same thing.  A child does not: a lean child
    # starts with the root file alone and picks the nested ones up on demand
    # when a tool touches their directory (CORE-round-cost).
    restored = await daemon.core.sessions.restore(session_id)
    assert restored is not None
    child = Session(id="s-child", workdir=workdir, is_subagent=True)
    lean = agent.build_system_prompt(child, [], core=daemon.core)
    assert "root rules" in lean
    assert "src rules" not in lean

    daemon.core.settings.agents.childContext = "full"
    agent.invalidate_environment(child)
    assert "src rules" in agent.build_system_prompt(child, [], core=daemon.core)
    daemon.core.settings.agents.childContext = "lean"
    await client.stop()


async def test_a_tiny_cap_leaves_the_rest_to_be_read(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    (workdir / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (workdir / "test" / "AGENTS.md").write_text("test rules", encoding="utf-8")
    daemon.core.settings.agent.contextFileMaxChars = 12
    daemon.core.settings.agent.contextFilesMaxChars = 12

    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await open_session(client, workdir)
    session = daemon.core.sessions.get(session_id)
    assert session is not None
    agent.invalidate_environment(session)

    prompt = agent.build_system_prompt(session, [], core=daemon.core)
    assert "root rules" in prompt
    assert "src rules" not in prompt
    assert (
        "Nested instructions not loaded (read_file when you work there): "
        "src/AGENTS.md, test/AGENTS.md" in prompt
    )
    assert session.loaded_context_files == {"AGENTS.md"}
    await client.stop()


# ---------------------------------------------------------------------------
# (5) the nearest file, attached to a tool result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("read_file", {"path": "src/app.py"}, "src/app.py"),
        ("write_file", {"path": "src/app.py", "content": ""}, "src/app.py"),
        ("edit_file", {"path": "src/app.py", "old": "a", "new": "b"}, "src/app.py"),
        ("list_dir", {"path": "src"}, "src"),
        ("glob", {"pattern": "*.py", "path": "src"}, "src"),
        ("grep", {"pattern": "x", "path": "src"}, "src"),
        ("shell", {"command": "cd src && pytest"}, "src"),
        ("shell", {"command": "cd 'src'; ls"}, "src"),
        ("shell", {"command": "pytest", "cwd": "src"}, "src"),
        ("shell", {"command": "pytest"}, None),
        ("web_search", {"query": "x"}, None),
    ],
)
def test_the_path_a_call_is_about(name: str, args: dict, expected: str | None) -> None:
    assert ctx_files.path_for_call(name, args) == expected


def test_the_nearest_file_is_the_one_below_the_root(workdir: Path) -> None:
    (workdir / "AGENTS.md").write_text("root", encoding="utf-8")
    (workdir / "src" / "AGENTS.md").write_text("src", encoding="utf-8")
    deep = workdir / "src" / "inner"
    deep.mkdir()
    (deep / "code.py").write_text("", encoding="utf-8")

    assert ctx_files.nearest_context_file(workdir, "src/inner/code.py") == (
        workdir / "src" / "AGENTS.md"
    )
    # A file directly under the root finds nothing: the root file is already in
    # the prompt, so repeating it would spend the window twice.
    (workdir / "top.py").write_text("", encoding="utf-8")
    assert ctx_files.nearest_context_file(workdir, "top.py") is None
    # A path outside the workdir is not ours to answer for.
    assert ctx_files.nearest_context_file(workdir, "/etc/hosts") is None


def test_the_nested_file_is_attached_once_per_session(workdir: Path) -> None:
    (workdir / "src" / "AGENTS.md").write_text("Use tabs in src.", encoding="utf-8")
    session = make_session(workdir)

    first = ctx_files.attach_nested(
        session, "read_file", {"path": "src/app.py"}, ToolResult(ok=True, output="code")
    )
    assert first.output.startswith("code")
    assert '<context file="src/AGENTS.md">' in first.output
    assert "Use tabs in src." in first.output
    assert session.seen_context_files == {"src/AGENTS.md"}

    second = ctx_files.attach_nested(
        session, "read_file", {"path": "src/other.py"}, ToolResult(ok=True, output="more")
    )
    assert second.output == "more"


def test_a_file_already_in_the_prompt_is_not_attached_again(workdir: Path) -> None:
    (workdir / "src" / "AGENTS.md").write_text("Use tabs in src.", encoding="utf-8")
    session = make_session(workdir)
    session.loaded_context_files.add("src/AGENTS.md")
    result = ctx_files.attach_nested(
        session, "read_file", {"path": "src/app.py"}, ToolResult(ok=True, output="code")
    )
    assert result.output == "code"
    assert session.seen_context_files == set()


def test_a_failed_call_gets_nothing_attached(workdir: Path) -> None:
    (workdir / "src" / "AGENTS.md").write_text("Use tabs.", encoding="utf-8")
    session = make_session(workdir)
    result = ctx_files.attach_nested(
        session, "read_file", {"path": "src/app.py"}, ToolResult(ok=False, error="nope")
    )
    assert result.output == ""
    assert session.seen_context_files == set()


def test_an_attached_file_is_clipped_with_the_same_marker(workdir: Path) -> None:
    (workdir / "src" / "AGENTS.md").write_text("z" * 500, encoding="utf-8")
    session = make_session(workdir)
    result = ctx_files.attach_nested(
        session, "list_dir", {"path": "src"}, ToolResult(ok=True, output="app.py"), limit=100
    )
    assert "[...truncated src/AGENTS.md" in result.output
    assert "read_file on src/AGENTS.md" in result.output


# ---------------------------------------------------------------------------
# (6) invalidation
# ---------------------------------------------------------------------------


def test_writing_an_instruction_file_clears_the_cache_and_the_marks(workdir: Path) -> None:
    (workdir / "src" / "AGENTS.md").write_text("old", encoding="utf-8")
    session = make_session(workdir)
    ctx_files.attach_nested(
        session, "read_file", {"path": "src/app.py"}, ToolResult(ok=True, output="code")
    )
    assert session.seen_context_files == {"src/AGENTS.md"}
    session.loaded_context_files.add("AGENTS.md")

    assert ctx_files.note_write(session, "write_file", {"path": "src/AGENTS.md"}) is True
    assert session.seen_context_files == set()
    assert session.loaded_context_files == set()
    # An ordinary source file changes nothing.
    session.seen_context_files.add("src/AGENTS.md")
    assert ctx_files.note_write(session, "write_file", {"path": "src/app.py"}) is False
    assert session.seen_context_files == {"src/AGENTS.md"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("AGENTS.md", True),
        ("src/AGENTS.md", True),
        ("src/AGENTS.override.md", True),
        ("deep/nested/CLAUDE.md", True),
        (".snowpea/instructions.md", True),
        (".cursorrules", True),
        ("src/app.py", False),
        ("AGENTS.md.bak", False),
    ],
)
def test_which_paths_count_as_instruction_files(path: str, expected: bool) -> None:
    assert ctx_files.is_context_file(path) is expected


async def test_the_turn_after_init_sees_the_new_file(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """The bug the user hit: /init writes AGENTS.md, the next turn ignores it."""
    assert "init" in CONTEXT_WRITING_COMMANDS
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await open_session(client, workdir)
    session = daemon.core.sessions.get(session_id)
    assert session is not None

    before = agent.build_system_prompt(session, [], core=daemon.core)
    assert "Freshly written rules." not in before

    (workdir / "AGENTS.md").write_text("Freshly written rules.", encoding="utf-8")
    # Still cached: the TTL has not expired.
    assert "Freshly written rules." not in agent.build_system_prompt(
        session, [], core=daemon.core
    )

    # Which is exactly what running the command clears.  ``/init`` hands the
    # work to the model, so with the scripted provider it writes nothing; the
    # cache drop is the part under test and it happens either way.
    await daemon.core.commands.run(daemon.core, session, "init", "")
    assert "Freshly written rules." in agent.build_system_prompt(
        session, [], core=daemon.core
    )
    await client.stop()


async def test_writing_an_instruction_file_in_a_turn_refreshes_the_prompt(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """A subagent's write_file of AGENTS.md must reach the parent's next prompt."""
    assert daemon.core is not None
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await open_session(client, workdir)
    session = daemon.core.sessions.get(session_id)
    assert session is not None
    agent.build_system_prompt(session, [], core=daemon.core)  # warm the cache

    (workdir / "AGENTS.md").write_text("Written by a tool.", encoding="utf-8")
    child = Session(id="s-child", workdir=workdir)
    ctx_files.note_write(child, "write_file", {"path": "AGENTS.md"})

    assert "Written by a tool." in agent.build_system_prompt(session, [], core=daemon.core)
    await client.stop()
