"""Build an :class:`~snowpea_core.exec.backend.ExecutionBackend` from a kind + config.

One place knows how ``backend.set`` params and ``/backend <kind> {json}`` turn
into a live backend, so the RPC handler and the command cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.exec.docker import DockerBackend, DockerConfig
from snowpea_core.exec.local import LocalBackend
from snowpea_core.exec.ssh import SshBackend, SshConfig

#: The kinds ``/backend`` and ``backend.set`` accept.
BACKEND_KINDS: tuple[str, ...] = ("local", "docker", "ssh")


def build_backend(
    kind: str,
    config: dict[str, Any] | None = None,
    *,
    workdir: str | Path,
    session_id: str = "local",
) -> ExecutionBackend:
    """Create a backend of ``kind``; raises :class:`ValueError` for anything else."""
    settings = dict(config or {})
    if kind == "local":
        return LocalBackend(settings.get("workdir") or workdir)
    if kind == "docker":
        return DockerBackend(
            DockerConfig.from_dict(settings, workdir=workdir),
            session_id=session_id,
            workdir=workdir,
            host_workdir=settings.get("hostWorkdir") or workdir,
        )
    if kind == "ssh":
        return SshBackend(SshConfig.from_dict(settings))
    raise ValueError(f"unknown backend kind: {kind!r} (use one of {', '.join(BACKEND_KINDS)})")


__all__ = ["BACKEND_KINDS", "build_backend"]
