"""Docker execution backend: tools run inside a per-session container.

The container is created lazily on the first call (``docker run -d … sleep
infinity``) with the session's workdir bind-mounted at the *same* absolute path,
so paths a model saw locally keep working.  ``close()`` removes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from snowpea_core.exec.backend import (
    DEFAULT_TIMEOUT,
    ExecResult,
    remote_resolve,
    truncate_output,
)

log = logging.getLogger("snowpea.exec.docker")

#: Image used when the backend config does not name one.
DEFAULT_IMAGE = "python:3.11-slim"


class DockerError(RuntimeError):
    """The ``docker`` CLI refused to do something we need."""


@dataclass
class DockerConfig:
    """Settings accepted by ``backend.set`` / ``/backend docker {…}``."""

    image: str = DEFAULT_IMAGE
    workdir: str = "/workspace"
    container_name: str | None = None
    mounts: list[str] = field(default_factory=list)
    docker_bin: str = "docker"

    @classmethod
    def from_dict(cls, config: dict[str, Any] | None, *, workdir: str | Path) -> DockerConfig:
        data = dict(config or {})
        return cls(
            image=str(data.get("image") or DEFAULT_IMAGE),
            workdir=str(data.get("workdir") or workdir),
            container_name=str(data.get("containerName") or data.get("container_name") or "")
            or None,
            mounts=[str(m) for m in data.get("mounts", [])],
            docker_bin=str(data.get("docker") or "docker"),
        )


def container_name_for(session_id: str) -> str:
    """``snowpea-<sessionId[:8]>`` with the ``s-`` prefix kept off the name."""
    stem = session_id.removeprefix("s-")[:8] or "session"
    return f"snowpea-{stem}"


class DockerBackend:
    """Runs everything through ``docker exec`` into one long-lived container."""

    kind = "docker"

    def __init__(
        self,
        config: DockerConfig | dict[str, Any] | None = None,
        *,
        session_id: str = "local",
        workdir: str | Path = "/workspace",
        host_workdir: str | Path | None = None,
    ) -> None:
        self.config = (
            config
            if isinstance(config, DockerConfig)
            else DockerConfig.from_dict(config, workdir=workdir)
        )
        self._cwd = PurePosixPath(self.config.workdir)
        host_root = host_workdir if host_workdir is not None else workdir
        self._host_workdir = Path(host_root).expanduser()
        self.container = self.config.container_name or container_name_for(session_id)
        self._started = False
        self._lock = asyncio.Lock()

    # -- paths ---------------------------------------------------------
    @property
    def cwd(self) -> PurePath:
        return self._cwd

    def resolve(self, path: str | PurePath) -> PurePath:
        return remote_resolve(self._cwd, path)

    # -- docker CLI ----------------------------------------------------
    async def _docker(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ExecResult:
        process = await asyncio.create_subprocess_exec(
            self.config.docker_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(stdin), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            return ExecResult(
                exit_code=124,
                stderr=f"docker command timed out after {timeout:g}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=truncate_output(raw_out.decode("utf-8", "replace")),
            stderr=truncate_output(raw_err.decode("utf-8", "replace")),
        )

    async def _ensure_container(self) -> None:
        """Start the container once; reuse it if a previous session left it up."""
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            running = await self._docker(
                "inspect", "-f", "{{.State.Running}}", self.container, timeout=30
            )
            if running.ok and running.stdout.strip() == "true":
                self._started = True
                return
            if running.ok:  # exists but stopped — replace it
                await self._docker("rm", "-f", self.container, timeout=60)
            args = [
                "run",
                "-d",
                "--name",
                self.container,
                "-v",
                f"{self._host_workdir}:{self._cwd}",
                "-w",
                str(self._cwd),
            ]
            for mount in self.config.mounts:
                args += ["-v", mount]
            args += [self.config.image, "sleep", "infinity"]
            created = await self._docker(*args, timeout=300)
            if not created.ok:
                raise DockerError(
                    f"could not start container {self.container}: "
                    f"{created.stderr.strip() or created.stdout.strip()}"
                )
            log.info("docker backend started container %s (%s)", self.container, self.config.image)
            self._started = True

    async def _exec(
        self,
        script: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        await self._ensure_container()
        args = ["exec"]
        if stdin is not None:
            args.append("-i")
        args += ["-w", str(self.resolve(cwd) if cwd else self._cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += [self.container, "sh", "-lc", script]
        return await self._docker(*args, stdin=stdin, timeout=timeout)

    # -- ExecutionBackend ----------------------------------------------
    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return await self._exec(command, cwd=cwd, timeout=timeout, env=env)

    async def read_file(self, path: str) -> str:
        target = self.resolve(path)
        result = await self._exec(f"cat -- {shlex.quote(str(target))}")
        if not result.ok:
            raise FileNotFoundError(result.stderr.strip() or f"cannot read {target}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        target = self.resolve(path)
        quoted = shlex.quote(str(target))
        script = f"mkdir -p -- {shlex.quote(str(target.parent))} && cat > {quoted}"
        result = await self._exec(script, stdin=content.encode("utf-8"))
        if not result.ok:
            raise OSError(result.stderr.strip() or f"cannot write {target}")

    async def exists(self, path: str) -> bool:
        target = self.resolve(path)
        result = await self._exec(f"test -e {shlex.quote(str(target))}")
        return result.ok

    async def list_dir(self, path: str) -> list[str]:
        target = self.resolve(path or ".")
        quoted = shlex.quote(str(target))
        script = (
            f"cd {quoted} && for e in $(ls -1A); do "
            'if [ -d "$e" ]; then echo "$e/"; else echo "$e"; fi; done'
        )
        result = await self._exec(script)
        if not result.ok:
            raise FileNotFoundError(result.stderr.strip() or f"cannot list {target}")
        return sorted(line for line in result.stdout.splitlines() if line)

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._docker("rm", "-f", self.container, timeout=60)
        log.info("docker backend removed container %s", self.container)


__all__ = [
    "DEFAULT_IMAGE",
    "DockerBackend",
    "DockerConfig",
    "DockerError",
    "container_name_for",
]
