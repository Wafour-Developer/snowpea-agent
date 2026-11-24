"""M37 §2.7: the ``snowpea-core hook report`` fast path, the hook script and the report socket."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.terminal import report as report_mod
from snowpea_core.terminal.integrations import hook_report, manager

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix socket / sh script")

ENV = {"SNOWPEA_TERMINAL_ID": "t1", "SNOWPEA_TERMINAL_TOKEN": "tok",
       "SNOWPEA_TERMINAL_REPORT": "/nowhere"}


def test_build_report_claude():
    payload = {"hook_event_name": "SessionStart", "session_id": "s1", "transcript_path": "/t.jsonl",
               "source": "startup"}
    rep = hook_report.build_report("claude", "session", payload, ENV)
    assert rep == {"v": 1, "terminalId": "t1", "token": "tok", "cli": "claude",
                   "event": "SessionStart", "state": None, "kind": None, "sessionId": "s1",
                   "transcriptPath": "/t.jsonl"}
    working = hook_report.build_report("claude", "working", {"session_id": "s1"}, ENV)
    assert working["state"] == "working"
    assert hook_report.build_report("claude", "done", {}, ENV)["state"] == "done"
    blocked = hook_report.build_report(
        "claude", "blocked", {"hook_event_name": "Notification",
                              "notification_type": "permission_prompt"}, ENV)
    assert (blocked["state"], blocked["kind"]) == ("blocked", "permission")
    question = hook_report.build_report(
        "claude", "blocked", {"notification_type": "elicitation_dialog"}, ENV)
    assert question["kind"] == "question"
    assert hook_report.build_report(
        "claude", "blocked", {"notification_type": "idle_prompt"}, ENV) is None
    # Subagents and Cursor reading Claude's hooks report nothing.
    assert hook_report.build_report("claude", "done", {"agent_id": "a"}, ENV) is None
    assert hook_report.build_report("claude", "done", {}, {**ENV, "CURSOR_VERSION": "1"}) is None
    # A session report needs a session id.
    assert hook_report.build_report("claude", "session", {}, ENV) is None


def test_build_report_other_clis():
    codex = {"hook_event_name": "SessionStart", "session_id": "c1", "transcript_path": "/r.jsonl"}
    assert hook_report.build_report("codex", "session", codex, ENV)["sessionId"] == "c1"
    assert hook_report.build_report("codex", "session", {"session_id": "c1"}, ENV) is None
    child = {**ENV, "CODEX_THREAD_ID": "parent"}
    assert hook_report.build_report("codex", "done", {"session_id": "c1"}, child) is None
    cursor = hook_report.build_report("cursor", "session", {"conversation_id": "k"}, ENV)
    assert cursor["sessionId"] == "k" and cursor["event"] == "session"
    agy = hook_report.build_report(
        "agy", "session", {"conversationId": "g", "transcriptPath": "/g"}, ENV)
    assert (agy["sessionId"], agy["transcriptPath"]) == ("g", "/g")
    assert hook_report.build_report("vim", "session", {}, ENV) is None
    assert hook_report.build_report("claude", "explode", {}, ENV) is None


class Sink:
    """Stands in for TerminalManager.report: checks the token, raises when wrong."""

    def __init__(self) -> None:
        self.tokens = {"t1": "tok"}
        self.reports: list[tuple[str, SimpleNamespace]] = []

    def report(self, terminal_id, token, report):
        if self.tokens.get(terminal_id) != token:
            raise PermissionError("bad terminal token")
        self.reports.append((terminal_id, report))
        return {"state": report.state}


def make_server(sink, home):
    return report_mod.ReportServer(sink, home, make_report=SimpleNamespace)


def test_parse_and_token_check(tmp_path):
    sink = Sink()
    server = make_server(sink, tmp_path)
    good = {"terminalId": "t1", "token": "tok", "cli": "claude", "event": "Stop", "state": "done",
            "kind": None, "sessionId": "s", "transcriptPath": None}
    assert server.handle_payload(good)
    assert server.handle_payload({**good, "token": "nope"}) is False
    assert server.handle_payload({**good, "terminalId": "t2"}) is False
    assert server.handle_payload({**good, "state": "dancing"}) is False
    assert server.handle_payload({**good, "sessionId": "a\x1bb"}) is False
    assert server.handle_payload([1]) is False
    assert len(sink.reports) == 1
    rep = sink.reports[0][1]
    assert (rep.state, rep.session_id, rep.event) == ("done", "s", "Stop")
    # kind only rides on blocked.
    assert server.handle_payload({**good, "kind": "permission"})
    assert sink.reports[-1][1].kind is None


def test_long_home_gets_a_short_socket_path(tmp_path):
    deep = tmp_path / ("x" * 120)
    path = report_mod.unix_socket_path(deep)
    assert len(str(path)) <= report_mod.MAX_UNIX_PATH + 40
    assert path.name.startswith("report-") and path.suffix == ".sock"
    short = report_mod.unix_socket_path(Path("/h"))
    assert short == Path("/h/terminals/report.sock")


@posix_only
async def test_socket_roundtrip_with_hook_report(tmp_path):
    sink = Sink()
    server = make_server(sink, tmp_path / "sp")
    address = await server.start()
    try:
        assert oct(os.stat(address).st_mode & 0o777) == "0o600"
        env = {**ENV, "SNOWPEA_TERMINAL_REPORT": address}
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s9"})
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "snowpea_core", "hook", "report", "--cli", "claude",
            "--action", "working", stdin=subprocess.PIPE,
            env={**os.environ, **env})
        await proc.communicate(payload.encode())
        assert proc.returncode == 0
        for _ in range(50):
            if sink.reports:
                break
            await asyncio.sleep(0.02)
        assert sink.reports and sink.reports[0][0] == "t1"
        assert sink.reports[0][1].state == "working"
        assert sink.reports[0][1].session_id == "s9"
    finally:
        await server.stop()
    assert not os.path.exists(address)


def test_hook_report_noop_and_unreachable_socket_exit_zero(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SNOWPEA_TERMINAL")}
    out = subprocess.run([sys.executable, "-m", "snowpea_core", "hook", "report", "--cli",
                          "claude", "--action", "done"], input=b"{}", env=env,
                         capture_output=True, timeout=10)
    assert out.returncode == 0 and out.stdout == b""
    started = time.monotonic()
    env.update({**ENV, "SNOWPEA_TERMINAL_REPORT": str(tmp_path / "missing.sock")})
    out = subprocess.run([sys.executable, "-m", "snowpea_core", "hook", "report", "--cli",
                          "claude", "--action", "done"], input=b"not json", env=env,
                         capture_output=True, timeout=10)
    assert out.returncode == 0
    assert time.monotonic() - started < 5


def test_hook_fast_path_imports_no_pydantic_or_asyncio():
    env = {**os.environ, **ENV}
    out = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "snowpea_core", "hook", "report", "--cli",
         "claude", "--action", "done"],
        input=b"{}", env=env, capture_output=True, timeout=20)
    assert out.returncode == 0
    imported = {line.split("|")[-1].strip().split(".")[0]
                for line in out.stderr.decode().splitlines() if line.startswith("import time:")}
    for heavy in ("pydantic", "asyncio", "aiohttp", "websockets", "httpx", "anthropic"):
        assert heavy not in imported, heavy


@posix_only
async def test_installed_script_reports_through_core_bin(tmp_path):
    """End to end: the installed sh script → the embedded snowpea-core → report socket.

    The environment's SNOWPEA_CORE_BIN points at a decoy; the script must ignore it (L4).
    """
    core_bin = tmp_path / "fake core's"  # a space and a quote survive the quoting
    core_bin.write_text(f"#!{sys.executable}\nimport sys\nfrom snowpea_core.__main__ import main\n"
                        "sys.exit(main())\n")
    core_bin.chmod(0o755)
    decoy_mark = tmp_path / "decoy-ran"
    (tmp_path / "decoybin").mkdir()
    decoy = tmp_path / "decoybin" / "snowpea-core"
    decoy.write_text(f"#!{sys.executable}\nopen({str(decoy_mark)!r}, 'w').close()\n")
    decoy.chmod(0o755)
    env = manager.Env(environ={"HOME": str(tmp_path), "PATH": ""}, snowpea_home=tmp_path / "sp",
                      windows=False, core_bin=str(core_bin))
    (tmp_path / ".claude").mkdir()
    manager.install("claude", env)
    script = env.script_path()
    sink = Sink()
    server = make_server(sink, tmp_path / "sp")
    address = await server.start()
    try:
        command = manager.hook_command(env, "claude", "session")
        run_env = {**os.environ, **ENV, "SNOWPEA_TERMINAL_REPORT": address,
                   "SNOWPEA_CORE_BIN": str(decoy),
                   "PATH": f"{tmp_path / 'decoybin'}:{os.environ['PATH']}"}
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "abc",
                              "transcript_path": "/x.jsonl"})
        proc = await asyncio.create_subprocess_shell(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=run_env)
        stdout, _ = await proc.communicate(payload.encode())
        assert proc.returncode == 0 and stdout == b""
        for _ in range(100):
            if sink.reports:
                break
            await asyncio.sleep(0.02)
        assert sink.reports[0][1].session_id == "abc"
        assert sink.reports[0][1].transcript_path == "/x.jsonl"
        assert not decoy_mark.exists()
    finally:
        await server.stop()
    # Outside a snowpea terminal the script is a no-op; for agy it still prints {}.
    bare = {k: v for k, v in os.environ.items() if not k.startswith("SNOWPEA_")}
    out = subprocess.run(["sh", str(script), "agy", "session"], input=b"{}", env=bare,
                         capture_output=True, timeout=10)
    assert out.returncode == 0 and out.stdout == b"{}\n"
    out = subprocess.run(["sh", str(script), "claude", "done"], input=b"{}", env=bare,
                         capture_output=True, timeout=10)
    assert out.returncode == 0 and out.stdout == b""


def test_core_bin_for_env_is_a_string():
    assert isinstance(report_mod.core_bin_for_env(), str)
