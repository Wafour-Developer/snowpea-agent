"""m32 (owner decision 2026-11-20): scratch folders are free.

``$SNOWPEA_HOME/tmp``, each session's workspace (``$SNOWPEA_HOME/sessions/<day>_<id>/``)
and the system temp folders (``/tmp``, ``/var/tmp``, ``$TMPDIR`` …) are scratch
space: writing, deleting and running files there is no protected-path block, no
control-plane ask and no outside-the-project ask, with the checks on or off, and
in a session in auto mode not even an unknown-code ask.  Symlinks, ``..`` and a
move of a protected file into scratch stay protected, and so does the rest of
the home.

Every command line and program below is DATA handed to the pure checker; nothing
here runs it.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.security import catalog, control_plane, enforce, written
from snowpea_core.security.check import Context, Verdict, check_call, check_command
from snowpea_core.security.lists import Lists, from_raw
from snowpea_core.session.session import Session
from snowpea_core.tools.config_guard import outside_paths, permission_for_write

WORKSPACE = ("sessions", "2026-11-20_abc123")


@pytest.fixture
def lay(tmp_path: Path) -> SimpleNamespace:
    written.reset()
    user = tmp_path / "u"
    home = user / ".snowpea"
    proj = user / "proj"
    for folder in (home / "tmp", home.joinpath(*WORKSPACE, "tmp"), home / "skills", proj):
        folder.mkdir(parents=True)
    (home / "settings.json").write_text("{}\n", encoding="utf-8")
    (user / ".bashrc").write_text("# rc\n", encoding="utf-8")
    # A folder of the system's temp space (pytest's own tmp lies in /tmp too).
    scratch = Path(os.path.realpath("/tmp")) / f"m32-{os.getpid()}-{tmp_path.name}"
    return SimpleNamespace(user=user, home=home, proj=proj, tmp_path=tmp_path,
                           ws=home.joinpath(*WORKSPACE), scratch=scratch)


def _ctx(lay: SimpleNamespace, *, on: bool) -> Context:
    # What enforce.context builds: checks on -> unknownCode "auto" (the default);
    # checks off -> "allow".
    return Context(workdir=lay.proj, user_home=lay.user, home=lay.home,
                   projects=(str(lay.proj),), mounts=(), env_venv=False,
                   unknown_code="auto" if on else "allow", checks_off=not on)


def _lists(on: bool) -> Lists:
    # enforce.lists_for: the defaults while on, nothing at all while off.
    return from_raw({}) if on else Lists()


def _line(lay: SimpleNamespace, line: str, *, on: bool) -> Verdict:
    return check_command(line, _lists(on), _ctx(lay, on=on), cwd=lay.proj)


def _call(lay: SimpleNamespace, tool: str, args: dict, tags: tuple[str, ...], *,
          on: bool) -> Verdict:
    return check_call(tool, args, _lists(on), _ctx(lay, on=on), tags=tags)


def _tag(lay: SimpleNamespace, path: str) -> str:
    session = Session(id="s-m32", workdir=lay.proj)
    return permission_for_write({"path": path}, session,
                                SimpleNamespace(paths=SimpleNamespace(home=lay.home)))


def _free(verdict: Verdict, mode: str = "auto") -> bool:
    return not verdict.blocked and not verdict.asks_in(mode)


BOTH = pytest.mark.parametrize("on", [True, False], ids=["checks-on", "checks-off"])


# -- the folders ------------------------------------------------------------------------


def test_scratch_names_exactly_the_scratch_folders(lay: SimpleNamespace) -> None:
    home = lay.home
    assert control_plane.scratch(home / "tmp" / "a" / "b.py", home=home)
    assert control_plane.scratch(home / "tmp", home=home)
    assert control_plane.scratch(lay.ws / "tmp" / "x.py", home=home)
    assert control_plane.scratch(lay.ws / "notes.md", home=home)
    assert control_plane.scratch("/tmp/x.sh", home=home)
    assert control_plane.scratch("/var/tmp/x.sh", home=home)
    # The rest of the home is not scratch, though the home itself lies in /tmp here.
    for rel in ("settings.json", "token", "state.db", "logs/daemon.log", "skills/x/SKILL.md",
                "sessions", "sessions/notes.md", "security/switch.json", "plans/p.md"):
        assert not control_plane.scratch(home / rel, home=home), rel
    assert not control_plane.scratch(home / "tmp" / ".." / "settings.json", home=home)
    assert not control_plane.scratch("relative/x", home=home)


def test_temp_dirs_hold_the_system_folders_and_never_the_home(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/private/var/folders/ab/cd/T")
    monkeypatch.setenv("TEMP", os.path.expanduser("~"))
    monkeypatch.setenv("TMP", "/")
    dirs = control_plane.temp_dirs()
    for want in ("/tmp", "/var/tmp", "/private/tmp", "/private/var/folders/ab/cd/T"):
        assert Path(want) in dirs
    assert Path("/") not in dirs
    assert Path(os.path.expanduser("~")) not in dirs


# -- writes, edits, deletes: no block, no ask ---------------------------------------------


@BOTH
def test_shell_writes_in_scratch_go_through(lay: SimpleNamespace, on: bool) -> None:
    H, W = lay.home, lay.ws
    for line in (f"echo hi > {H}/tmp/a.txt", f"rm -f {H}/tmp/a.txt", f"rm -rf {H}/tmp/build",
                 f"mkdir -p {H}/tmp/a/b && cp README {H}/tmp/a/b/",
                 f"echo hi > {W}/tmp/a.txt", f"echo hi > {W}/artifacts/report.md",
                 "echo hi > /tmp/m32-a.txt", "rm -f /var/tmp/m32-a.txt",
                 f"mv {H}/tmp/a.txt {H}/tmp/b.txt"):
        verdict = _line(lay, line, on=on)
        assert _free(verdict), (line, verdict)
        assert _free(verdict, "accept"), (line, verdict)


@BOTH
def test_file_tools_in_scratch_go_through(lay: SimpleNamespace, on: bool) -> None:
    H = lay.home
    for path in (f"{H}/tmp/a.py", f"{lay.ws}/tmp/b.py", "/tmp/m32-c.py"):
        for tool in ("write_file", "edit_file"):
            verdict = _call(lay, tool, {"path": path, "content": "x"}, ("write",), on=on)
            assert _free(verdict), (tool, path, verdict)
        verdict = _call(lay, "delete_file", {"path": path}, ("write",), on=on)
        assert _free(verdict), (path, verdict)


def test_scratch_writes_are_not_config_and_not_outside(lay: SimpleNamespace) -> None:
    session = Session(id="s-m32", workdir=lay.proj)
    core = SimpleNamespace(paths=SimpleNamespace(home=lay.home))

    def tag(path: str) -> str:
        return permission_for_write({"path": path}, session, core)

    assert tag(str(lay.home / "tmp" / "run.py")) == "write"
    assert tag(str(lay.ws / "tmp" / "run.py")) == "write"
    assert tag("/tmp/m32-run.py") == "write"
    assert tag("/var/tmp/m32-run.py") == "write"
    assert outside_paths({"path": "/tmp/m32-run.py"}, session, lay.home) == []
    # The rest of the home stays configuration.
    assert tag(str(lay.home / "settings.json")) == "config"
    assert tag(str(lay.home / "tmp" / ".." / "settings.json")) == "config"
    assert tag(str(lay.home / "skills" / "x" / "SKILL.md")) == "config"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_temp_link_out_of_scratch_is_still_outside(lay: SimpleNamespace) -> None:
    session = Session(id="s-m32", workdir=lay.proj)
    lay.scratch.mkdir()
    try:
        link = lay.scratch / "out"
        link.symlink_to("/opt/snowpea-m32-not-there")
        assert outside_paths({"path": str(link / "x")}, session, lay.home) == [
            "/opt/snowpea-m32-not-there/x"]
    finally:
        (lay.scratch / "out").unlink()
        lay.scratch.rmdir()


# -- running files from scratch (the owner's case: checks on, auto mode) ------------------


@BOTH
def test_writing_then_running_a_script_in_scratch_does_not_ask_in_auto(
        lay: SimpleNamespace, on: bool) -> None:
    H = lay.home
    lines = (
        "cat > /tmp/m32-x.sh <<'EOF'\necho hi\nEOF\nsh /tmp/m32-x.sh",
        "cat > /tmp/m32-x.sh && sh /tmp/m32-x.sh",
        "printf 'echo hi\\n' > /tmp/m32-x.sh && sh /tmp/m32-x.sh",
        "printf 'echo hi\\n' > /tmp/m32-x.sh && chmod +x /tmp/m32-x.sh && /tmp/m32-x.sh",
        "printf 'print(1)\\n' > /var/tmp/m32-x.py && python3 /var/tmp/m32-x.py",
        f"printf 'echo hi\\n' > {H}/tmp/x.sh && sh {H}/tmp/x.sh",
        f"printf 'print(1)\\n' > {lay.ws}/tmp/x.py && python3 {lay.ws}/tmp/x.py",
        "python3 /tmp/m32-not-written-yet.py",
    )
    for line in lines:
        verdict = _line(lay, line, on=on)
        assert _free(verdict), (line, verdict)


@BOTH
def test_execute_code_writing_and_running_in_scratch_does_not_ask_in_auto(
        lay: SimpleNamespace, on: bool) -> None:
    H = lay.home
    programs = (
        f"p = '{H}/tmp/run.py'\nopen(p, 'w').write('print(1)')\n"
        "import subprocess\nsubprocess.run(['python3', p], check=True)\n",
        "open('/tmp/m32-run.py', 'w').write('print(1)')\n"
        "import subprocess\nsubprocess.run(['python3', '/tmp/m32-run.py'])\n",
        f"import os\nos.remove('{H}/tmp/run.py')\n",
    )
    for code in programs:
        verdict = _call(lay, "execute_code", {"code": code}, ("exec",), on=on)
        assert _free(verdict), (code, verdict)


def test_mode_rules_are_unchanged(lay: SimpleNamespace) -> None:
    # accept asks for exec through the mode matrix, not through this check; the
    # check itself still only relaxes unread code in auto mode (unknownCode auto).
    from snowpea_core.permissions.policy import MODE_MATRIX

    assert MODE_MATRIX["accept"]["exec"] == "ask"
    assert MODE_MATRIX["plan"]["write"] == "deny"
    verdict = _line(lay, "printf 'echo hi' > /tmp/m32-x.sh && sh /tmp/m32-x.sh", on=True)
    assert verdict.asks_in("accept") and not verdict.asks_in("auto")


def test_a_scratch_script_whose_text_holds_a_blocked_command_is_not_let_through(
        lay: SimpleNamespace) -> None:
    # The raw text, including what the line writes into the script, is judged.
    payload = "printf 'rm -rf ~' > /tmp/m32-x.sh && sh /tmp/m32-x.sh"
    assert not _free(_line(lay, payload, on=True))
    payload = "cat > /tmp/m32-x.sh <<'EOF'\nrm -rf ~\nEOF\nsh /tmp/m32-x.sh"
    assert not _free(_line(lay, payload, on=True))
    payload = f"printf 'echo x > {lay.home}/settings.json' > /tmp/m32-x.sh && sh /tmp/m32-x.sh"
    assert not _free(_line(lay, payload, on=True))
    payload = (f"printf 'echo hi' > {lay.home}/tmp/x.sh && cd {lay.home}/tmp && cd .. "
               f"&& sh {lay.home}/tmp/x.sh")
    assert not _free(_line(lay, payload, on=True))


def test_a_script_written_outside_scratch_still_asks(lay: SimpleNamespace,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest's tmp (the project here) lies in /tmp: take the temp folders away.
    monkeypatch.setattr(control_plane, "temp_dirs", lambda: ())
    verdict = _line(lay, f"printf 'echo hi' > {lay.proj}/x.sh && sh {lay.proj}/x.sh", on=True)
    assert "script-written" in verdict.unseen and verdict.asks_in("auto")


# -- escapes stay protected ---------------------------------------------------------------


@BOTH
def test_dot_dot_out_of_scratch_is_protected(lay: SimpleNamespace, on: bool) -> None:
    H = lay.home
    for line in (f"echo x > {H}/tmp/../settings.json",
                 f"echo x > {lay.ws}/../../settings.json",
                 f"rm -f {H}/tmp/../token"):
        assert _line(lay, line, on=on).blocked, line
    # A file tool's write to settings.json is judged by its tag: ``config`` asks.
    assert _tag(lay, f"{H}/tmp/../settings.json") == "config"


@BOTH
@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_link_in_scratch_to_a_protected_file_is_that_file(lay: SimpleNamespace,
                                                           on: bool) -> None:
    H = lay.home
    (H / "tmp" / "s").symlink_to(H / "settings.json")
    (H / "tmp" / "home").symlink_to(H)
    for line in (f"echo x > {H}/tmp/s", f"echo x > {H}/tmp/home/settings.json",
                 f"rm -f {H}/tmp/home/state.db"):
        assert _line(lay, line, on=on).blocked, line
    assert _tag(lay, f"{H}/tmp/s") == "config"
    assert _tag(lay, f"{H}/tmp/home/state.db") == "config"
    assert not control_plane.scratch(H / "tmp" / "s", home=H)


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_temp_link_to_a_protected_path_stays_protected(lay: SimpleNamespace) -> None:
    lay.scratch.mkdir()
    try:
        (lay.scratch / "rc").symlink_to(lay.user / ".bashrc")
        verdict = _line(lay, f"echo x >> {lay.scratch}/rc", on=True)
        assert verdict.blocked
    finally:
        (lay.scratch / "rc").unlink()
        lay.scratch.rmdir()


def test_a_new_link_in_scratch_still_asks(lay: SimpleNamespace) -> None:
    H = lay.home
    verdict = _line(lay, f"ln -s {H}/settings.json {H}/tmp/s2 && echo x > {H}/tmp/s2", on=True)
    assert not _free(verdict)


@BOTH
def test_moving_a_protected_file_into_scratch_is_still_a_write_to_it(lay: SimpleNamespace,
                                                                       on: bool) -> None:
    H = lay.home
    for line in (f"mv {H}/settings.json {H}/tmp/s.json", f"mv {H}/token /tmp/m32-t",
                 f"mv {H}/skills {H}/tmp/", f"mv {H}/state.db {lay.ws}/tmp/"):
        assert not _free(_line(lay, line, on=on)), line


def test_token_reads_keep_their_rules(lay: SimpleNamespace) -> None:
    H = lay.home
    for line in (f"cat {H}/token", f"cp {H}/token {H}/tmp/t", f"cp {H}/token /tmp/m32-t"):
        assert _line(lay, line, on=True).blocked, line
        # Checks off: a read goes through the mode rules (1.66.0), as before.
        assert not _line(lay, line, on=False).secret_read, line


@BOTH
def test_the_rest_of_the_home_stays_protected(lay: SimpleNamespace, on: bool) -> None:
    H = lay.home
    for line in (f"echo x > {H}/settings.json", f"echo x > {H}/state.db",
                 f"echo x > {H}/security/switch.json", f"echo x > {H}/logs/approvals.jsonl",
                 f"echo x > {H}/tmpfile",
                 f"echo x > {H}/skills/x/SKILL.md"):
        assert not _free(_line(lay, line, on=on)), line


# -- the presets ----------------------------------------------------------------------------


def test_the_snowpea_home_preset_leaves_scratch_out(lay: SimpleNamespace) -> None:
    lists = from_raw({"protectedPaths": {"presets": ["essential", "snowpea-home"]}})
    ctx = _ctx(lay, on=True)
    H = lay.home
    for line in (f"echo x > {H}/tmp/a", f"echo x > {lay.ws}/tmp/a", f"echo x > {H}/plans/p.md"):
        assert _free(check_command(line, lists, ctx, cwd=lay.proj)), line
    for line in (f"echo x > {H}/settings.json", f"echo x > {H}/tmp/../attachments/a",
                 f"echo x > {H}/attachments/a"):
        assert check_command(line, lists, ctx, cwd=lay.proj).blocked, line


def test_the_mounts_preset_leaves_temp_folders_out(lay: SimpleNamespace) -> None:
    disk = lay.tmp_path / "disk"
    entries = catalog.path_entries("mounts", user_home=Path("/nonexistent-u"), home=None,
                                   workdir=None, projects=(),
                                   mounts=(Path("/tmp"), Path("/var/tmp"), disk))
    shown = {entry.shown for entry in entries}
    assert "/tmp" not in shown and "/var/tmp" not in shown


def test_preset_descriptions_name_the_scratch_folders() -> None:
    preset = catalog.path_preset("snowpea-home")
    assert preset is not None
    for locale in catalog.LOCALES:
        assert "$SNOWPEA_HOME/tmp" in preset.description[locale], locale
    mounts = catalog.path_preset("mounts")
    assert mounts is not None
    for locale in catalog.LOCALES:
        assert "/tmp" in mounts.description[locale], locale


# -- through enforce (the daemon's wiring) --------------------------------------------------


@BOTH
def test_enforce_judge_lets_scratch_work_through(lay: SimpleNamespace, on: bool) -> None:
    settings = Settings.model_validate({"security": {"enabled": on}})
    for tool, args, tags in (
        ("shell", {"command": "printf 'echo hi' > /tmp/m32-x.sh && sh /tmp/m32-x.sh"},
         ("exec",)),
        ("shell", {"command": f"echo hi > {lay.home}/tmp/a.txt"}, ("exec",)),
        ("write_file", {"path": f"{lay.home}/tmp/a.py", "content": "x"}, ("write",)),
        ("execute_code", {"code": f"open('{lay.home}/tmp/a.py', 'w').write('x')\n"},
         ("exec",)),
    ):
        verdict = enforce.judge(settings=settings, home=lay.home, workdir=lay.proj, tool=tool,
                                args=args, tags=tags, projects=(str(lay.proj),))
        assert _free(verdict), (tool, args, verdict)
