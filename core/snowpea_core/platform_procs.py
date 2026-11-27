"""macOS process listing via libproc in libSystem (no subprocess, no PATH).

Other platforms call nothing here today; Linux session scans use ``/proc`` in
``terminal.pty_unix``.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable

log = logging.getLogger("snowpea.platform_procs")

_lib: ctypes.CDLL | bool | None = None
_lib_fail_logged = False
# Tests replace this to inject a fake ``proc_listallpids``.
_proc_listallpids: Callable[[ctypes.c_void_p, int], int] | None = None


def _try_load() -> ctypes.CDLL | bool:
    for source in ("/usr/lib/libSystem.B.dylib", None):
        try:
            lib = ctypes.CDLL(source) if source else ctypes.CDLL(None)
        except OSError:
            continue
        fn = lib.proc_listallpids
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        fn.restype = ctypes.c_int
        try:
            fn(None, 0)
        except (AttributeError, OSError):
            continue
        return lib
    return False


def _darwin_lib() -> ctypes.CDLL | None:
    global _lib, _lib_fail_logged
    if _lib is None:
        _lib = _try_load()
    if _lib is False:
        if not _lib_fail_logged:
            _lib_fail_logged = True
            log.warning(
                "libproc unavailable; holder drain will rely on PTY EOF only"
            )
        return None
    return _lib


def _listallpids(lib: ctypes.CDLL) -> list[int]:
    fn = _proc_listallpids or lib.proc_listallpids
    n = fn(None, 0)
    if n < 0:
        return []
    buf = (ctypes.c_int * (max(n, 0) + 64))()
    got = fn(buf, ctypes.sizeof(buf))
    if got < 0:
        return []
    return [pid for pid in buf[:got] if pid > 0]


def list_all_pids() -> list[int] | None:
    """All live pids on macOS; ``None`` when libproc cannot be used."""
    if sys.platform != "darwin":
        return None
    lib = _darwin_lib()
    if lib is None:
        return None
    return _listallpids(lib)


def reset_for_tests() -> None:
    """Clear lazy-loaded libproc state (tests only)."""
    global _lib, _lib_fail_logged, _proc_listallpids
    _lib = None
    _lib_fail_logged = False
    _proc_listallpids = None