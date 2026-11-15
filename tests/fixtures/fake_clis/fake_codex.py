#!/usr/bin/env python3
"""Fake ``codex app-server`` (newline JSON-RPC without ``"jsonrpc"``), after spike S2.

    fake_codex.py --version
    fake_codex.py app-server

Client methods: ``initialize`` (+ ``initialized`` notification), ``account/read``,
``thread/start``, ``thread/resume``, ``thread/list``, ``thread/read``,
``thread/turns/list``, ``thread/items/list``, ``thread/loaded/list``,
``thread/unsubscribe``, ``turn/start``, ``turn/steer``, ``turn/interrupt``, ``model/list``.
Server notifications: ``thread/started``, ``thread/status/changed``,
``turn/started``, ``item/started``, ``item/agentMessage/delta``,
``item/completed``, ``turn/completed``, ``serverRequest/resolved``, ``error``.
Server requests (ids from 0, independent of the client's):
``item/commandExecution/requestApproval``; answer ``{"decision": "accept" |
"acceptForSession" | "decline" | "cancel"}``.

Writer lock: loading a thread holds ``flock`` on
``$CODEX_HOME/thread-writer-locks/<id>.lock`` (released on unsubscribe or
exit); ``thread/resume`` of a thread whose lock another process holds answers
``-32600 "thread <id> already has an active writer"``.  Threads persist in
``$CODEX_HOME/fake-threads.json`` when ``CODEX_HOME`` is set.  Extra scenarios:
``noauth`` (``account/read`` says no account; turns fail with a 401 error
notification), ``bypass_refused`` (exits 2 if any bypass flag or sandbox value
reaches it).  See ``_common.py`` for the rest.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as c  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

VERSION = "codex-cli 0.167.1 (fake)"


def uuid7ish() -> str:
    return c.new_id()


class Codex:
    def __init__(self) -> None:
        self.cfg = c.config()
        self.turns = c.Turns(self.cfg)
        self.home = os.environ.get("CODEX_HOME")
        self.state_path = os.path.join(self.home, "fake-threads.json") if self.home else None
        self.threads: dict[str, dict] = (
            c.read_json(self.state_path, {}) if self.state_path else {}
        )
        self.loaded: dict[str, object] = {}
        self.active: dict[str, dict] = {}
        self.pending: dict[int, c.Waiter] = {}
        self.server_ids = 0
        self.lock = threading.Lock()
        self.eof = threading.Event()
        self.workers: list[threading.Thread] = []

    # -- plumbing ---------------------------------------------------------
    def save(self) -> None:
        if self.state_path:
            # Turns on several threads finish concurrently: one writer at a time.
            with self.lock:
                c.write_json(self.state_path, self.threads)

    def reply(self, rid: object, result: dict) -> None:
        c.write({"id": rid, "result": result})

    def fail(self, rid: object, code: int, message: str) -> None:
        c.write({"id": rid, "error": {"code": code, "message": message}})

    def notify(self, method: str, params: dict) -> None:
        c.write({"method": method, "params": params, "emittedAtMs": c.now_ms()})

    def lock_path(self, thread_id: str) -> str | None:
        if not self.home:
            return None
        return os.path.join(self.home, "thread-writer-locks", f"{thread_id}.lock")

    def take_writer(self, thread_id: str) -> bool:
        if thread_id in self.loaded:
            return True
        path = self.lock_path(thread_id)
        if path is None or fcntl is None:
            self.loaded[thread_id] = None
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.loaded[thread_id] = handle
        return True

    def release(self, thread_id: str) -> None:
        handle = self.loaded.pop(thread_id, None)
        if handle is not None:
            handle.close()  # type: ignore[attr-defined]

    def thread_row(self, thread: dict, *, with_turns: bool = False) -> dict:
        row = {key: thread[key] for key in (
            "id", "name", "preview", "cwd", "createdAt", "updatedAt", "source", "model",
            "cliVersion") if key in thread}
        row["status"] = (
            {"type": "active", "activeFlags": []} if thread["id"] in self.active
            else ({"type": "idle"} if thread["id"] in self.loaded else {"type": "notLoaded"})
        )
        row["forkedFromId"] = None
        row["parentThreadId"] = None
        if with_turns:
            row["turns"] = thread.get("turns", [])
        return row

    # -- input ------------------------------------------------------------
    def on_frame(self, frame: dict | None) -> None:
        if frame is None:
            self.eof.set()
            for waiter in list(self.pending.values()):
                waiter.set(None)
            return
        if "method" not in frame:
            # A response to one of our approval requests.
            waiter = self.pending.get(frame.get("id"))  # type: ignore[arg-type]
            if waiter is not None:
                waiter.set(frame.get("result") or {"decision": "decline"})
            return
        method = frame["method"]
        rid = frame.get("id")
        params = frame.get("params") or {}
        handler = getattr(self, "m_" + method.replace("/", "_"), None)
        if rid is None:
            return  # notifications: initialized, ...
        if handler is None:
            self.fail(rid, -32601, f"unknown method {method}")
            return
        try:
            handler(rid, params)
        except KeyError as exc:
            self.fail(rid, -32600, f"missing {exc}")

    # -- methods ----------------------------------------------------------
    def m_initialize(self, rid: object, params: dict) -> None:
        self.reply(rid, {"userAgent": f"snowpea/{VERSION}", "codexHome": self.home or "",
                         "platformFamily": "unix", "platformOs": "linux"})

    def m_account_read(self, rid: object, params: dict) -> None:
        if self.cfg["scenario"] == "noauth":
            self.reply(rid, {"account": None, "requiresOpenaiAuth": True})
        else:
            self.reply(rid, {"account": {"type": "chatgpt", "planType": "fake"},
                             "requiresOpenaiAuth": True})

    def m_model_list(self, rid: object, params: dict) -> None:
        models = [
            {"id": "gpt-fake-mini", "model": "gpt-fake-mini", "hidden": False,
             "isDefault": False},
            {"id": "gpt-fake", "model": "gpt-fake", "hidden": False, "isDefault": True},
            {"id": "gpt-fake-internal", "model": "gpt-fake-internal", "hidden": True,
             "isDefault": False},
        ]
        self.reply(rid, {"data": models, "nextCursor": None})

    def _check_policy(self, params: dict) -> None:
        if self.cfg["scenario"] == "bypass_refused":
            values = (str(params.get("approvalPolicy")), str(params.get("sandbox")))
            if "never" in values or "danger-full-access" in values:
                c.log("bypass", values)
                sys.exit(2)

    def m_thread_start(self, rid: object, params: dict) -> None:
        self._check_policy(params)
        thread_id = uuid7ish()
        now = int(time.time())
        thread = {"id": thread_id, "name": None, "preview": "", "cwd": params["cwd"],
                  "createdAt": now, "updatedAt": now, "source": "vscode",
                  "model": params.get("model") or "gpt-fake", "cliVersion": VERSION,
                  "approvalPolicy": params.get("approvalPolicy", "on-request"),
                  "sandbox": params.get("sandbox", "workspace-write"), "turns": []}
        self.threads[thread_id] = thread
        self.take_writer(thread_id)
        self.save()
        self.reply(rid, {"thread": self.thread_row(thread), "model": thread["model"],
                         "approvalPolicy": thread["approvalPolicy"],
                         "sandbox": {"type": thread["sandbox"]}, "cwd": thread["cwd"]})
        self.notify("thread/started", {"thread": self.thread_row(thread)})

    def m_thread_resume(self, rid: object, params: dict) -> None:
        self._check_policy(params)
        thread = self.threads.get(params["threadId"])
        if thread is None:
            self.fail(rid, -32600, f"thread {params['threadId']} not found")
            return
        if self.cfg["scenario"] == "second_writer" or not self.take_writer(thread["id"]):
            self.fail(rid, -32600, f"thread {thread['id']} already has an active writer")
            return
        for key in ("approvalPolicy", "sandbox", "model"):
            if params.get(key):
                thread[key] = params[key]
        self.reply(rid, {"thread": self.thread_row(thread, with_turns=True),
                         "model": thread.get("model"),
                         "approvalPolicy": thread.get("approvalPolicy"),
                         "sandbox": {"type": thread.get("sandbox")}})

    def m_thread_unsubscribe(self, rid: object, params: dict) -> None:
        self.release(params["threadId"])
        self.reply(rid, {"status": "unsubscribed"})

    def m_thread_loaded_list(self, rid: object, params: dict) -> None:
        self.reply(rid, {"data": sorted(self.loaded)})

    def m_thread_list(self, rid: object, params: dict) -> None:
        cwd = params.get("cwd")
        cwds = cwd if isinstance(cwd, list) else ([cwd] if cwd else None)
        rows = [t for t in self.threads.values() if cwds is None or t["cwd"] in cwds]
        rows.sort(key=lambda t: t["updatedAt"], reverse=True)
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or 50)
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else None
        self.reply(rid, {"data": [self.thread_row(t) for t in page], "nextCursor": nxt,
                         "backwardsCursor": None})

    def m_thread_read(self, rid: object, params: dict) -> None:
        thread = self.threads.get(params["threadId"])
        if thread is None:
            self.fail(rid, -32600, "thread not found")
            return
        self.reply(rid, {"thread": self.thread_row(thread,
                                                   with_turns=bool(params.get("includeTurns")))})

    def m_thread_turns_list(self, rid: object, params: dict) -> None:
        thread = self.threads.get(params["threadId"])
        if thread is None:
            self.fail(rid, -32600, "thread not found")
            return
        turns = thread.get("turns", [])
        self.reply(rid, {"data": turns, "nextCursor": None, "backwardsCursor": None})

    def m_thread_items_list(self, rid: object, params: dict) -> None:
        thread = self.threads.get(params["threadId"])
        if thread is None:
            self.fail(rid, -32600, "thread not found")
            return
        items = [item for turn in thread.get("turns", [])
                 if params.get("turnId") in (None, turn["id"]) for item in turn["items"]]
        self.reply(rid, {"data": items, "nextCursor": None, "backwardsCursor": None})

    def m_turn_start(self, rid: object, params: dict) -> None:
        thread = self.threads.get(params["threadId"])
        if thread is None or thread["id"] not in self.loaded:
            self.fail(rid, -32600, "thread not loaded")
            return
        text = " ".join(part.get("text", "") for part in params.get("input", []))
        turn = {"id": uuid7ish(), "status": "inProgress", "items": [], "error": None,
                "interrupted": threading.Event(), "steer": []}
        self.active[thread["id"]] = turn
        self.reply(rid, {"turn": {"id": turn["id"], "status": "inProgress", "items": [],
                                  "error": None}})
        worker = threading.Thread(target=self.run_turn, args=(thread, turn, text), daemon=True)
        self.workers.append(worker)
        worker.start()

    def m_turn_steer(self, rid: object, params: dict) -> None:
        turn = self.active.get(params["threadId"])
        if turn is None or turn["id"] != params.get("expectedTurnId"):
            found = turn["id"] if turn else "none"
            self.fail(rid, -32600,
                      f"expected active turn id `{params.get('expectedTurnId')}` but found "
                      f"`{found}`")
            return
        turn["steer"].append(params.get("input"))
        self.reply(rid, {"turnId": turn["id"]})

    def m_turn_interrupt(self, rid: object, params: dict) -> None:
        turn = self.active.get(params["threadId"])
        if turn is None:
            self.fail(rid, -32600, "no active turn to interrupt")
            return
        turn["interrupted"].set()
        for waiter in list(self.pending.values()):
            waiter.set({"decision": "cancel"})
        self.reply(rid, {})

    # -- a turn -------------------------------------------------------------
    def run_turn(self, thread: dict, turn: dict, text: str) -> None:
        scenario = self.turns.next()
        tid = thread["id"]
        base = {"threadId": tid, "turnId": turn["id"]}
        self.notify("thread/status/changed", {"threadId": tid,
                                              "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": tid, "turn": {"id": turn["id"],
                                                               "status": "inProgress"}})
        if scenario == "garbage":
            c.garbage()
        user_item = {"type": "userMessage", "id": uuid7ish(),
                     "content": [{"type": "text", "text": text, "text_elements": []}]}
        self.item(base, user_item, turn)
        if not thread.get("preview"):
            thread["preview"] = text
        status = "completed"
        error = None
        if scenario == "noauth":
            self.notify("error", {**base, "error": {
                "message": "Reconnecting... 2/5",
                "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}},
                "additionalDetails": "unexpected status 401 Unauthorized"}, "willRetry": False})
            status, error = "failed", {"message": "401 Unauthorized"}
        elif scenario == "approval" and thread.get("approvalPolicy") != "never":
            decision = self.command(base, thread, turn)
            if decision == "interrupted":
                status = "interrupted"
            else:
                reply = ("Created note.txt" if decision in ("accept", "acceptForSession")
                         else "I could not create the file")
                self.message(base, turn, reply, scenario)
        else:
            if scenario == "hang":
                c.hang()
            status = self.message(base, turn, str(self.cfg["reply"]), scenario)
        self.finish(thread, turn, status, error)

    def item(self, base: dict, item: dict, turn: dict) -> None:
        self.notify("item/started", {**base, "item": item})
        self.notify("item/completed", {**base, "item": item})
        turn["items"].append(item)

    def message(self, base: dict, turn: dict, text: str, scenario: str) -> str:
        item_id = "msg_" + c.new_id().replace("-", "")
        self.notify("item/started", {**base, "item": {"type": "agentMessage", "id": item_id,
                                                      "text": "", "phase": "final_answer"}})
        written = ""
        for index, part in enumerate(c.chunks(text, scenario)):
            if turn["interrupted"].is_set():
                return "interrupted"
            self.notify("item/agentMessage/delta", {**base, "itemId": item_id, "delta": part})
            written += part
            if scenario == "die" and index == 0:
                c.die()
            if scenario == "slow":
                time.sleep(float(self.cfg["delay"]))
        item = {"type": "agentMessage", "id": item_id, "text": written, "phase": "final_answer"}
        self.notify("item/completed", {**base, "item": item})
        turn["items"].append(item)
        return "completed"

    def command(self, base: dict, thread: dict, turn: dict) -> str:
        command = "/bin/bash -lc 'printf hi > note.txt'"
        item_id = "exec-" + c.new_id()
        started = {"type": "commandExecution", "id": item_id, "command": command,
                   "cwd": thread["cwd"], "status": "inProgress", "aggregatedOutput": None,
                   "exitCode": None}
        self.notify("item/started", {**base, "item": started})
        with self.lock:
            request_id = self.server_ids
            self.server_ids += 1
        waiter = c.Waiter()
        self.pending[request_id] = waiter
        self.notify("thread/status/changed", {"threadId": base["threadId"], "status": {
            "type": "active", "activeFlags": ["waitingOnApproval"]}})
        c.write({"method": "item/commandExecution/requestApproval", "id": request_id,
                 "params": {**base, "kind": "command", "itemId": item_id, "command": command,
                            "cwd": thread["cwd"],
                            "availableDecisions": ["accept", "acceptForSession", "cancel"]}})
        answer = waiter.wait()
        self.pending.pop(request_id, None)
        self.notify("serverRequest/resolved", {"threadId": base["threadId"],
                                               "requestId": request_id})
        decision = (answer or {}).get("decision", "decline")
        if turn["interrupted"].is_set() or decision == "cancel" or answer is None:
            return "interrupted"
        if decision in ("accept", "acceptForSession"):
            with open(os.path.join(thread["cwd"], "note.txt"), "w", encoding="utf-8") as fh:
                fh.write("hi")
            done = {**started, "status": "completed", "aggregatedOutput": "hi", "exitCode": 0}
        else:
            done = {**started, "status": "declined"}
        self.notify("item/completed", {**base, "item": done})
        turn["items"].append(done)
        return decision

    def finish(self, thread: dict, turn: dict, status: str, error: object) -> None:
        tid = thread["id"]
        self.active.pop(tid, None)
        thread["updatedAt"] = int(time.time())
        thread.setdefault("turns", []).append({"id": turn["id"], "status": status,
                                               "items": turn["items"], "error": error})
        self.save()
        self.notify("thread/status/changed", {"threadId": tid, "status": {"type": "idle"}})
        self.notify("turn/completed", {"threadId": tid, "turn": {
            "id": turn["id"], "status": status, "items": turn["items"], "error": error,
            "durationMs": 1}})

    def run(self) -> int:
        c.prepare(self.cfg)
        c.maybe_spawn_child()
        c.StdinReader(self.on_frame).start()
        self.eof.wait()
        for worker in self.workers:
            worker.join()
        for thread_id in list(self.loaded):
            self.release(thread_id)
        c.log("exit", 0)
        return 0


def main() -> int:
    if c.version_requested(VERSION):
        return 0
    c.log_start()
    args = sys.argv[1:]
    if not args or args[0] != "app-server":
        print("fake codex: only `app-server` is supported", file=sys.stderr)
        return 2
    return Codex().run()


if __name__ == "__main__":
    sys.exit(main())
