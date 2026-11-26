#!/usr/bin/env python3
"""Fake ``cursor-agent`` print mode (one process per message), after spike S4.

    fake_cursor.py --version
    fake_cursor.py --help                      lists ``--mode`` choices ``plan``, ``ask``
                                               (``FAKE_CURSOR_MODES`` overrides them)
    fake_cursor.py create-chat                 prints a new chat id (no model call)
    fake_cursor.py --list-models               "<id> - <name>" lines
    fake_cursor.py -p --output-format stream-json [--stream-partial-output]
        [--trust] [--sandbox enabled] --workspace <root> [--resume <chatId>]
        [--mode ask|plan] [--model <m>] [--yolo | -f | --force] [--] <prompt>

The prompt is the last argument (cursor does not read stdin); everything after
``--`` is the prompt.  Events on stdout: ``system/init``, ``user``, ``thinking``
deltas + ``completed``, ``tool_call`` started/completed (scenario
``approval``: a Write that runs with no prompt — cursor has no permission
mechanism — or, under ``--mode ask``, a ``permissionDenied`` result),
``assistant`` deltas with ``timestamp_ms`` then the full message without it,
``result``.  There is no in-band cancel: interrupt = kill the process group.

Options are read only before ``--`` (as commander does), so a prompt that looks
like a flag stays text.  ``status --format json`` reports the login
(``FAKE_CURSOR_LOGGED_OUT=1`` for logged out, which also makes print mode fail
like the real CLI).  ``FAKE_CURSOR_WORKER_PIDFILE`` makes print mode leave a
detached ``worker-server`` behind (its own session, reparented away, like the
real one) and write its pid there.  Scenario ``error`` ends the turn with an
``is_error`` result; ``linger`` stays up after its result, as the real CLI does
while its language servers and shell run.

With ``FAKE_CLI_STATE_DIR`` set, chats persist like the real layout:
``<state>/.cursor/chats/<md5(workspace)>/<chatId>/meta.json`` and
``<state>/.cursor/projects/<encoded>/agent-transcripts/<chatId>/<chatId>.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as c  # noqa: E402

VERSION = "2026.11.13-fake"


def state_root() -> str | None:
    state = os.environ.get("FAKE_CLI_STATE_DIR")
    return os.path.join(state, ".cursor") if state else None


def chat_dir(workspace: str, chat_id: str) -> str | None:
    root = state_root()
    if root is None:
        return None
    digest = hashlib.md5(workspace.encode("utf-8")).hexdigest()
    return os.path.join(root, "chats", digest, chat_id)


def transcript_path(workspace: str, chat_id: str) -> str | None:
    root = state_root()
    if root is None:
        return None
    encoded = workspace.lstrip("/").replace("/", "-")
    return os.path.join(root, "projects", encoded, "agent-transcripts", chat_id,
                        f"{chat_id}.jsonl")


def touch_meta(workspace: str, chat_id: str) -> None:
    folder = chat_dir(workspace, chat_id)
    if folder is None:
        return
    path = os.path.join(folder, "meta.json")
    meta = c.read_json(path, {"schemaVersion": 1, "createdAtMs": c.now_ms(),
                              "hasConversation": False, "cwd": workspace})
    meta["updatedAtMs"] = c.now_ms()
    c.write_json(path, meta)


def options(args: list[str]) -> list[str]:
    return args[:args.index("--")] if "--" in args else args


def opt(flag: str) -> str | None:
    return c.arg_value(flag, options(sys.argv[1:]))


def prompt_of(args: list[str]) -> str:
    if "--" in args:
        return " ".join(args[args.index("--") + 1:])
    return args[-1] if args else ""


def logged_out() -> bool:
    return os.environ.get("FAKE_CURSOR_LOGGED_OUT") == "1"


def leave_worker() -> None:
    """Like the real CLI: a detached worker-server that outlives the run."""
    path = os.environ.get("FAKE_CURSOR_WORKER_PIDFILE")
    if not path:
        return
    code = (
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time\\nwhile True: time.sleep(60)', 'worker-server'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "start_new_session=True)\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
    )
    subprocess.run([sys.executable, "-c", code, path], check=False)


def run_print() -> int:
    cfg = c.config()
    c.prepare(cfg)
    c.maybe_spawn_child()
    args = sys.argv[1:]
    if logged_out():
        print("Error: Authentication required. Please run 'agent login' first, or set "
              "CURSOR_API_KEY.", file=sys.stderr)
        return 1
    leave_worker()
    workspace = opt("--workspace") or os.getcwd()
    chat_id = opt("--resume") or c.new_id()
    mode = opt("--mode") or "agent"
    partial = "--stream-partial-output" in options(args)
    run_everything = any(flag in options(args) for flag in ("--yolo", "-f", "--force"))
    scenario = str(cfg["scenario"])
    text = prompt_of(args)
    base = {"session_id": chat_id}
    touch_meta(workspace, chat_id)
    transcript = transcript_path(workspace, chat_id)

    def keep(record: dict) -> None:
        if transcript is not None:
            c.append_jsonl(transcript, record)

    c.write({"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": workspace,
             "session_id": chat_id, "model": opt("--model") or "Composer fake",
             "permissionMode": "default"})
    if scenario == "garbage":
        c.garbage()
    c.write({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}, **base})
    keep({"role": "user", "message": {"content": [{"type": "text", "text": (
        f"<timestamp>{c.iso_now()}</timestamp>\n<user_query>\n{text}\n</user_query>")}]}})
    for part in ("Let me", " think."):
        c.write({"type": "thinking", "subtype": "delta", "text": part,
                 "timestamp_ms": c.now_ms(), **base})
    c.write({"type": "thinking", "subtype": "completed", "timestamp_ms": c.now_ms(), **base})
    reply = str(cfg["reply"])
    if scenario == "approval":
        call_id = "call\n" + c.new_id()  # real ids contain a newline
        path = os.path.join(workspace, "note.txt")
        args_ = {"path": path, "contents": "hi"}
        c.write({"type": "tool_call", "subtype": "started", "call_id": call_id,
                 "tool_call": {"editToolCall": {"args": args_}}, **base})
        if mode in ("ask", "plan") and not run_everything:
            result = {"permissionDenied": {"error": "read-only mode", "isReadonly": True}}
            reply = "I cannot edit files in ask mode."
        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hi")
            diff = "--- a/note.txt\n+++ b/note.txt\n@@ -0,0 +1 @@\n+hi\n"
            result = {"success": {"path": path, "linesAdded": 1, "linesRemoved": 0,
                                  "diffString": diff, "afterFullFileContent": "hi"}}
            reply = "Wrote note.txt."
        c.write({"type": "tool_call", "subtype": "completed", "call_id": call_id,
                 "tool_call": {"editToolCall": {"args": args_, "result": result}}, **base})
        keep({"role": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": args_}]}})
    if scenario == "hang":
        c.hang()
    written = ""
    for index, part in enumerate(c.chunks(reply, scenario)):
        if partial:
            c.write({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": part}]}, "timestamp_ms": c.now_ms(), **base})
        written += part
        if scenario == "die" and index == 0:
            c.die()
        if scenario == "slow":
            time.sleep(float(cfg["delay"]))
    c.write({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": written}]}, **base})
    keep({"role": "assistant", "message": {"content": [{"type": "text", "text": written}]}})
    keep({"type": "turn_ended", "status": "success"})
    touch_meta(workspace, chat_id)
    failed = scenario == "error"
    c.write({"type": "result", "subtype": "error" if failed else "success", "duration_ms": 1,
             "is_error": failed, "result": "model unavailable" if failed else written,
             "session_id": chat_id, "request_id": c.new_id(),
             "usage": {"inputTokens": 1, "outputTokens": 1}})
    if scenario == "linger":
        c.log("linger")
        while True:
            time.sleep(3600)
    c.log("exit", 0)
    return 0


_EXIT_CMDS = frozenset({"exit", "quit", "/exit"})


def interactive_terminal() -> int:
    """Interactive cursor-agent in a terminal pane: liveness until EOF."""
    print("fake cursor-agent (interactive)", flush=True)
    try:
        for line in sys.stdin:
            text = line.rstrip("\n\r")
            c.log("in", text)
            if text.strip().lower() in _EXIT_CMDS:
                break
            if text.strip():
                print(f"> {text}", flush=True)
    finally:
        c.log("exit", 0)
    return 0


def help_text() -> str:
    choices = ", ".join(f'"{m}"' for m in c.help_modes("FAKE_CURSOR_MODES", ["plan", "ask"]))
    return (
        "Usage: cursor-agent [options] [command] [prompt...]\n\nOptions:\n"
        "  --mode <mode>                Start in the given execution mode. plan:\n"
        "                               read-only/planning (analyze, propose plans, no\n"
        f"                               edits). (choices: {choices})\n"
        "  --yolo                       Alias for --force (Run Everything) (default:\n"
        "                               false)\n"
    )


def main() -> int:
    if c.version_requested(VERSION):
        return 0
    if c.help_requested(help_text()):
        return 0
    c.log_start()
    args = sys.argv[1:]
    if args[:1] == ["create-chat"]:
        chat_id = c.new_id()
        touch_meta(c.arg_value("--workspace") or os.getcwd(), chat_id)
        print(chat_id)
        return 0
    if args[:1] in (["status"], ["whoami"]):
        if "json" in args:
            print(json.dumps({"status": "unauthenticated" if logged_out() else "authenticated",
                              "isAuthenticated": not logged_out()}))
        else:
            print("Not logged in" if logged_out() else "Logged in")
        return 0
    if "--list-models" in options(args):
        print("auto - Auto (default)\ncomposer-fake - Composer fake")
        return 0
    if "-p" not in args and "--print" not in args:
        return interactive_terminal()
    return run_print()


if __name__ == "__main__":
    sys.exit(main())
