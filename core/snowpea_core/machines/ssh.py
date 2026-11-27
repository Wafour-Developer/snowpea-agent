"""SSH host keys and non-interactive runners for machine connections (M40/M43)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from snowpea_core.cli import remote
from snowpea_core.cli.remote import RemoteError, RemoteTarget, find_ssh_tool
from snowpea_core.machines.ssh_askpass import SOCKET_ENV

#: Key types, most preferred first, as ``ssh-keyscan -t`` and known_hosts name them.
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "ssh-rsa")  # fmt: skip
_SCAN_NAMES = {
    "ssh-ed25519": "ed25519",
    "ecdsa-sha2-nistp256": "ecdsa",
    "ecdsa-sha2-nistp384": "ecdsa",
    "ecdsa-sha2-nistp521": "ecdsa",
    "ssh-rsa": "rsa",
}
_ALGORITHMS = {"ssh-rsa": "rsa-sha2-512,rsa-sha2-256,ssh-rsa"}


class _EmitterLike(Protocol):
    def scrub(self, text: str) -> str: ...


def _log(emitter: _EmitterLike | None, text: str) -> None:
    """A human line on stderr, without secrets when an emitter scrubs them."""
    shown = emitter.scrub(text) if emitter is not None else text
    print(f"snowpea: {shown}", file=sys.stderr, flush=True)


class _AsksLike(Protocol):
    def ask(self, kind: str, **fields: Any) -> bool: ...


Exec = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _exec(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed ssh tool names with validated values
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def ssh_runner(
    emitter: _EmitterLike | None,
    extra_env: Callable[[], dict[str, str]] | None = None,
    *,
    on_spawn: Callable[[int], None] | None = None,
    on_finish: Callable[[], None] | None = None,
) -> remote.Runner:
    """``RemoteSession``'s runner: no stdin, stderr captured, stdout never the client's pipe."""

    def run(
        argv: Sequence[str], stdin: str | None, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env is not None:
            extra = extra_env()
            if extra:
                env.update(extra)
        timeout = 240 if capture else 1800
        tracked = bool(extra_env and env.get(SOCKET_ENV))
        try:
            if tracked:
                proc = subprocess.Popen(  # noqa: S603 - argv built from a validated target
                    list(argv),
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE if capture else 2,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                if on_spawn is not None:
                    on_spawn(proc.pid)
                try:
                    try:
                        stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        with contextlib.suppress(OSError):
                            proc.wait(timeout=5)
                        raise RemoteError(
                            "ssh did not answer in time", code="unreachable"
                        ) from None
                    done = subprocess.CompletedProcess(
                        proc.args,
                        proc.returncode if proc.returncode is not None else proc.wait(),
                        stdout,
                        stderr,
                    )
                finally:
                    if on_finish is not None:
                        on_finish()
            else:
                done = subprocess.run(  # noqa: S603 - argv built from a validated target
                    list(argv),
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE if capture else 2,  # fd 2: installer output → stderr
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout,
                    env=env,
                )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError("ssh did not answer in time", code="unreachable") from exc
        except OSError as exc:
            raise RemoteError(f"could not run ssh: {exc}", reason="sshMissing") from exc
        if done.stderr:
            for line in done.stderr.strip().splitlines():
                _log(emitter, line)
        return done

    return run


def fingerprint(key_b64: str) -> str:
    """OpenSSH's ``SHA256:`` fingerprint of a base64 public key blob."""
    blob = base64.b64decode(key_b64)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class HostKey:
    key_type: str
    key_b64: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key_b64)


def parse_scan(output: str) -> HostKey | None:
    """The preferred key in ``ssh-keyscan`` output (``host keytype base64`` lines)."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1] in KEY_TYPES:
            found.setdefault(parts[1], parts[2])
    for key_type in KEY_TYPES:
        if key_type in found:
            return HostKey(key_type, found[key_type])
    return None


def parse_ssh_config(output: str) -> dict[str, str]:
    """``ssh -G`` output → lower-case option → value (the first line of a repeated one)."""
    config: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        if key and key.lower() not in config:
            config[key.lower()] = value.strip()
    return config


def _expand_home(path: str) -> str:
    """``~`` as ssh reads it: the account's home directory, not whatever ``$HOME`` says."""
    if not path.startswith("~"):
        return path
    if sys.platform != "win32":
        import pwd

        with contextlib.suppress(KeyError, OSError):
            home = pwd.getpwuid(os.getuid()).pw_dir
            return home + path[1:] if path[1:2] in ("", "/") else path
    return os.path.expanduser(path)


class HostKeys:
    """Learn whether the host is known to ssh, and ask before the first connect if not."""

    def __init__(
        self,
        ssh: str,
        asks: _AsksLike,
        *,
        known_hosts: str | None = None,
        execute: Exec = _exec,
        say: Callable[[str], None] = lambda _t: None,
    ) -> None:
        self._ssh = ssh
        self._asks = asks
        self._known_hosts = known_hosts
        self._exec = execute
        self._say = say
        self._keygen = find_ssh_tool("ssh-keygen")
        self._keyscan = find_ssh_tool("ssh-keyscan")

    def ensure(self, target: RemoteTarget) -> list[str]:
        """The ssh ``-o`` options that make the host key check strict (and pin a new key)."""
        strict = ["StrictHostKeyChecking=yes"]
        if not self._ssh:
            return strict  # RemoteSession reports the missing ssh
        argv = [self._ssh, "-G"]
        if target.port:
            argv += ["-p", str(target.port)]
        argv.append(target.destination)
        try:
            done = self._exec(argv)
        except (OSError, subprocess.TimeoutExpired):
            return strict
        if done.returncode != 0:
            return strict
        config = parse_ssh_config(done.stdout or "")
        if (
            config.get("proxycommand", "none") != "none"
            or config.get("proxyjump", "none") != "none"
        ):
            return strict  # cannot scan through a proxy; ssh refuses an unknown key itself
        host = (config.get("hostname") or target.host).strip("[]")
        port = int(config.get("port") or target.port or 22)
        alias = config.get("hostkeyalias")
        lookup = alias or (host if port == 22 else f"[{host}]:{port}")
        if self._known_hosts:
            user_files = [self._known_hosts]
        else:
            user_files = self._files(config.get("userknownhostsfile", "~/.ssh/known_hosts"))
        global_files = self._files(config.get("globalknownhostsfile", ""))
        if any(self._known(lookup, path) for path in [*user_files, *global_files]):
            return strict
        key = self._scan(host, port)
        if key is None:
            raise RemoteError(
                f"could not read the host key of {target.label} (is it reachable?)",
                code="unreachable",
            )
        if not self._asks.ask("hostKey", fingerprint=key.fingerprint, keyType=key.key_type,
                              host=target.label):  # fmt: skip
            raise RemoteError(
                f"the host key of {target.label} was not accepted", code="hostKey",
                reason="rejected",
            )  # fmt: skip
        pinned_path = (user_files or [_expand_home("~/.ssh/known_hosts")])[0]
        self._add(lookup, key, pinned_path)
        extra = (
            [f"UserKnownHostsFile={pinned_path}"]
            if self._known_hosts or not user_files
            else []
        )
        return [
            *strict,
            f"HostKeyAlgorithms={_ALGORITHMS.get(key.key_type, key.key_type)}",
            *extra,
        ]

    @staticmethod
    def _files(value: str) -> list[str]:
        return [_expand_home(item) for item in value.split() if item.lower() != "none"]

    def _known(self, lookup: str, path: str) -> bool:
        if not self._keygen or not Path(path).is_file():
            return False
        try:
            done = self._exec([self._keygen, "-F", lookup, "-f", path])
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0 and bool((done.stdout or "").strip())

    def _scan(self, host: str, port: int) -> HostKey | None:
        if not self._keyscan:
            return None
        types = ",".join(dict.fromkeys(_SCAN_NAMES.values()))
        try:
            done = self._exec([self._keyscan, "-T", "10", "-p", str(port), "-t", types, host])
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_scan(done.stdout or "")

    def _add(self, lookup: str, key: HostKey, path: str) -> None:
        known = Path(path)
        try:
            known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            prefix = ""
            if known.is_file() and known.stat().st_size:
                with known.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    prefix = "" if handle.read(1) == b"\n" else "\n"
            fresh = not known.exists()
            with known.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{lookup} {key.key_type} {key.key_b64}\n")
            if fresh and sys.platform != "win32":
                known.chmod(0o600)
        except OSError as exc:
            raise RemoteError(
                f"could not save the host key to {path}: {exc.strerror or exc}", code="internal"
            ) from exc
        self._say(f"added the host key {key.fingerprint} to {path}")