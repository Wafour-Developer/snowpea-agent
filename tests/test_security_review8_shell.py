"""Security review 8, part A: the check's tables after H8-2 (home default-deny,
sidecars, links), H8-3 (git, PATH) and M8-2 (variables as P1).

Every command line and every piece of code below is DATA, handed to the pure
checker; nothing runs.  ``/home/u`` is fictional; the project is a temporary
folder (the round-7 fixture).  Each row: ``(line, default lists / EMPTY lists /
default lists + unknownCode "allow")`` — ``b`` blocked, ``o`` asked, ``a`` allowed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_security_review7_shell import LEGIT as LEGIT7
from test_security_review7_shell import _ctx, _states, project  # noqa: F401 - the fixture

from snowpea_core.security import control_plane as cp
from snowpea_core.security import shell as sh
from snowpea_core.security import written
from snowpea_core.security.check import check_command
from snowpea_core.security.lists import from_raw

DEFAULT = from_raw({})

# -- what must not pass -----------------------------------------------------------
HOSTILE = [
    # H8-2: sidecars of secrets are the secret; the home is default-deny; links ask.
    ("echo '{}' > ~/.snowpea/credentials.json.tmp", "bbb"),
    ("ln -s /tmp/c ~/.snowpea/credentials.json.tmp", "bbb"),
    ("ln -s /tmp/s.json ~/.snowpea/settings.json.tmp", "bbb"),
    ("cat ~/.snowpea/credentials.json.tmp", "bbb"),
    ("cat ~/.snowpea/.credentials.json.0a1b2c3d.tmp", "bbb"),
    ("cat ~/.snowpea/daemon.json.tmp", "bbb"),
    ("cp ~/.snowpea/mcp-oauth.json.tmp /tmp/x", "bbb"),
    ("echo x > ~/.snowpea/settings.json.invalid", "bbb"),
    ("echo x > ~/.snowpea/some-new-file.json", "bbb"),
    ("ln -s /etc ~/.snowpea/sessions/s1/etc", "ooo"),
    ("ln ~/.snowpea/plans/p.md ~/.snowpea/sessions/p.md", "ooo"),
    ("python3 -c 'import os; os.symlink(\"/tmp/x\", \"/home/u/.snowpea/plans/p\")'", "ooo"),
    ("node -e \"require('fs').symlinkSync('/tmp/x', '/home/u/.snowpea/cache/c')\"", "ooo"),
    # H8-3 (b): git told to run a program, or its hooks and configuration written.
    ("git config core.hooksPath /tmp/hooks", "ooo"),
    ("git config core.fsmonitor ./watch.sh", "ooo"),
    ("git config --global credential.helper '!f(){ id; }; f'", "ooo"),
    ("git config diff.foo.textconv ./conv.sh", "ooo"),
    ("git config filter.lfs.smudge ./smudge.sh", "ooo"),
    ("git config set core.sshCommand 'ssh -o ProxyCommand=x'", "ooo"),
    ("git config --edit", "ooo"),
    ("git -c core.pager='sh -c x' log", "ooo"),
    ("git -c alias.st='!sh x' st", "ooo"),
    ("git --config-env=core.pager=PAGER_CMD log", "ooo"),
    ("echo '#!/bin/sh' > .git/hooks/pre-commit", "ooo"),
    ("cp hook.sh .git/hooks/post-checkout", "ooo"),
    ("cp other.cfg .git/config", "ooo"),
    ("printf '* filter=x' > .git/info/attributes", "ooo"),
    # H8-3 (c): a write where PATH finds programs, outside the project.
    # (the default lists protect the system folders: blocked there, asked without them)
    ("cp tool /usr/local/bin/git", "bob"),
    ("echo x > /usr/bin/make", "bob"),
    # M8-6: snowpea's own program files (``install_roots`` is set by the test below).
    # M8-2: variables that load code or point a tool at a configuration.
    ("LUA_INIT=@/tmp/x.lua lua -v", "ooo"),
    ("JDK_JAVA_OPTIONS=-javaagent:/tmp/a.jar java -version", "ooo"),
    ("DOTNET_STARTUP_HOOKS=/tmp/h.dll dotnet run", "ooo"),
    ("PHPRC=/tmp php -v", "ooo"),
    ("PHP_INI_SCAN_DIR=/tmp php -v", "ooo"),
    ("VIMINIT=':!id' vim -es", "ooo"),
    ("EXINIT=':!id' ex", "ooo"),
    ("ERL_AFLAGS='-eval x' erl", "ooo"),
    ("ERL_ZFLAGS='-eval x' erl", "ooo"),
    ("RSYNC_RSH='sh -c x' rsync a b:", "ooo"),
    ("CURL_HOME=/tmp curl https://example.com", "ooo"),
    ("WGETRC=/tmp/w wget https://example.com", "ooo"),
    ("AWS_CONFIG_FILE=/tmp/a aws s3 ls", "ooo"),
    ("KUBECONFIG=/tmp/k kubectl get pods", "ooo"),
    ("DOCKER_CONFIG=/tmp/d docker ps", "ooo"),
    ("GCONV_PATH=/tmp/g iconv -l", "ooo"),
    ("PYTHONPYCACHEPREFIX=/tmp/pc python3 app.py", "ooo"),
    ("LESSKEYIN=/tmp/k less file.txt", "ooo"),
    ("LESSOPEN='|x %s' less file.txt", "ooo"),
    ("LESSCLOSE='x %s' less file.txt", "ooo"),
    # … and a name the check does not know, with a value that is more than a word.
    ("TOOL_OPTS='-javaagent:/tmp/a.jar' ./gradlew build", "ooo"),
    ("SOME_TOOL_CONFIG=/tmp/x make", "ooo"),
    ("export TOOL_PLUGIN=/tmp/p.so; make", "ooo"),
]

# -- what must stay usable --------------------------------------------------------
LEGIT = [
    # The home's working areas: logs, plans, session workspaces, attachments, caches.
    ("cat ~/.snowpea/logs/daemon.log", "aaa"),
    ("tail -n 50 ~/.snowpea/logs/daemon.out", "aaa"),
    ("echo note >> ~/.snowpea/logs/notes.log", "aaa"),
    ("ls ~/.snowpea/sessions", "aaa"),
    ("echo '# plan' > ~/.snowpea/plans/p.md", "aaa"),
    ("cp report.md ~/.snowpea/sessions/s1/report.md", "aaa"),
    ("mkdir -p ~/.snowpea/sessions/s1/out", "aaa"),
    ("cat ~/.snowpea/attachments/s1/a.txt", "aaa"),
    ("cp ~/.snowpea/attachments/s1/a.txt ./a.txt", "aaa"),
    ("rm ~/.snowpea/cache/models-x.json", "aaa"),
    ("cat ~/.snowpea/memory.md", "aaa"),
    ("echo '- a fact' >> ~/.snowpea/memory.md", "aaa"),
    ("ls ~/.snowpea", "aaa"),
    ("cat ~/.snowpea/install.json", "aaa"),
    # git as people use it every day.
    ("git config user.name 'A Person'", "aaa"),
    ("git config --get core.pager", "aaa"),
    ("git config -l", "aaa"),
    ("git config core.autocrlf false", "aaa"),
    ("git config get core.editor", "aaa"),
    ("git -c core.pager=cat log -3", "aaa"),
    ("git -c color.ui=always diff", "aaa"),
    ("git -c user.email=a@b.c commit -m x", "aaa"),
    ("cat .git/config", "aaa"),
    ("ls .git/hooks", "aaa"),
    # Variables: common build, test and app knobs.
    ("DATABASE_URL=postgres://localhost/db pytest", "aaa"),
    ("API_KEY=abc123 npm test", "aaa"),
    ("RUST_LOG=debug cargo run", "aaa"),
    ("DJANGO_SETTINGS_MODULE=app.settings python manage.py migrate", "aaa"),
    ("FLASK_APP=app.py python -m flask --version", "aaa"),
    ("PYTEST_ADDOPTS=-x pytest", "aaa"),
    ("TZ=UTC date", "aaa"),
    ("LANG=en_US.UTF-8 make", "aaa"),
    ("PORT=8080 npm start", "aaa"),
    ("NODE_ENV=test npm test", "aaa"),
    ("VERSION=1.2.3 make", "aaa"),
    ("OUT_DIR=build make", "aaa"),
    ("CMAKE_BUILD_TYPE=Release cmake ..", "aaa"),
    ("GOOS=linux GOARCH=amd64 go build ./...", "aaa"),
    ("CGO_ENABLED=0 go build", "aaa"),
    ("FORCE_COLOR=1 npm test", "aaa"),
    ("PYTHONUNBUFFERED=1 python app.py", "aaa"),
    ("CUDA_VISIBLE_DEVICES=0 python app.py", "aaa"),
    ("REDIS_HOST=localhost REDIS_PORT=6379 npm start", "aaa"),
    ("export APP_ENV=staging", "aaa"),
    ("my_var=/tmp/x; echo $my_var", "aaa"),
    ("HEADLESS=true npm test", "aaa"),
    # Programs found on PATH that are the system's own.
    ("make", "aaa"),
    ("pytest -q", "aaa"),
]

# -- harmless forms that now ask: the measured cost of this round --------------------
FALSE_POSITIVES = [
    ("TAG=$(git describe --tags) make", "ooo"),
    ("BUILD_DIR=/tmp/build make", "ooo"),
    ("ANDROID_HOME=/opt/android ./gradlew build", "ooo"),
    ("CONFIG_FILE=config/dev.yaml npm start", "ooo"),
    ("JAVA_OPTS=-Xmx2g ./gradlew build", "ooo"),
    ("EXTRA_ARGS='--verbose' make", "ooo"),
    ("git config core.editor vim", "ooo"),
    ("git config --global core.pager 'less -R'", "ooo"),
    ("echo x > ~/.snowpea/notes.txt", "bbb"),
]


@pytest.mark.parametrize(("line", "expected"), HOSTILE)
def test_what_must_not_pass(project: Path, line: str, expected: str) -> None:  # noqa: F811
    assert _states(line, project) == expected, line


@pytest.mark.parametrize(("line", "expected"), LEGIT)
def test_what_stays_allowed(project: Path, line: str, expected: str) -> None:  # noqa: F811
    assert _states(line, project) == expected, line


@pytest.mark.parametrize(("line", "expected"), LEGIT7)
def test_round_7s_legit_rows_stay_allowed(project: Path, line: str,  # noqa: F811
                                          expected: str) -> None:
    assert _states(line, project) == expected, line


@pytest.mark.parametrize(("line", "expected"), FALSE_POSITIVES)
def test_the_harmless_forms_that_now_ask(project: Path, line: str,  # noqa: F811
                                         expected: str) -> None:
    assert _states(line, project) == expected, line


def test_the_home_is_default_deny_with_a_working_allow_list() -> None:
    for name in ("x.tmp", "settings.json.tmp", "settings.json.invalid", "notes.txt", "newdir"):
        entry = cp.unlisted((name,))
        assert entry is not None and entry.level == "write", name
        assert cp.judge_parts((name,), "write") is not None
        assert cp.judge_parts((name,), "read") is None       # read like its siblings
    for name in cp.WORKING:
        assert cp.unlisted((name, "x")) is None, name
    for rel in ("credentials.json.tmp", ".credentials.json.ab12.tmp", "daemon.json.x",
                "token.tmp", "mcp-oauth.json.tmp", ".mcp-oauth.json.1.tmp"):
        entry = cp.entry_for(tuple(rel.split("/")))
        assert entry is not None and entry.level == "all", rel


def test_an_agent_written_program_found_on_path_is_read_as_its_script(
        project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "mytool"
    tool.write_text("#!/bin/sh\necho x > /home/u/.snowpea/trust/projects.json\n",
                    encoding="utf-8")
    tool.chmod(0o755)
    old = 1_000_000_000
    os.utime(tool, (old, old))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    # pytest's folders are temporary ones: a program found there is read as the agent's.
    assert _states("mytool", project) == "bbb"
    monkeypatch.setattr(sh, "temporary_dirs", lambda: set())
    assert _states("mytool", project) == "aaa"           # the person's own program
    written.note([str(tool)], "s1")
    assert _states("mytool", project) == "bbb"           # the agent's: read, and it writes trust
    tool.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    assert _states("mytool", project) == "aaa"           # read: nothing to hold against it
    tool.write_bytes(b"\x7fELF\x00\x01binary")
    assert _states("mytool", project)[0] == "o"          # unreadable: asked


def test_a_git_command_in_a_repository_whose_hooks_the_agent_wrote_asks(
        project: Path) -> None:  # noqa: F811
    hooks = project / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (project / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    assert _states("git commit -m x", project) == "aaa"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    written.note([str(hook)], "s1")
    assert _states("git commit -m x", project) == "ooo"
    assert _states("git status", project) == "ooo"


def test_a_repository_configuration_the_agent_set_a_program_in_asks(
        project: Path) -> None:  # noqa: F811
    config = project / ".git" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("[core]\n\tbare = false\n", encoding="utf-8")
    written.note([str(config)], "s1")
    assert _states("git log -1", project) == "aaa"       # written, but nothing that runs
    config.write_text("[core]\n\tfsmonitor = ./watch.sh\n", encoding="utf-8")
    assert _states("git log -1", project) == "ooo"
    config.write_text('[diff "x"]\n\ttextconv = ./conv.sh\n', encoding="utf-8")
    assert _states("git diff", project) == "ooo"


def test_snowpeas_own_install_asks(project: Path, tmp_path: Path,  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    install = tmp_path / "venv" / "lib" / "snowpea_core"
    monkeypatch.setattr(cp, "install_roots", lambda: (install, tmp_path / "venv" / "bin" /
                                                      "snowpea"))
    assert _states(f"echo x > {install}/security/check.py", project) == "ooo"
    assert _states(f"cp evil {tmp_path}/venv/bin/snowpea", project) == "ooo"
    assert _states(f"cat {install}/security/check.py", project) == "aaa"
    # An editable checkout inside the project is the project's own.
    inside = project / "core" / "snowpea_core"
    monkeypatch.setattr(cp, "install_roots", lambda: (inside,))
    assert _states(f"echo x > {inside}/x.py", project) == "aaa"


def test_a_file_tools_write_into_git_hooks_asks(project: Path) -> None:  # noqa: F811
    from snowpea_core.security.check import check_call

    verdict = check_call("write_file", {"path": str(project / ".git" / "hooks" / "pre-push"),
                                        "content": "#!/bin/sh\n"}, DEFAULT, _ctx(project))
    assert verdict.opaque and "git-control" in verdict.unseen
    assert check_command("git status", DEFAULT, _ctx(project), cwd=project).blocked is False
