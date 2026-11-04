"""``--host``: the daemon listens where it is told, and says so when that is not loopback."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import aiohttp
import pytest

from snowpea_core.__main__ import build_parser
from snowpea_core.server.app_server import HOST, Daemon


def test_the_default_listen_address_is_loopback() -> None:
    assert build_parser().parse_args([]).host == HOST == "127.0.0.1"
    assert build_parser().parse_args(["--host", "0.0.0.0"]).host == "0.0.0.0"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="127.0.0.2 is loopback on Linux")
async def test_the_daemon_listens_on_the_address_it_was_given(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    daemon = Daemon(port=0, home=home, host="127.0.0.2")
    with caplog.at_level(logging.WARNING, logger="snowpea.server"):
        await daemon.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.2:{daemon.port}/health") as ok:
                assert ok.status == 200
            with pytest.raises(aiohttp.ClientConnectorError):
                await http.get(f"http://127.0.0.1:{daemon.port}/health")
        assert any("not loopback" in record.message for record in caplog.records)
    finally:
        await daemon.stop()


async def test_loopback_gives_no_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    home = tmp_path / "home"
    home.mkdir()
    daemon = Daemon(port=0, home=home)
    with caplog.at_level(logging.WARNING, logger="snowpea.server"):
        await daemon.start()
    try:
        assert not any("not loopback" in record.message for record in caplog.records)
    finally:
        await daemon.stop()
