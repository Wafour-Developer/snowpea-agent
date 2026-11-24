"""``snowpea-core hook report --cli <cli> --action <action>``: the hook fast path.

A CLI hook (installed by ``snowpea integration install``) runs this on every
prompt and turn end, so it must cost almost nothing: stdlib only (no pydantic,
no asyncio, no daemon code), one JSON line to the terminal's report socket, and
exit 0 in every case within 2 s — a hook must never slow down or break the
person's CLI.

The line sent is::

    {"v": 1, "terminalId", "token", "cli", "event", "state", "kind",
     "sessionId", "transcriptPath"}

``state`` is ``working`` / ``done`` / ``blocked`` or ``null`` (a session-only
report), ``kind`` is ``permission`` / ``question`` or ``null``.

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(the per-CLI payload rules of ``src/integration/assets/*/herdr-agent-state.sh``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

DEADLINE_S = 2.0
MAX_INPUT = 1 << 20
CLIS = ("claude", "codex", "cursor", "agy")
ACTIONS = {"session": None, "working": "working", "done": "done", "blocked": "blocked"}


def _text(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def build_report(cli: str, action: str, payload: dict[str, Any],
                 environ: dict[str, str] | os._Environ[str]) -> dict[str, Any] | None:
    """The report for one hook call, or ``None`` when this call reports nothing."""
    if cli not in CLIS or action not in ACTIONS:
        return None
    state = ACTIONS[action]
    kind: str | None = None
    event = _text(payload, "hook_event_name", "hookEventName") or action
    session_id: str | None
    transcript: str | None
    if cli == "claude":
        # Cursor's agent reads Claude's hooks too; its own integration reports it.
        if "CURSOR_VERSION" in environ or "cursor_version" in payload:
            return None
        if payload.get("agent_id"):  # a subagent's hook, not the session's
            return None
        session_id = _text(payload, "session_id")
        transcript = _text(payload, "transcript_path")
        if action == "blocked":
            note = _text(payload, "notification_type") or ""
            if note == "permission_prompt":
                kind = "permission"
            elif note == "elicitation_dialog":
                kind = "question"
            elif not note and "permission" in (_text(payload, "message") or "").lower():
                kind = "permission"
            else:
                return None  # idle_prompt, auth_success, …: not a blocker
    elif cli == "codex":
        session_id = _text(payload, "session_id")
        transcript = _text(payload, "transcript_path")
        inherited = environ.get("CODEX_THREAD_ID")
        if inherited and session_id and inherited != session_id:
            return None  # a child Codex session started by this one
        if action == "session" and not transcript:
            return None
    elif cli == "cursor":
        session_id = _text(payload, "session_id", "sessionId", "conversation_id",
                           "conversationId")
        transcript = _text(payload, "transcript_path", "transcriptPath")
    else:  # agy
        session_id = _text(payload, "conversationId", "conversation_id", "session_id")
        transcript = _text(payload, "transcriptPath", "transcript_path")
    if action == "session" and not session_id:
        return None
    return {
        "v": 1,
        "terminalId": environ.get("SNOWPEA_TERMINAL_ID", ""),
        "token": environ.get("SNOWPEA_TERMINAL_TOKEN", ""),
        "cli": cli,
        "event": event[:64],
        "state": state,
        "kind": kind,
        "sessionId": session_id,
        "transcriptPath": transcript,
    }


def send(address: str, line: bytes, timeout: float = 1.0) -> bool:
    """Write ``line`` to the report socket (Unix) or named pipe (Windows)."""
    if sys.platform == "win32" or address.startswith("\\\\.\\pipe\\"):
        with open(address, "r+b", buffering=0) as pipe:
            pipe.write(line)
            try:
                pipe.read(64)
            except OSError:
                pass
        return True
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(address)
        client.sendall(line)
        try:
            client.recv(64)
        except OSError:
            pass
    return True


def _read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read(MAX_INPUT + 1)
    except (OSError, ValueError, AttributeError):
        return b""


def _parse_args(argv: list[str]) -> tuple[str, str]:
    cli = action = ""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--cli", "--action") and index + 1 < len(argv):
            if arg == "--cli":
                cli = argv[index + 1]
            else:
                action = argv[index + 1]
            index += 2
            continue
        index += 1
    return cli, action


def run(argv: list[str], environ: dict[str, str] | os._Environ[str] | None = None) -> int:
    env = os.environ if environ is None else environ
    terminal_id = env.get("SNOWPEA_TERMINAL_ID")
    address = env.get("SNOWPEA_TERMINAL_REPORT")
    raw = _read_stdin()
    if not terminal_id or not address or len(raw) > MAX_INPUT:
        return 0
    cli, action = _parse_args(argv)
    try:
        payload = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    report = build_report(cli, action, payload, env)
    if report is None:
        return 0
    try:
        send(address, (json.dumps(report, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass
    return 0


def main(argv: list[str]) -> int:
    """Entry for ``snowpea-core hook report``; always exits 0, within :data:`DEADLINE_S`."""
    timer = threading.Timer(DEADLINE_S, os._exit, (0,))
    timer.daemon = True
    timer.start()
    try:
        return run(argv)
    except BaseException:  # noqa: BLE001 - a hook must never fail the person's CLI
        return 0
    finally:
        timer.cancel()
