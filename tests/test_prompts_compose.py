"""The prompt library: tier order, overrides, rendering, budgets, snapshots.

The golden snapshots exist so that a prompt change is a reviewed change rather
than an incidental one.  Regenerate them deliberately::

    uv run pytest tests/test_prompts_compose.py --update-golden
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from snowpea_core.prompts import compose, environment, loader, tool_descriptions

GOLDEN_DIR = Path(__file__).parent / "golden" / "prompts"

#: Per-tier ceilings, in tokens, approximated as ``words * 1.3``.
#:
#: The report suggested 1200 / 800 / 600.  ``base.md`` alone is 717 words
#: (~932 tokens) once gaps 1, 6, 7, 8, 9 and 12 are closed, and the stable tier
#: also carries the vendor layer, the config and search rules, the mode and a
#: role, so 1200 is not reachable with the text the report itself specifies.
#: The ceiling is set where the intended library actually fits, with headroom,
#: and its job is unchanged: the library cannot grow unbounded. Raised from
#: 2200 for CORE-skill-create's one-sentence addition to base.md's "Skills
#: and plugins" paragraph (/skill create); raised again from 2900 for M15 §D2's
#: memory-guidance fragment, which is twelve lines the stable tier did not
#: carry before; raised again from 3250 for CORE-vision's one-line ``view_image``
#: guidance in base.md; raised again for Hermes-style ``patch`` tool text and
#: ``small-local`` edit guidance.
TIER_BUDGET_TOKENS = {"stable": 3400, "context": 800, "volatile": 600}

MODES = ("plan", "accept", "auto")
VENDOR_CLASSES = ("anthropic", "openai-family", "small-local")
ROLES = (None, "executor")


def tokens(text: str) -> float:
    """The approximation the budget is written against."""
    return len(text.split()) * 1.3


@dataclass(frozen=True)
class FakeSpec:
    """Stands in for ``providers.base.ToolSpec`` — only these fields are read."""

    name: str
    description: str


TOOLS = (
    FakeSpec("read_file", tool_descriptions.READ_FILE),
    FakeSpec("patch", tool_descriptions.PATCH),
    FakeSpec("shell", tool_descriptions.SHELL),
)

MEMORY_BLOCK = (
    "Things you remember about this user from earlier sessions.\n"
    '<memory id="m1" tags="style">The user prefers short replies.</memory>'
)

FIXED_CLOCK = datetime(2026, 9, 12, 9, 30, tzinfo=UTC)

FIXED_GIT = environment.GitSnapshot(
    branch="main",
    upstream="origin/main",
    ahead=2,
    behind=0,
    modified=3,
    untracked=1,
)


@pytest.fixture(autouse=True)
def _isolated_prompts() -> None:
    """No project override leaks between tests."""
    loader.set_project_root(None)
    loader.clear_cache()


def fixed_environment() -> environment.Environment:
    """An environment with nothing probed from the host."""
    env = environment.collect(
        "/work/project",
        now=FIXED_CLOCK,
        git=FIXED_GIT,
        context_files=[],
        model="anthropic:claude-sonnet-5",
        mode="accept",
    )
    # ``collect`` reads the real platform; freeze it for the snapshots.
    return environment.Environment(
        workdir=env.workdir,
        platform="Linux (x86_64)",
        shell="bash",
        home="/home/dev",
        today=env.today,
        model=env.model,
        mode=env.mode,
        git=env.git,
    )


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_load_reads_the_shipped_prompt() -> None:
    assert "You are snowpea" in loader.load("base")
    assert loader.load("modes/plan").startswith("You are in PLAN mode.")


def test_load_is_cached() -> None:
    loader.clear_cache()
    loader.load("base")
    first = loader.load.cache_info()
    loader.load("base")
    assert loader.load.cache_info().hits == first.hits + 1


def test_project_override_shadows_a_shipped_prompt(tmp_path: Path) -> None:
    override = tmp_path / ".snowpea" / "prompts" / "modes"
    override.mkdir(parents=True)
    (override / "plan.md").write_text("LOCAL PLAN RULES", encoding="utf-8")

    loader.set_project_root(tmp_path)
    assert loader.load("modes/plan") == "LOCAL PLAN RULES"
    # Anything the project does not shadow still comes from the package.
    assert "You are snowpea" in loader.load("base")

    loader.set_project_root(None)
    assert loader.load("modes/plan").startswith("You are in PLAN mode.")


def test_override_applies_to_the_composed_prompt(tmp_path: Path) -> None:
    override = tmp_path / ".snowpea" / "prompts"
    override.mkdir(parents=True)
    (override / "base.md").write_text("LOCAL BASE", encoding="utf-8")
    loader.set_project_root(tmp_path)
    assert compose.build_system_prompt(mode="accept").startswith("LOCAL BASE")


def test_unknown_prompt_raises() -> None:
    with pytest.raises(loader.PromptNotFound):
        loader.load("modes/nope")


def test_a_name_cannot_escape_the_prompt_directory() -> None:
    with pytest.raises(loader.PromptNotFound):
        loader.load("../../secrets")


def test_render_substitutes_both_variable_forms(tmp_path: Path) -> None:
    override = tmp_path / ".snowpea" / "prompts"
    override.mkdir(parents=True)
    (override / "base.md").write_text("a $ONE b ${TWO} c $ONE", encoding="utf-8")
    loader.set_project_root(tmp_path)
    assert loader.render("base", ONE="1", TWO="2") == "a 1 b 2 c 1"


def test_render_leaves_unknown_placeholders_alone() -> None:
    # config_rule.md talks about $SNOWPEA_HOME and must keep saying so.
    assert "$SNOWPEA_HOME" in loader.render("config_rule")


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------


def test_tiers_are_joined_stable_then_context_then_volatile() -> None:
    tiers = compose.build_tiers(
        mode="accept",
        tools=TOOLS,
        memory_block=MEMORY_BLOCK,
        environment="Environment\n- Working directory: /work",
    )
    text = tiers.text()
    assert text.index(tiers.stable) < text.index(tiers.context) < text.index(tiers.volatile)
    assert text.startswith(tiers.stable)
    assert text.endswith(tiers.volatile)


def test_volatile_tier_comes_after_the_tool_list() -> None:
    """Prefix caching dies if the memory block is interleaved earlier."""
    text = compose.build_system_prompt(tools=TOOLS, memory_block=MEMORY_BLOCK)
    assert text.index("Available tools:") < text.index(MEMORY_BLOCK.splitlines()[0])


def test_stable_tier_carries_rules_mode_and_vendor() -> None:
    tiers = compose.build_tiers(mode="plan", vendor_class="small-local")
    assert "You are snowpea" in tiers.stable
    assert "You are in PLAN mode." in tiers.stable
    assert "Tool use is mandatory." in tiers.stable
    assert "$SNOWPEA_HOME" in tiers.stable
    assert "web_search output beginning with" in tiers.stable


def test_anthropic_vendor_layer_adds_nothing() -> None:
    a = compose.build_tiers(vendor_class="anthropic").stable
    b = compose.build_tiers(vendor_class="small-local").stable
    assert len(b) > len(a)
    assert "Tool use is mandatory." not in a


def test_role_brings_the_subagent_preamble_with_it() -> None:
    stable = compose.build_tiers(role="executor").stable
    assert "You are a focused subagent." in stable
    assert "Role: executor." in stable
    assert "You are a focused subagent." not in compose.build_tiers().stable


def test_subagent_without_a_role_still_gets_the_preamble() -> None:
    assert "You are a focused subagent." in compose.build_tiers(subagent=True).stable


def test_identity_replaces_the_base_prompt() -> None:
    stable = compose.build_tiers(identity="You are testbot.").stable
    assert stable.startswith("You are testbot.")
    assert "You are snowpea" not in stable


def test_persona_lands_after_the_role_and_keeps_the_rules() -> None:
    """A named agent's own prompt adds to the discipline; it never replaces it."""
    stable = compose.build_tiers(role="executor", persona="You are docbot.").stable
    assert "You are snowpea" in stable
    assert stable.index("Role: executor.") < stable.index("You are docbot.")


def test_an_empty_persona_adds_nothing() -> None:
    assert compose.build_tiers(persona="   ").stable == compose.build_tiers().stable


def test_base_prompt_closes_the_named_gaps() -> None:
    base = loader.load("base")
    for marker in (
        "Read the relevant files with read_file",  # gap 1
        "How to answer.",  # gap 6
        "Finishing the job.",  # gap 7
        "Batching.",  # gap 8
        "Language.",  # gap 9
        "Trust.",  # gap 12
    ):
        assert marker in base


def test_plan_mode_states_its_output_contract() -> None:
    plan = loader.load("modes/plan")
    for heading in ("Goal —", "Files —", "Steps —", "Risks —", "Acceptance —", "Open questions —"):
        assert heading in plan


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def test_tool_list_is_exactly_the_supplied_specs() -> None:
    text = compose.build_system_prompt(tools=TOOLS)
    block = text.split("Available tools:\n", 1)[1]
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert lines == [f"- {spec.name}: {spec.description}" for spec in TOOLS]


def test_no_tools_means_no_tool_section() -> None:
    assert "Available tools:" not in compose.build_system_prompt(tools=())


# ---------------------------------------------------------------------------
# volatile tier
# ---------------------------------------------------------------------------


def test_context_pressure_line_appears_only_above_the_threshold() -> None:
    marker = "The context window is nearly full."
    assert marker not in compose.build_system_prompt(context_fill=0.5)
    assert marker not in compose.build_system_prompt(context_fill=None)
    assert marker in compose.build_system_prompt(context_fill=0.9)
    assert marker in compose.build_system_prompt(
        context_fill=compose.CONTEXT_PRESSURE_THRESHOLD
    )


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------


def test_auto_language_leaves_the_base_rule_to_do_the_work() -> None:
    assert compose.reply_language_rule("auto") == ""
    assert "Reply in the language the user wrote in." in compose.build_system_prompt()


def test_an_explicit_language_names_it() -> None:
    rule = compose.reply_language_rule("ko")
    assert "Reply in Korean" in rule
    assert rule in compose.build_system_prompt(reply_language="ko")


def test_an_unknown_tag_is_used_as_written() -> None:
    assert "Reply in sv-SE" in compose.reply_language_rule("sv-SE")


# ---------------------------------------------------------------------------
# vendor classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", "anthropic"),
        ("openai", "openai-family"),
        ("openrouter", "openai-family"),
        ("xai", "openai-family"),
        ("gemini", "openai-family"),
        ("local", "small-local"),
        ("qwen", "small-local"),
        ("glm", "small-local"),
        ("deepseek", "small-local"),
        ("minimax", "small-local"),
        ("kimi", "small-local"),
        ("something-new", "openai-family"),
        (None, "anthropic"),
    ],
)
def test_vendor_class_for(provider: str | None, expected: str) -> None:
    assert compose.vendor_class_for(provider) == expected


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def test_git_status_is_parsed_from_one_porcelain_v2_call() -> None:
    raw = "\n".join(
        [
            "# branch.oid abc123",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 .M N... 100644 100644 100644 aaa bbb core/a.py",
            "2 R. N... 100644 100644 100644 ccc ddd R100 new.py\told.py",
            "u UU N... 100644 100644 100644 100644 e f g conflict.py",
            "? untracked.py",
        ]
    )
    snapshot = environment.parse_git_status(raw)
    assert snapshot.branch == "main"
    assert snapshot.upstream == "origin/main"
    assert (snapshot.ahead, snapshot.behind) == (2, 1)
    assert (snapshot.modified, snapshot.untracked, snapshot.conflicted) == (2, 1, 1)
    assert snapshot.branch_line() == "main -> origin/main (ahead 2, behind 1)"
    assert snapshot.summary() == "2 modified, 1 untracked, 1 conflicted"


def test_a_clean_detached_tree_reads_sensibly() -> None:
    snapshot = environment.parse_git_status("# branch.head (detached)\n")
    assert snapshot.branch is None
    assert snapshot.branch_line() == "(detached)"
    assert snapshot.summary() == "clean"


def test_environment_block_renders_date_workdir_and_workspace() -> None:
    block = environment.build_environment_block(fixed_environment())
    assert "- Working directory: /work/project" in block
    assert "- Today: Saturday, 12 September 2026" in block
    assert "- Branch: main -> origin/main (ahead 2)" in block
    assert "- Status: 3 modified, 1 untracked" in block
    assert "Model: anthropic:claude-sonnet-5" in block


def test_workspace_block_is_dropped_when_git_says_nothing() -> None:
    env = fixed_environment()
    without = environment.Environment(
        workdir=env.workdir, platform=env.platform, today=env.today, git=None
    )
    block = environment.build_environment_block(without)
    assert "Workspace" not in block
    assert "- Working directory: /work/project" in block


def test_a_remote_backend_suppresses_host_facts() -> None:
    env = environment.collect(
        "/srv/app", now=FIXED_CLOCK, read_git=False, context_files=[], backend="docker"
    )
    lines = environment.environment_lines(env)
    assert "- Backend: docker" in lines
    assert "Home:" not in lines
    assert "Platform:" not in lines


def test_context_files_are_read_and_capped(tmp_path: Path) -> None:
    """The first type that matches wins, and it is capped (CORE-context-files)."""
    (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
    (tmp_path / ".snowpea").mkdir()
    (tmp_path / ".snowpea" / "instructions.md").write_text("x" * 50, encoding="utf-8")
    project = environment.build_project_context(tmp_path, max_chars=10)
    # ``.snowpea/instructions.md`` outranks AGENTS.md, and only it is loaded.
    assert [item.name for item in project.files] == [".snowpea/instructions.md"]
    assert project.files[0].truncated is True

    block = environment.context_files_block(project)
    assert '<context file=".snowpea/instructions.md">' in block
    assert "truncated" in block
    assert environment.context_files_block([]) == ""


def test_context_files_land_in_the_context_tier() -> None:
    tiers = compose.build_tiers(
        environment="Environment\n- Working directory: /work",
        context_files=environment.context_files_block(
            [environment.ContextFile(name="AGENTS.md", text="TWO SPACES AFTER A FULL STOP")]
        ),
    )
    assert "TWO SPACES AFTER A FULL STOP" in tiers.context
    assert "TWO SPACES AFTER A FULL STOP" not in tiers.stable


# ---------------------------------------------------------------------------
# workflow briefs
# ---------------------------------------------------------------------------


def test_a_worker_brief_carries_the_rules_and_the_language() -> None:
    brief = compose.workflow_brief(
        "ralph-story",
        reply_language="ko",
        TASK="ship the parser",
        STORY_ID="S1",
        STORY_TITLE="tokenise",
        ACCEPTANCE="Acceptance criteria: it tokenises\n",
        VERIFY="It must make these commands exit zero: pytest -q\n",
    )
    assert brief.startswith(compose.BRIEF_RULES)
    assert "Reply in Korean" in brief
    assert "ship the parser" in brief
    assert "Story S1: tokenise" in brief
    assert "definition of done" in brief
    assert "${" not in brief


def test_json_workflows_carry_the_gap_11_rules() -> None:
    prd = compose.workflow_brief("ralph-prd", MIN_STORIES=1, MAX_STORIES=8)
    assert "A verify command must fail today and pass once the story is done." in prd
    assert "between 1 and 8 stories" in prd

    split = compose.workflow_brief("ultrawork-split", MAX_SUBTASKS=8)
    assert "Two subtasks that edit the same file are not independent." in split
    assert "disjoint set of files" in split
    assert "merged back into one before they run" in split

    plan = compose.workflow_brief("team-plan")
    assert "Two tasks that edit the same file are not independent." in plan


def test_the_conflict_brief_explains_why_and_says_to_re_read() -> None:
    brief = compose.workflow_brief("team-conflict", HUNKS="@@ -1 +1 @@")
    assert "another agent has since merged changes" in brief
    assert "Re-read the current contents" in brief
    assert "@@ -1 +1 @@" in brief


def test_every_workflow_prompt_renders_with_no_placeholders_left() -> None:
    names = sorted(
        path.stem for path in (loader.PACKAGE_DIR / "workflows").glob("*.md")
    )
    # the workflow briefs, init.md, skill-generate.md, the review trio,
    # and the six team-pipeline stage briefs (M6/M7 §9)
    assert len(names) in (21, 22)
    for name in names:
        text = compose.workflow_brief(
            name,
            TASK="t",
            STORY_ID="S1",
            STORY_TITLE="s",
            ACCEPTANCE="",
            VERIFY="",
            SUMMARY="-",
            APPROVAL_WORD="APPROVE",
            MIN_STORIES=1,
            MAX_STORIES=8,
            MAX_SUBTASKS=8,
            WORKER_N=1,
            WORKERS=2,
            TASK_ID="T1",
            TASK_TITLE="t",
            WORKTREE_PATH="/tmp/wt",
            BRANCH="team/1",
            HUNKS="",
            AGENTS_PATH="/tmp/AGENTS.md",
            AGENTS_STATUS="absent",
            SETTINGS_NOTE="",
            WORKDIR="/tmp/project",
            REPO="/tmp/project",
            FOCUS="",
            FILES="- a.py",
            DIFF="@@ -1 +1 @@",
            FINDINGS="- a.py:1 — nothing reads it",
            TASK_BRIEF="do the thing",
            HANDOFF="",
            ROUND_NOTE="",
            MAX_TASKS=8,
            TESTS="PASS",
        )
        assert "${" not in text, name
        assert text.strip(), name


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("vendor_class", VENDOR_CLASSES)
@pytest.mark.parametrize("role", ROLES)
def test_stable_tier_stays_under_budget(mode: str, vendor_class: str, role: str | None) -> None:
    tiers = compose.build_tiers(
        mode=mode, vendor_class=vendor_class, role=role, reply_language="ko"
    )
    assert tokens(tiers.stable) <= TIER_BUDGET_TOKENS["stable"]


def test_context_tier_stays_under_budget() -> None:
    tiers = compose.build_tiers(
        tools=TOOLS,
        environment=environment.build_environment_block(fixed_environment()),
        context_files=environment.context_files_block(
            [environment.ContextFile(name="AGENTS.md", text="project rules")]
        ),
    )
    assert tokens(tiers.context) <= TIER_BUDGET_TOKENS["context"]


def test_volatile_tier_stays_under_budget() -> None:
    tiers = compose.build_tiers(memory_block=MEMORY_BLOCK, context_fill=0.9)
    assert tokens(tiers.volatile) <= TIER_BUDGET_TOKENS["volatile"]


# ---------------------------------------------------------------------------
# golden snapshots
# ---------------------------------------------------------------------------


def golden_cases() -> list[tuple[str, str, bool, str | None]]:
    return [
        (mode, vendor, memory, role)
        for mode in MODES
        for vendor in VENDOR_CLASSES
        for memory in (False, True)
        for role in ROLES
    ]


def golden_name(mode: str, vendor: str, memory: bool, role: str | None) -> str:
    return f"{mode}__{vendor}__mem-{'on' if memory else 'off'}__role-{role or 'none'}.txt"


def golden_prompt(mode: str, vendor: str, memory: bool, role: str | None) -> str:
    return compose.build_system_prompt(
        mode=mode,
        vendor_class=vendor,
        role=role,
        tools=TOOLS,
        memory_block=MEMORY_BLOCK if memory else "",
        environment=environment.build_environment_block(fixed_environment()),
        context_files="",
        context_fill=0.2,
        reply_language="auto",
    )


@pytest.mark.parametrize(("mode", "vendor", "memory", "role"), golden_cases())
def test_golden_prompt(
    mode: str,
    vendor: str,
    memory: bool,
    role: str | None,
    update_golden: bool,
) -> None:
    path = GOLDEN_DIR / golden_name(mode, vendor, memory, role)
    text = golden_prompt(mode, vendor, memory, role)
    if update_golden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return
    assert path.is_file(), f"missing snapshot {path.name}; run with --update-golden"
    assert text == path.read_text(encoding="utf-8").rstrip("\n")


def test_no_stale_snapshots(update_golden: bool) -> None:
    expected = {golden_name(*case) for case in golden_cases()}
    found = {path.name for path in GOLDEN_DIR.glob("*.txt")}
    if update_golden:
        for name in found - expected:
            (GOLDEN_DIR / name).unlink()
        return
    assert found == expected
