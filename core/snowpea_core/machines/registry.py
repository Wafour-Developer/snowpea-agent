"""Local registry of remote SSH targets (M40)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.cli.remote import RemoteError, RemoteTarget, parse_target
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.util.atomic import write_atomic

_UUID = uuid.UUID


def _bad_target_char(text: str) -> bool:
    return not text or text.startswith("-") or any(c.isspace() or ord(c) < 32 for c in text)


def _validate_record_target(host: str, user: str | None, port: int | None) -> RemoteTarget | None:
    if _bad_target_char(host) or (user is not None and _bad_target_char(user)):
        return None
    label = f"{user}@{host}" if user else host
    if port:
        label = f"{label}:{port}"
    try:
        return parse_target(label)
    except RemoteError:
        return None


def _valid_machine_id(machine_id: str) -> bool:
    try:
        _UUID(str(machine_id))
    except (ValueError, AttributeError):
        return False
    return True


def validate_key_path(path: str | None) -> str | None:
    """Refuse paths that could break ssh argv or point outside a regular key file."""
    if path is None or path == "":
        return None
    text = str(path)
    if not os.path.isabs(text):
        raise RpcError(errors.INVALID_PARAMS, "keyPath must be absolute", {"field": "keyPath"})
    if text.startswith("-") or "\n" in text or "\r" in text or "=" in text:
        raise RpcError(errors.INVALID_PARAMS, "keyPath is not a safe path", {"field": "keyPath"})
    try:
        st = os.lstat(text)
    except OSError as exc:
        raise RpcError(
            errors.INVALID_PARAMS, f"keyPath does not exist: {text}", {"field": "keyPath"}
        ) from exc
    if not stat.S_ISREG(st.st_mode):
        raise RpcError(
            errors.INVALID_PARAMS, "keyPath must be a regular file", {"field": "keyPath"}
        )
    return text

REGISTRY_NAME = "machines.json"
KEY_NAME = "id_key"


@dataclass
class MachineRecord:
    id: str
    label: str
    user: str | None
    host: str
    port: int | None
    key_path: str | None = None
    pasted_key: bool = False
    remote_cwd: str | None = None
    auto_connect: bool = False
    jarvis_allowed: bool = False
    session_view: str | None = None
    status: str = "off"
    error: str | None = None

    def target(self) -> RemoteTarget:
        parsed = _validate_record_target(self.host, self.user, self.port)
        if parsed is None:
            raise RpcError(
                errors.INVALID_PARAMS,
                "machine target is not safe to connect to",
                {"field": "host"},
            )
        return parsed

    def to_row(self, **extra: Any) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "user": self.user,
            "host": self.host,
            "port": self.port,
            "keyPath": self.key_path,
            "pastedKey": self.pasted_key,
            "remoteCwd": self.remote_cwd,
            "autoConnect": self.auto_connect,
            "jarvisAllowed": self.jarvis_allowed,
            "sessionView": self.session_view,
            "status": self.status,
            "error": self.error,
            **extra,
        }


@dataclass
class MachineRegistry:
    home: Path
    records: dict[str, MachineRecord] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.home / REGISTRY_NAME

    @property
    def dir(self) -> Path:
        return self.home / "machines"

    def load(self) -> None:
        self.records.clear()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("machines") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            ident = str(item["id"])
            if not _valid_machine_id(ident):
                continue
            key_path = item.get("keyPath")
            if key_path:
                try:
                    key_path = validate_key_path(str(key_path))
                except RpcError:
                    key_path = None
            host = str(item["host"])
            user = item.get("user")
            port = int(item["port"]) if item.get("port") else None
            target = _validate_record_target(host, user, port)
            if target is None:
                continue
            rec = MachineRecord(
                id=ident,
                label=str(item.get("label") or target.label),
                user=target.user,
                host=target.host,
                port=target.port,
                key_path=key_path,
                pasted_key=bool(item.get("pastedKey")),
                remote_cwd=item.get("remoteCwd"),
                auto_connect=bool(item.get("autoConnect")),
                jarvis_allowed=bool(item.get("jarvisAllowed")),
                session_view=item.get("sessionView"),
            )
            self.records[rec.id] = rec

    def save(self) -> None:
        payload = {
            "machines": [
                {
                    "id": rec.id,
                    "label": rec.label,
                    "user": rec.user,
                    "host": rec.host,
                    "port": rec.port,
                    "keyPath": rec.key_path,
                    "pastedKey": rec.pasted_key,
                    "remoteCwd": rec.remote_cwd,
                    "autoConnect": rec.auto_connect,
                    "jarvisAllowed": rec.jarvis_allowed,
                    "sessionView": rec.session_view,
                }
                for rec in self.records.values()
            ]
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        write_atomic(self.path, text.encode("utf-8"), mode=0o600)
        self._ensure_dir()
        self._check_modes()

    def _check_modes(self) -> None:
        if self.path.is_file():
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode != 0o600:
                os.chmod(self.path, 0o600)
        if self.dir.is_dir():
            mode = stat.S_IMODE(self.dir.stat().st_mode)
            if mode != 0o700:
                os.chmod(self.dir, 0o700)

    def add(
        self,
        target_text: str,
        *,
        label: str | None,
        key_path: str | None,
        pasted_key: str | None,
        remote_cwd: str | None,
        auto_connect: bool,
    ) -> MachineRecord:
        target = parse_target(target_text)
        ident = str(uuid.uuid4())
        key_path = validate_key_path(key_path)
        if pasted_key and not pasted_key.endswith("\n"):
            pasted_key = pasted_key + "\n"
        rec = MachineRecord(
            id=ident,
            label=label or target.label,
            user=target.user,
            host=target.host,
            port=target.port,
            key_path=key_path,
            pasted_key=bool(pasted_key and not key_path),
            remote_cwd=remote_cwd,
            auto_connect=auto_connect,
        )
        if pasted_key and not key_path:
            self._ensure_dir()
            key_dir = self.dir / ident
            key_dir.mkdir(mode=0o700)
            key_file = key_dir / KEY_NAME
            write_atomic(key_file, pasted_key.encode("utf-8"), mode=0o600)
        self.records[ident] = rec
        self.save()
        return rec

    def remove(self, machine_id: str) -> MachineRecord | None:
        if not _valid_machine_id(machine_id):
            return None
        rec = self.records.pop(machine_id, None)
        if rec is None:
            return None
        self.save()
        key_dir = self.dir / machine_id
        try:
            st = os.lstat(key_dir)
        except OSError:
            return rec
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return rec
        if key_dir.resolve().parent != self.dir.resolve():
            return rec
        for child in key_dir.iterdir():
            if not child.is_file():
                continue
            child.unlink(missing_ok=True)
        key_dir.rmdir()
        return rec

    def get(self, machine_id: str) -> MachineRecord | None:
        return self.records.get(machine_id)

    def _ensure_dir(self) -> None:
        parent = self.dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self.dir.exists():
            if self.dir.is_symlink():
                raise RpcError(errors.UNAVAILABLE, "machines folder may not be a symlink")
            if not self.dir.is_dir():
                raise RpcError(errors.UNAVAILABLE, "machines path is not a directory")
        else:
            self.dir.mkdir(mode=0o700)
        if self.dir.is_dir():
            mode = stat.S_IMODE(self.dir.stat().st_mode)
            if mode != 0o700:
                os.chmod(self.dir, 0o700)

    def known_hosts_file(self) -> Path:
        self._ensure_dir()
        path = self.dir / "known_hosts"
        if not path.is_file():
            path.touch(mode=0o600)
        return path

    def ssh_identity(self, rec: MachineRecord) -> list[str]:
        """OpenSSH ``-o`` options only (not ``-i`` argv entries)."""
        return ["IdentitiesOnly=yes"]

    def ssh_extra_args(self, rec: MachineRecord) -> list[str]:
        """Separate ssh argv entries (e.g. ``-i``) for :meth:`RemoteSession.ssh_argv`."""
        if rec.pasted_key:
            path = self.dir / rec.id / KEY_NAME
            if path.is_file():
                return ["-i", str(path.resolve())]
        if rec.key_path:
            return ["-i", validate_key_path(rec.key_path) or rec.key_path]
        return []


__all__ = ["MachineRegistry", "MachineRecord", "REGISTRY_NAME", "validate_key_path"]