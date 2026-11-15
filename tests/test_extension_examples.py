"""Every extension under examples/extensions loads, works and leaves nothing behind.

Each test goes through :func:`snowpea_core.extensions.testing.load_extension`:
the example is copied into a temporary ``SNOWPEA_HOME``, loaded by the real
``ExtensionManager`` of a private daemon, exercised, unloaded, and checked to
have left nothing registered.  No network: the tracker of ``ticket-workflow``
is a loopback server started here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from snowpea_core.extensions.manager import read_manifest, render_template
from snowpea_core.extensions.testing import ExtensionLoadError, load_extension

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "extensions"


def _git_branch(workdir: Path, branch: str) -> None:
    git = workdir / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def test_every_example_has_a_valid_manifest() -> None:
    folders = sorted(path for path in EXAMPLES.iterdir() if path.is_dir())
    assert len(folders) >= 9
    for folder in folders:
        assert (folder / "plugin.py").is_file(), folder
        manifest, problem = read_manifest(folder)
        assert not problem, (folder, problem)
        assert manifest["name"] == folder.name and manifest["engines"]["snowpea"] == "^1"


def test_snowpea_extension_new_writes_the_hello_example() -> None:
    for file_name, text in render_template("hello").items():
        assert (EXAMPLES / "hello" / file_name).read_text(encoding="utf-8") == text, file_name


async def test_hello_and_its_own_test(tmp_path: Path) -> None:
    # The example's own test file is the harness the guide shows: run it as is.
    spec = importlib.util.spec_from_file_location(
        "hello_example_test", EXAMPLES / "hello" / "tests" / "test_hello.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.test_greets_by_name(tmp_path)


async def test_file_stats(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "file-stats", home=tmp_path) as ext:
        project = Path(ext.session.workdir)
        (project / "src").mkdir()
        (project / "src" / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        (project / "src" / "b.txt").write_text("three\n", encoding="utf-8")
        result = await ext.call_tool("file_stats", {"path": "src", "pattern": "*.py"})
        assert result.ok and result.output == "1 files, 2 lines, 8 bytes"
        assert result.meta == {"files": 1, "lines": 2, "bytes": 8}
        whole = await ext.call_tool("file_stats", {"path": "src"})
        assert whole.output.startswith("2 files, 3 lines")
        outside = await ext.call_tool("file_stats", {"path": "../.."})
        assert not outside.ok and "outside the project" in outside.error
        missing = await ext.call_tool("file_stats", {"path": "nope"})
        assert not missing.ok and "does not exist" in missing.error
        tool = ext.core.tools.get("file_stats")
        assert tool.permission == "read" and tool.parallel_safe
        assert tool.source == "extension:file-stats"


async def test_house_style(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "house-style", home=tmp_path) as ext:
        prompt = ext.system_prompt()
        assert "House style for this team:" in prompt and "list the commits" not in prompt
        ext.session.mode = "plan"
        assert "list the commits you would make" in ext.system_prompt()


async def test_git_branch(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "git-branch", home=tmp_path) as ext:
        assert "git-branch:branch" not in ext.runtime_context()  # not a repository yet
        _git_branch(Path(ext.session.workdir), "feature/login")
        assert ext.runtime_context()["git-branch:branch"] == "Current git branch: feature/login"


async def test_redact_secrets(tmp_path: Path) -> None:
    from snowpea_core.extensions import Tool, ToolResult

    config = {"patterns": [r"hunter\d"]}
    async with load_extension(EXAMPLES / "redact-secrets", home=tmp_path, config=config) as ext:
        assert ext.extension.context.config == {
            "patterns": [r"hunter\d"],
            "replacement": "[redacted]",
        }

        async def leak(tool_ctx: Any, args: dict[str, Any]) -> ToolResult:
            return ToolResult(ok=True, output="key=sk-" + "a" * 24 + " pw=hunter2 ok")

        dispose = ext.extension.context.tools.register(
            Tool(name="leaky", category="t", description="d", input_schema={"type": "object"},
                 permission="read", run=leak)
        )
        result = await ext.call_tool("leaky", {})
        assert result.output == "key=[redacted] pw=[redacted] ok"
        assert result.contexts == ["A credential in this result was redacted."]
        dispose()
    with pytest.raises(ExtensionLoadError, match=r"config\.patterns\[0\]"):
        async with load_extension(
            EXAMPLES / "redact-secrets", home=tmp_path / "bad", config={"patterns": [""]}
        ):
            pass


async def test_finish_the_job(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "finish-the-job", home=tmp_path) as ext:
        assert await ext.stop_turn("All done.") is None
        first = await ext.stop_turn("Done, but TODO: tests")
        assert first is not None and first.producer == "finish-the-job"
        assert (first.count, first.limit) == (1, 2)
        assert (await ext.stop_turn("TODO still")).count == 2
        assert await ext.stop_turn("TODO forever") is None  # the cap holds


async def test_scratchpad(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "scratchpad", home=tmp_path) as ext:
        assert ext.core.commands.get("scratch").source == "extension:scratchpad"
        assert await ext.run_command("scratch") == [
            "The scratchpad is empty. Add a note with /scratch add <text>."
        ]
        assert await ext.run_command("scratch", "add ship it") == ["Noted (1 in the scratchpad)."]
        assert await ext.run_command("scratch") == ["1. ship it"]
        store = tmp_path / "extension-data" / "scratchpad" / "notes.json"
        assert json.loads(store.read_text()) == ["ship it"]
        await ext.reload()  # the notes outlive a reload
        assert await ext.run_command("scratch") == ["1. ship it"]


async def test_glossary_mcp(tmp_path: Path) -> None:
    async with load_extension(EXAMPLES / "glossary-mcp", home=tmp_path) as ext:
        for _ in range(300):
            if ext.core.tools.get("mcp__glossary__define") is not None:
                break
            await asyncio.sleep(0.05)
        result = await ext.call_tool("mcp__glossary__define", {"term": "Daemon"})
        assert result.ok and "background process" in result.output
    from snowpea_core.tools import mcp_client

    assert mcp_client.MANAGER.get("glossary") is None


async def test_ticket_workflow(tmp_path: Path) -> None:
    async def ticket(request: web.Request) -> web.Response:
        if request.match_info["id"] != "SP-12":
            return web.json_response({}, status=404)
        assert request.headers.get("Authorization") == "Bearer t0k"
        return web.json_response({"title": "Fix login", "status": "open"})

    app = web.Application()
    app.router.add_get("/api/tickets/{id}", ticket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    config = {"project": "SP", "baseUrl": base, "token": "t0k"}
    try:
        folder = EXAMPLES / "ticket-workflow"
        async with load_extension(folder, home=tmp_path, config=config) as ext:
            assert ext.registrations == [
                "dispose:Tracker.close",
                "tool:ticket_get",
                "section:ticket-workflow:commits",
                "context:ticket-workflow:current",
                "command:ticket",
            ]
            found = await ext.call_tool("ticket_get", {"id": "sp-12"})
            assert found.ok and found.output == f"SP-12: Fix login [open]\n{base}/browse/SP-12"
            missing = await ext.call_tool("ticket_get", {"id": "SP-99"})
            assert not missing.ok and missing.error == "SP-99 does not exist"
            wrong = await ext.call_tool("ticket_get", {"id": "XY-1"})
            assert "look like SP-123" in wrong.error
            assert "(SP-123: ...)" in ext.system_prompt()
            not_yet = await ext.run_command("ticket")
            assert not_yet == ["This branch is not named after a SP ticket."]
            _git_branch(Path(ext.session.workdir), "SP-12-fix-login")
            assert ext.runtime_context()["ticket-workflow:current"].endswith("SP-12")
            assert await ext.run_command("ticket") == [f"SP-12: {base}/browse/SP-12"]
            client = ext.extension.context  # the HTTP client closes on unload
            assert client is not None
        with pytest.raises(ExtensionLoadError, match="config.project"):
            async with load_extension(
                EXAMPLES / "ticket-workflow",
                home=tmp_path / "bad",
                config={"project": "lower", "baseUrl": base},
            ):
                pass
    finally:
        await runner.cleanup()


# ---- the guide's code blocks are the examples ------------------------------------

GUIDES = sorted((EXAMPLES.parents[1] / "docs" / "manual").glob("*/extensions.md"))
MARKER = "<!-- example: "


def _blocks(text: str) -> list[tuple[str | None, str, str]]:
    """``(example path or None, info string, body)`` of every fenced block."""
    lines = text.splitlines()
    found: list[tuple[str | None, str, str]] = []
    marker: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(MARKER):
            marker = line[len(MARKER) :].removesuffix("-->").strip()
        elif line.startswith("```"):
            info = line[3:].strip()
            end = index + 1
            while not lines[end].startswith("```"):
                end += 1
            found.append((marker, info, "\n".join(lines[index + 1 : end]) + "\n"))
            marker = None
            index = end
        elif line.strip():
            marker = None
        index += 1
    return found


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.parent.name)
def test_every_code_block_in_the_guide_is_tested(guide: Path) -> None:
    repo = EXAMPLES.parents[1]
    blocks = _blocks(guide.read_text(encoding="utf-8"))
    assert blocks
    for marker, info, body in blocks:
        if marker is not None:
            source = (repo / marker).read_text(encoding="utf-8")
            assert body in source, f"{guide.name}: block marked {marker} is not in that file"
            continue
        # Otherwise only snowpea commands, which scripts/check_docs_cli.py checks.
        assert info == "bash", f"{guide}: an unmarked {info or 'plain'} block:\n{body}"
        for line in body.splitlines():
            assert not line.strip() or line.startswith("snowpea "), (guide, line)


def test_every_translation_shows_the_same_examples() -> None:
    def markers(guide: Path) -> list[str | None]:
        return [marker for marker, _info, _body in _blocks(guide.read_text("utf-8"))]

    assert [guide.parent.name for guide in GUIDES] == ["en", "es", "ja", "ko", "zh-CN"]
    english = markers(GUIDES[0])
    for guide in GUIDES[1:]:
        assert markers(guide) == english, guide
