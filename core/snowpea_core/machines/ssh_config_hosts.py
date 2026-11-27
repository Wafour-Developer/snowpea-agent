"""Parse ``~/.ssh/config`` (and safe ``Include``s) into concrete host aliases."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.machines.ssh import _expand_home

MAX_CONFIG_BYTES = 256 * 1024
MAX_HOSTS = 500


@dataclass
class _HostBlock:
    aliases: list[str] = field(default_factory=list)
    host_name: str | None = None
    user: str | None = None
    port: int | None = None
    identity_files: list[str] = field(default_factory=list)


def _under_ssh(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _concrete_host_token(text: str) -> bool:
    token = (text or "").strip()
    if not token or token.startswith("!"):
        return False
    return "*" not in token and "?" not in token


def _split_args(value: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in value.strip():
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch.isspace():
            if cur:
                out.append("".join(cur))
                cur.clear()
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _safe_config_path(raw: str, base_dir: Path, ssh_root: Path) -> Path | None:
    expanded = _expand_home(raw.strip())
    path = Path(expanded)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    if not _under_ssh(path, ssh_root):
        return None
    if path.is_symlink():
        target = path.resolve()
        if not _under_ssh(target, ssh_root):
            return None
        path = target
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return None
    except OSError:
        return None
    return path


def _expand_include(pattern: str, base_dir: Path, ssh_root: Path) -> list[Path]:
    if any(ch in pattern for ch in ("*", "?", "[", "{")):
        parent = base_dir
        try:
            import glob

            if pattern.startswith("~"):
                glob_target = str(_expand_home(pattern))
            else:
                glob_target = str(parent / pattern)
            matches = glob.glob(glob_target)
        except OSError:
            return []
        paths: list[Path] = []
        for match in matches:
            safe = _safe_config_path(match, parent, ssh_root)
            if safe is not None:
                paths.append(safe)
        return paths
    safe = _safe_config_path(pattern, base_dir, ssh_root)
    return [safe] if safe is not None else []


def _read_lines(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > MAX_CONFIG_BYTES:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _parse_file(
    path: Path,
    ssh_root: Path,
    *,
    seen: set[Path],
    hosts_out: list[_HostBlock],
    host_count: list[int],
) -> None:
    if path in seen or host_count[0] >= MAX_HOSTS:
        return
    seen.add(path)
    in_match = False
    current: _HostBlock | None = None
    base_dir = path.parent

    def finish() -> None:
        nonlocal current
        if current is None or not current.aliases:
            return
        for alias in current.aliases:
            if host_count[0] >= MAX_HOSTS:
                break
            hosts_out.append(
                _HostBlock(
                    aliases=[alias],
                    host_name=current.host_name,
                    user=current.user,
                    port=current.port,
                    identity_files=list(current.identity_files),
                )
            )
            host_count[0] += 1
        current = None

    for line in _read_lines(path):
        if in_match and line and line[:1].isspace():
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(" ")
        key_l = key.lower()
        val = value.strip()
        if key_l == "match":
            finish()
            in_match = True
            current = None
            continue
        if key_l == "include":
            if in_match:
                continue
            for inc in _split_args(val):
                for child in _expand_include(inc, base_dir, ssh_root):
                    _parse_file(
                        child,
                        ssh_root,
                        seen=seen,
                        hosts_out=hosts_out,
                        host_count=host_count,
                    )
            continue
        if key_l == "host":
            in_match = False
            finish()
            tokens = [t for t in _split_args(val) if _concrete_host_token(t)]
            current = _HostBlock(aliases=tokens)
            continue
        if current is None:
            continue
        if key_l == "hostname":
            current.host_name = val
        elif key_l == "user":
            current.user = val
        elif key_l == "port":
            try:
                port = int(val)
                current.port = port if 0 < port < 65536 else current.port
            except ValueError:
                pass
        elif key_l == "identityfile":
            current.identity_files.append(_expand_home(val))
    finish()


def list_ssh_config_hosts(ssh_dir: Path) -> list[dict[str, Any]]:
    """Concrete ``Host`` aliases and their connection fields."""
    if not ssh_dir.is_dir():
        return []
    ssh_root = ssh_dir.resolve()
    config = ssh_dir / "config"
    if not config.is_file():
        return []
    safe = _safe_config_path(str(config), ssh_dir, ssh_root)
    if safe is None:
        return []
    hosts: list[_HostBlock] = []
    _parse_file(safe, ssh_root, seen=set(), hosts_out=hosts, host_count=[0])
    rows: list[dict[str, Any]] = []
    for block in hosts:
        alias = block.aliases[0]
        identity = block.identity_files[-1] if block.identity_files else None
        rows.append(
            {
                "alias": alias,
                "hostName": block.host_name,
                "user": block.user,
                "port": block.port,
                "identityFile": identity,
            }
        )
    return rows


def drop_known_hosts_for_labels(path: Path, labels: set[str]) -> None:
    """Remove ``known_hosts`` lines whose first token is in ``labels``."""
    if not labels or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return
    kept: list[str] = []
    for line in lines:
        parts = line.split(maxsplit=1)
        if not parts or parts[0] not in labels:
            kept.append(line)
    if kept == lines:
        return
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    try:
        path.write_text(text, encoding="utf-8")
        if sys_platform_not_win32():
            path.chmod(0o600)
    except OSError:
        return


def sys_platform_not_win32() -> bool:
    import sys

    return sys.platform != "win32"


def known_host_labels(host: str, user: str | None, port: int | None) -> set[str]:
    labels = {host}
    if user:
        labels.add(f"{user}@{host}")
    if port and port != 22:
        labels.add(f"[{host}]:{port}")
    return labels


__all__ = ["list_ssh_config_hosts", "drop_known_hosts_for_labels", "known_host_labels"]