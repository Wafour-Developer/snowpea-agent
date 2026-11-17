"""grep and glob on a file path, a missing path, .gitignore and lock files.

A DeepSeek session on a Java project (no ripgrep on the machine) got "no
matches" from grep with ``path`` set to a file that held the pattern, and its
first search returned 100 lines from ``package-lock.json``, ``yarn.lock`` and
``target/classes``.  Each test runs on the Python fallback and, when ``rg``
is installed, on ripgrep, and both must print the same paths.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from snowpea_core.exec.local import LocalBackend
from snowpea_core.session.session import Session
from snowpea_core.tools import glob as glob_tools
from snowpea_core.tools import grep as grep_tools
from snowpea_core.tools import ignore
from snowpea_core.tools.registry import ToolContext

BACKENDS = [
    "python",
    pytest.param(
        "rg",
        marks=pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed"),
    ),
]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


@pytest.fixture(params=BACKENDS)
def ctx(
    request: pytest.FixtureRequest, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> ToolContext:
    use_rg = request.param == "rg"

    async def _has(_ctx: ToolContext) -> bool:
        return use_rg

    monkeypatch.setattr(grep_tools, "has_ripgrep", _has)
    session = Session(id="s-grep", workdir=workdir)
    return ToolContext(session=session, core=None, backend=LocalBackend(workdir))  # type: ignore[arg-type]


async def grep(ctx: ToolContext, **args: object):
    return await grep_tools.grep(ctx, dict(args))


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def paths_of(output: str) -> set[str]:
    return {
        line.split(":", 1)[0]
        for line in output.splitlines()
        if not line.startswith(("[", "…", "no "))
    }


# ---------------------------------------------------------------------------
# path = a file, a directory, nothing
# ---------------------------------------------------------------------------


async def test_grep_on_a_file_finds_a_korean_pattern(ctx: ToolContext, workdir: Path) -> None:
    rel = "src/main/java/com/app/api/ApiController.java"
    write(
        workdir,
        rel,
        "class A {\n  /* @Method 설명 : 상품뽑기 */\n  @RequestMapping(value = { \"/x\" })\n}\n",
    )

    result = await grep(ctx, pattern="Method 설명", path=rel)
    assert result.ok, result.error
    assert result.output == f"{rel}:2:/* @Method 설명 : 상품뽑기 */"

    mapping = await grep(ctx, pattern=r"RequestMapping\(value", path=rel)
    assert mapping.ok and mapping.output.startswith(f"{rel}:3:")


async def test_grep_on_an_absolute_file_prints_it_relative(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, "pkg/a.py", "needle = 1\n")
    result = await grep(ctx, pattern="needle", path=str(workdir / "pkg" / "a.py"))
    assert result.ok, result.error
    assert result.output == "pkg/a.py:1:needle = 1"


async def test_grep_in_a_subdirectory_prints_workdir_relative_paths(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, "src/main/java/com/A.java", "// attendance\n")
    for path in ("src/main/java", "./src/main/java", "src/main/java/"):
        result = await grep(ctx, pattern="attendance", path=path)
        assert result.ok, result.error
        assert result.output == "src/main/java/com/A.java:1:// attendance", path


async def test_grep_on_the_workdir_has_no_dot_slash(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, "a.txt", "hello\n")
    result = await grep(ctx, pattern="hello")
    assert result.ok and result.output == "a.txt:1:hello"


async def test_grep_on_a_missing_path_is_an_error(ctx: ToolContext) -> None:
    result = await grep(ctx, pattern="x", path="src/nope.java")
    assert not result.ok
    assert result.error == "no such file or directory: src/nope.java"


async def test_grep_on_a_file_without_a_match_says_no_matches(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, "a.txt", "hello\n")
    result = await grep(ctx, pattern="absent", path="a.txt")
    assert result.ok and result.output == "no matches for absent"


# ---------------------------------------------------------------------------
# .gitignore, build output, lock files
# ---------------------------------------------------------------------------


async def test_gitignore_hides_ignored_dirs_and_honours_negation(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, ".gitignore", "target/\n*.log\n/secret.txt\n!keep.log\n")
    write(workdir, "src/A.java", "attendance\n")
    write(workdir, "target/classes/A.xml", "attendance\n")
    write(workdir, "app.log", "attendance\n")
    write(workdir, "keep.log", "attendance\n")
    write(workdir, "secret.txt", "attendance\n")
    write(workdir, "docs/secret.txt", "attendance\n")  # /secret.txt is anchored

    result = await grep(ctx, pattern="attendance")
    assert result.ok, result.error
    assert paths_of(result.output) == {"src/A.java", "keep.log", "docs/secret.txt"}


async def test_nested_gitignore_applies_below_its_directory(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, ".gitignore", "*.gen\n")
    write(workdir, "web/.gitignore", "dist-web/\n!special.gen\n")
    write(workdir, "web/dist-web/bundle.js", "token\n")
    write(workdir, "web/src/app.js", "token\n")
    write(workdir, "web/special.gen", "token\n")
    write(workdir, "other.gen", "token\n")
    write(workdir, "dist-web/kept.js", "token\n")  # the nested rule stays in web/

    result = await grep(ctx, pattern="token")
    assert result.ok, result.error
    assert paths_of(result.output) == {"web/src/app.js", "web/special.gen", "dist-web/kept.js"}


async def test_gitignore_double_star(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, ".gitignore", "**/generated/**\nlogs/**/*.txt\n")
    write(workdir, "a/generated/x.py", "token\n")
    write(workdir, "a/b.py", "token\n")
    write(workdir, "logs/2026/one.txt", "token\n")
    write(workdir, "logs/keep.md", "token\n")
    result = await grep(ctx, pattern="token")
    assert paths_of(result.output) == {"a/b.py", "logs/keep.md"}


async def test_a_gitignore_above_the_searched_dir_applies(
    ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, ".gitignore", "*.class\n")
    write(workdir, "src/A.java", "token\n")
    write(workdir, "src/A.class", "token\n")
    result = await grep(ctx, pattern="token", path="src")
    assert paths_of(result.output) == {"src/A.java"}


async def test_naming_an_ignored_dir_searches_it(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, ".gitignore", "target/\n")
    write(workdir, "target/classes/A.xml", "token\n")
    result = await grep(ctx, pattern="token", path="target")
    assert paths_of(result.output) == {"target/classes/A.xml"}


async def test_lock_files_are_skipped_unless_named(ctx: ToolContext, workdir: Path) -> None:
    write(workdir, "cashpp/package-lock.json", '"lodash.flattendeep": {}\n')
    write(workdir, "cashpp/yarn.lock", "lodash.flattendeep@^4.4.0:\n")
    write(workdir, "Cargo.lock", "flattendeep\n")
    write(workdir, "cashpp/src/App.js", "// flattendeep\n")

    result = await grep(ctx, pattern="flattendeep")
    assert result.ok, result.error
    assert paths_of(result.output) == {"cashpp/src/App.js"}

    by_glob = await grep(ctx, pattern="flattendeep", glob="*.lock")
    assert paths_of(by_glob.output) == {"cashpp/yarn.lock", "Cargo.lock"}

    by_name = await grep(ctx, pattern="flattendeep", glob="package-lock.json")
    assert paths_of(by_name.output) == {"cashpp/package-lock.json"}

    by_path = await grep(ctx, pattern="flattendeep", path="cashpp/yarn.lock")
    assert by_path.output == "cashpp/yarn.lock:1:lodash.flattendeep@^4.4.0:"

    json_only = await grep(ctx, pattern="flattendeep", glob="*.json")
    assert json_only.output.startswith("no matches")


# ---------------------------------------------------------------------------
# Python fallback only: build output without .gitignore, the skipped line
# ---------------------------------------------------------------------------


@pytest.fixture
def py_ctx(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    async def _no(_ctx: ToolContext) -> bool:
        return False

    monkeypatch.setattr(grep_tools, "has_ripgrep", _no)
    return ToolContext(
        session=Session(id="s-grep", workdir=workdir), core=None, backend=LocalBackend(workdir)  # type: ignore[arg-type]
    )


async def test_python_fallback_states_what_it_skipped(py_ctx: ToolContext, workdir: Path) -> None:
    write(workdir, ".gitignore", "target/\n*.tmp\n")
    write(workdir, "target/classes/a.xml", "token\n")
    write(workdir, "x.tmp", "token\n")
    write(workdir, "yarn.lock", "token\n")
    write(workdir, "src/A.java", "token\n")
    result = await grep_tools.grep(py_ctx, {"pattern": "token"})
    assert result.ok
    lines = result.output.splitlines()
    assert lines[0] == "src/A.java:1:token"
    assert lines[-1] == (
        "[skipped 2 paths ignored by .gitignore, 1 lock files; name one in path "
        "(or a lock file in glob) to search it]"
    )

    nothing = await grep_tools.grep(py_ctx, {"pattern": "absent"})
    assert nothing.output.splitlines()[0] == "no matches for absent"
    assert nothing.output.splitlines()[1].startswith("[skipped 2 paths")


async def test_without_gitignore_maven_target_is_pruned_but_a_source_target_is_not(
    py_ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, "pom.xml", "<project/>\n")
    write(workdir, "target/classes/mappers/api.xml", "attendance\n")
    write(workdir, "src/main/resources/mappers/api.xml", "attendance\n")
    write(workdir, "tools/target/run.py", "attendance\n")  # no manifest beside it
    result = await grep_tools.grep(py_ctx, {"pattern": "attendance"})
    assert paths_of(result.output) == {
        "src/main/resources/mappers/api.xml",
        "tools/target/run.py",
    }
    assert "1 build output dirs" in result.output


async def test_with_gitignore_the_build_output_heuristic_is_off(
    py_ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, ".gitignore", "*.log\n")
    write(workdir, "pom.xml", "<project/>\n")
    write(workdir, "target/gen/A.java", "token\n")  # not ignored by this .gitignore
    result = await grep_tools.grep(py_ctx, {"pattern": "token"})
    assert paths_of(result.output) == {"target/gen/A.java"}


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


@pytest.fixture
def glob_ctx(workdir: Path) -> ToolContext:
    return ToolContext(
        session=Session(id="s-glob", workdir=workdir), core=None, backend=LocalBackend(workdir)  # type: ignore[arg-type]
    )


async def test_glob_missing_path_and_file_path(glob_ctx: ToolContext, workdir: Path) -> None:
    write(workdir, "src/A.java", "x\n")
    missing = await glob_tools.glob_tool(glob_ctx, {"pattern": "*.java", "path": "nope"})
    assert not missing.ok and missing.error == "no such file or directory: nope"

    hit = await glob_tools.glob_tool(glob_ctx, {"pattern": "*.java", "path": "src/A.java"})
    assert hit.ok and hit.output == "src/A.java"

    miss = await glob_tools.glob_tool(glob_ctx, {"pattern": "*.py", "path": "src/A.java"})
    assert miss.ok and "does not match" in miss.output


async def test_glob_under_a_subdir_prints_workdir_relative_paths_and_skips(
    glob_ctx: ToolContext, workdir: Path
) -> None:
    write(workdir, ".gitignore", "generated/\n")
    write(workdir, "src/a/B.java", "x\n")
    write(workdir, "src/generated/C.java", "x\n")
    result = await glob_tools.glob_tool(glob_ctx, {"pattern": "**/*.java", "path": "src"})
    assert result.ok
    assert result.output.splitlines() == [
        "src/a/B.java",
        "[skipped 1 paths ignored by .gitignore; name one in path (or a lock file in glob) "
        "to search it]",
    ]


# ---------------------------------------------------------------------------
# the matcher on its own
# ---------------------------------------------------------------------------


def test_ignore_matcher_rules() -> None:
    rules = ignore.parse("# comment\n\nbuild/\n*.o\n!keep.o\n/root.txt\ndocs/*.md\n\\#hash\n", "")
    assert ignore.ignored(rules, "build", is_dir=True)
    assert not ignore.ignored(rules, "build", is_dir=False)  # dir-only rule
    assert ignore.ignored(rules, "a/b/x.o", is_dir=False)
    assert not ignore.ignored(rules, "a/keep.o", is_dir=False)
    assert ignore.ignored(rules, "root.txt", is_dir=False)
    assert not ignore.ignored(rules, "a/root.txt", is_dir=False)
    assert ignore.ignored(rules, "docs/a.md", is_dir=False)
    assert not ignore.ignored(rules, "docs/sub/a.md", is_dir=False)
    assert ignore.ignored(rules, "#hash", is_dir=False)

    nested = ignore.parse("*.tmp\n", "web")
    assert ignore.ignored(nested, "web/a/x.tmp", is_dir=False)
    assert not ignore.ignored(nested, "x.tmp", is_dir=False)
    assert not ignore.ignored(nested, "webby/x.tmp", is_dir=False)


def test_lock_file_names() -> None:
    for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
                 "poetry.lock", "a/b/Gemfile.lock", "uv.lock"):
        assert ignore.is_lock_file(name), name
    for name in ("package.json", "lock.py", "locks/a.txt"):
        assert not ignore.is_lock_file(name), name
