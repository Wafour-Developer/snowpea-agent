#!/usr/bin/env python3
"""Fake Claude Code (``claude -p`` stream-json both ways), after spike S1.

Supported command lines::

    fake_claude.py --version
    fake_claude.py -p --input-format stream-json --output-format stream-json --verbose
        [--permission-mode default|plan|acceptEdits] [--permission-prompts host]
        [--permission-prompt-tool stdio] (--session-id <uuid> | --resume <uuid>)
        [--model <m>] [--replay-user-messages] [--include-partial-messages]
        [--dangerously-skip-permissions]
    fake_claude.py --fake-terminal --resume <uuid>
        stands in for the person's interactive ``claude`` holding the session:
        writes ``sessions/<pid>.json`` and waits until stdin closes or it is killed.
    fake_claude.py (--session-id <uuid> | --resume <uuid>) [--permission-mode m] [--model m]
        the interactive UI as tmux sees it (spike S5): reads keys from its
        terminal (``Enter`` submits, bracketed paste keeps newlines, ``Escape``
        interrupts a busy turn or declines an open permission dialog, ``1``
        allows it), keeps the transcript and the status file (``idle`` /
        ``busy`` / ``waiting``, plus ``tmux`` when it runs in a pane).
        ``FAKE_CLAUDE_TRUST=ask`` shows the folder trust dialog instead.
    fake_claude.py auth status --json
        ``loggedIn`` (false when ``FAKE_CLAUDE_LOGGED_IN=0``) plus account
        fields carrying ``owner@example.com`` that adapters must not keep.

stdin frames: ``control_request`` (``initialize``, ``interrupt``), ``user``,
``control_response`` (answers to ``can_use_tool``).  stdout frames: ``system/init``
(every turn), ``user`` replay, ``stream_event`` partials, ``assistant`` (one
content block each), ``control_request can_use_tool``, ``user`` tool_result,
``result``.  An interrupted turn ends with a ``result`` whose
``terminal_reason`` is ``interrupted``.

With ``CLAUDE_CONFIG_DIR`` set it keeps a transcript at
``$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/<id>.jsonl`` (including an
``attachment`` and a ``session-owner`` record carrying ``owner@example.com``,
which adapters must drop) and ``$CLAUDE_CONFIG_DIR/sessions/<pid>.json`` while
it runs.  See ``_common.py`` for the scenarios.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as c  # noqa: E402

VERSION = "2.1.325 (Claude Code, fake)"
OWNER_EMAIL = "owner@example.com"


def encoded(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def proc_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            raw = handle.read()
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


class Store:
    """Transcript and liveness files under ``CLAUDE_CONFIG_DIR`` (if set)."""

    def __init__(self, session_id: str, cwd: str, *, kind: str) -> None:
        self.root = os.environ.get("CLAUDE_CONFIG_DIR")
        self.session_id = session_id
        self.cwd = cwd
        self.kind = kind
        self.parent: str | None = None
        self.extra: dict = {}
        if self.root:
            self.transcript = os.path.join(
                self.root, "projects", encoded(cwd), f"{session_id}.jsonl"
            )
            self.status_file = os.path.join(self.root, "sessions", f"{os.getpid()}.json")
            if os.path.exists(self.transcript):
                for line in open(self.transcript, encoding="utf-8"):
                    try:
                        uid = json.loads(line).get("uuid")
                    except ValueError:
                        continue
                    self.parent = uid or self.parent

    def status(self, value: str) -> None:
        if not self.root:
            return
        c.write_json(self.status_file, {
            "pid": os.getpid(),
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "startedAt": c.now_ms(),
            "procStart": proc_start(os.getpid()),
            "version": VERSION,
            "kind": "interactive",
            "entrypoint": "cli" if self.kind == "terminal" else "sdk-cli",
            "name": "fake",
            "status": value,
            "updatedAt": c.now_ms(),
            "statusUpdatedAt": c.now_ms(),
            **self.extra,
        })

    def remove_status(self) -> None:
        if self.root:
            try:
                os.unlink(self.status_file)
            except OSError:
                pass

    def record(self, rtype: str, **fields: object) -> str:
        uid = c.new_id()
        if not self.root:
            return uid
        record = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "type": rtype,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "uuid": uid,
            "timestamp": c.iso_now(),
            **fields,
        }
        c.append_jsonl(self.transcript, record)
        self.parent = uid
        return uid

    def private_records(self) -> None:
        """What a real transcript holds and nobody may see."""
        if not self.root or os.path.exists(self.transcript):
            return
        c.append_jsonl(self.transcript, {
            "type": "attachment", "sessionId": self.session_id,
            "attachment": {"type": "system", "userEmail": OWNER_EMAIL,
                           "organizationUuid": "org-1234"},
        })
        c.append_jsonl(self.transcript, {
            "type": "session-owner", "sessionId": self.session_id,
            "accountEmail": OWNER_EMAIL, "accountUuid": "acct-5678",
        })


class Claude:
    def __init__(self) -> None:
        self.cfg = c.config()
        self.turns = c.Turns(self.cfg)
        resume = c.arg_value("--resume")
        self.session_id = c.arg_value("--session-id") or resume or c.new_id()
        self.cwd = os.getcwd()
        self.model = c.arg_value("--model") or "claude-haiku-fake"
        self.mode = c.arg_value("--permission-mode") or "default"
        self.bypass = c.has_flag("--dangerously-skip-permissions")
        self.host_prompts = (
            c.arg_value("--permission-prompts") == "host"
            and c.arg_value("--permission-prompt-tool") == "stdio"
        )
        self.replay = c.has_flag("--replay-user-messages")
        self.partial = c.has_flag("--include-partial-messages")
        self.store = Store(self.session_id, self.cwd, kind="sdk")
        self.lock = threading.Lock()
        self.pending: dict[str, c.Waiter] = {}
        self.queue: list[dict] = []
        self.running: threading.Thread | None = None
        self.interrupted = threading.Event()
        self.eof = threading.Event()

    # -- input ------------------------------------------------------------
    def on_frame(self, frame: dict | None) -> None:
        if frame is None:
            self.eof.set()
            for waiter in list(self.pending.values()):
                waiter.set(None)
            return
        kind = frame.get("type")
        if kind == "control_request":
            request = frame.get("request") or {}
            rid = frame.get("request_id")
            if request.get("subtype") == "initialize":
                c.write({"type": "control_response", "response": {
                    "subtype": "success", "request_id": rid,
                    "response": {"commands": [], "models": [], "account": {}}}})
            elif request.get("subtype") == "interrupt":
                with self.lock:
                    queued = [q["message"]["content"] for q in self.queue]
                    self.queue.clear()
                self.interrupted.set()
                for waiter in list(self.pending.values()):
                    waiter.set({"behavior": "deny", "message": "interrupted"})
                c.write({"type": "control_response", "response": {
                    "subtype": "success", "request_id": rid,
                    "response": {"still_queued": queued}}})
            else:
                c.write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": rid, "error": "unsupported"}})
        elif kind == "control_response":
            response = frame.get("response") or {}
            waiter = self.pending.get(response.get("request_id"))
            if waiter is not None:
                waiter.set(response.get("response") or {})
        elif kind == "user":
            with self.lock:
                self.queue.append(frame)
            self.kick()

    def kick(self) -> None:
        with self.lock:
            if self.running is not None and self.running.is_alive():
                return
            if not self.queue:
                return
            frame = self.queue.pop(0)
            self.running = threading.Thread(target=self.turn, args=(frame,), daemon=True)
            self.running.start()

    # -- a turn -------------------------------------------------------------
    def base(self) -> dict:
        return {"session_id": self.session_id}

    def turn(self, frame: dict) -> None:
        scenario = self.turns.next()
        self.interrupted.clear()
        text = (frame.get("message") or {}).get("content")
        if isinstance(text, list):
            text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
        text = str(text or "")
        self.store.status("busy")
        self.store.private_records()
        user_uuid = self.store.record("user", message={"role": "user", "content": text},
                                      promptSource="sdk", turnOrigin="sdk")
        c.write({"type": "system", "subtype": "init", "cwd": self.cwd,
                 "session_id": self.session_id, "model": self.model,
                 "permissionMode": "bypassPermissions" if self.bypass else self.mode,
                 "tools": ["<trimmed>"], "claude_code_version": VERSION})
        if scenario == "garbage":
            c.garbage()
        if scenario == "autherror":
            failure = "Invalid API key · Please run /login"
            self.assistant("msg_autherror", {"type": "text", "text": failure})
            c.write({"type": "result", "subtype": "success", "is_error": True,
                     "result": failure, "terminal_reason": "completed", "num_turns": 1,
                     "session_id": self.session_id, "permission_denials": []})
            self.store.status("idle")
            with self.lock:
                self.running = None
            return None
        if self.replay:
            c.write({"type": "user", "isReplay": True, "uuid": user_uuid,
                     "timestamp": c.iso_now(), "message": {"role": "user", "content": text},
                     **self.base()})
        message_id = "msg_" + c.new_id().replace("-", "")[:20]
        self.assistant(message_id, {"type": "thinking", "thinking": "", "signature": "sig"})
        denials: list[dict] = []
        if scenario == "approval" and self.mode != "plan":
            outcome = self.tool(message_id, denials)
            if outcome == "interrupted":
                return self.finish(interrupted=True, denials=denials)
            reply = "Allowed: wrote note.txt" if outcome == "allow" else "Denied: did not write"
            message_id = "msg_" + c.new_id().replace("-", "")[:20]
        else:
            reply = str(self.cfg["reply"])
        if scenario == "hang":
            c.hang()
        parts = c.chunks(reply, scenario)
        written = ""
        for index, part in enumerate(parts):
            if self.interrupted.is_set():
                return self.finish(interrupted=True, denials=denials)
            if self.partial:
                c.write({"type": "stream_event", "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": part}}, **self.base()})
            written += part
            if scenario == "die" and index == 0:
                c.die()
            if scenario == "slow":
                time.sleep(float(self.cfg["delay"]))
        self.assistant(message_id, {"type": "text", "text": written})
        self.finish(interrupted=False, denials=denials, result=written)

    def assistant(self, message_id: str, block: dict) -> None:
        message = {"id": message_id, "type": "message", "role": "assistant",
                   "model": self.model, "content": [block]}
        uid = self.store.record("assistant", message=message)
        c.write({"type": "assistant", "message": message, "parent_tool_use_id": None,
                 "uuid": uid, **self.base()})

    def tool(self, message_id: str, denials: list[dict]) -> str:
        tool_id = "toolu_" + c.new_id().replace("-", "")[:20]
        path = os.path.join(self.cwd, "note.txt")
        tool_input = {"file_path": path, "content": "hi\n"}
        self.assistant(message_id, {"type": "tool_use", "id": tool_id, "name": "Write",
                                    "input": tool_input})
        decision = "allow"
        message = ""
        if not self.bypass and self.mode != "acceptEdits":
            if not self.host_prompts:
                decision, message = "deny", "permission prompts are not answered by a host"
            else:
                rid = c.new_id()
                waiter = c.Waiter()
                self.pending[rid] = waiter
                c.write({"type": "control_request", "request_id": rid, "request": {
                    "subtype": "can_use_tool", "tool_name": "Write", "display_name": "Write",
                    "input": tool_input, "description": "note.txt",
                    "permission_suggestions": [{"type": "setMode", "mode": "acceptEdits",
                                                "destination": "session"}],
                    "tool_use_id": tool_id}})
                answer = waiter.wait()
                self.pending.pop(rid, None)
                if answer is None or self.interrupted.is_set():
                    return "interrupted"
                decision = "allow" if answer.get("behavior") == "allow" else "deny"
                message = str(answer.get("message") or "Denied")
        if decision == "allow":
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hi\n")
            content = f"File created successfully at: {path}"
            result = {"tool_use_id": tool_id, "type": "tool_result", "content": content}
            extra = {"type": "create", "filePath": path, "content": "hi\n",
                     "structuredPatch": [], "originalFile": None}
        else:
            result = {"tool_use_id": tool_id, "type": "tool_result", "content": message,
                      "is_error": True}
            extra = {}
            denials.append({"tool_name": "Write", "tool_use_id": tool_id,
                            "tool_input": tool_input})
        uid = self.store.record("user", message={"role": "user", "content": [result]},
                                toolUseResult=extra)
        c.write({"type": "user", "message": {"role": "user", "content": [result]},
                 "tool_use_result": extra, "uuid": uid, **self.base()})
        return decision

    def finish(self, *, interrupted: bool, denials: list[dict], result: str = "") -> None:
        c.write({"type": "result", "subtype": "success", "is_error": False,
                 "result": result, "stop_reason": "end_turn",
                 "terminal_reason": "interrupted" if interrupted else "completed",
                 "num_turns": 1, "session_id": self.session_id, "total_cost_usd": 0.0,
                 "permission_denials": denials, "duration_ms": 1})
        self.store.status("idle")
        with self.lock:
            self.running = None
        self.kick()

    def run(self) -> int:
        c.prepare(self.cfg)
        c.maybe_spawn_child()
        self.store.status("idle")
        reader = c.StdinReader(self.on_frame)
        reader.start()
        self.eof.wait()
        running = self.running
        if running is not None:
            running.join()
        self.store.remove_status()
        c.log("exit", 0)
        return 0


def tmux_address() -> str | None:
    """``<session>:@<window>.%<pane>`` when running in a tmux pane (as Claude records it)."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{session_name}:#{window_id}.#{pane_id}"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class Interactive:
    """The terminal UI, driven by keys (tmux ``send-keys`` / ``paste-buffer``)."""

    def __init__(self) -> None:
        self.cfg = c.config()
        self.turns = c.Turns(self.cfg)
        resume = c.arg_value("--resume")
        self.session_id = c.arg_value("--session-id") or resume or c.new_id()
        self.cwd = os.getcwd()
        self.model = c.arg_value("--model") or "claude-haiku-fake"
        self.mode = c.arg_value("--permission-mode") or "default"
        self.bypass = c.has_flag("--dangerously-skip-permissions")
        self.store = Store(self.session_id, self.cwd, kind="terminal")
        address = tmux_address()
        if address:
            self.store.extra["tmux"] = address
        self.state = "idle"
        self.text = bytearray()
        self.pending = b""
        self.paste = False
        self.decision: c.Waiter | None = None
        self.interrupted = threading.Event()
        self.worker: threading.Thread | None = None

    def out(self, text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    # -- keys -------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self.pending += data
        while self.pending:
            if self.paste:
                end = self.pending.find(b"\x1b[201~")
                if end < 0:
                    self.text.extend(self.pending.replace(b"\r", b"\n"))
                    self.pending = b""
                    return
                self.text.extend(self.pending[:end].replace(b"\r", b"\n"))
                self.pending = self.pending[end + 6:]
                self.paste = False
                continue
            byte = self.pending[0]
            if byte == 0x1B:
                if self.pending.startswith(b"\x1b[200~"):
                    self.paste = True
                    self.pending = self.pending[6:]
                    continue
                if len(self.pending) == 1:
                    ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.05)
                    if ready:
                        more = os.read(sys.stdin.fileno(), 4096)
                        if more:
                            self.pending += more
                            continue
                    self.pending = b""
                    self.escape()
                    continue
                if self.pending[1:2] == b"[":
                    index = 2
                    while index < len(self.pending) and not 0x40 <= self.pending[index] <= 0x7E:
                        index += 1
                    self.pending = self.pending[index + 1:]
                    continue
                self.pending = self.pending[1:]
                self.escape()
                continue
            self.pending = self.pending[1:]
            if byte in (0x0D, 0x0A):
                self.enter()
            elif byte == 0x7F:
                del self.text[-1:]
            elif self.state == "waiting":
                if byte == ord("1") and self.decision is not None:
                    self.decision.set("allow")
            else:
                self.text.append(byte)

    def escape(self) -> None:
        c.log("key", "Escape")
        if self.state == "waiting" and self.decision is not None:
            self.decision.set("deny")
        elif self.state == "busy":
            self.interrupted.set()
        else:
            self.text.clear()

    def enter(self) -> None:
        if self.state != "idle":
            return  # queued in the real CLI; the fake keeps it in the input line
        text = self.text.decode("utf-8", "replace")
        self.text.clear()
        if not text.strip():
            return
        c.log("prompt", text)
        self.state = "busy"
        self.store.status("busy")
        self.worker = threading.Thread(target=self.turn, args=(text,), daemon=True)
        self.worker.start()

    # -- a turn -------------------------------------------------------------
    def assistant(self, message_id: str, block: dict) -> None:
        self.store.record("assistant", message={
            "id": message_id, "type": "message", "role": "assistant", "model": self.model,
            "content": [block]})

    def turn(self, text: str) -> None:
        scenario = self.turns.next()
        self.interrupted.clear()
        self.store.private_records()
        self.store.record("user", message={"role": "user", "content": text},
                          promptSource="typed", turnOrigin="human")
        self.out("❯ " + text.replace("\n", "\n  "))
        message_id = "msg_" + c.new_id().replace("-", "")[:20]
        self.assistant(message_id, {"type": "thinking", "thinking": "", "signature": "sig"})
        reply = str(self.cfg["reply"])
        if scenario == "die":
            c.die()
        if scenario == "approval" and self.mode != "plan":
            tool_id = "toolu_" + c.new_id().replace("-", "")[:20]
            path = os.path.join(self.cwd, "note.txt")
            self.assistant(message_id, {"type": "tool_use", "id": tool_id, "name": "Write",
                                        "input": {"file_path": path, "content": "hi\n"}})
            answer = "allow"
            if not self.bypass:
                self.decision = c.Waiter()
                self.state = "waiting"
                self.store.status("waiting")
                self.out("● Write(note.txt)\n Do you want to create note.txt?\n ❯ 1. Yes\n"
                         "   3. No\n Esc to cancel")
                answer = self.decision.wait()
                self.decision = None
                self.state = "busy"
                self.store.status("busy")
            if answer == "allow":
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("hi\n")
                self.store.record("user", message={"role": "user", "content": [
                    {"tool_use_id": tool_id, "type": "tool_result",
                     "content": f"File created successfully at: {path}"}]},
                    toolUseResult={"type": "create", "filePath": path, "content": "hi\n",
                                   "structuredPatch": [], "originalFile": None})
                reply = "Allowed: wrote note.txt"
            else:
                self.store.record("user", message={"role": "user", "content": [
                    {"tool_use_id": tool_id, "type": "tool_result", "is_error": True,
                     "content": "The user doesn't want to proceed with this tool use."}]},
                    toolUseResult="User rejected tool use")
                self.store.record("user", message={"role": "user", "content":
                                  "[Request interrupted by user for tool use]"})
                return self.finish()
        written = ""
        for part in c.chunks(reply, scenario):
            if self.interrupted.is_set():
                self.store.record("user", message={"role": "user",
                                                   "content": "[Request interrupted by user]"})
                return self.finish()
            written += part
            if scenario == "slow":
                time.sleep(float(self.cfg["delay"]))
        self.assistant("msg_" + c.new_id().replace("-", "")[:20], {"type": "text", "text": written})
        self.out("● " + written)
        self.store.record("system", subtype="turn_duration", durationMs=1)
        return self.finish()

    def finish(self) -> None:
        self.state = "idle"
        self.store.status("idle")
        self.out("❯ ")

    def run(self) -> int:
        c.prepare(self.cfg)
        fd = sys.stdin.fileno()
        saved = None
        if os.isatty(fd):
            import termios
            import tty

            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        try:
            if os.environ.get("FAKE_CLAUDE_TRUST") == "ask":
                self.out(" Quick safety check: Is this a project you created or one you trust?\n"
                         " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm")
                while True:
                    data = os.read(fd, 1024)
                    if not data or b"\r" in data or b"\n" in data:
                        return 0
            # Like the real UI: ask the terminal for bracketed paste, so a
            # pasted newline is text, not Enter.
            sys.stdout.write("\x1b[?2004h")
            self.store.status("idle")
            self.out(f"Claude Code (fake) {self.session_id}\n❯ ")
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                self.feed(data)
        finally:
            if saved is not None:
                import termios

                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            self.store.remove_status()
        return 0


def auth_status() -> int:
    logged_in = os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") != "0"
    print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else "none",
                      "email": OWNER_EMAIL, "orgId": "org-1234", "orgName": "Owner Org"}))
    return 0 if logged_in else 1


def fake_terminal() -> int:
    """The person's interactive claude holding a session: liveness only."""
    session_id = c.arg_value("--resume") or c.new_id()
    store = Store(session_id, os.getcwd(), kind="terminal")
    store.status("idle")
    try:
        for _line in sys.stdin:
            pass
    finally:
        store.remove_status()
    return 0


def main() -> int:
    if c.version_requested(VERSION):
        return 0
    c.log_start()
    if c.has_flag("--fake-terminal"):
        return fake_terminal()
    if sys.argv[1:3] == ["auth", "status"]:
        return auth_status()
    if not c.has_flag("-p"):
        return Interactive().run()
    return Claude().run()


if __name__ == "__main__":
    sys.exit(main())
