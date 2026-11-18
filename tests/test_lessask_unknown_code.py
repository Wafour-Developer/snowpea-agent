"""m21: ``security.unknownCode`` is tri-state — ``ask`` | ``auto`` (default) | ``allow``.

Owner decision 2026-11-17: what only says "snowpea cannot tell what this will
touch before it runs" (``check.UNRESOLVED_REASONS``) is not asked in a session
in auto mode under ``auto``, and in no mode under ``allow``; ``ask`` keeps
asking everywhere.  snowpea's own controls, the block list, protected paths,
writes where git or PATH find programs and an unjudged call are never let
through by it.

Every command line below is DATA handed to the pure checker; nothing runs.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.agent.loop import _asks_unseen
from snowpea_core.config.settings import SecuritySettings, Settings
from snowpea_core.security import code as code_mod
from snowpea_core.security import enforce, written
from snowpea_core.security.check import (
    CONTROL_REASON,
    UNRESOLVED_REASONS,
    Context,
    Verdict,
    check_command,
    unresolved,
)
from snowpea_core.security.lists import from_raw, problems_in

DEFAULT = from_raw({})
MODES = ("plan", "accept", "auto")
VALUES = ("ask", "auto", "allow")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    written.reset()
    root = tmp_path / "proj"
    (root / "bin").mkdir(parents=True)
    # A script the agent just wrote: what it computes counts.
    (root / "bin" / "shot.sh").write_text(
        "#!/bin/sh\nOUT=$(date +%s).png\nscreencap > \"$OUT\"\n", encoding="utf-8")
    old = time.time() - 2 * 86400
    (root / "README.md").write_text("x\n", encoding="utf-8")
    os.utime(root / "README.md", (old, old))
    monkeypatch.setattr(code_mod, "RECENT_SEC", 10.0 ** 9)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    return root


def _ctx(root: Path, value: str) -> Context:
    return Context(workdir=root, user_home=Path("/home/u"), home=Path("/home/u/.snowpea"),
                   projects=(str(root),), mounts=(), env_venv=False, unknown_code=value)


def _verdict(line: str, root: Path, value: str) -> Verdict:
    return check_command(line, DEFAULT, _ctx(root, value), cwd=root)


def _asked(line: str, root: Path, value: str, mode: str) -> str:
    verdict = _verdict(line, root, value)
    if verdict.blocked:
        return "b"
    session = SimpleNamespace(mode=mode)
    return "o" if _asks_unseen(verdict, session) else "a"


# -- settings -------------------------------------------------------------------------


def test_default_is_auto_and_unknown_values_read_as_ask() -> None:
    assert SecuritySettings().unknownCode == "auto"
    assert Settings().security.unknownCode == "auto"
    for value in VALUES:
        assert SecuritySettings(unknownCode=value).unknownCode == value
    for value in ("ALLOW", "yes", 1, None, ""):
        assert SecuritySettings(unknownCode=value).unknownCode == "ask"
    assert problems_in({"unknownCode": "auto"}) == []
    assert problems_in({"unknownCode": "sometimes"}) != []


def test_enforce_reads_the_global_value() -> None:
    for value in VALUES:
        settings = Settings.model_validate({"security": {"unknownCode": value}})
        assert enforce.unknown_code(settings) == value
    assert enforce.unknown_code(None) == "ask"


def test_settings_set_takes_auto_globally_only() -> None:
    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.protocol import SecurityGetResult
    from snowpea_core.server.security_handlers import settings_problems

    for value in VALUES:
        settings_problems({"security": {"unknownCode": value}})  # global: accepted
        with pytest.raises(RpcError):
            settings_problems({"security": {"unknownCode": value}}, project=True)
    assert SecurityGetResult(writable=True).unknownCode == "auto"
    assert SecurityGetResult(writable=True, unknownCode="auto").unknownCode == "auto"


def test_degraded_security_falls_back_to_ask() -> None:
    settings = Settings()
    settings.mark_degraded(["security"])
    assert settings.security.unknownCode == "ask"


# -- mode x value -----------------------------------------------------------------------

#: An unknown write target in a script the agent wrote: unresolved only.
UNKNOWN = "bash bin/shot.sh"
#: (line, {(value, mode): expected}) — ``a`` runs unasked, ``o`` asked, ``b`` blocked.
TABLE = {
    UNKNOWN: {
        ("ask", "plan"): "o", ("ask", "accept"): "o", ("ask", "auto"): "o",
        ("auto", "plan"): "o", ("auto", "accept"): "o", ("auto", "auto"): "a",
        ("allow", "plan"): "a", ("allow", "accept"): "a", ("allow", "auto"): "a",
    },
}


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("mode", MODES)
def test_mode_by_value(value: str, mode: str, project: Path) -> None:
    for line, expected in TABLE.items():
        assert _asked(line, project, value, mode) == expected[(value, mode)], (line, value, mode)


def test_auto_verdict_is_marked_relaxable(project: Path) -> None:
    asked = _verdict(UNKNOWN, project, "ask")
    auto = _verdict(UNKNOWN, project, "auto")
    allowed = _verdict(UNKNOWN, project, "allow")
    assert asked.opaque and not asked.relaxable
    assert auto.opaque and auto.relaxable and auto.unseen == asked.unseen
    assert not auto.asks_in("auto") and auto.asks_in("accept") and auto.asks_in("plan")
    assert not allowed.opaque


# -- what no value lets through ---------------------------------------------------------
KEPT = [
    # snowpea's own controls
    ("snowpea project trust --yes .", "c"),
    ("python3 -c 'import snowpea_core'", "c"),
    ("echo '{}' > ~/.snowpea/trust/projects.json", "b"),
    # the block list / protected paths
    ("rm -rf ~", "b"),
    ("echo x >> ~/.ssh/authorized_keys", "b"),
    # writes where git or PATH find programs
    ("cp hook.sh .git/hooks/pre-commit", "o"),
    # a variable the reader resolves paths with
    ("HOME=/home/u/.snowpea/trust; echo '{}' > ~/projects.json", "o"),
    ("export HOME=/tmp/h; echo x > ~/a", "o"),
    ("read HOME < f; echo x > ~/a", "o"),
    # a PATH that changes which program a name is
    ("PATH=./tools:/usr/bin:/bin git status", "o"),
    ("export PATH=/tmp/bin:$PATH; make", "o"),
    # a script the same line writes before running it
    ("echo 'snowpea project trust --yes .' > t.sh; bash t.sh", "o"),
    # a builtin named by a variable, with paths that depend on the shell's state
    ("$X HOME=/home/u/.snowpea/trust; echo '{}' > ~/projects.json", "o"),
    ("eval \"$CMD\"; echo '{}' > ~/projects.json", "o"),
    # snowpea named in what cannot be read: a variable's command line, code's strings
    ("EDITOR='snowpea project trust --yes . #' git commit --amend", "o"),
    ("bun -e \"Bun.spawn(['snowpea','project','trust','--yes','.'])\"", "o"),
    ("python3 -c 'import subprocess; subprocess.run([\"snow\" + \"pea\", x])'; ls ~/.snowpea",
     "o"),
    # an unknown program given a protected path or a control file
    ("`echo rm` -rf /", "o"),
    ("$X -rf ~", "o"),
    ("$TOOL ~/.ssh/authorized_keys", "o"),
    ("$(printf rm) -f /home/u/.snowpea/trust/projects.json", "o"),
    # a command line in a variable the reader can read is judged against the block list
    ("EDITOR='rm -rf ~' git commit --amend", "b"),
    ("GIT_SSH_COMMAND='mkfs.ext4 /dev/sdb1' git fetch", "b"),
]


@pytest.mark.parametrize(("line", "kind"), KEPT)
@pytest.mark.parametrize("value", VALUES)
def test_never_let_through(line: str, kind: str, value: str, project: Path) -> None:
    verdict = _verdict(line, project, value)
    if kind == "b":
        assert verdict.blocked, (line, value)
        return
    assert verdict.opaque and not verdict.relaxable, (line, value, verdict.unseen)
    assert verdict.asks_in("auto"), (line, value)
    if kind == "c":
        assert verdict.reason == CONTROL_REASON, (line, value)


#: The owner's screenshot script of 2026-11-17, reconstructed (anonymised).
SHOT = ("#!/bin/bash\nset -e\nOUT=${1:-/tmp/emu_native_s.png}\n"
        "ADB=/home/u/Android/Sdk/platform-tools/adb\n"
        "$ADB shell am start -n com.example.weather/.MainActivity\nsleep 2\n"
        "$ADB exec-out screencap -p > \"$OUT\"\necho saved $OUT\n")
#: A script that changes the state of the shell that sources it.
SHIFTY = "#!/bin/sh\n$X HOME=/home/u/elsewhere\n"


def test_an_unreadable_script_runs_unasked_in_auto_mode(project: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "bin" / "try_native_s.sh").write_text(SHOT, encoding="utf-8")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin:/home/u/Android/Sdk/platform-tools")
    lines = [
        "bash bin/try_native_s.sh /tmp/emu_ns5.png",
        f"cd {project} && ./gradlew :app:assembleDebug -q 2>&1|tail -8; "
        "export PATH=$PATH:/home/u/Android/Sdk/platform-tools; adb -s emulator-5554 install "
        "-r app/build/outputs/apk/debug/app-debug.apk 2>&1|tail -2; "
        "bash bin/try_native_s.sh /tmp/emu_ns5.png",
    ]
    for line in lines:
        assert _asked(line, project, "ask", "auto") == "o", line
        assert _asked(line, project, "auto", "accept") == "o", line
        assert _asked(line, project, "auto", "auto") == "a", (
            line, _verdict(line, project, "auto").unseen)


def test_a_state_change_in_this_shell_still_asks(project: Path) -> None:
    (project / "bin" / "shifty.sh").write_text(SHIFTY, encoding="utf-8")
    for line in ("source bin/shifty.sh; echo '{}' > ~/projects.json",
                 ". bin/shifty.sh; cat ~/token",
                 "$X ~/.snowpea/trust; cd ~; echo x > projects.json"):
        for value in VALUES:
            verdict = _verdict(line, project, value)
            assert verdict.blocked or (verdict.opaque and not verdict.relaxable), (line, value)
    # The same script run in a shell of its own cannot change ours.
    verdict = _verdict("bash bin/shifty.sh; echo ok > out.txt", project, "auto")
    assert verdict.relaxable, verdict.unseen


def test_reason_table() -> None:
    for why in ("unknown-path", "dynamic-program", "env-code:OUT", "env-code:ADB",
                "script-missing", "code:write:non-literal", "code:spawn:non-literal",
                "shell-stdin", "env-path:PYTHONPATH"):
        assert unresolved(why), why
    for why in ("snowpea-cli", "snowpea-code", "control-plane:trust", "internal",
                "code:internal", "parse:error", "nesting", "function", "env-code:HOME",
                "env-code:PWD", "env-code:SNOWPEA_HOME", "env-code:XDG_CONFIG_HOME",
                "env-code:PATH",
                "env-code:dynamic", "git-control", "path-write", "git-program", "git-hook",
                "deferred-run", "script-written", "control-chars", "printf-v", "alias",
                "something-new"):
        assert not unresolved(why), why
    assert unresolved("dynamic-program") and not unresolved("dynamic-program", stateful=True)
    assert "unknown-path" in UNRESOLVED_REASONS
