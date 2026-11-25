"""M43: ``snowpea --remote`` and ``snowpea remote up`` removed; machines and --docker stay."""

# ruff: noqa: E402

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from snowpea_core.cli import main as cli_main
from snowpea_core.cli.render import EXIT_USAGE

pytest_plugins = ["test_m40_machines"]

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh and ssh")


def test_snowpea_remote_flag_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main.main(["--remote", "me@box"])
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "removed in 1.78.0" in err
    assert "machine add" in err


def test_snowpea_core_remote_up_prints_removed_json() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "snowpea_core", "remote", "up", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    line = proc.stdout.strip().splitlines()
    assert len(line) == 1
    payload = json.loads(line[0])
    assert payload == {
        "event": "error",
        "code": "removed",
        "message": (
            "snowpea remote up was removed in 1.78.0; "
            "connect machines with machine.add / snowpea machine add"
        ),
    }


def test_snowpea_remote_up_before_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main.main(["remote", "up", "me@box", "--json"])
    assert code == 2
    out = capsys.readouterr().out.strip()
    assert json.loads(out)["code"] == "removed"


def test_docker_still_parses() -> None:
    parser = cli_main.build_parser()
    args = parser.parse_args(["--docker", "img", "--cwd", "/x"])
    assert args.docker == "img" and args.cwd == "/x"


def test_bare_snowpea_remote_fails_with_machine_hint(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main.main(["remote"])
    assert code == EXIT_USAGE
    assert "machine add" in capsys.readouterr().err


@posix_only
@pytest.mark.asyncio
async def test_machines_still_connect_via_fake_ssh(fake_ssh_stack: dict) -> None:
    import aiohttp
    from _support import connect
    from test_m40_machines import _accept_host_key, _wait_machine

    daemon = fake_ssh_stack["daemon"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, status="connected", timeout=90)
        assert row["status"] == "connected"
        await client.stop()