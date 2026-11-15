#!/usr/bin/env python3
"""Fake ``agy acp`` (Agent Client Protocol, JSON-RPC 2.0 NDJSON), after spike S3.

    fake_agy.py --version
    fake_agy.py acp [--mode default|accept-edits|auto|plan]
        [--dangerously-skip-permissions]

Client methods: ``initialize``, ``session/new``, ``session/load`` (history is
replayed as ``session/update`` *before* the result, a ``session_info_update``
title after it), ``session/list`` (``{sessions:[{sessionId,cwd,title,
updatedAt}]}``, newest first, no cwd filter), ``session/prompt`` (updates, then
``{stopReason: end_turn | cancelled}``), ``session/set_mode``,
``session/set_model``, ``session/close``; notification ``session/cancel``.
``fake_agy.py models`` lists two models like ``agy models``.  Agent request:
``session/request_permission`` with a *string* id and options
``allow``/``deny`` (``allow_once`` / ``reject_once``); answer
``{"outcome": {"outcome": "selected", "optionId": "allow" | "deny"}}``.  A
deny fails the tool call and ends the turn with no assistant text.

``FAKE_AGY_POLICY=auto`` (the owner's ``toolPermission: always-proceed``)
makes it run tools without asking.  Sessions persist in
``$FAKE_CLI_STATE_DIR/agy-sessions.json`` when that is set.  An interactive
TUI holding a conversation is simulated with ``--fake-terminal --conversation
<id>``: it flocks ``$FAKE_CLI_STATE_DIR/presence/<id>.lock`` until stdin closes.
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

VERSION = "1.5.7 (fake agy)"
MODES = ["default", "accept-edits", "auto", "plan"]


class Agy:
    def __init__(self) -> None:
        self.cfg = c.config()
        self.turns = c.Turns(self.cfg)
        state = os.environ.get("FAKE_CLI_STATE_DIR")
        self.state_path = os.path.join(state, "agy-sessions.json") if state else None
        self.sessions: dict[str, dict] = c.read_json(self.state_path, {}) if self.state_path else {}
        self.policy = os.environ.get("FAKE_AGY_POLICY", "asks")
        self.mode = c.arg_value("--mode") or "default"
        self.bypass = c.has_flag("--dangerously-skip-permissions")
        self.prompts: dict[str, dict] = {}
        self.pending: dict[str, c.Waiter] = {}
        self.next_request = 1
        self.lock = threading.Lock()
        self.eof = threading.Event()
        self.workers: list[threading.Thread] = []

    def save(self) -> None:
        if self.state_path:
            c.write_json(self.state_path, self.sessions)

    def reply(self, rid: object, result: dict) -> None:
        c.write({"jsonrpc": "2.0", "id": rid, "result": result})

    def fail(self, rid: object, code: int, message: str) -> None:
        c.write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def update(self, session_id: str, update: dict) -> None:
        c.write({"jsonrpc": "2.0", "method": "session/update",
                 "params": {"sessionId": session_id, "update": update}})

    # -- input ------------------------------------------------------------
    def on_frame(self, frame: dict | None) -> None:
        if frame is None:
            self.eof.set()
            for waiter in list(self.pending.values()):
                waiter.set(None)
            return
        method = frame.get("method")
        rid = frame.get("id")
        params = frame.get("params") or {}
        if method is None:
            waiter = self.pending.get(str(rid))
            if waiter is not None:
                waiter.set(frame.get("result") or {})
            return
        if method == "session/cancel":
            prompt = self.prompts.get(params.get("sessionId", ""))
            if prompt is not None:
                prompt["cancelled"].set()
                for waiter in list(self.pending.values()):
                    waiter.set({"outcome": {"outcome": "cancelled"}})
            return
        if rid is None:
            return
        handler = getattr(self, "m_" + method.replace("/", "_"), None)
        if handler is None:
            self.fail(rid, -32601, f"Method not found: {method}")
            return
        try:
            handler(rid, params)
        except KeyError as exc:
            self.fail(rid, -32602, f"missing {exc}")

    # -- methods ----------------------------------------------------------
    def m_initialize(self, rid: object, params: dict) -> None:
        self.reply(rid, {"protocolVersion": 1, "agentCapabilities": {
            "loadSession": True, "promptCapabilities": {"image": True, "embeddedContext": True},
            "sessionCapabilities": {"resume": {}, "close": {}, "load": {}, "list": {}}},
            "agentInfo": {"name": "agy", "version": VERSION},
            "authMethods": [{"id": "oauth-personal"}]})

    def _modes(self) -> dict:
        return {"currentModeId": self.mode,
                "availableModes": [{"id": m, "name": m} for m in MODES]}

    def m_session_new(self, rid: object, params: dict) -> None:
        if self.cfg["scenario"] == "noauth":
            self.fail(rid, -32603, "Failed to start Antigravity: not signed in")
            return
        session_id = c.new_id()
        self.sessions[session_id] = {"sessionId": session_id, "cwd": params["cwd"], "title": "",
                                     "updatedAt": c.iso_now(), "history": []}
        self.save()
        self.reply(rid, {"sessionId": session_id, "modes": self._modes(),
                         "models": {"currentModelId": "gemini-fake", "availableModels": []}})

    def m_session_load(self, rid: object, params: dict) -> None:
        session = self.sessions.get(params["sessionId"])
        if session is None:
            self.fail(rid, -32602, "session not found")
            return
        for update in session.get("history", []):
            self.update(session["sessionId"], update)
        self.reply(rid, {"modes": self._modes()})
        if session.get("title"):
            self.update(session["sessionId"], {"sessionUpdate": "session_info_update",
                                               "title": session["title"]})

    def m_session_list(self, rid: object, params: dict) -> None:
        rows = sorted(self.sessions.values(), key=lambda s: s["updatedAt"], reverse=True)
        self.reply(rid, {"sessions": [{key: s[key] for key in
                                       ("sessionId", "cwd", "title", "updatedAt")}
                                      for s in rows[:500]]})

    def m_session_set_mode(self, rid: object, params: dict) -> None:
        if params.get("modeId") not in MODES:
            self.fail(rid, -32602, "unknown mode")
            return
        self.mode = params["modeId"]
        self.reply(rid, {})

    def m_session_set_model(self, rid: object, params: dict) -> None:
        if not str(params.get("modelId", "")).startswith("gemini-fake"):
            self.fail(rid, -32602, "unknown model")
            return
        self.reply(rid, {})

    def m_session_close(self, rid: object, params: dict) -> None:
        self.reply(rid, {})

    def m_session_prompt(self, rid: object, params: dict) -> None:
        session = self.sessions.get(params["sessionId"])
        if session is None:
            self.fail(rid, -32602, "session not loaded")
            return
        text = " ".join(part.get("text", "") for part in params.get("prompt", []))
        prompt = {"cancelled": threading.Event()}
        self.prompts[session["sessionId"]] = prompt
        worker = threading.Thread(target=self.run_prompt, args=(rid, session, text, prompt),
                                  daemon=True)
        self.workers.append(worker)
        worker.start()

    # -- a turn -------------------------------------------------------------
    def record(self, session: dict, update: dict) -> None:
        self.update(session["sessionId"], update)
        if update.get("sessionUpdate") in ("user_message_chunk", "agent_thought_chunk",
                                           "agent_message_chunk", "tool_call",
                                           "tool_call_update"):
            session.setdefault("history", []).append(update)

    def run_prompt(self, rid: object, session: dict, text: str, prompt: dict) -> None:
        scenario = self.turns.next()
        sid = session["sessionId"]
        if scenario == "garbage":
            c.garbage()
        self.record(session, {"sessionUpdate": "user_message_chunk",
                              "content": {"type": "text", "text": text}})
        if not session.get("title"):
            session["title"] = text[:40]
            self.update(sid, {"sessionUpdate": "session_info_update", "title": session["title"]})
        self.record(session, {"sessionUpdate": "agent_thought_chunk",
                              "content": {"type": "text", "text": "Thinking about it."}})
        stop = "end_turn"
        if scenario == "approval" and self.mode != "plan":
            outcome = self.tool(session, prompt)
            if outcome == "cancelled":
                stop = "cancelled"
            elif outcome == "allow":
                stop = self.message(session, prompt, "Ran the command.", scenario)
        else:
            if scenario == "hang":
                c.hang()
            stop = self.message(session, prompt, str(self.cfg["reply"]), scenario)
        session["updatedAt"] = c.iso_now()
        self.save()
        self.prompts.pop(sid, None)
        self.reply(rid, {"stopReason": stop, "usage": {"inputTokens": 1, "outputTokens": 1,
                                                        "totalTokens": 2}})

    def message(self, session: dict, prompt: dict, text: str, scenario: str) -> str:
        for index, part in enumerate(c.chunks(text, scenario)):
            if prompt["cancelled"].is_set():
                return "cancelled"
            self.update(session["sessionId"], {"sessionUpdate": "agent_message_chunk",
                                               "content": {"type": "text", "text": part}})
            if scenario == "die" and index == 0:
                c.die()
            if scenario == "slow":
                time.sleep(float(self.cfg["delay"]))
        session.setdefault("history", []).append(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        return "end_turn"

    def tool(self, session: dict, prompt: dict) -> str:
        call_id = "call_" + c.new_id().replace("-", "")[:16]
        tool_call = {"sessionUpdate": "tool_call", "toolCallId": call_id, "name": "run_command",
                     "args": {"CommandLine": "echo hi > shell_allow.txt"}, "status": "pending",
                     "kind": "execute", "title": "echo hi > shell_allow.txt",
                     "rawInput": {"CommandLine": "echo hi > shell_allow.txt"}}
        self.record(session, tool_call)
        decision = "allow"
        if self.policy != "auto" and not self.bypass and self.mode not in ("auto",):
            with self.lock:
                request_id = str(self.next_request)
                self.next_request += 1
            waiter = c.Waiter()
            self.pending[request_id] = waiter
            c.write({"jsonrpc": "2.0", "id": request_id, "method": "session/request_permission",
                     "params": {"sessionId": session["sessionId"], "toolCall": tool_call,
                                "options": [
                                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                                ]}})
            answer = waiter.wait()
            self.pending.pop(request_id, None)
            outcome = ((answer or {}).get("outcome") or {})
            if answer is None or outcome.get("outcome") == "cancelled":
                return "cancelled"
            decision = "allow" if outcome.get("optionId") == "allow" else "deny"
        if decision == "allow":
            with open(os.path.join(session["cwd"], "shell_allow.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("hi\n")
            self.record(session, {**tool_call, "sessionUpdate": "tool_call_update",
                                  "status": "completed", "rawOutput": {"output": ""}})
        else:
            self.record(session, {**tool_call, "sessionUpdate": "tool_call_update",
                                  "status": "failed", "rawOutput": {"output": ""}})
        return decision

    def run(self) -> int:
        c.prepare(self.cfg)
        c.maybe_spawn_child()
        c.StdinReader(self.on_frame).start()
        self.eof.wait()
        for worker in self.workers:
            worker.join()
        c.log("exit", 0)
        return 0


def fake_terminal() -> int:
    conversation = c.arg_value("--conversation") or c.new_id()
    state = os.environ.get("FAKE_CLI_STATE_DIR")
    handle = None
    if state and fcntl is not None:
        path = os.path.join(state, "presence", f"{conversation}.lock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    for _line in sys.stdin:
        pass
    if handle is not None:
        handle.close()
    return 0


def main() -> int:
    if c.version_requested(VERSION):
        return 0
    c.log_start()
    if c.has_flag("--fake-terminal"):
        return fake_terminal()
    if sys.argv[1:2] == ["models"]:
        print("Fetching available models...")
        print("gemini-fake\tGemini Fake")
        print("gemini-fake-low\tGemini Fake (Low)")
        return 0
    if not sys.argv[1:] or sys.argv[1] != "acp":
        print("fake agy: only `acp` is supported", file=sys.stderr)
        return 2
    return Agy().run()


if __name__ == "__main__":
    sys.exit(main())
