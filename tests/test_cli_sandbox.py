"""``snowpea --docker``: the docker argv, the image, the container's life, and one real run.

Everything above the last section uses an injected docker, so it runs anywhere.
The last test needs a working Docker daemon and builds (or reuses) the sandbox
image from a wheel of this checkout, so it skips when Docker is missing or down.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

import aiohttp
import pytest

from snowpea_core import __version__
from snowpea_core.cli import main as cli_main
from snowpea_core.cli import remote, sandbox
from snowpea_core.cli.render import EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE
from snowpea_core.cli.sandbox import Sandbox, SandboxError, SandboxOptions
from snowpea_core.server.protocol import PROTOCOL_VERSION

TOKEN_MARK = "never-in-argv"


def options(tmp_path: Path, **overrides: Any) -> SandboxOptions:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    home = tmp_path / "snowpea-home"
    home.mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "cwd": project.resolve(),
        "host_home": home,
        "uid": 1234,
        "gid": 5678,
    }
    return SandboxOptions(**{**values, **overrides})


def argv_for(opts: SandboxOptions, staged: Sequence[Path] = ()) -> list[str]:
    return sandbox.build_run_argv(
        "docker", opts, image="img:1", name="snowpea-sandbox-ab12", host_port=40123, staged=staged
    )


# ---------------------------------------------------------------------------
# the docker run argv
# ---------------------------------------------------------------------------


def test_the_port_is_published_on_host_loopback_only(tmp_path: Path) -> None:
    argv = argv_for(options(tmp_path))
    published = argv[argv.index("--publish") + 1]
    assert published == f"127.0.0.1:40123:{sandbox.CONTAINER_PORT}"
    assert argv.count("--publish") == 1
    assert "--publish-all" not in argv and "-P" not in argv
    assert "0.0.0.0" not in " ".join(argv[: argv.index("--entrypoint")])  # only the script uses it


def test_the_container_runs_as_the_host_user_with_the_project_at_the_same_path(
    tmp_path: Path,
) -> None:
    opts = options(tmp_path)
    argv = argv_for(opts)
    assert argv[argv.index("--user") + 1] == "1234:5678"
    path = str(opts.cwd)
    assert f"type=bind,source={path},target={path}" in argv
    assert argv[argv.index("--workdir") + 1] == path


def test_nothing_privileged_is_asked_for(tmp_path: Path) -> None:
    argv = argv_for(options(tmp_path, volume="keepme"), staged=[tmp_path / "settings.json"])
    text = " ".join(argv)
    for forbidden in (
        "--privileged", "--cap-add", "--device", "--network", "--net ", "--pid", "--ipc",
        "--userns", "--add-host", "--volumes-from", "docker.sock", "--security-opt seccomp",
        "--security-opt apparmor", "--user 0", "--user root",
    ):  # fmt: skip
        assert forbidden not in text
    assert "no-new-privileges" in text
    # the only mounts: the project, the staged files, the state volume
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
    assert len(mounts) == 3


def test_the_container_is_removed_unless_it_is_kept(tmp_path: Path) -> None:
    assert "--rm" in argv_for(options(tmp_path))
    assert "--rm" not in argv_for(options(tmp_path, keep=True))


def test_the_token_is_not_on_the_command_line(tmp_path: Path) -> None:
    argv = argv_for(options(tmp_path))
    envs = [argv[i + 1] for i, item in enumerate(argv) if item == "--env"]
    assert not any("TOKEN" in env.upper() for env in envs)  # nor in `docker inspect`
    assert "--token" not in argv
    assert "--token" not in sandbox.START_SCRIPT  # nor in the script's own argv inside


def test_host_settings_are_mounted_read_only_and_state_only_with_a_volume(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    (home / "credentials.json").write_text("{}")
    (home / "state.db").write_text("never")
    staged = sandbox.host_files(home)
    assert [p.name for p in staged] == ["settings.json", "credentials.json"]
    argv = argv_for(options(tmp_path), staged=staged)
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
    assert any(m.endswith(f"target={sandbox.STAGING_DIR}/settings.json,readonly") for m in mounts)
    assert not any("state.db" in m or "type=volume" in m for m in mounts)
    with_volume = argv_for(options(tmp_path, volume="mine"))
    assert f"type=volume,source=mine,target={sandbox.CONTAINER_STATE}" in with_volume


def test_the_container_command_is_sh_even_when_the_image_has_an_entrypoint(tmp_path: Path) -> None:
    argv = argv_for(options(tmp_path))
    assert argv[argv.index("--entrypoint") + 1] == "sh"
    assert argv[-3:] == ["img:1", "-c", sandbox.START_SCRIPT]


# ---------------------------------------------------------------------------
# what may be mounted, what a volume is called
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_a_project_directory_is_resolved_and_a_wide_one_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert sandbox.validate_cwd(project) == project.resolve()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for wide in (Path("/"), tmp_path, tmp_path.parent):
        with pytest.raises(SandboxError, match="too wide"):
            sandbox.validate_cwd(wide)
    with pytest.raises(SandboxError, match="does not exist"):
        sandbox.validate_cwd(tmp_path / "nope")
    comma = tmp_path / "a,b"
    comma.mkdir()
    with pytest.raises(SandboxError, match="cannot mount"):
        sandbox.validate_cwd(comma)


@pytest.mark.parametrize("name", ["", "-x", "a b", "a/b", "a,type=bind", "x" * 65])
def test_a_volume_name_that_could_change_the_mount_is_refused(name: str) -> None:
    with pytest.raises(SandboxError):
        sandbox.validate_volume(name)


def test_a_plain_volume_name_is_accepted() -> None:
    assert sandbox.validate_volume("snowpea-work_1.0") == "snowpea-work_1.0"
    assert sandbox.validate_volume(None) is None


# ---------------------------------------------------------------------------
# Docker present, daemon up
# ---------------------------------------------------------------------------


def completed(code: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, None)


def test_a_missing_docker_says_to_install_it() -> None:
    with pytest.raises(SandboxError, match="not installed"):
        sandbox.check_docker(None, lambda *_: completed())


def test_a_stopped_docker_daemon_says_how_to_start_it() -> None:
    def down(argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
        return completed(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")

    with pytest.raises(SandboxError, match="not running.*systemctl start docker"):
        sandbox.check_docker("docker", down)


def test_a_docker_socket_permission_error_says_so() -> None:
    def denied(argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
        return completed(1, "permission denied while trying to connect to the Docker daemon")

    with pytest.raises(SandboxError, match="docker group"):
        sandbox.check_docker("docker", denied)


def test_a_working_docker_is_returned() -> None:
    assert sandbox.check_docker("/usr/bin/docker", lambda *_: completed(0, "27.1.2")) == (
        "/usr/bin/docker"
    )


# ---------------------------------------------------------------------------
# the image
# ---------------------------------------------------------------------------


def test_the_image_installs_the_clients_own_version_tag() -> None:
    source = sandbox.resolve_source(None)
    assert source.wheel is None
    assert source.requirement.startswith("snowpea-agent[images] @ git+https://")
    assert source.requirement.endswith(f"@v{__version__}")


def test_a_wheel_override_is_copied_in_and_changes_the_tag(tmp_path: Path) -> None:
    wheel = tmp_path / "snowpea_agent-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"one")
    first = sandbox.resolve_source(str(wheel))
    assert first.wheel == wheel and first.requirement == (
        "/tmp/wheels/snowpea_agent-9.9.9-py3-none-any.whl[images]"
    )
    wheel.write_bytes(b"two")
    second = sandbox.resolve_source(str(wheel))
    docker = sandbox.dockerfile_text()
    assert sandbox.image_tag(first, docker) != sandbox.image_tag(second, docker)
    default = sandbox.image_tag(sandbox.resolve_source(None), docker)
    assert default.startswith(f"snowpea-sandbox:{__version__}-")
    assert sandbox.image_tag(first, docker) != sandbox.image_tag(first, docker + "# changed")


def test_a_requirement_override_and_a_directory_override(tmp_path: Path) -> None:
    source = sandbox.resolve_source("snowpea-agent==0.2.99")
    assert source.requirement == "snowpea-agent==0.2.99[images]" and source.wheel is None
    with pytest.raises(SandboxError, match="build a wheel"):
        sandbox.resolve_source(str(tmp_path))
    text = tmp_path / "x.txt"
    text.write_text("x")
    with pytest.raises(SandboxError, match="not a .whl"):
        sandbox.resolve_source(str(text))


def test_the_dockerfile_ships_with_the_package_and_is_unprivileged() -> None:
    text = sandbox.dockerfile_text()
    assert "SNOWPEA_REQUIREMENT" in text and "ripgrep" in text and "ffmpeg" in text
    assert "USER snowpea" in text and "ENTRYPOINT" not in text


class FakeDocker:
    """Answers ``docker`` calls from a table; records every argv."""

    def __init__(self, *, have_image: bool = False, build_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.have_image = have_image
        self.build_code = build_code
        self.context_files: list[str] = []

    def __call__(self, argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "info":
            return completed(0, "27.1.2")
        if verb == "image":
            return completed(0 if self.have_image else 1)
        if verb == "build":
            context = Path(argv[-1])
            self.context_files = sorted(
                str(p.relative_to(context)) for p in context.rglob("*") if p.is_file()
            )
            return completed(self.build_code)
        return completed(0)

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


def sandbox_with(
    tmp_path: Path, fake: Any, popen: Any = None, **kwargs: Any
) -> tuple[Sandbox, list[str]]:
    said: list[str] = []
    box = Sandbox(
        options(tmp_path, **kwargs.pop("opts", {})),
        local_protocol=PROTOCOL_VERSION,
        docker="docker",
        run=fake,
        popen=popen or (lambda *_: pytest.fail("no container expected")),
        say=said.append,
        **kwargs,
    )
    return box, said


def test_the_image_is_built_once_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sandbox.SOURCE_ENV, raising=False)
    fake = FakeDocker()
    box, said = sandbox_with(tmp_path, fake)
    tag = box.prepare_image()
    assert tag.startswith(f"snowpea-sandbox:{__version__}-")
    assert fake.verbs() == ["info", "image", "build"]
    build = fake.calls[-1]
    assert build[build.index("--tag") + 1] == tag
    assert any(
        a.startswith("SNOWPEA_REQUIREMENT=snowpea-agent[images] @ git+https://") for a in build
    )
    assert fake.context_files == ["Dockerfile", "wheels/README"]
    assert any("building the sandbox image" in line for line in said)

    reuse = FakeDocker(have_image=True)
    box, _ = sandbox_with(tmp_path, reuse)
    assert box.prepare_image() == tag
    assert reuse.verbs() == ["info", "image"]  # no build


def test_a_local_wheel_goes_into_the_build_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "snowpea_agent-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"w")
    monkeypatch.setenv(sandbox.SOURCE_ENV, str(wheel))
    fake = FakeDocker()
    box, _ = sandbox_with(tmp_path, fake)
    box.prepare_image()
    assert "wheels/snowpea_agent-0.0.1-py3-none-any.whl" in fake.context_files


def test_a_failed_build_names_the_override_for_development_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sandbox.SOURCE_ENV, raising=False)
    box, _ = sandbox_with(tmp_path, FakeDocker(build_code=1))
    with pytest.raises(SandboxError, match=sandbox.SOURCE_ENV):
        box.prepare_image()


def test_a_given_image_is_used_as_it_is(tmp_path: Path) -> None:
    fake = FakeDocker()
    box, _ = sandbox_with(tmp_path, fake, opts={"image": "me/snowpea-dev:1"})
    assert box.prepare_image() == "me/snowpea-dev:1"
    assert fake.verbs() == ["info"]  # no build, no inspect


# ---------------------------------------------------------------------------
# the container's life, with a fake `docker run`
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, log: IO[Any], *, die_with: int | None = None, output: bytes = b"") -> None:
        self.stdin: io.BytesIO | None = io.BytesIO()
        self.written = b""
        self.code = die_with
        self.log = log
        if output:
            log.write(output)
            log.flush()

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            self.code = 0
        return self.code

    def kill(self) -> None:
        self.code = -9


class FakePopen:
    def __init__(self, **proc_args: Any) -> None:
        self.proc_args = proc_args
        self.argvs: list[list[str]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv: Sequence[str], log: IO[Any]) -> FakeProc:
        self.argvs.append(list(argv))
        proc = FakeProc(log, **self.proc_args)
        # BytesIO.close() drops the buffer, so keep what was written for the test
        real_close = proc.stdin.close  # type: ignore[union-attr]

        def close() -> None:
            proc.written = proc.stdin.getvalue()  # type: ignore[union-attr]
            real_close()

        proc.stdin.close = close  # type: ignore[union-attr, method-assign]
        self.procs.append(proc)
        return proc


def healthy_after(n: int):
    state = {"calls": 0}

    def probe(_port: int) -> bool:
        state["calls"] += 1
        return state["calls"] > n

    return probe


def test_start_sends_the_token_on_stdin_and_gives_the_tui_its_flags(tmp_path: Path) -> None:
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, said = sandbox_with(tmp_path, fake, popen, probe=healthy_after(2), ready_timeout=5)
    box.prepare_image()
    flags = box.start()
    argv = popen.argvs[0]
    proc = popen.procs[0]
    token = flags[flags.index("--token") + 1]
    assert len(token) >= 32
    assert all(token not in part for part in argv)  # not in `ps`
    assert proc.stdin is not None and token.encode() in proc.stdin.getvalue()
    assert flags[flags.index("--port") + 1] == argv[argv.index("--publish") + 1].split(":")[1]
    assert flags[flags.index("--cwd") + 1] == str(box.options.cwd)
    label = flags[flags.index("--remote") + 1]
    assert label == f"docker:{box.name}" and box.name.startswith("snowpea-sandbox-")
    assert box.name in argv
    assert any("starting the sandbox container" in line for line in said)
    box.close()


def test_a_container_that_exits_early_is_reported_with_its_output(tmp_path: Path) -> None:
    popen = FakePopen(
        die_with=125, output=b"docker: Error response from daemon: port is already allocated\n"
    )
    box, _ = sandbox_with(
        tmp_path, FakeDocker(have_image=True), popen, probe=lambda _p: False, ready_timeout=5
    )
    box.prepare_image()
    with pytest.raises(SandboxError, match="port is already allocated"):
        box.start()
    assert not box.alive


def test_a_custom_image_failure_says_what_the_image_needs(tmp_path: Path) -> None:
    popen = FakePopen(die_with=127, output=b"sh: 1: snowpea-core: not found\n")
    box, _ = sandbox_with(
        tmp_path,
        FakeDocker(),
        popen,
        probe=lambda _p: False,
        ready_timeout=5,
        opts={"image": "python:3.12"},
    )
    box.prepare_image()
    with pytest.raises(SandboxError, match="snowpea-core.*on PATH"):
        box.start()


def test_a_daemon_that_never_answers_times_out_and_stops_the_container(tmp_path: Path) -> None:
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, _ = sandbox_with(tmp_path, fake, popen, probe=lambda _p: False, ready_timeout=0.3)
    box.prepare_image()
    with pytest.raises(SandboxError, match="did not come up"):
        box.start()
    assert "stop" in fake.verbs() and "rm" in fake.verbs()


def test_close_stops_and_removes_the_container(tmp_path: Path) -> None:
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, _ = sandbox_with(tmp_path, fake, popen, probe=lambda _p: True)
    box.prepare_image()
    box.start()
    name = box.name
    box.close()
    assert ["docker", "stop", "--time", "5", name] in fake.calls
    assert ["docker", "rm", "--force", "--volumes", name] in fake.calls
    assert popen.procs[0].written  # stdin was closed: the container's own watchdog fires too


def test_a_kept_container_is_stopped_but_not_removed_and_the_user_is_told(tmp_path: Path) -> None:
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, said = sandbox_with(
        tmp_path, fake, popen, probe=lambda _p: True, opts={"keep": True, "volume": "vol1"}
    )
    box.prepare_image()
    box.start()
    name = box.name
    box.close()
    assert "stop" in fake.verbs() and "rm" not in fake.verbs()
    assert any(name in line and "docker rm" in line for line in said)
    assert any("vol1" in line and "docker volume rm" in line for line in said)


def test_the_restart_exit_finds_the_same_container_again(tmp_path: Path) -> None:
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, _ = sandbox_with(tmp_path, fake, popen, probe=lambda _p: True)
    box.prepare_image()
    with box.open_session() as first:
        pass
    with box.open_session() as second:
        pass
    assert first == second and len(popen.argvs) == 1  # one container served both rounds
    popen.procs[0].code = 1  # the daemon is gone (an update restarted it)
    with box.open_session() as third:
        pass
    assert len(popen.argvs) == 2 and third != first  # a new one, with a new token
    box.close()


def test_a_different_major_protocol_in_the_image_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import urllib.request

    class Reply(io.BytesIO):
        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: Reply(
            json.dumps({"version": "9.0.0", "protocolVersion": "9.0.0"}).encode()
        ),
    )
    fake, popen = FakeDocker(have_image=True), FakePopen()
    box, _ = sandbox_with(tmp_path, fake, popen, probe=lambda _p: True)
    box.prepare_image()
    with pytest.raises(SandboxError, match="update the older side"):
        box.start()
    assert "rm" in fake.verbs()


# ---------------------------------------------------------------------------
# the start script, run for real under sh with a stand-in daemon
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_the_start_script_copies_files_hides_the_token_and_stops_with_stdin(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "settings.json").write_text('{"s": 1}')
    (staging / "credentials.json").write_text('{"c": "k"}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "snowpea-core"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$HOME/argv"; echo "env:$(env | grep -c SNOWPEA_TOKEN)" >> "$HOME/argv"\n'
        'touch "$HOME/up"; exec sleep 30\n'
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    script = sandbox.START_SCRIPT.replace(sandbox.STAGING_DIR, str(staging))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "SNOWPEA_HOME": str(home / ".s")}
    proc = subprocess.Popen(  # noqa: S603
        ["sh", "-c", script], stdin=subprocess.PIPE, env=env, stdout=subprocess.DEVNULL
    )
    assert proc.stdin is not None
    proc.stdin.write(b"tok-123\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 10
    while not (home / "up").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        state = home / ".s"
        assert (state / "token").read_text() == "tok-123"
        assert stat.S_IMODE((state / "token").stat().st_mode) == 0o600
        assert (state / "settings.json").read_text() == '{"s": 1}'
        assert (state / "credentials.json").read_text() == '{"c": "k"}'
        assert not (state / "mcp-oauth.json").exists()  # absent on the host, absent here
        argv = (home / "argv").read_text()
        assert f"--host 0.0.0.0 --port {sandbox.CONTAINER_PORT}" in argv
        assert "tok-123" not in argv
        time.sleep(0.5)
        assert proc.poll() is None  # an open stdin keeps the daemon up
        proc.stdin.close()  # snowpea was killed: the script ends the daemon and itself
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        subprocess.run(["pkill", "-f", "sleep 30"], check=False)  # noqa: S603, S607


# ---------------------------------------------------------------------------
# the shared pieces and the CLI wiring
# ---------------------------------------------------------------------------


def test_wait_until_ready_stops_when_the_host_of_the_daemon_is_gone() -> None:
    assert remote.wait_until_ready(1, probe=lambda _p: False, alive=lambda: False) is False
    assert remote.wait_until_ready(1, probe=healthy_after(3), interval=0.01) is True
    assert remote.wait_until_ready(1, probe=lambda _p: False, timeout=0.1, interval=0.02) is False


def test_the_tui_loop_runs_again_after_the_restart_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = iter([75, 75, 3])
    argvs: list[list[str]] = []

    def call(argv: list[str]) -> int:
        argvs.append(argv)
        return next(codes)

    monkeypatch.setattr(subprocess, "call", call)
    opened: list[int] = []

    @contextlib.contextmanager
    def session():
        opened.append(1)
        yield ["--port", "1"]

    code = remote.run_tui_loop(
        ["node", "tui.js"], session, mode="auto", inline=True, restart_code=75,
        say=lambda _t: None, restart_delay=0,
    )  # fmt: skip
    assert code == 3 and len(opened) == 3
    assert argvs[0] == ["node", "tui.js", "--port", "1", "--mode", "auto", "--inline"]


def test_the_tui_loop_reports_a_session_that_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []

    @contextlib.contextmanager
    def broken():
        raise SandboxError("docker is down")
        yield []  # pragma: no cover

    code = remote.run_tui_loop(
        ["node"], broken, mode=None, inline=False, restart_code=75, say=said.append
    )
    assert code == EXIT_NO_DAEMON and said == ["docker is down"]


def test_docker_flags_parse_with_and_without_an_image() -> None:
    parser = cli_main.build_parser()
    assert parser.parse_args(["--docker"]).docker == ""
    assert parser.parse_args(["--docker", "--cwd", "/x"]).docker == ""
    args = parser.parse_args(["--docker", "my/image:1", "--docker-volume", "v", "--docker-keep"])
    assert (args.docker, args.docker_volume, args.docker_keep) == ("my/image:1", "v", True)
    assert parser.parse_args([]).docker is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--docker", "--remote", "me@box"],
        ["--docker-volume", "v"],
        ["--docker-keep"],
        ["--docker", "-c", "hi"],
    ],
)
def test_docker_combinations_that_make_no_sense_are_usage_errors(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "launch_sandbox_tui", lambda _a: pytest.fail("must not launch"))
    monkeypatch.setattr(cli_main, "launch_remote_tui", lambda _a: pytest.fail("must not launch"))
    assert cli_main.main(argv) == EXIT_USAGE


def test_docker_alone_launches_the_sandbox_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(cli_main, "launch_sandbox_tui", lambda a: seen.append(a) or EXIT_OK)
    assert cli_main.main(["--docker", "img", "--mode", "auto"]) == EXIT_OK
    assert seen[0].docker == "img" and seen[0].mode == "auto"


def test_a_docker_that_is_down_is_exit_3_with_the_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_main, "resolve_tui_command", lambda: ["node", "tui.js"])
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox, "_run", lambda argv, capture: completed(1, "Cannot connect to the Docker daemon")
    )
    project = tmp_path / "proj"
    project.mkdir()
    code = cli_main.main(["--docker", "--cwd", str(project), "--home", str(tmp_path / "h")])
    assert code == EXIT_NO_DAEMON
    assert "Docker daemon is not running" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the real thing
# ---------------------------------------------------------------------------


def docker_usable() -> bool:
    binary = shutil.which("docker")
    if not binary or sys.platform == "win32":
        return False
    try:
        done = subprocess.run(  # noqa: S603
            [binary, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def build_wheel(into: Path) -> Path | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(into)],
        cwd=root, capture_output=True, timeout=300, check=False,
        env={**os.environ, "SNOWPEA_SKIP_TUI": "1"},
    )  # fmt: skip
    wheels = sorted(into.glob("*.whl"))
    return wheels[0] if done.returncode == 0 and wheels else None


@pytest.mark.timeout(1500)
@pytest.mark.skipif(not docker_usable(), reason="needs a working docker daemon")
async def test_a_real_sandbox_container_serves_the_daemon_through_the_published_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builds the image from a wheel of this checkout (minutes on the first run; the
    image is removed afterwards unless SNOWPEA_SANDBOX_TEST_KEEP_IMAGE is set)."""
    wheel = build_wheel(tmp_path / "wheel")
    if wheel is None:
        pytest.skip("could not build a wheel of this checkout (needs uv and network)")
    monkeypatch.setenv(sandbox.SOURCE_ENV, str(wheel))
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    (home / "credentials.json").write_text('{"probe": "kept-out-of-argv"}')
    opts = sandbox.options_from_args(
        cwd=str(project), home=str(home), image=None, volume=None, keep=False
    )
    said: list[str] = []
    box = Sandbox(opts, local_protocol=PROTOCOL_VERSION, say=said.append)
    existed = subprocess.run(  # noqa: S603, S607
        ["docker", "image", "inspect", sandbox.image_tag(
            sandbox.resolve_source(str(wheel)), sandbox.dockerfile_text()
        )],
        capture_output=True, check=False,
    ).returncode == 0  # fmt: skip
    try:
        await asyncio.to_thread(box.prepare_image)
        flags = await asyncio.to_thread(box.start)
        port, token = int(flags[flags.index("--port") + 1]), flags[flags.index("--token") + 1]
        name = box.name
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws")
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {
                        "token": token,
                        "clientVersion": "test",
                        "protocolVersion": PROTOCOL_VERSION,
                    },
                }
            )
            reply = await asyncio.wait_for(ws.receive_json(), timeout=15)
            assert reply["result"]["serverVersion"] == __version__
            await ws.close()

        def inside(script: str) -> str:
            done = subprocess.run(  # noqa: S603, S607
                ["docker", "exec", name, "sh", "-c", script],
                capture_output=True, text=True, timeout=60, check=False,
            )  # fmt: skip
            return done.stdout

        assert inside("id -u").strip() == str(os.getuid())
        assert inside("cat $HOME/.snowpea/credentials.json") == '{"probe": "kept-out-of-argv"}'
        inside(f"touch {project}/from-the-container")
        written = project / "from-the-container"
        assert written.exists() and written.stat().st_uid == os.getuid()  # the host user owns it
        ports = subprocess.run(  # noqa: S603, S607
            ["docker", "port", name], capture_output=True, text=True, check=False
        ).stdout
        assert "127.0.0.1" in ports and "0.0.0.0" not in ports
        host_ps = subprocess.run(  # noqa: S603, S607
            ["ps", "-eo", "args"], capture_output=True, text=True, check=False
        ).stdout
        assert token not in host_ps
        inspected = subprocess.run(  # noqa: S603, S607
            ["docker", "inspect", name], capture_output=True, text=True, check=False
        ).stdout
        assert token not in inspected
    finally:
        box.close()
        listed = subprocess.run(  # noqa: S603, S607
            ["docker", "ps", "-a", "--filter", "name=snowpea-sandbox-", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        ).stdout  # fmt: skip
        if box.name:
            assert box.name not in listed.split()  # --rm took it away
        if not existed and not os.environ.get("SNOWPEA_SANDBOX_TEST_KEEP_IMAGE"):
            subprocess.run(  # noqa: S603, S607
                ["docker", "rmi", "--force", box.image], capture_output=True, check=False
            )
