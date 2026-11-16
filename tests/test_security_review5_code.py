"""Security review 5, H4 (+ M5, M6): interpreter code and script files.

Every command and every piece of code below is DATA: it is only handed to the
pure checker (``check_command`` / ``check_call`` / ``code.analyse``), which
parses it and never runs anything.  Script "files" are written into the test's
own tmp dir with harmless or never-run text; nothing executes them.  Paths
such as ``/home/u`` are fictional.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from snowpea_core.security import code as code_mod
from snowpea_core.security import shell as sh
from snowpea_core.security.check import Context, check_call, check_command, scripts_changed
from snowpea_core.security.lists import Lists

USER = "/home/u"
ESSENTIAL = Lists(command_presets=("essential",), path_presets=("essential",))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    (root / "package.json").write_text('{"name": "x", "version": "1.0.0"}', encoding="utf-8")
    (root / "manage.py").write_text(
        "#!/usr/bin/env python\n"
        "import os\nimport sys\n\n\n"
        "def main():\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'site.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n\n\n"
        "if __name__ == '__main__':\n    main()\n", encoding="utf-8")
    (root / "scripts" / "build.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nnpm ci\nnpm run build\nmkdir -p dist\n"
        "cp -r public/. dist/\necho done\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _no_tool_writes() -> None:
    # Review 6, M-8: the written paths moved to ``security.written``.
    from snowpea_core.security import written

    written.reset()


def _ctx(project: Path) -> Context:
    return Context(workdir=project, user_home=Path(USER), home=Path(USER) / ".snowpea",
                   projects=(str(project),), mounts=(), env_venv=False)


def _verdict(line: str, project: Path) -> str:
    verdict = check_command(line, ESSENTIAL, _ctx(project), cwd=project)
    return "blocked" if verdict.blocked else "opaque" if verdict.opaque else "allowed"


BLOCKED = [
    # Inline interpreter code (H4 a).
    """python3 -c "import shutil;shutil.rmtree('/')\"""",
    """node -e "require('fs').rmSync('/home/u',{recursive:true})\"""",
    """perl -MFile::Path -e 'rmtree("/")'""",
    """python3 -c "import os; os.system('rm -rf /')\"""",
    """python -c "import subprocess; subprocess.run(['rm','-rf','/'])\"""",
    """python -c "from shutil import rmtree; rmtree('/home/u')\"""",
    """python -c "__import__('os').system('rm -rf /')\"""",
    """python -c "import os; os.execv('/bin/rm', ['rm', '-rf', '/'])\"""",
    """python3 -c "open('/etc/passwd', 'w').write('x')\"""",
    # Review 6, H-3: a concatenation of literals is folded, so this is the
    # command it spells (it was "opaque" while the analysis could not fold).
    """python3 -c "import os; os.system('rm ' + '-rf /')\"""",
    """python3 -c "from pathlib import Path; Path('/etc/hosts').unlink()\"""",
    """node -e "require('child_process').execSync('rm -rf ~')\"""",
    """node -e "require('child_process').spawnSync('rm', ['-rf', '/'])\"""",
    """ruby -e 'require "fileutils"; FileUtils.rm_rf("/")'""",
    """ruby -e 'system("rm -rf /")'""",
    """php -r 'system("rm -rf /");'""",
    """perl -e 'system("rm", "-rf", "/")'""",
    """perl -e '`rm -rf /`'""",
    # H4 c: the shapes the essential presets must stop.
    "busybox rm -rf /",
    "toybox rm -rf /",
    "\\rm -rf /",
    "command rm -rf /",
    "tar -cf /tmp/a.tar --remove-files /etc",
    "zip -rm /tmp/a.zip /etc",
    "dd if=/dev/zero of=/dev/sda",
    "find / -delete",
    "git -C / clean -fdx",
]

OPAQUE = [
    """python -c "import os,sys; os.remove(sys.argv[1])\"""",
    """python -c "import subprocess,sys; subprocess.run(sys.argv[1:])\"""",
    """python3 -c "import os, sys; os.system('rm ' + sys.argv[1])\"""",
    """python -c "exec(input())\"""",
    """node -e "require('child_process').exec(process.argv[1])\"""",
    """node -e "require('fs').rmSync(process.argv[1], {recursive: true})\"""",
    """perl -e 'unlink $ARGV[0]'""",
    """ruby -e 'FileUtils.rm_rf(ARGV[0])'""",
    """php -r 'unlink($argv[1]);'""",
    "bash ./not-there-yet.sh",
    "python ./not-there-yet.py",
]

ALLOWED = [
    """python -c "print(1)\"""",
    """python3 -c "import os; print(os.getcwd())\"""",
    """python -c "import json; print(json.load(open('package.json'))['name'])\"""",
    """python -c "import re; print(re.compile('x').pattern)\"""",
    """python -c "open('out.txt', 'w').write('x')\"""",
    """node -e "console.log(require('./package.json').version)\"""",
    """node -e "const r = /a/; console.log(r.exec('a'))\"""",
    """node -e "console.log(require('fs').readFileSync('package.json', 'utf8'))\"""",
    """perl -e 'print "please unlink me and rmtree the rest\\n"'""",
    """ruby -e 'puts File.read("package.json")'""",
    """php -r 'echo phpversion();'""",
    "python manage.py test",
    "python3 manage.py migrate --plan",
    "bash scripts/build.sh",
    "sh scripts/build.sh",
    "python -m pytest -q",
    "rm -rf build",
    "rm notes.txt",
]


@pytest.mark.parametrize("line", BLOCKED)
def test_blocked(line: str, project: Path) -> None:
    assert _verdict(line, project) == "blocked", line


@pytest.mark.parametrize("line", OPAQUE)
def test_opaque(line: str, project: Path) -> None:
    assert _verdict(line, project) == "opaque", line


@pytest.mark.parametrize("line", ALLOWED)
def test_allowed(line: str, project: Path) -> None:
    assert _verdict(line, project) == "allowed", line


def test_execute_code_bodies(project: Path) -> None:
    ctx = _ctx(project)
    computed = check_call("execute_code", {"code": 'import os; os.system("rm " + input())'},
                          ESSENTIAL, ctx)
    assert computed.opaque and not computed.blocked
    literal = check_call("execute_code", {"code": "import shutil\nshutil.rmtree('/home/u')"},
                         ESSENTIAL, ctx)
    assert literal.blocked
    plain = check_call("execute_code", {"code": "total = sum(range(10))\nprint(total)"},
                       ESSENTIAL, ctx)
    assert not plain.blocked and not plain.opaque
    node = check_call("execute_code", {"code": "require('fs').rmSync('/', {recursive: true})",
                                       "language": "javascript"}, ESSENTIAL, ctx)
    assert not node.blocked  # "javascript" is not a family snowpea maps: read as python
    node = check_call("execute_code", {"code": "require('fs').rmSync('/', {recursive: true})",
                                       "language": "node"}, ESSENTIAL, ctx)
    assert node.blocked


def test_a_script_file_is_read(project: Path) -> None:
    script = project / "x.sh"
    script.write_text("echo start\nrm -rf /\n", encoding="utf-8")
    assert _verdict("bash x.sh", project) == "blocked"
    assert _verdict("sh ./x.sh", project) == "blocked"
    assert _verdict("source x.sh", project) == "blocked"
    assert _verdict(". ./x.sh", project) == "blocked"
    script.chmod(0o700)
    assert _verdict("./x.sh", project) == "blocked"
    py = project / "clean.py"
    py.write_text("import shutil\nshutil.rmtree('/home/u')\n", encoding="utf-8")
    assert _verdict("python clean.py", project) == "blocked"
    node = project / "clean.js"
    node.write_text("require('fs').rmSync('/', {recursive: true})\n", encoding="utf-8")
    assert _verdict("node clean.js", project) == "blocked"


def test_recent_scripts_count_their_opaque_parts(project: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    script = project / "dyn.sh"
    script.write_text("X=rm\n$X -rf /tmp/thing\n", encoding="utf-8")
    assert _verdict("bash dyn.sh", project) == "opaque"  # just written: the agent's own
    monkeypatch.setattr(code_mod, "RECENT_SEC", -1.0)
    assert _verdict("bash dyn.sh", project) == "allowed"  # a pre-existing script
    # … unless a tool call this daemon let through wrote it.
    code_mod.note_write(script)
    assert _verdict("bash dyn.sh", project) == "opaque"


def test_unreadable_scripts(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = project / "tool.bin"
    binary.write_bytes(b"\x7fELF\x00\x00binary")
    binary.chmod(0o700)
    big = project / "big.sh"
    big.write_text("echo hi\n" * (code_mod.MAX_SCRIPT_BYTES // 8 + 10), encoding="utf-8")
    assert _verdict("./tool.bin", project) == "opaque"
    assert _verdict("bash big.sh", project) == "opaque"
    monkeypatch.setattr(code_mod, "RECENT_SEC", -1.0)
    assert _verdict("./tool.bin", project) == "allowed"
    assert _verdict("bash big.sh", project) == "allowed"


def test_the_digest_catches_a_swap(project: Path) -> None:
    script = project / "run.sh"
    script.write_text("X=echo\n$X hi\n", encoding="utf-8")
    verdict = check_command("bash run.sh", ESSENTIAL, _ctx(project), cwd=project)
    assert verdict.opaque and verdict.scripts
    assert scripts_changed(verdict) == []
    script.write_text("rm -rf ~\n", encoding="utf-8")
    assert scripts_changed(verdict) == [str(script)]


def test_the_analysis_is_pure_and_bounded() -> None:
    found = code_mod.analyse("import os\n" + "os.remove('a')\n" * 200, "python")
    assert "code:too-many" in found.opaque
    # Review 6, H-3: code that does not parse is not "nothing found" any more.
    assert code_mod.analyse("x = (", "python").opaque == ["code:parse"]
    assert code_mod.analyse("print('rm -rf /')", "python").commands == []
    assert sh.view("python -c 'print(1)'", cwd=None).opaque == []


# -- L2: a hand-edited presets value ------------------------------------------------


@pytest.mark.parametrize("value", ["essential", None, 3, {"x": 1}])
def test_a_non_list_presets_value_means_the_defaults(value: object) -> None:
    from snowpea_core.config import settings as settings_mod
    from snowpea_core.config.settings import Settings
    from snowpea_core.security.lists import from_raw, global_lists

    loaded = Settings.model_validate({"security": {"blockedCommands": {"presets": value},
                                                   "protectedPaths": {"presets": value}}})
    lists = global_lists(loaded)
    assert lists.command_presets == ("essential",) and lists.path_presets == ("essential",)
    assert {"blockedCommands", "protectedPaths"} <= set(settings_mod.SECURITY_PRESET_PROBLEMS)
    raw = from_raw({"blockedCommands": {"presets": value}})
    assert raw.command_presets == ("essential",) and raw.problems
    Settings.model_validate({"security": {"blockedCommands": {"presets": []},
                                          "protectedPaths": {"presets": []}}})
    assert settings_mod.SECURITY_PRESET_PROBLEMS == {}  # an explicit empty list is honoured


# -- M5: case-insensitive file systems --------------------------------------------


def test_protected_paths_fold_case_on_case_insensitive_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.security import check as check_mod

    lists = Lists(paths=(("/Users/u/proj", "write"),))
    ctx = Context(workdir=tmp_path, user_home=Path("/Users/u"), home=None, projects=(),
                  mounts=(), env_venv=False)
    monkeypatch.setattr(check_mod, "_FOLD_CASE", False)
    assert not check_command("rm -rf /users/u/proj", lists, ctx).blocked
    monkeypatch.setattr(check_mod, "_FOLD_CASE", True)
    assert check_command("rm -rf /users/u/proj", lists, ctx).blocked
    assert check_command("rm -rf /USERS/U/PROJ/src", lists, ctx).blocked
    # NFD and NFC spellings of the same name are one path on macOS.
    nfc = Lists(paths=(("/Users/u/café", "write"),))
    assert check_command("rm -rf /Users/u/café", nfc, ctx).blocked


# -- M6: hard links ---------------------------------------------------------------


def test_a_hard_link_to_a_protected_file_is_a_write_to_it(tmp_path: Path) -> None:
    protected = tmp_path / "secret"
    protected.mkdir()
    (protected / "key").write_text("k", encoding="utf-8")
    lists = Lists(paths=((str(protected), "write"),))
    ctx = Context(workdir=tmp_path / "proj", user_home=Path(USER), home=None, projects=(),
                  mounts=(), env_venv=False)
    (tmp_path / "proj").mkdir()
    assert check_command(f"ln {protected}/key ./x", lists, ctx).blocked
    assert not check_command(f"ln -s {protected}/key ./x", lists, ctx).blocked


def test_a_write_to_a_multiply_linked_file_is_opaque(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "x"
    target.write_text("x", encoding="utf-8")
    os.link(target, tmp_path / "elsewhere")
    lists = Lists(paths=((str(tmp_path / "secret"), "write"),))
    ctx = Context(workdir=proj, user_home=Path(USER), home=None, projects=(), mounts=(),
                  env_venv=False)
    verdict = check_command("echo hi > x", lists, ctx, cwd=proj)
    assert verdict.opaque and not verdict.blocked
    single = proj / "y"
    single.write_text("y", encoding="utf-8")
    assert not check_command("echo hi > y", lists, ctx, cwd=proj).opaque
