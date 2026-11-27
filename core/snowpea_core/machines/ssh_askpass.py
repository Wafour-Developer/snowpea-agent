"""OpenSSH SSH_ASKPASS bridge for machine password login (M53)."""

from __future__ import annotations

import contextlib
import os
import secrets
import shlex
import shutil
import socket
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

TOKEN_ENV = "SNOWPEA_SSH_ASKPASS_TOKEN"
SOCKET_ENV = "SNOWPEA_SSH_ASKPASS_SOCKET"
PROMPT_MAX = 4096
ANSWER_MAX = 4096
ASK_TIMEOUT_SEC = 600.0


class AskpassRejected(Exception):
    """The core refused or closed the ask before an answer arrived."""


PromptCallback = Callable[[str], str | None]


def request_secret(socket_path: str, token: str, prompt: str) -> str | None:
    """Ask the core for a secret over the private askpass channel."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(ASK_TIMEOUT_SEC)
        sock.connect(socket_path)
        with sock.makefile("rwb", buffering=0) as stream:
            stream.write(f"{token}\n".encode())
            stream.write(f"{prompt}\n".encode())
            stream.flush()
            raw = stream.readline(ANSWER_MAX + 2)
    if not raw:
        return None
    line = raw.rstrip(b"\n")
    if line == b"\x01":
        raise AskpassRejected()
    try:
        return line.decode("utf-8", errors="surrogateescape")
    except ValueError:
        return None


def _serve_one(
    conn: socket.socket,
    token: str,
    callback: PromptCallback,
) -> None:
    try:
        conn.settimeout(ASK_TIMEOUT_SEC)
        with conn:
            reader = conn.makefile("rb")
            got = (reader.readline(256) or b"").decode(errors="replace").strip()
            if not secrets.compare_digest(got, token):
                conn.sendall(b"\x01\n")
                return
            prompt = (reader.readline(PROMPT_MAX + 2) or b"").decode(errors="replace").strip()
            if not prompt:
                conn.sendall(b"\x01\n")
                return
            secret = callback(prompt)
            if secret is None:
                conn.sendall(b"\x01\n")
                return
            payload = secret.encode("utf-8", errors="surrogateescape")
            if len(payload) > ANSWER_MAX:
                conn.sendall(b"\x01\n")
                return
            conn.sendall(payload + b"\n")
    except OSError:
        with contextlib.suppress(OSError):
            conn.sendall(b"\x01\n")


class SshAskpassBridge:
    """One private channel per ssh connect attempt; helpers share the token."""

    def __init__(self, prompt: PromptCallback) -> None:
        self._prompt = prompt
        self._token = secrets.token_hex(16)
        self._dir = Path()
        self._socket_path = ""
        self._launcher = Path()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        import tempfile

        self._dir = Path(tempfile.mkdtemp(prefix="snowpea-ssh-askpass-"))
        os.chmod(self._dir, stat.S_IMODE(self._dir.stat().st_mode) | 0o700)
        self._socket_path = str(self._dir / "sock")
        self._launcher = self._dir / "askpass"
        py = shlex.quote(sys.executable)
        self._launcher.write_text(
            "#!/bin/sh\n"
            f"exec {py} -m snowpea_core.machines.ssh_askpass \"$@\"\n",
            encoding="utf-8",
        )
        self._launcher.chmod(0o700)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        Path(self._socket_path).chmod(0o600)
        self._server.listen(8)
        self._thread = threading.Thread(target=self._accept_loop, name="ssh-askpass", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                self._server.settimeout(0.5)
                conn, _addr = self._server.accept()
            except (TimeoutError, InterruptedError):
                continue
            except OSError:
                break
            threading.Thread(
                target=_serve_one,
                args=(conn, self._token, self._prompt),
                daemon=True,
            ).start()

    def env(self) -> dict[str, str]:
        display = os.environ.get("DISPLAY") or ":1"
        return {
            "SSH_ASKPASS": str(self._launcher),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": display,
            TOKEN_ENV: self._token,
            SOCKET_ENV: self._socket_path,
        }

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._dir.is_dir():
            shutil.rmtree(self._dir, ignore_errors=True)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(1)
    prompt = sys.argv[1]
    token = os.environ.get(TOKEN_ENV, "")
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not token or not socket_path:
        raise SystemExit(1)
    try:
        secret = request_secret(socket_path, token, prompt)
    except AskpassRejected:
        raise SystemExit(1) from None
    if secret is None:
        raise SystemExit(1)
    sys.stdout.write(secret + "\n")
    sys.stdout.flush()
    raise SystemExit(0)


if __name__ == "__main__":
    main()