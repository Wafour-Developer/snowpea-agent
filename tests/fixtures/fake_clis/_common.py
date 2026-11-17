"""Shared plumbing for the fake external CLIs (M19 WP5).

Every fake reads its scenario from the environment:

``FAKE_CLI_SCENARIO``
    ``normal`` (default), ``approval`` (asks before a tool), ``die`` (exits with
    status 3 after the first reply chunk), ``hang`` (stops answering mid-turn and
    ignores SIGTERM, so only SIGKILL ends it), ``garbage`` (non-JSON and
    malformed lines around a normal turn), ``slow`` (many small chunks,
    ``FAKE_CLI_DELAY`` seconds apart).
``FAKE_CLI_SCENARIO_FILE``
    JSON object overriding the above: ``{"scenario", "reply", "delay", "tool",
    "version", "turns": [scenario per turn, ...]}``.  fake_claude also takes
    ``"tools": [tool per turn, ...]`` (``write`` or ``bash``) and ``"command"``
    (what a ``bash`` approval asks to run; never executed).
``FAKE_CLI_REPLY``
    The assistant's answer (default ``OK``).
``FAKE_CLI_LOG``
    A file the fake appends JSON lines to: ``start`` (argv, cwd, environment
    variable *names*), every frame it received (``in``), ``exit``.
``FAKE_CLI_CHILD_PIDFILE``
    When set, the fake starts a sleeping grandchild and writes its pid there
    (process-tree kill tests).
``FAKE_CLI_STATE_DIR``
    Where fakes without a CLI-specific home keep their sessions (agy, cursor).
    Claude uses ``CLAUDE_CONFIG_DIR`` and Codex ``CODEX_HOME``.  With none set,
    nothing is written: a fake never touches the real ``~/.claude`` and friends.

Standard library only; runs on any Python 3.8+.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

_OUT_LOCK = threading.Lock()


def config() -> dict[str, Any]:
    data: dict[str, Any] = {
        "scenario": os.environ.get("FAKE_CLI_SCENARIO", "normal"),
        "reply": os.environ.get("FAKE_CLI_REPLY", "OK"),
        "delay": float(os.environ.get("FAKE_CLI_DELAY", "0.2")),
        "tool": "write",
        "version": None,
        "turns": None,
    }
    path = os.environ.get("FAKE_CLI_SCENARIO_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            data.update(json.load(handle))
    return data


class Turns:
    """Scenario per turn: ``turns`` list when given, else the one scenario."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.count = 0

    def next(self) -> str:
        turns = self.cfg.get("turns")
        self.count += 1
        if turns:
            return str(turns[min(self.count, len(turns)) - 1])
        return str(self.cfg["scenario"])


def log(kind: str, data: Any = None) -> None:
    path = os.environ.get("FAKE_CLI_LOG")
    if not path:
        return
    record = {"pid": os.getpid(), "kind": kind, "data": data, "t": time.time()}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


_DANGER = re.compile(r"danger|yolo|bypass|skip[-_]?permission|full[-_]?access", re.I)


def danger(arg: str) -> bool:
    """True for an argument that reads like a permission bypass."""
    return bool(_DANGER.search(arg))


def log_start() -> None:
    args = sys.argv[1:]
    log("start", {"argv": args, "cwd": os.getcwd(), "env": sorted(os.environ),
                  "bypass": [arg for arg in args if danger(arg)]})


def write(obj: Any) -> None:
    """One frame on stdout (a JSON object, or a raw string for garbage)."""
    line = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    with _OUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def garbage() -> None:
    for junk in ("this is not json", "{broken", "[1, 2, 3]", "\x1b[31mred\x1b[0m", "42"):
        write(junk)


def chunks(text: str, scenario: str) -> list[str]:
    if scenario == "slow":
        return [char for char in text] if len(text) < 40 else [text[i:i + 3]
                                                                for i in range(0, len(text), 3)]
    size = max(1, len(text) // 2)
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def die() -> None:
    log("die")
    sys.stdout.flush()
    os._exit(3)


def prepare(cfg: dict[str, Any]) -> None:
    """Main-thread setup: a fake that will hang ignores SIGTERM from the start."""
    scenarios = {str(cfg.get("scenario"))} | {str(t) for t in (cfg.get("turns") or [])}
    if "hang" in scenarios and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def hang() -> None:
    """Stop answering for good (SIGTERM is ignored since :func:`prepare`)."""
    log("hang")
    while True:
        time.sleep(3600)


def maybe_spawn_child() -> None:
    path = os.environ.get("FAKE_CLI_CHILD_PIDFILE")
    if not path:
        return
    child = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))


def version_requested(default: str) -> bool:
    if "--version" in sys.argv[1:2] or sys.argv[1:] == ["-v"]:
        cfg = config()
        print(cfg.get("version") or default)
        return True
    return False


def help_requested(text: str, *, stderr: bool = False) -> bool:
    """``<cli> --help``: print ``text`` (where the real CLI prints its usage)."""
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(text, file=sys.stderr if stderr else sys.stdout)
        return True
    return False


def help_modes(env: str, default: list[str]) -> list[str]:
    """The mode choices a fake's ``--help`` lists (``env``: comma-separated override)."""
    raw = os.environ.get(env)
    if raw is None:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def arg_value(flag: str, argv: list[str] | None = None) -> str | None:
    args = sys.argv[1:] if argv is None else argv
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def has_flag(flag: str) -> bool:
    return flag in sys.argv[1:]


def new_id() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.replace(tmp, path)


def append_jsonl(path: str, record: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class StdinReader(threading.Thread):
    """Reads stdin lines into a callback on a daemon thread; ``None`` at EOF."""

    def __init__(self, on_line: Any) -> None:
        super().__init__(daemon=True)
        self.on_line = on_line

    def run(self) -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                log("in-garbage", line[:200])
                continue
            log("in", frame)
            self.on_line(frame)
        self.on_line(None)


class Waiter:
    """A one-shot slot a worker thread blocks on until the reader fills it."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.value: Any = None

    def set(self, value: Any) -> None:
        self.value = value
        self._event.set()

    def wait(self, timeout: float | None = None) -> Any:
        self._event.wait(timeout)
        return self.value
