"""OpenSSH SSH_ASKPASS bridge for machine password login (M53)."""

from __future__ import annotations

import contextlib
import os
import secrets
import shlex
import shutil
import socket
import stat
import struct
import sys
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path

from snowpea_core.config.paths import resolve_home

TOKEN_ENV = "SNOWPEA_SSH_ASKPASS_TOKEN"
SOCKET_ENV = "SNOWPEA_SSH_ASKPASS_SOCKET"
PROMPT_MAX = 200
ANSWER_MAX = 4096
ASK_TIMEOUT_SEC = 240.0
PASSPHRASE_PREFIX = "Enter passphrase for key "
_SUN_PATH_MAX = 100


class AskpassRejected(Exception):
    """The core refused or closed the ask before an answer arrived."""


PromptCallback = Callable[[str], str | None]
StopCallback = Callable[[], None]


def sanitize_ssh_prompt(prompt: str) -> str:
    """Drop Unicode control/format chars (category C*) and cap length."""
    kept = [ch for ch in prompt if not unicodedata.category(ch).startswith("C")]
    return "".join(kept)[:PROMPT_MAX]


def classify_prompt_kind(prompt: str) -> str:
    if prompt.startswith(PASSPHRASE_PREFIX):
        return "passphrase"
    return "password"


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


def _ppid_linux(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    after = raw[raw.rfind(")") + 2 :]
    fields = after.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def _peer_pid(conn: socket.socket) -> int | None:
    if sys.platform == "darwin":
        try:
            local_peerpid = getattr(socket, "LOCAL_PEERPID", 0x00200001)
            raw = conn.getsockopt(socket.SOL_LOCAL, local_peerpid, struct.calcsize("i"))
            return struct.unpack("i", raw)[0]
        except (AttributeError, OSError):
            return None
    if sys.platform.startswith("linux"):
        try:
            so_peercred = 17
            raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", raw)
            if uid != os.getuid():
                return None
            return pid
        except OSError:
            return None
    return None


def _descendant_of(pid: int, root: int) -> bool:
    current = pid
    for _ in range(256):
        if current == root:
            return True
        if current <= 1:
            return False
        parent = _ppid_darwin(current) if sys.platform == "darwin" else _ppid_linux(current)
        if parent is None:
            return False
        current = parent
    return False


def _token_ok(got: bytes, token: str) -> bool:
    try:
        expect = token.encode("utf-8")
    except UnicodeEncodeError:
        return False
    try:
        return secrets.compare_digest(got, expect)
    except TypeError:
        return False


def _ppid_darwin(pid: int) -> int | None:
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out) if out.isdigit() else None


class SshAskpassBridge:
    """One private channel per ssh connect attempt."""

    def __init__(
        self,
        prompt: PromptCallback,
        *,
        home: Path | str | None = None,
        on_stop: StopCallback | None = None,
        max_prompts: int = 1,
    ) -> None:
        self._prompt = prompt
        self._home = resolve_home(home)
        self._on_stop = on_stop
        self._max_prompts = max(1, max_prompts)
        self._token = secrets.token_hex(16)
        self._dir = Path()
        self._socket_path = ""
        self._launcher = Path()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ssh_pid: int | None = None
        self._declined = False
        self._prompts_left = self._max_prompts
        self._ask_slot = threading.Lock()
        self._active = False
        self._token_lock = threading.Lock()

    def set_ssh_pid(self, pid: int) -> None:
        self._ssh_pid = pid

    def note_declined(self) -> None:
        self._declined = True

    def _peek_token(self) -> str | None:
        with self._token_lock:
            if not self._active:
                return None
            return self._token

    def _consume_token(self) -> None:
        with self._token_lock:
            self._token = secrets.token_hex(16)

    def _make_run_dir(self) -> Path:
        base = self._home / "run"
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, stat.S_IMODE(base.stat().st_mode) | 0o700)
        for _ in range(8):
            candidate = base / secrets.token_hex(6)
            try:
                candidate.mkdir(mode=0o700)
            except OSError:
                continue
            sock = candidate / "s"
            if len(sock.as_posix().encode()) <= _SUN_PATH_MAX:
                return candidate
            shutil.rmtree(candidate, ignore_errors=True)
        fallback = Path(os.environ.get("TMPDIR", "/tmp")) / f"sp-ap-{secrets.token_hex(4)}"
        fallback.mkdir(parents=True, exist_ok=True)
        os.chmod(fallback, 0o700)
        return fallback

    def start(self) -> None:
        self._dir = self._make_run_dir()
        self._socket_path = str(self._dir / "s")
        self._launcher = self._dir / "askpass"
        py = shlex.quote(sys.executable)
        self._launcher.write_text(
            "#!/bin/sh\n"
            f"exec {py} -I -m snowpea_core.machines.ssh_askpass \"$@\"\n",
            encoding="utf-8",
        )
        self._launcher.chmod(0o700)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        Path(self._socket_path).chmod(0o600)
        self._server.listen(1)
        self._active = True
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
                target=self._serve_one,
                args=(conn,),
                daemon=True,
            ).start()

    def _serve_one(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(ASK_TIMEOUT_SEC)
            with conn:
                if self._declined or self._prompts_left <= 0:
                    conn.sendall(b"\x01\n")
                    return
                if self._ssh_pid is None:
                    conn.sendall(b"\x01\n")
                    return
                peer = _peer_pid(conn)
                if peer is None or not _descendant_of(peer, self._ssh_pid):
                    conn.sendall(b"\x01\n")
                    return
                if not self._ask_slot.acquire(blocking=False):
                    conn.sendall(b"\x01\n")
                    return
                try:
                    reader = conn.makefile("rb")
                    got = reader.readline(256)
                    token = self._peek_token()
                    if token is None or not _token_ok((got or b"").strip(), token):
                        conn.sendall(b"\x01\n")
                        return
                    self._consume_token()
                    prompt_raw = (
                        (reader.readline(PROMPT_MAX + 2) or b"").decode(errors="replace").strip()
                    )
                    if not prompt_raw:
                        conn.sendall(b"\x01\n")
                        return
                    prompt = sanitize_ssh_prompt(prompt_raw)
                    if not prompt:
                        conn.sendall(b"\x01\n")
                        return
                    secret = self._prompt(prompt)
                    if secret is None:
                        self.note_declined()
                        conn.sendall(b"\x01\n")
                        return
                    payload = secret.encode("utf-8", errors="surrogateescape")
                    if len(payload) > ANSWER_MAX:
                        conn.sendall(b"\x01\n")
                        return
                    self._prompts_left -= 1
                    conn.sendall(payload + b"\n")
                finally:
                    self._ask_slot.release()
        except Exception:  # noqa: BLE001 - one bad helper must not kill the bridge
            with contextlib.suppress(OSError):
                conn.sendall(b"\x01\n")

    def env(self) -> dict[str, str]:
        if not self._active:
            return {}
        out: dict[str, str] = {
            "SSH_ASKPASS": str(self._launcher),
            "SSH_ASKPASS_REQUIRE": "force",
            TOKEN_ENV: self._token,
            SOCKET_ENV: self._socket_path,
        }
        display = os.environ.get("DISPLAY")
        if display:
            out["DISPLAY"] = display
        return out

    def stop(self) -> None:
        if self._on_stop is not None:
            with contextlib.suppress(Exception):
                self._on_stop()
        self._active = False
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