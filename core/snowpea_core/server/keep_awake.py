"""Keep the machine from idle-sleeping while an agent turn runs.

A long turn can sit for minutes with nobody touching the keyboard: the model
server is generating on another host and no tool is running.  macOS (and a
Linux desktop) sees no user activity and puts the machine to sleep; the
network goes with it and the model call fails until the turn gives up.

:class:`KeepAwake` holds an idle-sleep inhibitor while at least one session
turn is running — root, delegated, scheduled or a goal round — and lets go a
little after the last one ends, so back-to-back turns do not flap.  A turn
waiting on an approval or a question to the person does not count.  Only idle
*system* sleep is prevented: the display still sleeps, and closing a laptop's
lid still sleeps the machine.

- macOS: ``caffeinate -i -w <daemon pid>``, which dies with the daemon;
- Linux: ``systemd-inhibit --what=idle … sleep infinity`` when it exists;
- Windows: ``SetThreadExecutionState`` from one dedicated thread, since the
  request belongs to the thread that made it.

Anything that fails is logged once and becomes a no-op: keeping awake must
never fail a turn.  ``daemon.keepAwake: false`` turns it off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("snowpea.keep_awake")

#: Seconds the inhibitor is kept after the last turn ends.
LINGER_SEC = 30.0
#: How often the running-turn count is looked at.
TICK_SEC = 2.0

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def command_for(
    platform: str = sys.platform,
    *,
    pid: int | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The inhibitor command for ``platform``, or ``None`` when it has none.

    Windows has no command: it is handled in-process (:class:`_WindowsInhibitor`).
    """
    if platform == "darwin":
        caffeinate = which("caffeinate")
        if caffeinate is None:
            return None
        return [caffeinate, "-i", "-w", str(pid if pid is not None else os.getpid())]
    if platform.startswith("linux"):
        inhibit = which("systemd-inhibit")
        if inhibit is None:
            return None
        return [
            inhibit,
            "--what=idle",
            "--who=snowpea",
            "--why=agent turn running",
            "--mode=block",
            "sleep",
            "infinity",
        ]
    return None


def _spawn(argv: list[str]) -> Any:
    return subprocess.Popen(  # noqa: S603 - argv is built above, never from input
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Its own process group, so release stops ``sleep infinity`` too and
        # not only the ``systemd-inhibit`` that started it.
        start_new_session=os.name == "posix",
    )


class _ProcessInhibitor:
    """A child process that inhibits sleep for as long as it lives."""

    def __init__(self, proc: Any) -> None:
        self._proc = proc

    def release(self) -> None:
        proc = self._proc
        try:
            if proc.poll() is None:
                if os.name == "posix" and isinstance(proc, subprocess.Popen):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - releasing never fails a turn
            log.debug("could not stop the keep-awake process", exc_info=True)


def _set_thread_execution_state(flags: int) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]


class _WindowsInhibitor:
    """``SetThreadExecutionState`` held by one thread until :meth:`release`."""

    def __init__(self, call: Callable[[int], int] = _set_thread_execution_state) -> None:
        self._call = call
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._thread = threading.Thread(target=self._run, name="snowpea-keep-awake", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if not self._ok:
            raise OSError("SetThreadExecutionState failed")

    def _run(self) -> None:
        try:
            self._ok = bool(self._call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        except Exception:  # noqa: BLE001 - reported by the constructor
            self._ok = False
        self._ready.set()
        if not self._ok:
            return
        self._stop.wait()
        try:
            self._call(ES_CONTINUOUS)
        except Exception:  # noqa: BLE001 - the thread ends either way
            pass

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class KeepAwake:
    """Holds an idle-sleep inhibitor while turns run; see the module doc."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        linger_sec: float = LINGER_SEC,
        platform: str = sys.platform,
        pid: int | None = None,
        spawner: Callable[[list[str]], Any] = _spawn,
        which: Callable[[str], str | None] = shutil.which,
        win_call: Callable[[int], int] = _set_thread_execution_state,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.linger_sec = linger_sec
        self._platform = platform
        self._pid = pid
        self._spawner = spawner
        self._which = which
        self._win_call = win_call
        self._clock = clock
        self._inhibitor: Any = None
        self._idle_since: float | None = None
        #: Set after the first failure: no further attempts, no further logs.
        self._broken = False
        self.turns = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def held(self) -> bool:
        return self._inhibitor is not None

    def update(self, running: int) -> None:
        """Follow the number of running turns: hold while above zero, linger after."""
        self.turns = max(0, int(running))
        if self.turns > 0:
            self._idle_since = None
            if self._inhibitor is None:
                self._acquire()
            return
        if self._inhibitor is None:
            return
        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since >= self.linger_sec:
            self.release()

    def _acquire(self) -> None:
        if not self.enabled or self._broken:
            return
        try:
            if self._platform == "win32":
                self._inhibitor = _WindowsInhibitor(self._win_call)
            else:
                argv = command_for(self._platform, pid=self._pid, which=self._which)
                if argv is None:
                    self._broken = True
                    log.info("keep-awake: nothing to inhibit idle sleep with on %s", self._platform)
                    return
                self._inhibitor = _ProcessInhibitor(self._spawner(argv))
        except Exception as exc:  # noqa: BLE001 - keeping awake never fails a turn
            self._broken = True
            self._inhibitor = None
            log.info("keep-awake unavailable, the machine may idle-sleep during turns: %s", exc)
            return
        log.debug("keep-awake on")

    def release(self) -> None:
        """Let the machine sleep again (also on daemon shutdown)."""
        inhibitor, self._inhibitor = self._inhibitor, None
        self._idle_since = None
        if inhibitor is not None:
            inhibitor.release()
            log.debug("keep-awake off")

    def summary(self) -> str:
        """``on (1 turn)`` / ``off`` / ``disabled``."""
        if not self.enabled:
            return "disabled"
        if self.held:
            return f"on ({self.turns} turn{'' if self.turns == 1 else 's'})"
        return "off"

    # -- watcher -------------------------------------------------------
    def start(
        self,
        running: Callable[[], int],
        *,
        enabled: Callable[[], bool] | None = None,
        tick: float = TICK_SEC,
    ) -> None:
        """Poll ``running`` (the number of turns in flight) until :meth:`stop`.

        ``enabled`` is read on every tick too, so the setting applies without
        a restart.
        """
        if self._task is None:
            self._task = asyncio.ensure_future(self._watch(running, enabled, tick))

    async def _watch(
        self, running: Callable[[], int], enabled: Callable[[], bool] | None, tick: float
    ) -> None:
        while True:
            try:
                if enabled is not None:
                    self.enabled = bool(enabled())
                count = int(running())
            except Exception:  # noqa: BLE001 - a lookup never stops the watcher
                count = 0
            if not self.enabled:
                self.release()
            self.update(count)
            await asyncio.sleep(tick)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.release()


def _waiting(queue: Any, session_id: str) -> bool:
    count = getattr(queue, "count", None)
    return bool(count(session_id)) if count is not None else False


def running_turns(core: Any) -> int:
    """Sessions with a turn in flight and working, of every kind.

    A turn blocked on an approval or a question waits for the person, maybe
    for hours, and does not keep the machine awake; it counts again once the
    person answers.
    """
    approvals = getattr(core, "approvals", None)
    questions = getattr(core, "questions", None)
    return sum(
        1
        for session in core.sessions.all()
        if session.current_turn is not None
        and not _waiting(approvals, session.id)
        and not _waiting(questions, session.id)
    )


__all__ = ["LINGER_SEC", "KeepAwake", "command_for", "running_turns"]
