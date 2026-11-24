"""Local registry of remote SSH targets (M40)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.cli.remote import RemoteTarget, parse_target
from snowpea_core.util.atomic import write_atomic

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
    status: str = "off"
    error: str | None = None

    def target(self) -> RemoteTarget:
        text = self.label if "@" in self.label else ""
        if self.user:
            text = f"{self.user}@{self.host}"
            if self.port:
                text += f":{self.port}"
        else:
            text = f"{self.host}" + (f":{self.port}" if self.port else "")
        return RemoteTarget(host=self.host, user=self.user, port=self.port)

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
            rec = MachineRecord(
                id=str(item["id"]),
                label=str(item.get("label") or item.get("host") or ""),
                user=item.get("user"),
                host=str(item["host"]),
                port=int(item["port"]) if item.get("port") else None,
                key_path=item.get("keyPath"),
                pasted_key=bool(item.get("pastedKey")),
                remote_cwd=item.get("remoteCwd"),
                auto_connect=bool(item.get("autoConnect")),
                jarvis_allowed=bool(item.get("jarvisAllowed")),
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
                }
                for rec in self.records.values()
            ]
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        write_atomic(self.path, text.encode("utf-8"), mode=0o600)
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
            key_dir = self.dir / ident
            key_dir.mkdir(parents=True, mode=0o700)
            key_file = key_dir / KEY_NAME
            write_atomic(key_file, pasted_key.encode("utf-8"), mode=0o600)
        self.records[ident] = rec
        self.save()
        return rec

    def remove(self, machine_id: str) -> MachineRecord | None:
        rec = self.records.pop(machine_id, None)
        if rec is None:
            return None
        self.save()
        key_dir = self.dir / machine_id
        if key_dir.is_dir():
            for child in key_dir.iterdir():
                child.unlink(missing_ok=True)
            key_dir.rmdir()
        return rec

    def get(self, machine_id: str) -> MachineRecord | None:
        return self.records.get(machine_id)

    def known_hosts_file(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.dir / "known_hosts"
        if not path.is_file():
            path.touch(mode=0o600)
        return path

    def ssh_identity(self, rec: MachineRecord) -> list[str]:
        out = ["IdentitiesOnly=yes"]
        if rec.pasted_key:
            path = self.dir / rec.id / KEY_NAME
            if path.is_file():
                out.extend(["-i", str(path)])
                return out
        if rec.key_path:
            out.extend(["-i", rec.key_path])
        return out