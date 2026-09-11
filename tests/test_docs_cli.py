"""Behaviour tests for ``scripts/check_docs_cli.py``.

The headline test runs the script over the real documentation exactly as CI
does, so a README or manual page that documents a flag the CLI does not have,
or links to a file that does not exist, fails the suite.

The rest fabricate small worlds in ``tmp_path`` to prove the checker is not
vacuously passing: it must reject an unknown flag, an unknown subcommand and a
broken relative link, and it must accept the shapes the docs actually use.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_docs_cli.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cdc():
    return _load_module()


@pytest.fixture(scope="module")
def cli_tree(cdc):
    """The real CLI's help tree. Skips if the CLI cannot be run at all."""
    env = dict(os.environ)
    env["SNOWPEA_HOME"] = env.get("SNOWPEA_HOME", "/tmp/snowpea-docs-check")
    env["COLUMNS"] = "200"
    root = cdc.build_cli_tree([sys.executable, "-m", "snowpea_core.cli.main"], env)
    if root is None or not root.subcommands:
        root = cdc.build_cli_tree(["snowpea"], env)
    if root is None or not root.subcommands:
        pytest.skip("the snowpea CLI is not runnable in this environment")
    return root


# --------------------------------------------------------------------------
# The real thing
# --------------------------------------------------------------------------


def test_documentation_matches_the_cli():
    """Every documented `snowpea …` invocation and relative link must hold."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"scripts/check_docs_cli.py rejected the documentation:\n{proc.stdout}\n{proc.stderr}"
    )


def test_every_readme_and_manual_page_is_covered():
    module = _load_module()
    files = {p.relative_to(REPO_ROOT).as_posix() for p in module.doc_files()}
    # The ten READMEs.
    for lang in ["", ".ko", ".ja", ".zh-CN", ".zh-TW", ".es", ".fr", ".de", ".pt-BR", ".ru"]:
        assert f"README{lang}.md" in files
    # The manual, both complete languages plus the index.
    assert "docs/manual/README.md" in files
    for page in [
        "index",
        "install",
        "setup",
        "modes",
        "commands",
        "plugins",
        "scheduler",
        "gateway",
        "backends",
        "headless",
        "protocol",
    ]:
        assert f"docs/manual/en/{page}.md" in files
        assert f"docs/manual/ko/{page}.md" in files
    for lang in ["ja", "zh-CN", "es"]:
        for page in ["install", "setup", "commands"]:
            assert f"docs/manual/{lang}/{page}.md" in files


# --------------------------------------------------------------------------
# The checker rejects what it should
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "snowpea tools list --json",
        "snowpea commands list --json",
        "snowpea daemon status",
        "snowpea provider login openai",
        "snowpea setup --vendor deepseek --key sk-x",
        "snowpea skill install ./my-plugin",
        "snowpea job schedule --at '0 9 * * *' --task summarize --channel log",
        "snowpea gateway bind telegram TOKEN agent:scribe --user 1",
        "snowpea -c 'hello' --mode auto --json --cwd . --timeout 30",
        "uv run snowpea --version",
        "SNOWPEA_HOME=/tmp/x snowpea daemon status --json",
        # Landed in US-020 / US-022; validated against the real parsers now.
        "snowpea team status",
        "snowpea service install",
    ],
)
def test_valid_invocations_are_accepted(cdc, cli_tree, command):
    assert cdc.check_invocation(cli_tree, cdc.tokenize(command)) == []


@pytest.mark.parametrize(
    "command, fragment",
    [
        ("snowpea tools list --pretty", "--pretty"),
        ("snowpea frobnicate", "frobnicate"),
        ("snowpea job schedule --when now", "--when"),
        ("snowpea skill instal ./p", "instal"),
        ("snowpea daemon restart", "restart"),
    ],
)
def test_invalid_invocations_are_rejected(cdc, cli_tree, command, fragment):
    problems = cdc.check_invocation(cli_tree, cdc.tokenize(command))
    assert problems, f"{command!r} should have been rejected"
    assert any(fragment in p for p in problems), problems


def test_allowlist_entries_name_a_story(cdc):
    """An allowlist entry without a story is a permanent hole; refuse that.

    The list is empty once every documented command has a real parser (reviewed 2026-09-11).
    """
    for chain, reason in cdc.ALLOWLIST.items():
        assert isinstance(chain, tuple) and chain, chain
        assert "US-" in reason, f"{chain} must name the story that removes it, got {reason!r}"


# --------------------------------------------------------------------------
# Extraction and link checking
# --------------------------------------------------------------------------


def test_only_shell_blocks_are_scanned(cdc):
    text = (
        "# Title\n\n"
        '```json\n{"cmd": "snowpea --not-a-flag"}\n```\n\n'
        "```text\nsnowpea <subcommand> → schematic, not a command\n```\n\n"
        "```bash\nsnowpea tools list\n```\n"
    )
    scanned = [
        segment
        for _, info, body in cdc.iter_code_blocks(text)
        if info in cdc.SHELL_LANGUAGES
        for _, segment in cdc.extract_invocations(body)
    ]
    assert scanned == ["snowpea tools list"]


def test_pipelines_and_prompts_are_split(cdc):
    body = [
        "$ snowpea tools list --json | jq '.[].name'",
        "curl -fsSL https://example.invalid/install.sh | sh",
        "# snowpea commented out --bogus",
        "snowpea -c 'summarize' \\",
        "  --json",
    ]
    found = [segment for _, segment in cdc.extract_invocations(body)]
    assert found == ["snowpea tools list --json", "snowpea -c 'summarize' --json"]


def test_broken_relative_link_is_reported(cdc, tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md) [bad](missing.md) [ext](https://example.invalid/x) [anchor](#s)\n",
        encoding="utf-8",
    )
    (tmp_path / "other.md").write_text("hi\n", encoding="utf-8")
    problems = cdc.check_links(page)
    assert len(problems) == 1
    assert "missing.md" in problems[0]


def test_html_attribute_links_are_checked(cdc, tmp_path):
    page = tmp_path / "page.md"
    page.write_text('<a href="nope.md">x</a> <img src="https://example.invalid/a.png">\n', "utf-8")
    problems = cdc.check_links(page)
    assert len(problems) == 1
    assert "nope.md" in problems[0]


def test_link_check_runs_without_the_cli():
    """`--skip-cli` must still validate links, so docs CI works without a daemon."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-cli"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
