"""OpenSSH SSH_ASKPASS bridge for machine password login (M53)."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import secrets
import shlex
import shutil
import socket
import struct
import sys
import tempfile
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

PROC_BSDINFO_PBI_PPID_OFF = 16
PROC_BSDINFO_START_SEC_OFF = 120
PROC_BSDINFO_STRUCT_SIZE = 136

ProcFieldsReader = Callable[[int], tuple[int, str] | None]
_proc_fields_reader: ProcFieldsReader | None = None


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


def _linux_stat_after_comm(stat_text: str) -> list[str] | None:
    after = stat_text[stat_text.rfind(")") + 2 :]
    fields = after.split()
    if len(fields) < 20 or not fields[1].isdigit():
        return None
    return fields


def _linux_proc_fields(pid: int) -> tuple[int, str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = _linux_stat_after_comm(raw)
    if fields is None:
        return None
    return int(fields[1]), fields[19]


def parse_proc_bsdinfo(data: bytes) -> tuple[int, int, int] | None:
    """Parse ``proc_bsdinfo`` bytes (macOS); for unit tests with captured samples."""
    if len(data) < PROC_BSDINFO_STRUCT_SIZE:
        return None
    ppid = struct.unpack_from("<I", data, PROC_BSDINFO_PBI_PPID_OFF)[0]
    start_sec = struct.unpack_from("<Q", data, PROC_BSDINFO_START_SEC_OFF)[0]
    start_usec = struct.unpack_from("<Q", data, PROC_BSDINFO_START_SEC_OFF + 8)[0]
    return ppid, start_sec, start_usec


_log = logging.getLogger(__name__)
_libproc: ctypes.CDLL | None | bool = False
_libproc_unavailable_logged = False


def _darwin_libproc() -> ctypes.CDLL | None:
    global _libproc, _libproc_unavailable_logged
    if _libproc is False:
        loaded: ctypes.CDLL | None = None
        for path in ("/usr/lib/libSystem.B.dylib",):
            try:
                loaded = ctypes.CDLL(path)
                break
            except OSError:
                continue
        if loaded is None:
            try:
                loaded = ctypes.CDLL(None)
            except OSError:
                loaded = None
        _libproc = loaded
        if loaded is None and not _libproc_unavailable_logged:
            _log.warning("ssh password login is unavailable (libproc could not be loaded)")
            _libproc_unavailable_logged = True
    return _libproc if _libproc else None


def parse_xucred(data: bytes, *, expected_version: int = 0) -> int | None:
    """Parse ``struct xucred`` (``cr_version``, ``cr_uid``); unit-testable."""
    if len(data) < 8:
        return None
    version, uid = struct.unpack("II", data[:8])
    if version != expected_version:
        return None
    return uid


def _darwin_proc_bsdinfo(pid: int) -> tuple[int, int, int] | None:
    if sys.platform != "darwin":
        return None
    libproc = _darwin_libproc()
    if libproc is None:
        return None
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    buf = (ctypes.c_char * 4096)()
    got = proc_pidinfo(pid, 3, 0, buf, ctypes.sizeof(buf))
    if got <= 0:
        return None
    return parse_proc_bsdinfo(bytes(buf[:got]))


def _darwin_proc_fields(pid: int) -> tuple[int, str] | None:
    parsed = _darwin_proc_bsdinfo(pid)
    if parsed is None:
        return None
    ppid, start_sec, start_usec = parsed
    start = f"{start_sec}.{start_usec}"
    return ppid, start


def _proc_fields(pid: int) -> tuple[int, str] | None:
    if _proc_fields_reader is not None:
        return _proc_fields_reader(pid)
    if sys.platform.startswith("linux"):
        return _linux_proc_fields(pid)
    if sys.platform == "darwin":
        return _darwin_proc_fields(pid)
    return None


def _peer_pid_linux(conn: socket.socket) -> int | None:
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
        if uid != os.getuid():
            return None
        return pid
    except OSError:
        return None


def _peer_pid_darwin(conn: socket.socket) -> int | None:
    sol_local = 0
    local_peerpid = 0x002
    local_peercred = 0x001
    try:
        raw_pid = conn.getsockopt(sol_local, local_peerpid, struct.calcsize("i"))
        peer = struct.unpack("i", raw_pid)[0]
    except OSError:
        return None
    try:
        raw_cred = conn.getsockopt(sol_local, local_peercred, 104)
        uid = parse_xucred(raw_cred)
        if uid is None or uid != os.getuid():
            return None
    except OSError:
        libc = ctypes.CDLL(None, use_errno=True)
        euid = ctypes.c_uint32()
        egid = ctypes.c_uint32()
        if libc.getpeereid(
            int(conn.fileno()),
            ctypes.byref(euid),
            ctypes.byref(egid),
        ) != 0 or euid.value != os.getuid():
            return None
    return peer


def _peer_pid(conn: socket.socket) -> int | None:
    if sys.platform == "darwin":
        return _peer_pid_darwin(conn)
    if sys.platform.startswith("linux"):
        return _peer_pid_linux(conn)
    return None


def _descendant_of(peer: int, root_pid: int, root_start: str) -> bool:
    current = peer
    for _ in range(256):
        fields = _proc_fields(current)
        if fields is None:
            return False
        ppid, start = fields
        if current == root_pid:
            return start == root_start
        if current <= 1:
            return False
        current = ppid
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
        self._ssh_root: tuple[int, str] | None = None
        self._declined = False
        self._prompts_left = self._max_prompts
        self._ask_slot = threading.Lock()
        self._active = False
        self._token_lock = threading.Lock()

    def set_ssh_pid(self, pid: int) -> None:
        fields = _proc_fields(pid)
        if fields is None:
            self._ssh_root = None
        else:
            _ppid, start = fields
            self._ssh_root = (pid, start)
        self._ensure_listener()

    def finish_ssh_run(self) -> None:
        self._ssh_root = None
        with self._token_lock:
            self._token = secrets.token_hex(16)
        self._close_listener()

    def note_declined(self) -> None:
        self._declined = True

    def listener_open(self) -> bool:
        return self._server is not None

    def _peek_token(self) -> str | None:
        with self._token_lock:
            if not self._active:
                return None
            return self._token

    def _consume_token(self) -> None:
        with self._token_lock:
            self._token = secrets.token_hex(16)

    def _ensure_listener(self) -> None:
        if not self._active or self._server is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with contextlib.suppress(FileNotFoundError):
                Path(self._socket_path).unlink()
            sock.bind(self._socket_path)
            Path(self._socket_path).chmod(0o600)
            sock.listen(1)
        except OSError:
            with contextlib.suppress(OSError):
                sock.close()
            return
        self._server = sock
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._accept_loop,
                name="ssh-askpass",
                daemon=True,
            )
            self._thread.start()

    def _close_listener(self) -> None:
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None
        if self._socket_path:
            with contextlib.suppress(FileNotFoundError, OSError):
                Path(self._socket_path).unlink()

    def _make_run_dir(self) -> Path:
        base = self._home / "run"
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        short_names = ("s", "a", "b", "c", "d", "e", "f", "g")
        for name in short_names:
            candidate = base / name
            if candidate.exists():
                continue
            try:
                candidate.mkdir(mode=0o700)
            except OSError:
                continue
            sock = candidate / "s"
            if len(sock.as_posix().encode()) <= _SUN_PATH_MAX:
                return candidate
            shutil.rmtree(candidate, ignore_errors=True)
        for _ in range(8):
            candidate = base / secrets.token_hex(4)
            try:
                candidate.mkdir(mode=0o700)
            except OSError:
                continue
            sock = candidate / "s"
            if len(sock.as_posix().encode()) <= _SUN_PATH_MAX:
                return candidate
            shutil.rmtree(candidate, ignore_errors=True)
        fallback = Path(tempfile.mkdtemp(prefix="sp-ap-"))
        os.chmod(fallback, 0o700)
        return fallback

    def _launcher_body(self, py: str) -> str:
        import snowpea_core

        pkg_root = str(Path(snowpea_core.__file__).resolve().parent.parent)
        code = (
            "import sys; "
            f"sys.path.append({pkg_root!r}); "
            "from snowpea_core.machines.ssh_askpass import main; "
            "raise SystemExit(main())"
        )
        return (
            "#!/bin/sh\n"
            f"exec {py} -E -s -P -c {shlex.quote(code)} \"$@\"\n"
        )

    def start(self) -> None:
        self._dir = self._make_run_dir()
        self._socket_path = str(self._dir / "s")
        self._launcher = self._dir / "askpass"
        py = shlex.quote(sys.executable)
        self._launcher.write_text(self._launcher_body(py), encoding="utf-8")
        self._launcher.chmod(0o700)
        self._active = True
        self._ensure_listener()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                break
            try:
                server.settimeout(0.5)
                conn, _addr = server.accept()
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
                root = self._ssh_root
                if root is None:
                    conn.sendall(b"\x01\n")
                    return
                peer = _peer_pid(conn)
                if peer is None or not _descendant_of(peer, root[0], root[1]):
                    conn.sendall(b"\x01\n")
                    return
                if not self._ask_slot.acquire(blocking=False):
                    conn.sendall(b"\x01\n")
                    return
                try:
                    if self._declined or self._prompts_left <= 0:
                        conn.sendall(b"\x01\n")
                        return
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
        self._close_listener()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._dir.is_dir():
            shutil.rmtree(self._dir, ignore_errors=True)


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    prompt = sys.argv[1]
    token = os.environ.get(TOKEN_ENV, "")
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not token or not socket_path:
        return 1
    try:
        secret = request_secret(socket_path, token, prompt)
    except AskpassRejected:
        return 1
    if secret is None:
        return 1
    sys.stdout.write(secret + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())