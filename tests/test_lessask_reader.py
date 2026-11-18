"""m21 reader precision: a script or a line whose targets are knowable is checked.

``bash bin/shot.sh /tmp/a.png`` where the script says ``OUT=${1:-/tmp/b.png}``,
``ADB=<abs>/adb`` and ``$ADB exec-out screencap -p > "$OUT"`` writes ``/tmp/a.png``
or ``/tmp/b.png`` with that adb: both candidates are judged exactly as if the text
spelled them, and nothing about it is "unknown" any more.  What the text cannot
pin down (a reassignment, a branch, the environment, a substitution, a non-literal
argument) stays unknown, and a resolved value that reaches a blocked command, a
protected path or snowpea's own controls is stopped like the literal would be.

Every command line and script below is DATA handed to the pure checker; nothing
here runs any of it.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.agent.loop import _asks_unseen
from snowpea_core.security import code as code_mod
from snowpea_core.security import shell as sh
from snowpea_core.security import written
from snowpea_core.security.check import (
    CONTROL_REASON,
    Context,
    Verdict,
    check_command,
    scripts_changed,
)
from snowpea_core.security.lists import from_raw

DEFAULT = from_raw({})
VALUES = ("ask", "auto", "allow")
HOME = Path("/home/u")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    written.reset()
    root = tmp_path / "proj"
    (root / "bin").mkdir(parents=True)
    sdk = tmp_path / "sdk" / "platform-tools"
    sdk.mkdir(parents=True)
    (sdk / "adb").write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 64)
    # Nothing is "recent" by its timestamps: only the scripts the agent wrote
    # (``written.note``) count as its own, the installed adb does not.
    monkeypatch.setattr(code_mod, "RECENT_SEC", 0.0)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    return root


def _adb(root: Path) -> str:
    return str(root.parent / "sdk" / "platform-tools" / "adb")


def _write(root: Path, name: str, text: str, *, mode: int = 0o644) -> Path:
    path = root / "bin" / name
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    written.note([str(path)])
    return path


def _ctx(root: Path, value: str) -> Context:
    return Context(workdir=root, user_home=HOME, home=HOME / ".snowpea",
                   projects=(str(root),), mounts=(), env_venv=False, unknown_code=value)


def _verdict(line: str, root: Path, value: str = "ask") -> Verdict:
    return check_command(line, DEFAULT, _ctx(root, value), cwd=root)


def _outcome(line: str, root: Path, value: str, mode: str) -> str:
    """``a`` runs unasked, ``o`` asked, ``c`` asked as snowpea's controls, ``b`` blocked."""
    verdict = _verdict(line, root, value)
    if verdict.blocked:
        return "b"
    if not _asks_unseen(verdict, SimpleNamespace(mode=mode)):
        return "a"
    return "c" if verdict.reason == CONTROL_REASON else "o"


#: The owner's script of 2026-11-17 (22 lines, anonymised; ``{adb}`` is filled in).
OWNER = """#!/bin/bash
# capture the native screen of the emulator
set -e
OUT=${{1:-/tmp/emu_native_s.png}}
ADB={adb}
SERIAL=emulator-5554

echo "capturing to $OUT"
$ADB -s $SERIAL wait-for-device
$ADB -s $SERIAL shell input keyevent 224
sleep 1
$ADB -s $SERIAL exec-out screencap -p > "$OUT"
if [ ! -s "$OUT" ]; then
  echo "empty capture" >&2
  exit 1
fi
SIZE=$(stat -c %s "$OUT")
echo "saved $OUT ($SIZE bytes)"
ls -l "$OUT"
file "$OUT"
# done
exit 0
"""


def test_owner_case_is_fully_checkable(project: Path) -> None:
    text = OWNER.format(adb=_adb(project))
    assert len(text.splitlines()) == 22
    _write(project, "try_native_s.sh", text)
    line = "bash bin/try_native_s.sh /tmp/emu_ns5.png"
    for value in VALUES:
        verdict = _verdict(line, project, value)
        assert not verdict.blocked and not verdict.opaque, (value, verdict.unseen)
        for mode in ("plan", "accept", "auto"):
            assert _outcome(line, project, value, mode) == "a"
    view = sh.view(line, cwd=str(project), user_home=str(HOME), home=str(HOME / ".snowpea"))
    assert view.opaque == []
    programs = {cmd.words[0] for cmd in view.commands}
    assert _adb(project) in programs
    writes = {e.raw for e in view.effects if e.kind == "write"}
    # The argument and the default: both judged.
    assert writes == {"/tmp/emu_ns5.png", "/tmp/emu_native_s.png"}
    assert not view.unknown_programs


#: (script text or None, line): a script ``bin/s.sh`` the agent wrote, run by the line.
BENIGN: list[tuple[str | None, str]] = [
    ("#!/bin/sh\ncp /tmp/src.png \"$1\"\n", "bash bin/s.sh /tmp/a.png"),
    ("#!/bin/sh\necho hi > \"$1\"\n", "sh bin/s.sh /tmp/a.txt"),
    ("#!/bin/sh\necho hi > \"$1\"\n", "./bin/s.sh /tmp/a.txt"),
    ("#!/bin/sh\ncp \"$1\" \"$2\"\n", "bash bin/s.sh /tmp/a /tmp/b"),
    ("#!/bin/sh\n\"$@\"\n", "bash bin/s.sh ls -l /tmp"),
    ("#!/bin/sh\nLOG=${1:-/tmp/default.log}\necho run >> \"$LOG\"\n", "bash bin/s.sh"),
    ("#!/bin/sh\nOUT=${2:-/tmp/d.txt}\ncp \"$1\" \"$OUT\"\n", "bash bin/s.sh /tmp/a"),
    ("#!/bin/sh\nOUT=/tmp/x.png\necho > \"$OUT\"\n", "bash bin/s.sh"),
    ("#!/bin/sh\nDIR=/tmp/shots\nOUT=$DIR/a.png\ntouch \"$OUT\"\n", "bash bin/s.sh"),
    ("#!/bin/sh\nOUT=/tmp/a\ncat /etc/hostname > ${OUT}\n", "bash bin/s.sh"),
    ("#!/bin/sh\nif [ $# -lt 1 ]; then exit 1; fi\ntouch \"$1\"\n", "bash bin/s.sh /tmp/t"),
    ("#!/bin/sh\nout=/tmp/a\ntouch \"$out\"\n", "bash bin/s.sh"),
    ("#!/bin/sh\nGREP=grep\n$GREP -r foo /tmp/x\n", "bash bin/s.sh"),
    ("#!/bin/sh\nSRC=${1:-/tmp/in}\nDST=${2:-/tmp/out}\ncp \"$SRC\" \"$DST\"\n",
     "bash bin/s.sh /tmp/a"),
    ("#!/bin/sh\nOUT=/tmp/a\nif [ -n \"$X\" ]; then\n  touch \"$OUT\"\nfi\n", "bash bin/s.sh"),
    ("#!/bin/sh\nIN=/tmp/in.txt\nwc -l < \"$IN\"\n", "bash bin/s.sh"),
    ("#!/bin/sh\ntouch \"${1}\"\n", "bash bin/s.sh /tmp/b1"),
    ("#!/bin/sh\ntouch \"/tmp/shot-$1.png\"\n", "bash bin/s.sh 5"),
    ("#!/bin/bash\nN=${1:-3}\nOUT=/tmp/run.log\nfor i in 1 2; do echo $i >> \"$OUT\"; done\n",
     "bash bin/s.sh 4"),
    ("#!/bin/sh\nTOOL=/usr/bin/env\n$TOOL ls /tmp\n", "bash bin/s.sh"),
    # the agent's own line
    (None, "OUT=/tmp/a.txt; echo hi > \"$OUT\""),
    (None, "T=/usr/bin/ls; $T -la /tmp"),
    (None, "D=/tmp/build && mkdir -p \"$D\""),
    (None, "sh -c 'echo hi > \"$1\"' x /tmp/a.txt"),
    (None, "bash -c 'cp \"$1\" \"$2\"' _ /tmp/a /tmp/b"),
]


@pytest.mark.parametrize(("script", "line"), BENIGN)
def test_benign_forms_are_checkable(script: str | None, line: str, project: Path) -> None:
    if script is not None:
        _write(project, "s.sh", script, mode=0o755)
    for value in VALUES:
        verdict = _verdict(line, project, value)
        assert not verdict.blocked and not verdict.opaque, (line, value, verdict.unseen)


#: (script text or None, line, expected) — expected outcome in auto mode under
#: ``ask``, ``auto`` and ``allow``: ``b`` blocked, ``c`` asked as snowpea's own
#: controls, ``o`` asked, ``a`` runs unasked.  Under ``ask`` none runs unasked.
HOSTILE: list[tuple[str | None, str, str]] = [
    # a resolved value that is a protected path or a blocked command
    ("#!/bin/sh\necho k >> \"$1\"\n", "bash bin/s.sh ~/.ssh/authorized_keys", "bbb"),
    ("#!/bin/sh\necho k >> \"$1\"\n", "bash bin/s.sh /home/u/.ssh/authorized_keys", "bbb"),
    ("#!/bin/sh\nOUT=${1:-~/.ssh/authorized_keys}\necho k >> \"$OUT\"\n",
     "bash bin/s.sh /tmp/x", "bbb"),
    ("#!/bin/sh\nOUT=${1:-/tmp/ok}\necho k > \"$OUT\"\n", "bash bin/s.sh ~/.ssh/config", "bbb"),
    ("#!/bin/sh\nADB=rm\n$ADB -rf ~/\n", "bash bin/s.sh", "bbb"),
    ("#!/bin/sh\n\"$@\"\n", "bash bin/s.sh rm -rf ~", "bbb"),
    (None, "sh -c 'rm -rf \"$1\"' x ~", "bbb"),
    (None, "sh -c 'cat k > \"$1\"' x ~/.ssh/authorized_keys", "bbb"),
    (None, "X=rm; $X -rf ~", "bbb"),
    # snowpea's own controls
    ("#!/bin/sh\necho '{}' > \"$1\"\n", "bash bin/s.sh /home/u/.snowpea/trust/projects.json",
     "bbb"),
    ("#!/bin/sh\nADB=snowpea\n$ADB project trust --yes .\n", "bash bin/s.sh", "ccc"),
    ("#!/bin/sh\nTOOL=${1:-snowpea}\n$TOOL project trust --yes .\n", "bash bin/s.sh ls", "ccc"),
    # what the text cannot pin down stays unknown
    ("#!/bin/sh\nOUT=/tmp/a\nOUT=/tmp/b\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=/tmp/a\nif true; then OUT=/tmp/b; fi\ncat x > \"$OUT\"\n",
     "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nfor OUT in /tmp/a /tmp/b; do echo > $OUT; done\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ncat x > \"$TARGET\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=$(mktemp)\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    # (an unknown name in a folder's file name may be a settings file: blocked, as before)
    ("#!/bin/bash\nN=$((1+2))\ncat x > /tmp/f$N\n", "bash bin/s.sh", "bbb"),
    ("#!/bin/bash\nN=$((1+2))\ncat x > \"$N\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nread OUT\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=/tmp/a\nexport OUT\ncat x > \"$OUT\"\nprog\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=/tmp/a\neval \"OUT=/tmp/b\"\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=/tmp/a\n. ./other.sh\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ncat x > \"$OUT\"\nOUT=/tmp/a\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\n(OUT=/tmp/a)\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ntrue && OUT=/tmp/a\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ncat x > \"$1\"\n", "bash bin/s.sh \"$X\"", "oaa"),
    ("#!/bin/sh\nshift\ncat x > \"$1\"\n", "bash bin/s.sh /tmp/a /tmp/b", "oaa"),
    ("#!/bin/sh\nset -- /tmp/b\ncat x > \"$1\"\n", "bash bin/s.sh /tmp/a", "oaa"),
    ("#!/bin/sh\nf() { cat x > \"$1\"; }\nf /tmp/b\n", "bash bin/s.sh /tmp/a", "oaa"),
    ("#!/bin/sh\ntrap 'OUT=/tmp/b' USR1\nOUT=/tmp/a\ncat x > \"$OUT\"\n", "bash bin/s.sh",
     "oaa"),
    ("#!/bin/sh\nOUT=/tmp/*\ncat x > $OUT\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ncat x > \"${OUT:=/tmp/a}\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\ncat x > \"$1\"\n", "bash bin/s.sh '/tmp/*'", "oaa"),
    ("#!/bin/sh\ncat x > \"$1\"\n", "bash bin/s.sh '$HOME/x'", "oaa"),
    ("#!/bin/sh\ncat x > \"$2\"\n", "bash bin/s.sh /tmp/a", "oaa"),
    ("#!/bin/sh\nX=/tmp/ok\ncat x > '$X'$Y\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nOUT=/tmp/a\nOUT+=/b\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    ("#!/bin/sh\nset -a\nOUT=/tmp/a\nprog \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    # a prefix assignment is the program's environment, not a binding (the parser
    # splits ``X=$(…) prog`` in two; the text says otherwise)
    ("#!/bin/sh\nOUT=$(cat f) prog\ncat x > \"$OUT\"\n", "bash bin/s.sh", "oaa"),
    (None, "OUT=/tmp/a cat x > \"$OUT\"", "oaa"),
    # a name the line that runs the script may export to it
    ("#!/bin/sh\nOUT=${OUT:-/tmp/a}\ncat x > \"$OUT\"\n", "OUT=/x bash bin/s.sh", "oaa"),
    # a ``sh -c`` body inside the script does not see the script's arguments
    ("#!/bin/sh\nfind /tmp -name a -exec sh -c 'cat x > \"$1\"' _ {} \\;\n",
     "bash bin/s.sh /tmp/a", "oaa"),
]


@pytest.mark.parametrize(("script", "line", "expected"), HOSTILE)
def test_hostile_forms_are_still_stopped(script: str | None, line: str, expected: str,
                                         project: Path) -> None:
    if script is not None:
        _write(project, "s.sh", script, mode=0o755)
    got = "".join(_outcome(line, project, value, "auto") for value in VALUES)
    assert got[0] != "a", (line, _verdict(line, project).unseen)
    assert got == expected, (line, got)


def test_a_script_changed_after_judgement_is_seen(project: Path) -> None:
    path = _write(project, "s.sh", "#!/bin/sh\necho hi > \"$1\"\n")
    verdict = _verdict("bash bin/s.sh /tmp/a", project)
    assert not verdict.opaque
    assert str(path.resolve()) in {os.path.realpath(p) for p, _ in verdict.scripts}
    path.write_text("#!/bin/sh\nrm -rf ~\n", encoding="utf-8")
    assert scripts_changed(verdict)


def test_a_symlinked_script_is_read_where_it_points(project: Path) -> None:
    target = _write(project, "real.sh", "#!/bin/sh\necho k >> \"$1\"\n")
    link = project / "bin" / "link.sh"
    link.symlink_to(target)
    ok = _verdict("bash bin/link.sh /tmp/a", project)
    assert not ok.opaque and not ok.blocked
    assert _verdict("bash bin/link.sh ~/.ssh/authorized_keys", project).blocked


def test_too_many_candidates_stay_unknown(project: Path) -> None:
    script = "#!/bin/sh\n" + "".join(
        f"V{i}=${{{i}:-/tmp/d{i}}}\n" for i in range(1, 6)) + \
        "cat x > \"$V1$V2$V3$V4$V5\"\n"
    _write(project, "s.sh", script)
    verdict = _verdict("bash bin/s.sh a b c d e", project)
    assert verdict.opaque  # 32 combinations: more than MAX_VARIANTS


def test_the_walk_budget_bounds_the_work(project: Path) -> None:
    head = "#!/bin/sh\n" + "".join(f"V{i}=${{{i}:-/tmp/d{i}}}\n" for i in range(1, 5))
    body = "cp x \"$V1$V2$V3$V4\"\n"
    _write(project, "s.sh", head + body * 15)
    assert not _verdict("bash bin/s.sh a b c d", project).opaque  # 15 x 16 = 240 walks
    _write(project, "s.sh", head + body * 17)
    assert _verdict("bash bin/s.sh a b c d", project).opaque  # 272: past MAX_WALKS


def test_scope_rules() -> None:
    scope = sh._scope_for("OUT=/tmp/a\ncat > $OUT", positional=None, outer="")
    assert scope is not None and scope.bound["OUT"].values == ("/tmp/a",)
    assert sh._expand("$OUT", scope, 1) == ["/tmp/a"]
    assert sh._expand("$OUT", scope, 0) is None  # before the assignment
    assert sh._scope_for("eval x; OUT=/a", positional=None, outer="") is None
    scope = sh._scope_for("touch $1", positional=[("/tmp/a", False)], outer="")
    assert scope is not None and sh._expand("${1:-/tmp/b}", scope, 0) == ["/tmp/a", "/tmp/b"]
    scope = sh._scope_for("touch $1", positional=[("$X", True)], outer="")
    assert scope is not None and sh._expand("$1", scope, 0) is None


# -- with script grants (m21-remember) ------------------------------------------------


def test_a_resolved_script_needs_no_grant(project: Path) -> None:
    _write(project, "try_native_s.sh", OWNER.format(adb=_adb(project)))
    verdict = _verdict("bash bin/try_native_s.sh /tmp/emu_ns5.png", project)
    assert not verdict.opaque and not verdict.script_grant


def test_what_stays_unknown_is_still_attributed_to_the_file(project: Path) -> None:
    # The program resolves (the literal TOOL); the computed file name does not.
    _write(project, "shot.sh", "#!/bin/sh\nTOOL=screencap\nOUT=$(date +%s).png\n"
                               "$TOOL -p > \"$OUT\"\n")
    verdict = _verdict("bash bin/shot.sh", project)
    assert verdict.opaque and verdict.unseen == ("unknown-path",)
    assert verdict.script_grant
    assert [e["file"] for e in verdict.unchecked_why] == ["bin/shot.sh"]
    view = sh.view("bash bin/shot.sh", cwd=str(project), user_home=str(HOME),
                   home=str(HOME / ".snowpea"))
    assert view.own_opaque == []
    # The same unknown on the line itself is the line's own: no grant.
    line = "TOOL=screencap; OUT=$(date +%s).png; $TOOL -p > \"$OUT\""
    own = _verdict(line, project)
    assert own.opaque and not own.script_grant
