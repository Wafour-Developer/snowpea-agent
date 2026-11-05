"""``snowpea --docker [IMAGE]``: the whole session runs in a throwaway container.

The container runs the snowpea *daemon*, so the agent, its tools, the shell,
installs and memory all live inside.  The project directory is bind-mounted at
the same absolute path; this machine only holds the terminal, which reaches
the daemon through a port published on ``127.0.0.1`` of the host (never on
another address).  When the terminal UI exits the container is stopped and
removed.

Steps, in order:

1. check that Docker is installed and its daemon answers;
2. build the image on first use (tag = version + a digest of the Dockerfile and
   of ``SNOWPEA_SANDBOX_SOURCE``), or check ``IMAGE`` was given and use it;
3. ``docker run`` the daemon as the invoking user, with a token that travels on
   the container's stdin (never argv, never ``docker inspect``) and a read-only
   copy of the settings and credentials;
4. wait for ``/health`` through the published port, then run the terminal UI.

Nothing here prints the token.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import IO, Any, NoReturn

from snowpea_core import __version__
from snowpea_core import update as update_mod
from snowpea_core.cli import remote
from snowpea_core.cli.remote import Advert, RemoteError

IMAGE_REPOSITORY = "snowpea-sandbox"
#: A pip requirement or a path to a wheel, instead of the client's own version tag.
SOURCE_ENV = "SNOWPEA_SANDBOX_SOURCE"

#: The daemon's port inside the container; the host side is a free port.
CONTAINER_PORT = 8765
CONTAINER_HOME = "/home/snowpea"
CONTAINER_STATE = f"{CONTAINER_HOME}/.snowpea"
#: Where the host's settings land before the start script copies them into the state.
STAGING_DIR = "/run/snowpea-host"
#: Copied (read-only, one way) from the host's snowpea home into the sandbox.
HOST_FILES = ("settings.json", "credentials.json", "mcp-oauth.json")

#: How long the daemon in a fresh container gets to answer ``/health``.
READY_TIMEOUT_SEC = 90.0

_VOLUME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: ``sh -c`` script the container runs.  It reads the token from stdin (so it is
#: in no argv and no environment), copies the staged files, starts the daemon on
#: all container interfaces (the host publishes the port on loopback only) and
#: stops it again when stdin closes, which is what happens if snowpea is killed.
START_SCRIPT = f"""\
umask 077
IFS= read -r token || exit 70
home="${{SNOWPEA_HOME:-$HOME/.snowpea}}"
mkdir -p "$home" || exit 71
for f in {" ".join(HOST_FILES)}; do
  [ -f "{STAGING_DIR}/$f" ] && cp "{STAGING_DIR}/$f" "$home/$f"
done
printf %s "$token" > "$home/token"
unset token
trap 'kill -TERM "$daemon" 2>/dev/null' TERM INT
exec 3<&0
snowpea-core --host 0.0.0.0 --port {CONTAINER_PORT} --home "$home" 3<&- &
daemon=$!
( cat <&3 >/dev/null; kill -TERM "$daemon" 2>/dev/null ) &
wait "$daemon"
code=$?
if [ "$code" -gt 128 ]; then wait "$daemon"; code=$?; fi
exit "$code"
"""


class SandboxError(RemoteError):
    """The sandbox could not be set up; the message says what to do."""


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------

#: ``(argv, capture)`` → a finished process; stdout and stderr are text when captured.
DockerRun = Callable[[Sequence[str], bool], "subprocess.CompletedProcess[str]"]
#: ``(argv, stdout/stderr file)`` → the long-running ``docker run`` with a stdin pipe.
DockerPopen = Callable[[Sequence[str], IO[Any]], "subprocess.Popen[bytes]"]


def _run(argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is built here, not from user text
        list(argv),
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
        timeout=None if not capture else 120,
    )


def _popen(argv: Sequence[str], log: IO[Any]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - argv is built here, not from user text
        list(argv),
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=log,
        # The terminal's Ctrl+C belongs to the terminal UI; the container is
        # stopped on purpose, by name.
        start_new_session=True,
    )


def check_docker(binary: str | None, run: DockerRun) -> str:
    """The ``docker`` to use, once its daemon answers; :class:`SandboxError` otherwise."""
    if not binary:
        raise SandboxError(
            "Docker is not installed (no `docker` on PATH); install Docker Engine or Docker "
            "Desktop and run `snowpea --docker` again",
            code="dockerMissing",
            reason="notInstalled",
        )
    try:
        done = run([binary, "info", "--format", "{{.ServerVersion}}"], True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(
            "Docker did not answer in time; check that its daemon is running and try again",
            code="dockerMissing",
            reason="notRunning",
        ) from exc
    if done.returncode != 0:
        detail = (done.stdout or "").lower()
        if "permission denied" in detail:
            raise SandboxError(
                "this user cannot use the Docker daemon (permission denied); add yourself to the "
                "docker group (`sudo usermod -aG docker $USER`, then log in again) or use "
                "rootless Docker",
                code="dockerMissing",
                reason="denied",
            )
        raise SandboxError(
            "the Docker daemon is not running; start it (`sudo systemctl start docker`, or open "
            "Docker Desktop) and run `snowpea --docker` again",
            code="dockerMissing",
            reason="notRunning",
        )
    return binary


# ---------------------------------------------------------------------------
# the image
# ---------------------------------------------------------------------------


def dockerfile_text() -> str:
    """The Dockerfile shipped beside this module (it is package data in the wheel)."""
    try:
        return (resources.files("snowpea_core.cli") / "sandbox.Dockerfile").read_text("utf-8")
    except (OSError, ModuleNotFoundError) as exc:  # pragma: no cover - broken install
        raise SandboxError("this snowpea install is missing its sandbox.Dockerfile") from exc


@dataclass(frozen=True)
class ImageSource:
    """What the image installs: a pip requirement, and a wheel to copy in when it is one."""

    requirement: str
    wheel: Path | None = None
    digest: str = ""


def resolve_source(override: str | None = None, *, version: str = __version__) -> ImageSource:
    """The client's own version tag, or ``SNOWPEA_SANDBOX_SOURCE`` (a wheel or a requirement)."""
    override = (override or "").strip()
    if not override:
        source = remote.INSTALL_SOURCE.format(version=version)
        return ImageSource(update_mod.with_images(source), None, "")
    candidate = Path(override).expanduser()
    if candidate.is_dir():
        raise SandboxError(
            f"{SOURCE_ENV} names a directory; build a wheel first (`uv build --wheel`) and "
            "point it at the .whl file, or give a pip requirement",
            code="dockerBuild",
        )
    if candidate.is_file():
        if candidate.suffix != ".whl":
            raise SandboxError(
                f"{SOURCE_ENV} points at {candidate}, which is not a .whl file", code="dockerBuild"
            )
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()[:8]
        return ImageSource(
            update_mod.with_images(f"/tmp/wheels/{candidate.name}"), candidate.resolve(), digest
        )
    return ImageSource(
        update_mod.with_images(override), None, hashlib.sha256(override.encode()).hexdigest()[:8]
    )


def image_tag(source: ImageSource, dockerfile: str, *, version: str = __version__) -> str:
    """``snowpea-sandbox:<version>-<digest>``; the digest covers the Dockerfile and the source."""
    digest = hashlib.sha256((dockerfile + "\0" + source.digest).encode()).hexdigest()[:8]
    return f"{IMAGE_REPOSITORY}:{version}-{digest}"


# ---------------------------------------------------------------------------
# the container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxOptions:
    cwd: Path
    host_home: Path
    uid: int
    gid: int
    image: str | None = None
    volume: str | None = None
    keep: bool = False


def validate_cwd(cwd: Path) -> Path:
    """The resolved project directory, or :class:`SandboxError` for one that is unsafe to mount."""
    if sys.platform == "win32":
        raise SandboxError(
            "--docker is not available on Windows yet (the project directory is mounted at the "
            "same absolute path, which Windows drive paths cannot give a Linux container)",
            code="dockerMissing",
            reason="unsupportedPlatform",
        )
    try:
        resolved = cwd.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SandboxError(f"{cwd} does not exist") from exc
    if not resolved.is_dir():
        raise SandboxError(f"{resolved} is not a directory")
    text = str(resolved)
    if any(ch in text for ch in ',"\n\r'):
        raise SandboxError(f"{text} has a character (comma, quote or newline) docker cannot mount")
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        raise SandboxError(
            f"{text} is too wide to mount into a sandbox (the agent could change everything "
            "in it); run `snowpea --docker` from the project directory, or pass --cwd"
        )
    return resolved


def validate_volume(name: str | None) -> str | None:
    if name is None:
        return None
    if not _VOLUME.match(name):
        raise SandboxError(
            f"'{name}' is not a volume name; use letters, digits, '_', '.' and '-' (up to 64)"
        )
    return name


def host_files(home: Path) -> list[Path]:
    """The settings and credential files the host has, to be copied into the sandbox."""
    return [home / name for name in HOST_FILES if (home / name).is_file()]


def build_run_argv(
    docker: str,
    options: SandboxOptions,
    *,
    image: str,
    name: str,
    host_port: int,
    staged: Sequence[Path],
) -> list[str]:
    """``docker run`` for the sandbox.  The token is not here: it goes in on stdin."""
    cwd = str(options.cwd)
    argv = [
        docker, "run", "--interactive", "--init",
        "--name", name,
        "--user", f"{options.uid}:{options.gid}",
        "--security-opt", "no-new-privileges",
        "--publish", f"127.0.0.1:{host_port}:{CONTAINER_PORT}",
        "--mount", f"type=bind,source={cwd},target={cwd}",
        "--workdir", cwd,
    ]  # fmt: skip
    if not options.keep:
        argv.insert(2, "--rm")
    for path in staged:
        argv += [
            "--mount",
            f"type=bind,source={path},target={STAGING_DIR}/{path.name},readonly",
        ]
    if options.volume:
        argv += ["--mount", f"type=volume,source={options.volume},target={CONTAINER_STATE}"]
    argv += [
        "--env", f"HOME={CONTAINER_HOME}",
        "--env", f"SNOWPEA_HOME={CONTAINER_STATE}",
        "--env", "SNOWPEA_SANDBOX=1",
        "--entrypoint", "sh",
        image, "-c", START_SCRIPT,
    ]  # fmt: skip
    return argv


def tail(log: IO[bytes], lines: int = 15) -> str:
    """The last lines of the container's output, for an error message."""
    try:
        log.seek(0)
        text = log.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


class Sandbox:
    """One container at a time, started on demand and removed when :meth:`close` runs."""

    def __init__(
        self,
        options: SandboxOptions,
        *,
        local_protocol: str,
        docker: str | None = None,
        run: DockerRun | None = None,
        popen: DockerPopen | None = None,
        say: Callable[[str], None] | None = None,
        ready_timeout: float = READY_TIMEOUT_SEC,
        probe: Callable[[int], bool] = remote.daemon_healthy,
    ) -> None:
        self.options = options
        self.local_protocol = local_protocol
        self._docker = docker if docker is not None else shutil.which("docker")
        self._run: DockerRun = run or _run
        self._popen: DockerPopen = popen or _popen
        self._say = say or (lambda text: print(f"snowpea: {text}", file=sys.stderr))
        self._ready_timeout = ready_timeout
        self._probe = probe
        self.image = ""
        self.name = ""
        self.host_port = 0
        self._token = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None

    # -- image -----------------------------------------------------------

    def prepare_image(self) -> str:
        """Check Docker, then build (first use) or verify the image to run."""
        docker = check_docker(self._docker, self._run)
        self._docker = docker
        if self.options.image:
            self.image = self.options.image
            return self.image
        source = resolve_source(os.environ.get(SOURCE_ENV))
        dockerfile = dockerfile_text()
        tag = image_tag(source, dockerfile)
        if self._run([docker, "image", "inspect", tag], True).returncode == 0:
            self.image = tag
            return tag
        self._say(f"building the sandbox image {tag} (first use only; this takes a few minutes)")
        with tempfile.TemporaryDirectory(prefix="snowpea-sandbox-") as context:
            (Path(context) / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            wheels = Path(context) / "wheels"
            wheels.mkdir()
            (wheels / "README").write_text("local wheels for the sandbox image\n")
            if source.wheel is not None:
                shutil.copy2(source.wheel, wheels / source.wheel.name)
            done = self._run(
                [docker, "build", "--tag", tag,
                 "--build-arg", f"SNOWPEA_REQUIREMENT={source.requirement}", context],
                False,
            )  # fmt: skip
        if done.returncode != 0:
            raise SandboxError(
                f"building the sandbox image failed (docker build exit {done.returncode}); if "
                f"snowpea {__version__} is a development version with no release tag, point "
                f"{SOURCE_ENV} at a wheel (`uv build --wheel`) or a pip requirement",
                code="dockerBuild",
            )
        self.image = tag
        return tag

    # -- container -------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def label(self) -> str:
        return f"docker:{self.name}"

    @property
    def token(self) -> str:
        """The daemon's token (for a caller that hands it to the client itself)."""
        return self._token

    def start(self) -> list[str]:
        """Start the container and return the terminal UI's flags for it."""
        assert self.image, "prepare_image() first"
        assert self._docker
        self.stop()
        self.name = f"snowpea-sandbox-{secrets.token_hex(4)}"
        self.host_port = remote.free_port()
        self._token = secrets.token_urlsafe(32)
        argv = build_run_argv(
            self._docker,
            self.options,
            image=self.image,
            name=self.name,
            host_port=self.host_port,
            staged=host_files(self.options.host_home),
        )
        self._log = tempfile.TemporaryFile()  # noqa: SIM115 - closed in stop()
        self._say(f"starting the sandbox container {self.name}")
        try:
            self._proc = self._popen(argv, self._log)
            assert self._proc.stdin is not None
            self._proc.stdin.write(self._token.encode() + b"\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"could not start the sandbox container ({exc})")
        if not remote.wait_until_ready(
            self.host_port,
            alive=lambda: self.alive,
            timeout=self._ready_timeout,
            probe=self._probe,
        ):
            self._fail("the daemon in the sandbox container did not come up")
        self._check_version()
        return remote.tui_flags(
            port=self.host_port,
            token=self._token,
            cwd=str(self.options.cwd),
            label=self.label,
        )

    def _check_version(self) -> None:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{self.host_port}/version", timeout=3
            ) as reply:
                data = json.loads(reply.read(4096))
        except (OSError, ValueError):
            return
        advert = Advert(
            port=self.host_port,
            token="",
            version=str(data.get("version") or ""),
            protocol=str(data.get("protocolVersion") or ""),
            cwd="",
        )
        try:
            warning = remote.check_compatible(advert, self.local_protocol)
        except RemoteError as exc:
            self.stop()
            raise SandboxError(
                f"{exc} (image {self.image})", code=exc.code, reason=exc.reason
            ) from exc
        if warning:
            self._say(warning)

    def _fail(self, message: str) -> NoReturn:
        output = tail(self._log) if self._log is not None else ""
        code = self._proc.poll() if self._proc is not None else None
        self.stop()
        parts = [message + (f" (docker exited {code})" if code is not None else "")]
        if output:
            parts.append(output)
        if self.options.image:
            parts.append(
                f"{self.options.image} needs `snowpea-core` on PATH, `sh`, and a writable "
                f"{CONTAINER_STATE}; building FROM the image snowpea builds gives all three"
            )
        raise SandboxError("\n".join(parts), reason="containerFailed")

    def stop(self) -> None:
        """Stop the container (and remove it unless it is kept)."""
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(OSError, ValueError):
                if proc.stdin is not None:
                    proc.stdin.close()  # the start script stops the daemon on EOF
            if proc.poll() is None and self._docker and self.name:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    self._run([self._docker, "stop", "--time", "5", self.name], True)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._docker and self.name and not self.options.keep:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                self._run([self._docker, "rm", "--force", "--volumes", self.name], True)
        if self._log is not None:
            self._log.close()
            self._log = None

    # -- the loop's view ---------------------------------------------------

    @contextlib.contextmanager
    def open_session(self) -> Iterator[list[str]]:
        """The running container's flags, starting one when there is none.

        It is left running when the terminal UI exits with the restart code,
        so the next turn of the loop finds the same container; :meth:`close`
        ends it.
        """
        if self.alive and self._probe(self.host_port):
            yield remote.tui_flags(
                port=self.host_port,
                token=self._token,
                cwd=str(self.options.cwd),
                label=self.label,
            )
        else:
            yield self.start()

    def close(self) -> None:
        name, keep = self.name, self.options.keep
        self.stop()
        if keep and name:
            self._say(
                f"kept the stopped container {name}; `docker cp {name}:<path> .` copies files out, "
                f"`docker rm {name}` deletes it"
            )
        if self.options.volume:
            self._say(
                f"the state volume {self.options.volume} is kept; "
                f"`docker volume rm {self.options.volume}` deletes it"
            )


def options_from_args(
    *, cwd: str | None, home: str | None, image: str | None, volume: str | None, keep: bool
) -> SandboxOptions:
    from snowpea_core.config.paths import resolve_home

    if not hasattr(os, "getuid"):
        raise SandboxError("--docker needs a POSIX host (it runs the container as your user id)")
    return SandboxOptions(
        cwd=validate_cwd(Path(cwd) if cwd else Path.cwd()),
        host_home=Path(resolve_home(home)),
        uid=os.getuid(),
        gid=os.getgid(),
        image=image or None,
        volume=validate_volume(volume),
        keep=keep,
    )


__all__ = [
    "CONTAINER_PORT",
    "SOURCE_ENV",
    "START_SCRIPT",
    "Sandbox",
    "SandboxError",
    "SandboxOptions",
    "build_run_argv",
    "check_docker",
    "image_tag",
    "options_from_args",
    "resolve_source",
    "validate_cwd",
    "validate_volume",
]
