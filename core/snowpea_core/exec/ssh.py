"""SSH execution backend: tools run on a remote host over one asyncssh session.

Commands go through ``conn.run("cd <cwd> && <command>")``; file operations use
SFTP on the same connection.  The connection is opened lazily and reused.

Security note: ``known_hosts`` defaults to ``None``, which disables host-key
verification.  That is what the throwaway container fixture in
``tests/fixtures/ssh`` needs, and it is fine for a host you already trust on a
private network.  For anything else pass ``knownHosts`` in the backend config
(a path to a ``known_hosts`` file) so the server identity is actually checked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any

from snowpea_core.exec.backend import (
    DEFAULT_TIMEOUT,
    ExecResult,
    remote_resolve,
    truncate_output,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncssh

log = logging.getLogger("snowpea.exec.ssh")

DEFAULT_PORT = 22


class SshError(RuntimeError):
    """The remote host could not be reached or refused the operation."""


@dataclass
class SshConfig:
    """Settings accepted by ``backend.set`` / ``/backend ssh {…}``."""

    host: str = "localhost"
    port: int = DEFAULT_PORT
    user: str | None = None
    key: str | None = None
    password: str | None = None
    cwd: str | None = None
    known_hosts: str | None = None

    @classmethod
    def from_dict(cls, config: dict[str, Any] | None) -> SshConfig:
        data = dict(config or {})
        known = data.get("knownHosts") or data.get("known_hosts")
        return cls(
            host=str(data.get("host") or "localhost"),
            port=int(data.get("port") or DEFAULT_PORT),
            user=str(data["user"]) if data.get("user") else None,
            key=str(data["key"]) if data.get("key") else None,
            password=str(data["password"]) if data.get("password") else None,
            cwd=str(data["cwd"]) if data.get("cwd") else None,
            known_hosts=str(known) if known else None,
        )

    def target(self) -> str:
        who = f"{self.user}@" if self.user else ""
        return f"{who}{self.host}:{self.port}"


class SshBackend:
    """Runs everything on a remote host through one asyncssh connection."""

    kind = "ssh"

    def __init__(self, config: SshConfig | dict[str, Any] | None = None) -> None:
        self.config = config if isinstance(config, SshConfig) else SshConfig.from_dict(config)
        self._cwd = PurePosixPath(self.config.cwd or "~")
        self._conn: asyncssh.SSHClientConnection | None = None
        self._sftp: asyncssh.SFTPClient | None = None

    # -- paths ---------------------------------------------------------
    @property
    def cwd(self) -> PurePath:
        return self._cwd

    def resolve(self, path: str | PurePath) -> PurePath:
        text = str(path)
        if text.startswith("~"):
            return PurePosixPath(text)
        if str(self._cwd).startswith("~") and not text.startswith("/"):
            return PurePosixPath(str(self._cwd)) / text
        return remote_resolve(self._cwd, text)

    # -- connection ----------------------------------------------------
    async def _connect(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None:
            return self._conn
        try:
            import asyncssh
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SshError("the ssh backend needs the 'asyncssh' package") from exc
        options: dict[str, Any] = {
            "host": self.config.host,
            "port": self.config.port,
            "known_hosts": self.config.known_hosts,
        }
        if self.config.user:
            options["username"] = self.config.user
        if self.config.key:
            options["client_keys"] = [self.config.key]
        if self.config.password:
            options["password"] = self.config.password
        try:
            self._conn = await asyncssh.connect(**options)
        except Exception as exc:  # noqa: BLE001 - any failure is one error to the caller
            raise SshError(f"cannot connect to {self.config.target()}: {exc}") from exc
        log.info("ssh backend connected to %s", self.config.target())
        return self._conn

    async def _sftp_client(self) -> asyncssh.SFTPClient:
        if self._sftp is None:
            conn = await self._connect()
            self._sftp = await conn.start_sftp_client()
        return self._sftp

    # -- ExecutionBackend ----------------------------------------------
    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        conn = await self._connect()
        import asyncssh

        workdir = self.resolve(cwd) if cwd else self._cwd
        script = f"cd {workdir} && {command}"
        try:
            result = await conn.run(script, timeout=timeout, check=False, env=env or {})
        except asyncssh.ProcessError as exc:  # pragma: no cover - check=False
            return ExecResult(
                exit_code=exc.exit_status or 1,
                stdout=truncate_output(str(exc.stdout or "")),
                stderr=truncate_output(str(exc.stderr or "")),
            )
        except TimeoutError:
            return ExecResult(
                exit_code=124,
                stderr=f"command timed out after {timeout:g}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=int(result.exit_status or 0),
            stdout=truncate_output(_text(result.stdout)),
            stderr=truncate_output(_text(result.stderr)),
        )

    async def read_file(self, path: str) -> str:
        sftp = await self._sftp_client()
        async with sftp.open(str(self.resolve(path)), "rb") as handle:
            raw = await handle.read()
        return _text(raw)

    async def write_file(self, path: str, content: str) -> None:
        sftp = await self._sftp_client()
        target = self.resolve(path)
        parent = str(target.parent)
        if not await sftp.isdir(parent):
            await sftp.makedirs(parent, exist_ok=True)
        async with sftp.open(str(target), "wb") as handle:
            await handle.write(content.encode("utf-8"))

    async def read_bytes(self, path: str) -> bytes:
        sftp = await self._sftp_client()
        async with sftp.open(str(self.resolve(path)), "rb") as handle:
            raw = await handle.read()
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    async def write_bytes(self, path: str, content: bytes) -> None:
        sftp = await self._sftp_client()
        target = self.resolve(path)
        parent = str(target.parent)
        if not await sftp.isdir(parent):
            await sftp.makedirs(parent, exist_ok=True)
        async with sftp.open(str(target), "wb") as handle:
            await handle.write(content)

    async def remove_file(self, path: str) -> None:
        sftp = await self._sftp_client()
        target = str(self.resolve(path))
        if await sftp.exists(target):
            await sftp.remove(target)

    async def exists(self, path: str) -> bool:
        sftp = await self._sftp_client()
        return bool(await sftp.exists(str(self.resolve(path))))

    async def list_dir(self, path: str) -> list[str]:
        sftp = await self._sftp_client()
        target = str(self.resolve(path or "."))
        names = [name for name in await sftp.listdir(target) if name not in (".", "..")]
        entries: list[str] = []
        for name in names:
            is_dir = await sftp.isdir(f"{target}/{name}")
            entries.append(name + ("/" if is_dir else ""))
        return sorted(entries)

    async def close(self) -> None:
        if self._sftp is not None:
            self._sftp.exit()
            self._sftp = None
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
            log.info("ssh backend disconnected from %s", self.config.target())


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


__all__ = ["DEFAULT_PORT", "SshBackend", "SshConfig", "SshError"]
