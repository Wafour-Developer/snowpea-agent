"""m21 (less asking), step 2: snowpea's own CLI, read only.

A vetted read-only run of the ``snowpea`` program (``--help``, ``--version``,
``session list``, ``daemon status``, ``agents``, the ``project list`` /
``sessions`` usage errors) is judged like any read-only command; every other
run stays a control action (asked in every mode, never by Jarvis).

Every command line below is DATA handed to the pure checker; nothing runs.  The
"person" lives in a temporary home (``tmp/u``); the owner's real lines are
anonymised to it.  Each row: ``(line, expected)`` with ``a`` allowed (no
control action, nothing unseen), ``c`` a control action (``acts on snowpea's
own controls``), ``o`` asked for another reason.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from snowpea_core.security import code as code_mod
from snowpea_core.security import shell as sh
from snowpea_core.security import written
from snowpea_core.security.check import CONTROL_REASON, Context, check_command
from snowpea_core.security.lists import from_raw

DEFAULT = from_raw({})
EXPORT = 'export PATH="$HOME/.local/bin:$PATH"; '


class World:
    def __init__(self, root: Path) -> None:
        self.user = root / "u"
        self.home = self.user / ".snowpea"
        self.project = self.user / "work" / "proj"
        self.bin = self.user / ".local" / "bin"
        self.venv = self.user / ".local" / "share" / "uv" / "tools" / "snowpea-agent" / "bin"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    written.reset()
    found = World(tmp_path)
    for folder in (found.home / "sessions", found.project, found.bin, found.venv):
        folder.mkdir(parents=True, exist_ok=True)
    entry = found.venv / "snowpea"
    entry.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    entry.chmod(0o755)
    (found.bin / "snowpea").symlink_to(entry)
    planted = tmp_path / "evil"
    planted.mkdir()
    (planted / "snowpea").write_text("#!/bin/sh\n", encoding="utf-8")
    (planted / "snowpea").chmod(0o755)
    monkeypatch.setattr(code_mod, "RECENT_SEC", -1.0)
    monkeypatch.setenv("PATH", f"/usr/local/bin:/usr/bin:/bin:{found.bin}")
    monkeypatch.setenv("SNOWPEA_HOME", str(found.home))
    monkeypatch.setattr(sh, "cli_entry_points",
                        lambda: frozenset({os.path.realpath(entry)}))
    # tmp_path is a temporary folder itself: name another one as "temporary".
    monkeypatch.setattr(sh, "temporary_dirs", lambda: {tmp_path / "evil"})
    return found


def _ctx(world: World, **changes: object) -> Context:
    values: dict[str, object] = {
        "workdir": world.project, "user_home": world.user, "home": world.home,
        "projects": (str(world.project),), "mounts": (), "env_venv": False,
    }
    values.update(changes)
    return Context(**values)  # type: ignore[arg-type]


def _state(line: str, world: World, **changes: object) -> str:
    verdict = check_command(line, DEFAULT, _ctx(world, **changes), cwd=world.project)
    if verdict.blocked:
        return "b"
    if verdict.opaque:
        return "c" if verdict.reason == CONTROL_REASON else "o"
    return "a"


# -- the owner's lines of 2026-11-17 (anonymised): asked before, read-only now -----------
OWNER_READS = [
    'snowpea --help 2>&1 | head -50; echo "=== project help ==="; '
    "snowpea project --help 2>&1 | head -50",
    "snowpea project list 2>&1 | head -80",
    EXPORT + 'snowpea --help 2>&1 | head -60; echo "=== project --help ==="; '
    "snowpea project --help 2>&1 | head -60",
    EXPORT + "snowpea session --help 2>&1 | head -40",
    EXPORT + "snowpea session list --include-closed 2>&1 | head -100",
    EXPORT + "snowpea session clear --help 2>&1; echo ===; snowpea session delete --help 2>&1",
    EXPORT + 'snowpea session list --include-closed 2>&1 | grep -n "s-9a531c4d5982"; '
    'echo "exit=$?"',
    EXPORT + "snowpea daemon --help 2>&1 | head -20; echo ===; snowpea daemon status 2>&1 | "
    "head -10",
    EXPORT + "snowpea session list 2>&1",
    EXPORT + "snowpea agents 2>&1 | head -30; echo ===; snowpea jarvis --help 2>&1 | head -15",
    "snowpea project list 2>&1",
    "snowpea session list 2>&1; snowpea --help 2>&1 | head -40",
    "~/.local/bin/snowpea project list 2>&1",
    "snowpea session list 2>&1 | head -50",
    'snowpea project list 2>&1; echo "---SESSIONS---"; snowpea session list 2>&1 || '
    "snowpea sessions 2>&1",
    'snowpea project list; echo "---"; snowpea session list',
    EXPORT + "snowpea session list --json 2>&1 | python3 -c \"\nimport json,sys\n"
    "d=json.load(sys.stdin)\nitems = d if isinstance(d,list) else d.get('sessions',d)\n"
    "for s in items:\n    wid=(s.get('workdir') or s.get('cwd') or '')\n"
    "    print(s.get('id'), s.get('status'), wid)\n\" | head -40",
    "ls ~/.local/bin/snowpea ~/.npm-global/bin/snowpea 2>/dev/null; which -a snowpea "
    "2>/dev/null; echo ---; ls ~/.snowpea 2>/dev/null",
    "snowpea --version",
    "snowpea -V",
]

# -- the owner's lines that still act on snowpea's controls -------------------------------
OWNER_CONTROL = [
    EXPORT + "snowpea --home ~/.snowpea-deploy session list 2>&1 | head -60",
    EXPORT + "snowpea --home ~/.snowpea-deploy session list --include-closed 2>&1 | "
    'grep -E "9a531c|project" | head -20',
]


@pytest.mark.parametrize("line", OWNER_READS)
def test_owner_read_only_runs_are_ordinary_reads(line: str, world: World) -> None:
    assert _state(line, world) == "a", line


@pytest.mark.parametrize("line", OWNER_CONTROL)
def test_owner_home_override_stays_a_control_action(line: str, world: World) -> None:
    assert _state(line, world) == "c", line


# -- hostile: every one stays a control action (or worse) ---------------------------------
HOSTILE = [
    ("snowpea project trust --yes .", "c"),
    ("snowpea project list; snowpea project trust .", "c"),
    ("snowpea project list && snowpea project untrust .", "c"),
    ("snowpea --home /tmp/x project list", "c"),
    ("snowpea --home=/tmp/x session list", "c"),
    ("SNOWPEA_HOME=/tmp/x snowpea project list", "c"),
    ("env SNOWPEA_HOME=/tmp/x snowpea session list", "c"),
    ("export SNOWPEA_HOME=/tmp/x; snowpea session list", "c"),
    ("HOME=/tmp/h snowpea session list", "c"),
    ("snowpea session list --workdir /tmp", "c"),
    ("snowpea session delete s-1", "c"),
    ("snowpea session clear --all", "c"),
    ("snowpea daemon stop", "c"),
    ("snowpea daemon start", "c"),
    ("snowpea web token --rotate", "c"),
    ("snowpea web", "c"),
    ("snowpea", "c"),
    ("snowpea -c 'delete everything'", "c"),
    ("snowpea --json session list", "c"),
    ("snowpea session list --json --rotate", "c"),
    ("snowpea session list --json --json", "c"),
    ("snowpea project list --scope hooks", "c"),
    ("snowpea agents run x", "c"),
    ("snowpea remote up --help", "c"),
    ("snowpea remote --help", "c"),
    ("snowpea session list $X", "c"),
    ("snowpea-core project list", "c"),
    ("python3 -m snowpea_core project list", "c"),
    ("PATH=./bin:$PATH snowpea project list", "c"),
    ("export PATH=bin:$PATH; snowpea session list", "c"),
    ("snowpea(){ echo hi; }; snowpea project list", "c"),
    ("function snowpea { echo hi; }; snowpea session list", "c"),
    ("alias snowpea='snowpea project trust'; snowpea project list", "c"),
    ("command snowpea project trust .", "c"),
    ("timeout 5 snowpea project trust .", "c"),
    ("bash -c 'snowpea project list'", "c"),
    ("echo snowpea project trust . | sh", "o"),
    ("LD_PRELOAD=/tmp/x.so snowpea project list", "c"),
    ("PYTHONPATH=/tmp/evil snowpea project list", "c"),
]


@pytest.mark.parametrize(("line", "want"), HOSTILE)
def test_hostile_cli_runs_still_ask(line: str, want: str, world: World) -> None:
    assert _state(line, world) == want, line


def test_planted_program_is_not_the_installed_one(world: World, tmp_path: Path) -> None:
    planted = tmp_path / "evil" / "snowpea"
    assert _state(f"{planted} project list", world) == "c"
    assert _state(f"export PATH={tmp_path / 'evil'}:$PATH; snowpea --help", world) == "c"


def test_a_temporary_folder_on_path_is_never_followed(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evil = tmp_path / "evil"
    monkeypatch.setattr(sh, "temporary_dirs", lambda: {tmp_path})
    assert _state(f"export PATH={evil}:$PATH; snowpea project list", world) == "c"
    assert _state(f"export PATH=$PATH:{evil}; snowpea --help", world) == "c"


def test_read_only_runs_are_still_read_only_after_a_wrapper(world: World) -> None:
    assert _state("timeout 5 snowpea project list", world) == "a"
    assert _state("command snowpea session list", world) == "a"


def test_another_daemon_home_keeps_daemon_runs_controlled(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The CLI's own home is not the daemon judging the line: it could start another.
    monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path / "other-home"))
    assert _state("snowpea session list", world) == "c"
    assert _state("snowpea daemon status", world) == "c"
    # Help and the version never reach a daemon.
    assert _state("snowpea --help", world) == "a"
    assert _state("snowpea --version", world) == "a"


def test_path_prepend_of_an_agent_written_folder_is_not_followed(
        world: World) -> None:
    written.note([str(world.bin / "head")], "s-1")
    assert _state(EXPORT + "snowpea session list 2>&1 | head", world) != "a"


def test_path_prepend_that_changes_another_program_stays_unseen(
        world: World) -> None:
    other = world.bin / "head"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    # ``head`` would now be the person's ~/.local/bin/head, not /usr/bin/head.
    if os.path.isfile("/usr/bin/head") or os.path.isfile("/bin/head"):
        assert _state(EXPORT + "snowpea session list | head", world) != "a"


def test_path_prepend_with_a_write_into_it_stays_unseen(world: World) -> None:
    assert _state(EXPORT + "cp x ~/.local/bin/ls; snowpea --help", world) != "a"


def test_cli_read_only_shapes() -> None:
    assert sh.cli_read_only(["--help"])
    assert sh.cli_read_only(["session", "list", "--include-closed", "--json"])
    assert not sh.cli_read_only([])
    assert not sh.cli_read_only(["session", "list", "--workdir", "x"])
    assert not sh.cli_read_only(["session", "delete", "x"])
    assert not sh.cli_read_only(["a", "b", "c", "--help"])
    assert not sh.cli_read_only(["--home", "x", "--help"])
    assert not sh.cli_read_only(["remote", "up", "--help"])


def test_vetted_shapes_match_the_cli_parser() -> None:
    """Every vetted shape parses (or fails to parse) without running anything."""
    from snowpea_core.cli.main import parse_argv

    for shape, options in sh.CLI_READS.items():
        argv = [*shape, *sorted(options)]
        try:
            args = parse_argv(argv)
        except SystemExit as exc:  # a usage error: nothing ran
            assert exc.code == 2, argv
            assert shape in (("project", "list"), ("sessions",)), argv
            continue
        assert args.subcommand == shape[0], argv
        assert getattr(args, "home", None) is None
        assert getattr(args, "prompt", None) is None
